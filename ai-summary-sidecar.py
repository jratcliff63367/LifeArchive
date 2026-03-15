#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
import time
import traceback
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PIL import Image, ImageFile

# ============================================================================
# CONFIG
# ============================================================================

ARCHIVE_DB = r"C:\website-photos\archive_index.db"
OUTPUT_DB = r"C:\website-photos\ai_summaries.sqlite"
IMAGE_ROOT = r"C:\website-photos"

# Set to a small integer while evaluating output quality.
# Set to None for a full run later.
LIMIT_IMAGES = 50

# If True, delete and rebuild summary sidecar content.
REBUILD_DATABASE = False

# If True, retry rows that previously produced warnings.
RETRY_WARNING_ROWS = True

# Image normalization before captioning.
MAX_ANALYSIS_DIM = 1024
UPSCALE_SMALL_IMAGES = False

# Caption model.
CAPTION_MODEL = "Salesforce/blip-image-captioning-base"
DEVICE_PREFERENCE = "auto"   # "auto", "cuda", or "cpu"

# Generation knobs.
MAX_NEW_TOKENS = 30
MIN_NEW_TOKENS = 6
NUM_BEAMS = 4
TEMPERATURE = 1.0
DO_SAMPLE = False

# Progress / checkpointing.
HEARTBEAT_EVERY_N = 10
COMMIT_EVERY_N = 25

# Print each generated summary as it is produced.
PRINT_EACH_SUMMARY = True

# If True, any model-load failure aborts.
REQUIRE_CAPTION_MODEL = True

# ============================================================================
# IMPORTS THAT MAY NOT EXIST UNTIL INSTALLED
# ============================================================================

try:
    import torch
    from transformers import BlipProcessor, BlipForConditionalGeneration
except Exception as exc:
    print("ERROR: Missing required packages. Install into your venv with:")
    print(r"  .venv\Scripts\python -m pip install torch transformers pillow")
    print(f"\nImport error: {exc}")
    raise

# Pillow safety / robustness for very large images.
Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True
warnings.filterwarnings("ignore", category=UserWarning, module="PIL")

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class CandidateRow:
    sha1: str
    rel_fqn: str

@dataclass
class CaptionContext:
    device: str
    model_name: str
    model_version: str
    processor: Any
    model: Any

# ============================================================================
# UTILITIES
# ============================================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def choose_device() -> str:
    pref = DEVICE_PREFERENCE.lower().strip()
    if pref == "cpu":
        return "cpu"
    if pref == "cuda":
        return "cuda"
    return "cuda" if torch.cuda.is_available() else "cpu"

def normalize_working_image(pil_img: Image.Image) -> Image.Image:
    img = pil_img.convert("RGB")
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    long_side = max(w, h)
    if long_side <= MAX_ANALYSIS_DIM and not UPSCALE_SMALL_IMAGES:
        return img
    scale = MAX_ANALYSIS_DIM / float(long_side)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    if (new_w, new_h) == (w, h):
        return img
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)

def clean_caption(text: str) -> str:
    t = " ".join((text or "").strip().split())
    if not t:
        return ""
    # Light cleanup only. Keep it natural and short.
    t = t.replace("close up", "close-up")
    if len(t) > 240:
        t = t[:240].rstrip()
    return t

def open_image_for_analysis(path: str) -> Image.Image:
    with Image.open(path) as img:
        img.load()
        img = img.convert("RGB")
    return normalize_working_image(img)

