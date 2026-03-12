import os
import sqlite3
import cv2
import numpy as np
import time
from datetime import datetime

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

ARCHIVE_DB = r"C:\website-test\archive_index.db"
IMAGE_ROOT = r"C:\website-test"

OUTPUT_DB = r"C:\website-test\technical_scores.sqlite"

MODEL_VERSION = "tech_score_v1"

PROGRESS_INTERVAL = 5
COMMIT_INTERVAL = 500

# ------------------------------------------------------------
# IMAGE METRICS
# ------------------------------------------------------------

def compute_sharpness(gray):
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def compute_contrast(gray):
    return gray.std()


def compute_brightness(gray):
    return gray.mean()


def compute_edge_density(gray):
    edges = cv2.Canny(gray, 100, 200)
    return np.sum(edges > 0) / edges.size


def compute_resolution_score(width, height):
    megapixels = (width * height) / 1_000_000
    return min(megapixels / 12.0, 1.0)


def score_image(path):

    img = cv2.imread(path)

    if img is None:
        return None

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    height, width = gray.shape

    sharpness = compute_sharpness(gray)
    contrast = compute_contrast(gray)
    brightness = compute_brightness(gray)
    edge_density = compute_edge_density(gray)
    resolution_score = compute_resolution_score(width, height)

    # normalize ranges
    sharpness_n = min(sharpness / 1000, 1.0)
    contrast_n = min(contrast / 64, 1.0)
    brightness_n = 1 - abs(brightness - 127) / 127
    edge_n = min(edge_density * 4, 1.0)

    technical_score = (
        0.35 * sharpness_n +
        0.20 * contrast_n +
        0.15 * brightness_n +
        0.20 * edge_n +
        0.10 * resolution_score
    )

    return {
        "width": width,
        "height": height,
        "sharpness": sharpness,
        "contrast": contrast,
        "brightness": brightness,
        "edge_density": edge_density,
        "resolution_score": resolution_score,
        "technical_score": technical_score
    }


# ------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------

def init_db(conn):

    conn.execute("""
    CREATE TABLE IF NOT EXISTS image_scores(
        sha1 TEXT PRIMARY KEY,
        width INTEGER,
        height INTEGER,
        sharpness REAL,
        contrast REAL,
        brightness REAL,
        edge_density REAL,
        resolution_score REAL,
        technical_score REAL,
        model_version TEXT,
        scored_at TEXT
    )
    """)

    conn.commit()


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():

    print("Opening archive database...")
    archive_conn = sqlite3.connect(ARCHIVE_DB)

    print("Opening output database...")
    out_conn = sqlite3.connect(OUTPUT_DB)

    init_db(out_conn)

    cur = archive_conn.cursor()

    print("Reading archive index...")
    cur.execute("SELECT sha1, rel_fqn FROM media WHERE is_deleted=0")

    rows = cur.fetchall()
    total = len(rows)

    print(f"Found {total} images to score")

    start_time = time.time()
    last_print = start_time
    processed = 0

    for sha1, rel_path in rows:

        processed += 1

        full_path = os.path.join(IMAGE_ROOT, rel_path)

        score = score_image(full_path)

        if score is None:
            continue

        out_conn.execute("""
        INSERT OR REPLACE INTO image_scores VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sha1,
            score["width"],
            score["height"],
            score["sharpness"],
            score["contrast"],
            score["brightness"],
            score["edge_density"],
            score["resolution_score"],
            score["technical_score"],
            MODEL_VERSION,
            datetime.utcnow().isoformat()
        ))

        now = time.time()

        if now - last_print > PROGRESS_INTERVAL:

            elapsed = now - start_time
            rate = processed / elapsed if elapsed else 0

            remaining = total - processed
            eta = remaining / rate if rate else 0

            pct = processed / total * 100

            print(
                f"[Progress] {processed}/{total} | "
                f"{pct:.1f}% | "
                f"{rate:.1f} img/sec | "
                f"ETA {eta/60:.1f}m"
            )

            print(f"Current: {full_path}")

            last_print = now

        if processed % COMMIT_INTERVAL == 0:
            out_conn.commit()
            print("[Checkpoint] committed")

    out_conn.commit()

    archive_conn.close()
    out_conn.close()

    duration = time.time() - start_time

    print("\n--- COMPLETE ---")
    print(f"Processed: {processed}")
    print(f"Duration: {duration/60:.1f} minutes")


# ------------------------------------------------------------

if __name__ == "__main__":
    main()