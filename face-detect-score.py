import os
import sqlite3
import cv2
import time
from datetime import datetime

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

ARCHIVE_DB = r"C:\website-test\archive_index.db"
IMAGE_ROOT = r"C:\website-test"
OUTPUT_DB = r"C:\website-test\face_scores.sqlite"

MODEL_VERSION = "opencv_haar_face_v2"

PROGRESS_INTERVAL = 5      # seconds
COMMIT_INTERVAL = 500      # rows

# A face is considered "prominent" if it occupies at least this fraction
# of the total image area.
PROMINENT_FACE_THRESHOLD = 0.05

# ------------------------------------------------------------
# FACE DETECTOR
# ------------------------------------------------------------

CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
FACE_CASCADE = cv2.CascadeClassifier(CASCADE_PATH)

if FACE_CASCADE.empty():
    raise RuntimeError(f"Failed to load face cascade: {CASCADE_PATH}")

# ------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------

def init_db(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS image_face_summary(
        sha1 TEXT PRIMARY KEY,
        width INTEGER,
        height INTEGER,
        face_count INTEGER,
        prominent_face_count INTEGER,
        largest_face_area_ratio REAL,
        has_prominent_face INTEGER,
        model_version TEXT,
        scored_at TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS image_faces(
        sha1 TEXT NOT NULL,
        face_index INTEGER NOT NULL,

        img_width INTEGER NOT NULL,
        img_height INTEGER NOT NULL,

        x INTEGER NOT NULL,
        y INTEGER NOT NULL,
        w INTEGER NOT NULL,
        h INTEGER NOT NULL,

        x_norm REAL NOT NULL,
        y_norm REAL NOT NULL,
        w_norm REAL NOT NULL,
        h_norm REAL NOT NULL,

        area_ratio REAL NOT NULL,

        PRIMARY KEY (sha1, face_index)
    )
    """)

    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_image_faces_sha1
    ON image_faces(sha1)
    """)

    conn.commit()

# ------------------------------------------------------------
# FACE ANALYSIS
# ------------------------------------------------------------

def detect_faces(path):
    img = cv2.imread(path)

    if img is None:
        return None

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape

    faces = FACE_CASCADE.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(30, 30)
    )

    image_area = float(width * height)
    face_rows = []

    largest_face_area_ratio = 0.0
    prominent_face_count = 0

    for idx, (x, y, w, h) in enumerate(faces):
        area_ratio = (w * h) / image_area
        largest_face_area_ratio = max(largest_face_area_ratio, area_ratio)

        if area_ratio >= PROMINENT_FACE_THRESHOLD:
            prominent_face_count += 1

        face_rows.append({
            "face_index": idx,

            "img_width": int(width),
            "img_height": int(height),

            "x": int(x),
            "y": int(y),
            "w": int(w),
            "h": int(h),

            "x_norm": float(x / width),
            "y_norm": float(y / height),
            "w_norm": float(w / width),
            "h_norm": float(h / height),

            "area_ratio": float(area_ratio)
        })

    summary = {
        "width": int(width),
        "height": int(height),
        "face_count": len(face_rows),
        "prominent_face_count": prominent_face_count,
        "largest_face_area_ratio": float(largest_face_area_ratio),
        "has_prominent_face": 1 if prominent_face_count > 0 else 0
    }

    return summary, face_rows

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
    cur.execute("SELECT sha1, rel_fqn FROM media WHERE is_deleted = 0")
    rows = cur.fetchall()

    total = len(rows)
    print(f"Found {total} images to analyze")

    start_time = time.time()
    last_print = start_time
    processed = 0
    unreadable = 0
    with_faces = 0
    total_faces = 0
    current_file = ""

    for sha1, rel_path in rows:
        processed += 1
        current_file = os.path.join(IMAGE_ROOT, rel_path)

        try:
            result = detect_faces(current_file)
        except Exception:
            result = None

        if result is None:
            unreadable += 1
            continue

        summary, face_rows = result

        if summary["face_count"] > 0:
            with_faces += 1
            total_faces += summary["face_count"]

        out_conn.execute("""
        INSERT OR REPLACE INTO image_face_summary
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sha1,
            summary["width"],
            summary["height"],
            summary["face_count"],
            summary["prominent_face_count"],
            summary["largest_face_area_ratio"],
            summary["has_prominent_face"],
            MODEL_VERSION,
            datetime.utcnow().isoformat()
        ))

        out_conn.execute("DELETE FROM image_faces WHERE sha1 = ?", (sha1,))

        for face in face_rows:
            out_conn.execute("""
            INSERT INTO image_faces
            (
                sha1, face_index,
                img_width, img_height,
                x, y, w, h,
                x_norm, y_norm, w_norm, h_norm,
                area_ratio
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sha1,
                face["face_index"],
                face["img_width"],
                face["img_height"],
                face["x"],
                face["y"],
                face["w"],
                face["h"],
                face["x_norm"],
                face["y_norm"],
                face["w_norm"],
                face["h_norm"],
                face["area_ratio"]
            ))

        now = time.time()

        if now - last_print >= PROGRESS_INTERVAL:
            elapsed = now - start_time
            rate = processed / elapsed if elapsed else 0
            remaining = total - processed
            eta = remaining / rate if rate else 0
            pct = (processed / total) * 100 if total else 0

            print(
                f"[Progress] {processed}/{total} | "
                f"{pct:.1f}% | "
                f"{rate:.1f} img/sec | "
                f"ETA {eta/60:.1f}m | "
                f"With faces: {with_faces} | "
                f"Total faces: {total_faces} | "
                f"Unreadable: {unreadable}"
            )
            print(f"Current: {current_file}")

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
    print(f"Images with faces: {with_faces}")
    print(f"Total faces detected: {total_faces}")
    print(f"Unreadable: {unreadable}")
    print(f"Duration: {duration/60:.1f} minutes")

if __name__ == "__main__":
    main()