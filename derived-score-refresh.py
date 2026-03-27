#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

# ============================================================================
# CONFIG
# ============================================================================

ARCHIVE_DB = r"C:\LifeArchive\archive_index.db"

TECHNICAL_DB = r"C:\LifeArchive\technical_scores.sqlite"
FACE_DB = r"C:\LifeArchive\face_scores.sqlite"
AESTHETIC_DB = r"C:\LifeArchive\aesthetic_scores.sqlite"
SEMANTIC_DB = r"C:\LifeArchive\semantic_scores.sqlite"
AI_SUMMARY_DB = r"C:\LifeArchive\ai_summaries.sqlite"

HERO_OUTPUT_DB = r"C:\LifeArchive\hero_scores.sqlite"
CULL_OUTPUT_DB = r"C:\LifeArchive\cull_scores.sqlite"

# Set either or both to True.
RUN_HERO = True
RUN_CULL = True

# These are cheap to rebuild, so full refresh each run is the default.
REBUILD_HERO = True
REBUILD_CULL = True

HEARTBEAT_EVERY_N = 1000

# ============================================================================
# FORMULA VERSIONING
# ============================================================================

HERO_MODEL_NAME = "derived-hero-score"
HERO_MODEL_VERSION = "hero-v1"

CULL_MODEL_NAME = "derived-cull-score"
CULL_MODEL_VERSION = "cull-v1"

# ============================================================================
# UTILITIES
# ============================================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return default
        return float(value)
    except Exception:
        return default

def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return default
        return int(value)
    except Exception:
        return default

def note_join(parts: list[str]) -> str:
    return ",".join([p for p in parts if p])

# ============================================================================
# DB INIT
# ============================================================================

