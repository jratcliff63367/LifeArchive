import json
import sqlite3
from pathlib import Path

# ------------------------------------------------------------
# CONFIGURATION (EDIT THESE)
# ------------------------------------------------------------
DB_PATH = r"C:\website-test\archive_index.db"
OUTPUT_JSON = r"C:\website-test\archive_debug_export.json"

EXPORT_FULL_SAMPLE = False          # Export a sample of normal rows
EXPORT_UNDATED_ANOMALIES = True     # Rows that look undated but were not assigned zero date
EXPORT_ZERO_DATE_WITHOUT_HINT = True  # Rows with zero date but no obvious undated hint
EXPORT_TOP_TAGS = True
EXPORT_LIMIT = 5000
# ------------------------------------------------------------


def query_all(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_summary(conn):
    cur = conn.cursor()
    out = {}

    out["db_path"] = str(Path(DB_PATH))

    cur.execute("SELECT COUNT(*) AS n FROM media")
    out["total_rows"] = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM media WHERE is_deleted = 0")
    out["active_rows"] = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM media WHERE is_deleted != 0")
    out["deleted_rows"] = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM media WHERE final_dt = '0000-00-00 00:00:00'")
    out["zero_date_rows"] = cur.fetchone()[0]

    cur.execute("""
        SELECT dt_source, COUNT(*) AS n
        FROM media
        GROUP BY dt_source
        ORDER BY n DESC, dt_source
    """)
    out["dt_source_breakdown"] = [
        {"dt_source": row[0], "count": row[1]} for row in cur.fetchall()
    ]

    cur.execute("""
        SELECT COUNT(*)
        FROM media
        WHERE lower(coalesce(rel_fqn, '')) LIKE '%undated%'
           OR lower(coalesce(path_tags, '')) LIKE '%undated%'
    """)
    out["rows_with_undated_hint"] = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*)
        FROM media
        WHERE (lower(coalesce(rel_fqn, '')) LIKE '%undated%'
           OR lower(coalesce(path_tags, '')) LIKE '%undated%')
          AND final_dt = '0000-00-00 00:00:00'
    """)
    out["undated_hint_and_zero_date"] = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*)
        FROM media
        WHERE (lower(coalesce(rel_fqn, '')) LIKE '%undated%'
           OR lower(coalesce(path_tags, '')) LIKE '%undated%')
          AND final_dt != '0000-00-00 00:00:00'
    """)
    out["undated_hint_but_nonzero_date"] = cur.fetchone()[0]

    return out


def fetch_top_tags(conn):
    # SQLite has no robust CSV splitter built in, so do this in Python.
    cur = conn.cursor()
    cur.execute("SELECT path_tags, custom_tags FROM media WHERE is_deleted = 0")

    counts = {}
    for path_tags, custom_tags in cur.fetchall():
        merged = f"{path_tags or ''},{custom_tags or ''}"
        for raw in merged.split(','):
            tag = raw.strip()
            if not tag:
                continue
            counts[tag] = counts.get(tag, 0) + 1

    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:250]
    return [{"tag": tag, "count": count} for tag, count in items]



def fetch_undated_anomalies(conn):
    return query_all(
        conn,
        """
        SELECT sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source, is_deleted,
               custom_notes, custom_tags
        FROM media
        WHERE is_deleted = 0
          AND (
               lower(coalesce(rel_fqn, '')) LIKE '%undated%'
            OR lower(coalesce(path_tags, '')) LIKE '%undated%'
          )
          AND final_dt != '0000-00-00 00:00:00'
        ORDER BY final_dt DESC, rel_fqn
        LIMIT ?
        """,
        (EXPORT_LIMIT,),
    )



def fetch_zero_date_without_hint(conn):
    return query_all(
        conn,
        """
        SELECT sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source, is_deleted,
               custom_notes, custom_tags
        FROM media
        WHERE is_deleted = 0
          AND final_dt = '0000-00-00 00:00:00'
          AND NOT (
               lower(coalesce(rel_fqn, '')) LIKE '%undated%'
            OR lower(coalesce(path_tags, '')) LIKE '%undated%'
          )
        ORDER BY rel_fqn
        LIMIT ?
        """,
        (EXPORT_LIMIT,),
    )



def fetch_full_sample(conn):
    return query_all(
        conn,
        """
        SELECT sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source, is_deleted,
               custom_notes, custom_tags
        FROM media
        ORDER BY final_dt DESC, rel_fqn
        LIMIT ?
        """,
        (EXPORT_LIMIT,),
    )



def main():
    db_file = Path(DB_PATH)
    if not db_file.exists():
        raise FileNotFoundError(f"Database file not found: {db_file}")

    print(f"Opening database: {db_file}")
    conn = sqlite3.connect(str(db_file))

    # Make sure expected table exists.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "media" not in tables:
        raise RuntimeError(f"Expected table 'media' not found. Tables present: {sorted(tables)}")

    export = {}

    print("Collecting summary...")
    export["summary"] = fetch_summary(conn)

    if EXPORT_TOP_TAGS:
        print("Collecting top tags...")
        export["top_tags"] = fetch_top_tags(conn)

    if EXPORT_UNDATED_ANOMALIES:
        print("Collecting undated anomalies...")
        export["undated_anomalies"] = fetch_undated_anomalies(conn)

    if EXPORT_ZERO_DATE_WITHOUT_HINT:
        print("Collecting zero-date rows without undated hint...")
        export["zero_date_without_hint"] = fetch_zero_date_without_hint(conn)

    if EXPORT_FULL_SAMPLE:
        print("Collecting full sample...")
        export["full_sample"] = fetch_full_sample(conn)

    conn.close()

    out_file = Path(OUTPUT_JSON)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    print(f"Writing JSON: {out_file}")
    out_file.write_text(json.dumps(export, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Done.")


if __name__ == "__main__":
    main()
