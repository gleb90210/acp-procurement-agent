#!/usr/bin/env python3
"""Best-effort, cacheable Spanish -> English title translation for ACP procurements.

The original Spanish text is never overwritten. If translation service is unavailable,
leave English title blank (never silently treat an untranslated title as English).
"""
from __future__ import annotations
import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("acp-translate")

def ensure_columns(conn: sqlite3.Connection) -> None:
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tenders)")}
    for col in ("title_en", "title_en_source", "title_en_at"):
        if col not in columns:
            conn.execute(f"ALTER TABLE tenders ADD COLUMN {col} TEXT")
    conn.commit()

def translate_pending(conn: sqlite3.Connection, limit: int, delay: float) -> dict:
    ensure_columns(conn)
    from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source="auto", target="en")
    rows = conn.execute("""
        SELECT tender_id,title FROM tenders
        WHERE TRIM(COALESCE(title,'')) != ''
        AND (COALESCE(title_en,'')='' OR COALESCE(title_en_source,'') != title)
        ORDER BY published_at DESC, tender_id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    report = {"selected": len(rows), "translated":0, "failed":0, "errors":[]}
    for tender_id,title in rows:
        try:
            result = translator.translate(title)
            if not isinstance(result,str) or not result.strip():
                raise ValueError("Translation service returned empty text")
            conn.execute("""
                UPDATE tenders SET title_en=?, title_en_source=?, title_en_at=?
                WHERE tender_id=? AND title=?
            """,(result.strip(),title,datetime.now(timezone.utc).isoformat(timespec='seconds'),tender_id,title))
            report["translated"]+=1
        except Exception as exc:
            report["failed"]+=1
            if len(report["errors"]) < 10:
                report["errors"].append({"tender_id":tender_id,"error":str(exc)[:250]})
            LOG.warning("Could not translate %s: %s",tender_id,exc)
        if delay > 0:
            time.sleep(delay)
    conn.commit()
    report["remaining"]=conn.execute("""
        SELECT COUNT(*) FROM tenders WHERE TRIM(COALESCE(title,'')) != ''
        AND (COALESCE(title_en,'')='' OR COALESCE(title_en_source,'') != title)
    """).fetchone()[0]
    return report

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--db",type=Path,default=Path("data/procurements.sqlite"))
    parser.add_argument("--limit",type=int,default=350)
    parser.add_argument("--delay",type=float,default=0.4)
    args=parser.parse_args()
    if args.limit < 1 or args.limit > 5000 or args.delay < 0:
        parser.error("Unsupported translate limit/delay")
    logging.basicConfig(level=logging.INFO)
    with sqlite3.connect(args.db) as conn:
        report=translate_pending(conn,args.limit,args.delay)
    print(json.dumps(report,ensure_ascii=False))
    # A failed translation does not erase procurement data or block publishing.
    # The job report accurately exposes partial translation coverage.
    return 0

if __name__=="__main__":
    raise SystemExit(main())
