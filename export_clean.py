#!/usr/bin/env python3
"""Create a concise, validated bilingual export and factual source coverage report.

No inferred categories, suppliers, amounts, or fabricated English translations.
"""
from __future__ import annotations
import argparse
import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

FIELDS=(
    "tender_id","published_at","status_en","status","title_en","title",
    "procurement_type_en","procurement_type","purchasing_unit",
    "purchasing_agent","closes_at","source_url"
)

def export(dbpath:Path, csvpath:Path, reportpath:Path)->dict:
    with sqlite3.connect(dbpath) as conn:
        conn.row_factory=sqlite3.Row
        records=[dict(x) for x in conn.execute("""
            SELECT tender_id, published_at, status, status_en, title, title_en,
                   title_en_source, procurement_type, procurement_type_en,
                   purchasing_unit, purchasing_agent, closes_at, source_url,
                   category, supplier, amount
            FROM tenders
            ORDER BY CASE WHEN published_at IS NULL OR published_at='' THEN 1 ELSE 0 END,
                     published_at DESC,tender_id DESC
        """)]
    csvpath.parent.mkdir(parents=True,exist_ok=True)
    with csvpath.open("w",encoding="utf-8-sig",newline="") as out:
        writer=csv.DictWriter(out,fieldnames=FIELDS)
        writer.writeheader()
        for r in records:
            record={k:(r.get(k) or "") for k in FIELDS}
            # Do not show stale machine translations as valid when Spanish title changed.
            if r.get("title_en_source") != r.get("title"):
                record["title_en"]=""
            writer.writerow(record)
    def nonempty(key):
        return sum(bool(str(r.get(key) or "").strip()) for r in records)
    dates=[str(r["published_at"])[:10] for r in records if r.get("published_at")]
    translated=sum(bool(r.get("title_en")) and r.get("title_en_source")==r.get("title")
                   for r in records)
    report={
        "generated_utc":datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope":"Accessible public ACP SLI listings, not an independently complete three-year archive",
        "requested_earliest_publication_date":"2023-11-10",
        "record_count":len(records),
        "actual_earliest_publication_date":min(dates) if dates else None,
        "actual_latest_publication_date":max(dates) if dates else None,
        "counts_by_status":dict(sorted(Counter(r.get("status") or "(unknown)" for r in records).items())),
        "field_coverage":{key:{"present":nonempty(key),"missing":len(records)-nonempty(key)}
                          for key in ("title","title_en","status","status_en","category",
                                      "procurement_type","procurement_type_en","published_at",
                                      "purchasing_agent","purchasing_unit","supplier","amount")},
        "english_title_translated":translated,
        "english_title_pending":sum(bool(r.get("title")) for r in records)-translated,
        "limitations":[
            "Categories, award suppliers and amounts are not available in the captured summary card fields; blanks must not be interpreted as zero.",
            "Procurements absent from the public portal cannot be reconstructed by filtering for a three-year date range.",
            "Original Spanish titles are retained; English titles are machine translated and may require review."
        ]
    }
    reportpath.parent.mkdir(parents=True,exist_ok=True)
    reportpath.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:report[k] for k in
        ("record_count","actual_earliest_publication_date","actual_latest_publication_date",
         "english_title_translated","english_title_pending")},ensure_ascii=False))
    return report

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--db",type=Path,default=Path("data/procurements.sqlite"))
    p.add_argument("--csv",type=Path,default=Path("data/procurements_clean.csv"))
    p.add_argument("--report",type=Path,default=Path("data/quality_report.json"))
    p.add_argument("--require-english",action="store_true")
    args=p.parse_args()
    report=export(args.db,args.csv,args.report)
    if args.require_english and report["english_title_pending"]:
        raise SystemExit(f"English title translation incomplete: {report['english_title_pending']} remaining")
if __name__=="__main__":
    main()
