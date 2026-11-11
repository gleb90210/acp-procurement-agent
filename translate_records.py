#!/usr/bin/env python3
"""Offline Spanish-to-English tender titles; original Spanish data is never overwritten.

Uses an Argos Translate open-source model installed in the GitHub runner.
Translate failures are reported and return a non-zero exit status.
"""
from __future__ import annotations
import argparse
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

LOG=logging.getLogger("acp-translate")
STATUS_EN={
    "ABIERTAS":"Open",
    "ANUNCIO VENCIDO":"Notice expired",
    "CANCELACIÓN DEL ACTO":"Cancelled",
    "EVALUACIÓN":"Under evaluation",
    "ENMENDADA":"Amended",
    "ACTO DESIERTO":"No award",
    "ADJUDICACIÓN":"Awarded",
    "PRECALIFICACIÓN CONCLUÍDA":"Prequalification completed",
}
PROCUREMENT_TYPES_EN={
    "Licitación Pública":"Public tender",
    "Licitación Pública Precio Mas Bajo":"Public tender — lowest price",
    "Licitación Pública Precio Más Bajo":"Public tender — lowest price",
}

def ensure_columns(conn):
    columns={r[1] for r in conn.execute("PRAGMA table_info(tenders)")}
    for col in ("title_en","title_en_source","title_en_at","status_en","procurement_type_en"):
        if col not in columns:
            conn.execute(f"ALTER TABLE tenders ADD COLUMN {col} TEXT")
    conn.commit()

def load_model():
    import argostranslate.package
    import argostranslate.translate
    def select_translation():
        languages={lang.code: lang for lang in argostranslate.translate.get_installed_languages()}
        if "es" not in languages or "en" not in languages: return None
        try:
            return languages["es"].get_translation(languages["en"])
        except Exception:
            return None
    translation=select_translation()
    if translation is not None: return translation
    LOG.info("Installing the offline Argos Spanish-to-English model")
    argostranslate.package.update_package_index()
    choices=[pkg for pkg in argostranslate.package.get_available_packages()
             if pkg.from_code == "es" and pkg.to_code == "en"]
    if not choices: raise RuntimeError("Argos package index contains no Spanish-English model")
    package_path=choices[0].download()
    argostranslate.package.install_from_path(package_path)
    translation=select_translation()
    if translation is None: raise RuntimeError("Argos translation model failed to initialize")
    return translation

def translate_pending(conn, limit, translator):
    ensure_columns(conn)
    for spanish,english in STATUS_EN.items():
        conn.execute("UPDATE tenders SET status_en=? WHERE status=?",(english,spanish))
    procurement_types=[row[0] for row in conn.execute(
        "SELECT DISTINCT procurement_type FROM tenders WHERE TRIM(COALESCE(procurement_type,'')) != ''")]
    for spanish in procurement_types:
        english=PROCUREMENT_TYPES_EN.get(spanish)
        if english is None:
            english=translator.translate(spanish).strip()
        if english:
            conn.execute("UPDATE tenders SET procurement_type_en=? WHERE procurement_type=?",(english,spanish))

    rows=conn.execute("""
        SELECT tender_id,title FROM tenders
        WHERE TRIM(COALESCE(title,'')) != ''
        AND (COALESCE(title_en,'')='' OR COALESCE(title_en_source,'') != title)
        ORDER BY published_at DESC,tender_id DESC LIMIT ?
    """,(limit,)).fetchall()
    result={"selected":len(rows),"translated":0,"failed":0,"errors":[]}
    for i,(tender_id,title) in enumerate(rows,1):
        try:
            output=translator.translate(title)
            if not isinstance(output,str) or not output.strip():
                raise ValueError("Translation returned empty text")
            conn.execute("""
                UPDATE tenders SET title_en=?,title_en_source=?,title_en_at=?
                WHERE tender_id=? AND title=?
            """,(output.strip(),title,datetime.now(timezone.utc).isoformat(timespec="seconds"),tender_id,title))
            result["translated"]+=1
        except Exception as exc:
            result["failed"]+=1
            if len(result["errors"])<10:
                result["errors"].append({"tender_id":tender_id,"error":str(exc)[:250]})
            LOG.warning("Failed translation for tender %s: %s",tender_id,exc)
        if i % 100 == 0:
            conn.commit()
            LOG.info("English translations: %d/%d attempted (%d succeeded)",i,len(rows),result["translated"])
    conn.commit()
    result["remaining"]=conn.execute("""
        SELECT COUNT(*) FROM tenders WHERE TRIM(COALESCE(title,'')) != ''
        AND (COALESCE(title_en,'')='' OR COALESCE(title_en_source,'') != title)
    """).fetchone()[0]
    return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--db",type=Path,default=Path("data/procurements.sqlite"))
    parser.add_argument("--limit",type=int,default=1500)
    args=parser.parse_args()
    if not 1<=args.limit<=5000: parser.error("--limit must be between 1 and 5000")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    try:
        translator=load_model()
        with sqlite3.connect(args.db) as conn:
            result=translate_pending(conn,args.limit,translator)
    except Exception:
        LOG.exception("Offline translation failed")
        return 1
    print(json.dumps(result,ensure_ascii=False))
    if result["failed"] or result["remaining"]:
        LOG.error("Translation incomplete: %s",result)
        return 2
    return 0

if __name__=="__main__":
    raise SystemExit(main())