def init_hero_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hero_scores (
            sha1 TEXT PRIMARY KEY,
            hero_score REAL NOT NULL,
            breakdown_json TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            scored_at TEXT NOT NULL,
            warnings TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hero_score_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            images_attempted INTEGER DEFAULT 0,
            images_scored INTEGER DEFAULT 0,
            images_failed INTEGER DEFAULT 0,
            notes TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hero_scores_model_version ON hero_scores(model_version)")
    conn.commit()

def init_cull_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cull_scores (
            sha1 TEXT PRIMARY KEY,
            cull_score REAL NOT NULL,
            breakdown_json TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            scored_at TEXT NOT NULL,
            warnings TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cull_score_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            images_attempted INTEGER DEFAULT 0,
            images_scored INTEGER DEFAULT 0,
            images_failed INTEGER DEFAULT 0,
            notes TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cull_scores_model_version ON cull_scores(model_version)")
    conn.commit()

def rebuild_hero_db(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS hero_scores")
    conn.execute("DROP TABLE IF EXISTS hero_score_runs")
    conn.commit()
    init_hero_db(conn)

def rebuild_cull_db(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS cull_scores")
    conn.execute("DROP TABLE IF EXISTS cull_score_runs")
    conn.commit()
    init_cull_db(conn)

def begin_run(conn: sqlite3.Connection, table_prefix: str, model_name: str, model_version: str, notes: str = "") -> int:
    cur = conn.execute(f"""
        INSERT INTO {table_prefix}_runs (
            started_at, completed_at, model_name, model_version,
            images_attempted, images_scored, images_failed, notes
        ) VALUES (?, NULL, ?, ?, 0, 0, 0, ?)
    """, (utc_now_iso(), model_name, model_version, notes))
    conn.commit()
    return int(cur.lastrowid)

def finish_run(conn: sqlite3.Connection, table_prefix: str, run_id: int, attempted: int, scored: int, failed: int) -> None:
    conn.execute(f"""
        UPDATE {table_prefix}_runs
        SET completed_at=?, images_attempted=?, images_scored=?, images_failed=?
        WHERE run_id=?
    """, (utc_now_iso(), attempted, scored, failed, run_id))
    conn.commit()

# ============================================================================
# SOURCE DATA
# ============================================================================

def read_active_media() -> list[dict[str, Any]]:
    with sqlite3.connect(ARCHIVE_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT sha1, rel_fqn, original_filename
            FROM media
            WHERE is_deleted = 0
            ORDER BY rowid
        """).fetchall()
    return [dict(r) for r in rows]

def read_table_map(db_path: str, query: str, key_field: str = "sha1") -> dict[str, dict[str, Any]]:
    if not os.path.exists(db_path):
        return {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        k = str(d.get(key_field) or "")
        if k:
            out[k] = d
    return out

def load_all_sidecars() -> dict[str, dict[str, dict[str, Any]]]:
    print("Loading source sidecars...")
    technical = read_table_map(
        TECHNICAL_DB,
        "SELECT * FROM image_scores"
    )
    faces = read_table_map(
        FACE_DB,
        "SELECT * FROM image_face_summary"
    )
    aesthetic = read_table_map(
        AESTHETIC_DB,
        "SELECT * FROM aesthetic_scores"
    )
    semantic = read_table_map(
        SEMANTIC_DB,
        "SELECT * FROM semantic_scores"
    )
    summaries = read_table_map(
        AI_SUMMARY_DB,
        "SELECT * FROM ai_summaries"
    )
    print(f"  technical rows: {len(technical)}")
    print(f"  face rows     : {len(faces)}")
    print(f"  aesthetic rows: {len(aesthetic)}")
    print(f"  semantic rows : {len(semantic)}")
    print(f"  ai summary rows: {len(summaries)}")
    return {
        "technical": technical,
        "faces": faces,
        "aesthetic": aesthetic,
        "semantic": semantic,
        "summaries": summaries,
    }

# ============================================================================
# SCORING HELPERS
# ============================================================================

def face_score(face_row: dict[str, Any] | None) -> float:
    if not face_row:
        return 0.0
    face_count = safe_int(face_row.get("face_count"), 0)
    prominent_count = safe_int(face_row.get("prominent_face_count"), 0)
    largest_ratio = safe_float(face_row.get("largest_face_area_ratio"), 0.0)

    score = 0.0
    if face_count > 0:
        score += 0.30 * min(1.0, math.log1p(face_count))
        if largest_ratio > 0:
            score += 0.45 * min(1.0, largest_ratio / 0.18)
        score += 0.15 * min(1.0, prominent_count / 2.0)
        score += 0.10
    return clamp01(score)

def summary_bonus(summary_text: str, keywords: tuple[str, ...], bonus: float) -> float:
    s = (summary_text or "").lower()
    if any(term in s for term in keywords):
        return bonus
    return 0.0

def compute_hero_breakdown(
    media_row: dict[str, Any],
    technical_row: dict[str, Any] | None,
    face_row: dict[str, Any] | None,
    aesthetic_row: dict[str, Any] | None,
    semantic_row: dict[str, Any] | None,
    summary_row: dict[str, Any] | None,
) -> dict[str, Any]:
    technical = safe_float((technical_row or {}).get("technical_score"), 0.0)
    aesthetic = safe_float((aesthetic_row or {}).get("overall_aesthetic_score"), 0.0)
    if aesthetic <= 0.0:
        aesthetic = safe_float((aesthetic_row or {}).get("aesthetic_score"), 0.0)
    subject_prominence = safe_float((aesthetic_row or {}).get("subject_prominence_score"), 0.0)
    semantic = safe_float((semantic_row or {}).get("semantic_score"), 0.0)
    faces = face_score(face_row)

    contains_people = safe_int((semantic_row or {}).get("contains_people"), 0)
    contains_animals = safe_int((semantic_row or {}).get("contains_animals"), 0)
    is_document = safe_int((semantic_row or {}).get("is_document_like"), 0)
    is_screenshot = safe_int((semantic_row or {}).get("is_screenshot_like"), 0)
    is_landscape = safe_int((semantic_row or {}).get("is_landscape_like"), 0)
    is_food = safe_int((semantic_row or {}).get("is_food_like"), 0)
    ai_summary = str((summary_row or {}).get("summary_text") or "")

    dog_bonus = summary_bonus(ai_summary, ("dog", "puppy", "golden retriever", "great pyrenees", "pet"), 0.05)

    score = (
        technical * 0.14 +
        aesthetic * 0.20 +
        subject_prominence * 0.22 +
        semantic * 0.14 +
        faces * 0.30
    )

    notes: list[str] = []
    if contains_people:
        score += 0.07
        notes.append("people")
    if contains_animals:
        score += 0.06
        score += subject_prominence * 0.10
        notes.append("animals")
        notes.append("animal_prominence")
    if dog_bonus > 0:
        score += dog_bonus
        notes.append("dog_summary_bonus")
    if is_landscape:
        score += 0.03
        notes.append("landscape")
    if is_food:
        score += 0.02
        notes.append("food")

    if technical < 0.25:
        score *= 0.55
        notes.append("low_tech_penalty")
    if is_document:
        score *= 0.45
        notes.append("document_penalty")
    if is_screenshot:
        score *= 0.35
        notes.append("screenshot_penalty")

    final_score = clamp01(score)
    return {
        "hero_score": round(final_score, 6),
        "technical_score": round(technical, 6),
        "aesthetic_score": round(aesthetic, 6),
        "subject_prominence_score": round(subject_prominence, 6),
        "semantic_score": round(semantic, 6),
        "face_score": round(faces, 6),
        "contains_people": contains_people,
        "contains_animals": contains_animals,
        "is_landscape_like": is_landscape,
        "is_food_like": is_food,
        "note": note_join(notes),
    }

def compute_cull_breakdown(
    media_row: dict[str, Any],
    technical_row: dict[str, Any] | None,
    face_row: dict[str, Any] | None,
    aesthetic_row: dict[str, Any] | None,
    semantic_row: dict[str, Any] | None,
    summary_row: dict[str, Any] | None,
) -> dict[str, Any]:
    technical = safe_float((technical_row or {}).get("technical_score"), 0.0)
    aesthetic = safe_float((aesthetic_row or {}).get("overall_aesthetic_score"), 0.0)
    if aesthetic <= 0.0:
        aesthetic = safe_float((aesthetic_row or {}).get("aesthetic_score"), 0.0)
    subject_prominence = safe_float((aesthetic_row or {}).get("subject_prominence_score"), 0.0)
    semantic = safe_float((semantic_row or {}).get("semantic_score"), 0.0)
    faces = face_score(face_row)

    contains_people = safe_int((semantic_row or {}).get("contains_people"), 0)
    contains_animals = safe_int((semantic_row or {}).get("contains_animals"), 0)
    is_document = safe_int((semantic_row or {}).get("is_document_like"), 0)
    is_screenshot = safe_int((semantic_row or {}).get("is_screenshot_like"), 0)
    ai_summary = str((summary_row or {}).get("summary_text") or "")

    dog_bonus = 0.0
    if contains_animals:
        dog_bonus = summary_bonus(ai_summary, ("dog", "puppy", "pet"), 0.03)

    score = (
        technical * 0.34 +
        aesthetic * 0.20 +
        subject_prominence * 0.26 +
        faces * 0.14 +
        semantic * 0.06
    )

    notes: list[str] = []
    if contains_people:
        score += 0.03
        notes.append("people")
    if contains_animals:
        score += 0.04
        score += subject_prominence * 0.08
        notes.append("animals")
        notes.append("animal_prominence")
    if dog_bonus > 0:
        score += dog_bonus
        notes.append("dog_summary_bonus")

    if technical < 0.20:
        score *= 0.45
        notes.append("low_tech_penalty")
    if is_document:
        score *= 0.70
        notes.append("document_penalty")
    if is_screenshot:
        score *= 0.60
        notes.append("screenshot_penalty")

    final_score = clamp01(score)
    return {
        "cull_score": round(final_score, 6),
        "technical_score": round(technical, 6),
        "aesthetic_score": round(aesthetic, 6),
        "subject_prominence_score": round(subject_prominence, 6),
        "semantic_score": round(semantic, 6),
        "face_score": round(faces, 6),
        "contains_people": contains_people,
        "contains_animals": contains_animals,
        "note": note_join(notes),
    }

# ============================================================================
# WRITERS
# ============================================================================

def write_hero_scores(media_rows: list[dict[str, Any]], sidecars: dict[str, dict[str, dict[str, Any]]]) -> None:
    print("Writing hero scores...")
    with sqlite3.connect(HERO_OUTPUT_DB) as conn:
        if REBUILD_HERO:
            rebuild_hero_db(conn)
        else:
            init_hero_db(conn)

        run_id = begin_run(conn, "hero_score", HERO_MODEL_NAME, HERO_MODEL_VERSION)
        attempted = scored = failed = 0
        started = time.time()

        for row in media_rows:
            attempted += 1
            sha1 = str(row["sha1"])
            try:
                breakdown = compute_hero_breakdown(
                    row,
                    sidecars["technical"].get(sha1),
                    sidecars["faces"].get(sha1),
                    sidecars["aesthetic"].get(sha1),
                    sidecars["semantic"].get(sha1),
                    sidecars["summaries"].get(sha1),
                )
                warnings_text = ""
                conn.execute("""
                    INSERT INTO hero_scores (
                        sha1, hero_score, breakdown_json, model_name, model_version, scored_at, warnings
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sha1) DO UPDATE SET
                        hero_score=excluded.hero_score,
                        breakdown_json=excluded.breakdown_json,
                        model_name=excluded.model_name,
                        model_version=excluded.model_version,
                        scored_at=excluded.scored_at,
                        warnings=excluded.warnings
                """, (
                    sha1,
                    breakdown["hero_score"],
                    json.dumps(breakdown, sort_keys=True),
                    HERO_MODEL_NAME,
                    HERO_MODEL_VERSION,
                    utc_now_iso(),
                    warnings_text,
                ))
                scored += 1
            except Exception as exc:
                failed += 1
                conn.execute("""
                    INSERT INTO hero_scores (
                        sha1, hero_score, breakdown_json, model_name, model_version, scored_at, warnings
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sha1) DO UPDATE SET
                        hero_score=excluded.hero_score,
                        breakdown_json=excluded.breakdown_json,
                        model_name=excluded.model_name,
                        model_version=excluded.model_version,
                        scored_at=excluded.scored_at,
                        warnings=excluded.warnings
                """, (
                    sha1,
                    0.0,
                    "{}",
                    HERO_MODEL_NAME,
                    HERO_MODEL_VERSION,
                    utc_now_iso(),
                    f"compute_failed:{exc}",
                ))

            if attempted % HEARTBEAT_EVERY_N == 0:
                rate = attempted / max(1e-6, (time.time() - started))
                print(f"  hero {attempted}/{len(media_rows)} | {rate:.1f} img/sec")

        finish_run(conn, "hero_score", run_id, attempted, scored, failed)
    print(f"Hero complete. attempted={attempted} scored={scored} failed={failed}")

def write_cull_scores(media_rows: list[dict[str, Any]], sidecars: dict[str, dict[str, dict[str, Any]]]) -> None:
    print("Writing cull scores...")
    with sqlite3.connect(CULL_OUTPUT_DB) as conn:
        if REBUILD_CULL:
            rebuild_cull_db(conn)
        else:
            init_cull_db(conn)

        run_id = begin_run(conn, "cull_score", CULL_MODEL_NAME, CULL_MODEL_VERSION)
        attempted = scored = failed = 0
        started = time.time()

        for row in media_rows:
            attempted += 1
            sha1 = str(row["sha1"])
            try:
                breakdown = compute_cull_breakdown(
                    row,
                    sidecars["technical"].get(sha1),
                    sidecars["faces"].get(sha1),
                    sidecars["aesthetic"].get(sha1),
                    sidecars["semantic"].get(sha1),
                    sidecars["summaries"].get(sha1),
                )
                warnings_text = ""
                conn.execute("""
                    INSERT INTO cull_scores (
                        sha1, cull_score, breakdown_json, model_name, model_version, scored_at, warnings
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sha1) DO UPDATE SET
                        cull_score=excluded.cull_score,
                        breakdown_json=excluded.breakdown_json,
                        model_name=excluded.model_name,
                        model_version=excluded.model_version,
                        scored_at=excluded.scored_at,
                        warnings=excluded.warnings
                """, (
                    sha1,
                    breakdown["cull_score"],
                    json.dumps(breakdown, sort_keys=True),
                    CULL_MODEL_NAME,
                    CULL_MODEL_VERSION,
                    utc_now_iso(),
                    warnings_text,
                ))
                scored += 1
            except Exception as exc:
                failed += 1
                conn.execute("""
                    INSERT INTO cull_scores (
                        sha1, cull_score, breakdown_json, model_name, model_version, scored_at, warnings
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sha1) DO UPDATE SET
                        cull_score=excluded.cull_score,
                        breakdown_json=excluded.breakdown_json,
                        model_name=excluded.model_name,
                        model_version=excluded.model_version,
                        scored_at=excluded.scored_at,
                        warnings=excluded.warnings
                """, (
                    sha1,
                    0.0,
                    "{}",
                    CULL_MODEL_NAME,
                    CULL_MODEL_VERSION,
                    utc_now_iso(),
                    f"compute_failed:{exc}",
                ))

            if attempted % HEARTBEAT_EVERY_N == 0:
                rate = attempted / max(1e-6, (time.time() - started))
                print(f"  cull {attempted}/{len(media_rows)} | {rate:.1f} img/sec")

        finish_run(conn, "cull_score", run_id, attempted, scored, failed)
    print(f"Cull complete. attempted={attempted} scored={scored} failed={failed}")

# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    if not RUN_HERO and not RUN_CULL:
        print("Nothing to do. Set RUN_HERO and/or RUN_CULL to True.")
        return 0

    media_rows = read_active_media()
    print(f"Active media rows: {len(media_rows)}")

    sidecars = load_all_sidecars()

    t0 = time.time()
    if RUN_HERO:
        write_hero_scores(media_rows, sidecars)
    if RUN_CULL:
        write_cull_scores(media_rows, sidecars)

    elapsed = time.time() - t0
    print("=" * 80)
    print(f"Derived score refresh complete in {elapsed:.2f} seconds.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