# ============================================================================
# DATABASE
# ============================================================================

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_summaries (
            sha1 TEXT PRIMARY KEY,
            summary_text TEXT,
            model_name TEXT,
            model_version TEXT,
            scored_at TEXT,
            warnings TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_summary_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            completed_at TEXT NULL,
            model_name TEXT,
            model_version TEXT,
            images_attempted INTEGER DEFAULT 0,
            images_scored INTEGER DEFAULT 0,
            images_failed INTEGER DEFAULT 0,
            notes TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_summaries_model_version ON ai_summaries(model_version)")
    conn.commit()

def rebuild_db(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS ai_summaries")
    conn.execute("DROP TABLE IF EXISTS ai_summary_runs")
    conn.commit()
    init_db(conn)

def begin_run(conn: sqlite3.Connection, model_name: str, model_version: str, notes: str = "") -> int:
    cur = conn.execute("""
        INSERT INTO ai_summary_runs (
            started_at, completed_at, model_name, model_version, images_attempted, images_scored, images_failed, notes
        ) VALUES (?, NULL, ?, ?, 0, 0, 0, ?)
    """, (utc_now_iso(), model_name, model_version, notes))
    conn.commit()
    return int(cur.lastrowid)

def update_run(conn: sqlite3.Connection, run_id: int, attempted: int, scored: int, failed: int) -> None:
    conn.execute("""
        UPDATE ai_summary_runs
        SET images_attempted = ?, images_scored = ?, images_failed = ?
        WHERE run_id = ?
    """, (attempted, scored, failed, run_id))

def finish_run(conn: sqlite3.Connection, run_id: int, attempted: int, scored: int, failed: int) -> None:
    conn.execute("""
        UPDATE ai_summary_runs
        SET completed_at = ?, images_attempted = ?, images_scored = ?, images_failed = ?
        WHERE run_id = ?
    """, (utc_now_iso(), attempted, scored, failed, run_id))
    conn.commit()

def upsert_summary_row(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute("""
        INSERT INTO ai_summaries (
            sha1,
            summary_text,
            model_name,
            model_version,
            scored_at,
            warnings
        ) VALUES (
            :sha1,
            :summary_text,
            :model_name,
            :model_version,
            :scored_at,
            :warnings
        )
        ON CONFLICT(sha1) DO UPDATE SET
            summary_text = excluded.summary_text,
            model_name = excluded.model_name,
            model_version = excluded.model_version,
            scored_at = excluded.scored_at,
            warnings = excluded.warnings
    """, row)

def attach_sidecar(archive_conn: sqlite3.Connection, output_db: str) -> None:
    archive_conn.execute("ATTACH DATABASE ? AS sidecar", (output_db,))

def read_candidate_rows(archive_conn: sqlite3.Connection, model_version: str, limit_images: int | None) -> list[CandidateRow]:
    base_limit = ""
    if limit_images is not None:
        base_limit = f" LIMIT {int(limit_images)}"

    if REBUILD_DATABASE:
        query = f"""
            SELECT m.sha1, m.rel_fqn
            FROM media m
            WHERE m.is_deleted = 0
            ORDER BY m.rowid
            {base_limit}
        """
        rows = archive_conn.execute(query).fetchall()
        return [CandidateRow(sha1=r["sha1"], rel_fqn=r["rel_fqn"]) for r in rows]

    print("Reading active archive rows needing summary update...")
    if RETRY_WARNING_ROWS:
        query = f"""
            SELECT m.sha1, m.rel_fqn
            FROM media m
            LEFT JOIN sidecar.ai_summaries s
              ON s.sha1 = m.sha1 AND s.model_version = ?
            WHERE m.is_deleted = 0
              AND (
                    s.sha1 IS NULL
                    OR s.warnings <> ''
                  )
            ORDER BY m.rowid
            {base_limit}
        """
    else:
        query = f"""
            SELECT m.sha1, m.rel_fqn
            FROM media m
            LEFT JOIN sidecar.ai_summaries s
              ON s.sha1 = m.sha1 AND s.model_version = ?
            WHERE m.is_deleted = 0
              AND s.sha1 IS NULL
            ORDER BY m.rowid
            {base_limit}
        """
    rows = archive_conn.execute(query, (model_version,)).fetchall()
    return [CandidateRow(sha1=r["sha1"], rel_fqn=r["rel_fqn"]) for r in rows]

# ============================================================================
# MODEL LOADING / INFERENCE
# ============================================================================

def load_caption_model() -> CaptionContext:
    device = choose_device()
    print(f"Loading BLIP caption model on {device}...")
    processor = BlipProcessor.from_pretrained(CAPTION_MODEL)
    model = BlipForConditionalGeneration.from_pretrained(CAPTION_MODEL)
    model.to(device)
    model.eval()

    model_name = CAPTION_MODEL
    model_version = "blip-base-caption-v1"

    print("BLIP ready.")
    return CaptionContext(
        device=device,
        model_name=model_name,
        model_version=model_version,
        processor=processor,
        model=model,
    )

def generate_summary(ctx: CaptionContext, pil_img: Image.Image) -> str:
    inputs = ctx.processor(images=pil_img, return_tensors="pt")
    inputs = {k: v.to(ctx.device) for k, v in inputs.items()}

    with torch.no_grad():
        generated_ids = ctx.model.generate(
            **inputs,
            max_new_tokens=MAX_NEW_TOKENS,
            min_new_tokens=MIN_NEW_TOKENS,
            num_beams=NUM_BEAMS,
            temperature=TEMPERATURE,
            do_sample=DO_SAMPLE,
        )
    text = ctx.processor.decode(generated_ids[0], skip_special_tokens=True)
    return clean_caption(text)

# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    print("Opening archive database...")
    archive_conn = sqlite3.connect(ARCHIVE_DB)
    archive_conn.row_factory = sqlite3.Row

    print("Opening output database...")
    out_conn = sqlite3.connect(OUTPUT_DB)
    out_conn.row_factory = sqlite3.Row

    if REBUILD_DATABASE:
        rebuild_db(out_conn)
    else:
        init_db(out_conn)

    attach_sidecar(archive_conn, OUTPUT_DB)

    try:
        ctx = load_caption_model()
    except Exception as exc:
        print(f"[ERROR] Caption model load failed: {exc}")
        if REQUIRE_CAPTION_MODEL:
            raise
        return 1

    candidates = read_candidate_rows(archive_conn, ctx.model_version, LIMIT_IMAGES)
    print(f"Found {len(candidates)} images to summarize")

    run_id = begin_run(out_conn, ctx.model_name, ctx.model_version)

    total = len(candidates)
    attempted = 0
    scored = 0
    failed = 0
    start_time = time.time()

    for idx, cand in enumerate(candidates, start=1):
        full_path = os.path.join(IMAGE_ROOT, cand.rel_fqn)
        warning_text = ""
        summary_text = ""

        try:
            img = open_image_for_analysis(full_path)
            summary_text = generate_summary(ctx, img)
            if not summary_text:
                warning_text = "empty_summary"
        except Exception:
            warning_text = "summary_failed"
            tb = traceback.format_exc()
            print(f"[ERROR] Failed on {full_path}\n{tb}")

        row = {
            "sha1": cand.sha1,
            "summary_text": summary_text,
            "model_name": ctx.model_name,
            "model_version": ctx.model_version,
            "scored_at": utc_now_iso(),
            "warnings": warning_text,
        }
        upsert_summary_row(out_conn, row)

        attempted += 1
        if warning_text:
            failed += 1
        else:
            scored += 1

        if PRINT_EACH_SUMMARY:
            print("-" * 100)
            print(f"[{attempted}/{total}] {full_path}")
            if warning_text:
                print(f"WARNING: {warning_text}")
            print(f"SUMMARY: {summary_text}")

        if attempted % COMMIT_EVERY_N == 0:
            update_run(out_conn, run_id, attempted, scored, failed)
            out_conn.commit()
            print("[Checkpoint] committed")

        if attempted % HEARTBEAT_EVERY_N == 0 or idx == total:
            elapsed = max(1e-6, time.time() - start_time)
            rate = attempted / elapsed
            eta_sec = ((total - attempted) / max(1e-6, rate)) if attempted < total else 0.0
            pct = (attempted / total * 100.0) if total else 100.0
            print(
                f"[Progress] {attempted}/{total} | {pct:.1f}% | "
                f"scored={scored} failed={failed} | {rate:.2f} img/sec | ETA {eta_sec/60:.1f}m"
            )

    update_run(out_conn, run_id, attempted, scored, failed)
    finish_run(out_conn, run_id, attempted, scored, failed)

    out_conn.close()
    archive_conn.close()

    elapsed = time.time() - start_time
    print("=" * 100)
    print(f"Completed AI summary generation in {elapsed/60:.2f} minutes")
    print(f"Attempted={attempted} Scored={scored} Failed={failed}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
