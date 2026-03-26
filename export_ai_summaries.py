#!/usr/bin/env python3
r"""
Export AI summaries from a SQLite database into a reviewable file.

Default use:
    .\.venv\Scripts\python.exe .\export_ai_summaries.py

Examples:
    .\.venv\Scripts\python.exe .\export_ai_summaries.py --format jsonl
    .\.venv\Scripts\python.exe .\export_ai_summaries.py --format text
    .\.venv\Scripts\python.exe .\export_ai_summaries.py --db "C:\website-photos\ai_summaries.sqlite" --out "C:\temp\ai_summaries.jsonl"
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = r"C:\website-photos\ai_summaries.sqlite"
DEFAULT_OUT = r"C:\website-photos\ai_summaries_export.jsonl"

SUMMARY_COLUMN_CANDIDATES = (
    "summary",
    "summary_text",
    "ai_summary",
    "caption",
    "description",
    "text",
)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--table", default="ai_summaries")
    return parser.parse_args()

def main():
    args = parse_args()
    db_path = Path(args.db)
    out_path = Path(args.out)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # find columns
    cursor.execute(f'PRAGMA table_info("{args.table}")')
    cols = [r[1] for r in cursor.fetchall()]

    summary_col = None
    for c in SUMMARY_COLUMN_CANDIDATES:
        if c in cols:
            summary_col = c
            break

    if not summary_col:
        print(f"No summary column found. Columns: {cols}")
        return

    cursor.execute(f'SELECT sha1, {summary_col}, model_name, model_version, scored_at, warnings FROM "{args.table}"')

    out_path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for row in cursor:
            sha1, summary, model, version, ts, warnings = row
            if not summary:
                continue

            record = {
                "sha1": sha1,
                "summary": summary.strip(),
                "model": model,
                "model_version": version,
                "timestamp": ts,
                "warnings": warnings
            }

            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1

    print(f"Exported {count} rows to {out_path}")

if __name__ == "__main__":
    main()
