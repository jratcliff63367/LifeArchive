import os
import sqlite3
import cv2
import time
import urllib.request
from datetime import datetime

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

ARCHIVE_DB = r"C:\LifeArchive\archive_index.db"
IMAGE_ROOT = r"C:\LifeArchive"
OUTPUT_DB = r"C:\LifeArchive\face_scores.sqlite"

MODEL_DIR = r"C:\LifeArchive\models"
MODEL_PATH = os.path.join(MODEL_DIR, "face_detection_yunet_2023mar.onnx")

# Official OpenCV YuNet model mirror
MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

MODEL_VERSION = "opencv_yunet_v2_scaled"

PROGRESS_INTERVAL = 5
COMMIT_INTERVAL = 500

# Detection parameters
SCORE_THRESHOLD = 0.70
NMS_THRESHOLD = 0.30
TOP_K = 5000
PROMINENT_FACE_THRESHOLD = 0.05

# Normalize images before detection.
# The image is resized so that max(width, height) == DETECTION_MAX_DIM,
# preserving aspect ratio. Smaller images are left unchanged unless
# UPSCALE_SMALL_IMAGES is set True.
DETECTION_MAX_DIM = 1800
UPSCALE_SMALL_IMAGES = False

# ------------------------------------------------------------
# MODEL SETUP
# ------------------------------------------------------------

def ensure_model():
    os.makedirs(MODEL_DIR, exist_ok=True)
    if os.path.exists(MODEL_PATH):
        return
    print(f"Downloading YuNet model to: {MODEL_PATH}")
    urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    print("Model download complete.")


def create_detector(width: int, height: int):
    return cv2.FaceDetectorYN.create(
        MODEL_PATH,
        "",
        (width, height),
        SCORE_THRESHOLD,
        NMS_THRESHOLD,
        TOP_K
    )

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
        confidence REAL NOT NULL,

        right_eye_x REAL,
        right_eye_y REAL,
        left_eye_x REAL,
        left_eye_y REAL,
        nose_x REAL,
        nose_y REAL,
        mouth_right_x REAL,
        mouth_right_y REAL,
        mouth_left_x REAL,
        mouth_left_y REAL,

        PRIMARY KEY (sha1, face_index)
    )
    """)

    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_image_faces_sha1
    ON image_faces(sha1)
    """)

    conn.commit()

def load_existing_versions(conn):
    cur = conn.cursor()
    cur.execute("SELECT sha1, model_version FROM image_face_summary")
    return {str(sha1): (model_version or "") for sha1, model_version in cur.fetchall()}

# ------------------------------------------------------------
# IMAGE NORMALIZATION
# ------------------------------------------------------------

def normalize_for_detection(img):
    """
    Resize image so detector sees a consistent scale.

    Returns:
        resized_img,
        scale_x_to_original,
        scale_y_to_original
    """
    orig_h, orig_w = img.shape[:2]
    max_dim = max(orig_w, orig_h)

    if max_dim == 0:
        return img, 1.0, 1.0

    if max_dim <= DETECTION_MAX_DIM and not UPSCALE_SMALL_IMAGES:
        return img, 1.0, 1.0

    scale = DETECTION_MAX_DIM / float(max_dim)
    new_w = max(1, int(round(orig_w * scale)))
    new_h = max(1, int(round(orig_h * scale)))

    resized = cv2.resize(
        img,
        (new_w, new_h),
        interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    )

    # Convert detection-space coords back to original-space coords
    scale_x_to_original = orig_w / float(new_w)
    scale_y_to_original = orig_h / float(new_h)

    return resized, scale_x_to_original, scale_y_to_original

# ------------------------------------------------------------
# FACE ANALYSIS
# ------------------------------------------------------------

def detect_faces(path):
    img = cv2.imread(path)
    if img is None:
        return None

    orig_h, orig_w = img.shape[:2]
    det_img, scale_x, scale_y = normalize_for_detection(img)
    det_h, det_w = det_img.shape[:2]

    detector = create_detector(det_w, det_h)
    _, faces = detector.detect(det_img)

    image_area = float(orig_w * orig_h)
    face_rows = []
    largest_face_area_ratio = 0.0
    prominent_face_count = 0

    if faces is None:
        faces = []

    for idx, face in enumerate(faces):
        # YuNet output:
        # [x, y, w, h, right_eye_x, right_eye_y, left_eye_x, left_eye_y,
        #  nose_x, nose_y, mouth_right_x, mouth_right_y,
        #  mouth_left_x, mouth_left_y, score]
        x, y, w, h = face[0], face[1], face[2], face[3]
        score = float(face[14])

        # Map detection coords back to original image coords
        x = max(0, int(round(x * scale_x)))
        y = max(0, int(round(y * scale_y)))
        w = max(1, int(round(w * scale_x)))
        h = max(1, int(round(h * scale_y)))

        # Clamp to original image bounds
        if x >= orig_w or y >= orig_h:
            continue
        if x + w > orig_w:
            w = max(1, orig_w - x)
        if y + h > orig_h:
            h = max(1, orig_h - y)

        area_ratio = (w * h) / image_area
        largest_face_area_ratio = max(largest_face_area_ratio, area_ratio)

        if area_ratio >= PROMINENT_FACE_THRESHOLD:
            prominent_face_count += 1

        face_rows.append({
            "face_index": idx,
            "img_width": int(orig_w),
            "img_height": int(orig_h),
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "x_norm": float(x / orig_w),
            "y_norm": float(y / orig_h),
            "w_norm": float(w / orig_w),
            "h_norm": float(h / orig_h),
            "area_ratio": float(area_ratio),
            "confidence": score,
            "right_eye_x": float(face[4] * scale_x),
            "right_eye_y": float(face[5] * scale_y),
            "left_eye_x": float(face[6] * scale_x),
            "left_eye_y": float(face[7] * scale_y),
            "nose_x": float(face[8] * scale_x),
            "nose_y": float(face[9] * scale_y),
            "mouth_right_x": float(face[10] * scale_x),
            "mouth_right_y": float(face[11] * scale_y),
            "mouth_left_x": float(face[12] * scale_x),
            "mouth_left_y": float(face[13] * scale_y),
        })

    summary = {
        "width": int(orig_w),
        "height": int(orig_h),
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
    ensure_model()

    print("Opening archive database...")
    archive_conn = sqlite3.connect(ARCHIVE_DB)

    print("Opening output database...")
    out_conn = sqlite3.connect(OUTPUT_DB)
    init_db(out_conn)

    existing_versions = load_existing_versions(out_conn)
    print(f"Loaded {len(existing_versions)} existing face-summary rows")

    cur = archive_conn.cursor()
    print("Reading archive index...")
    cur.execute("SELECT sha1, rel_fqn FROM media WHERE is_deleted = 0")
    all_rows = cur.fetchall()

    rows = []
    skipped_existing = 0
    for sha1, rel_path in all_rows:
        if existing_versions.get(str(sha1), "") == MODEL_VERSION:
            skipped_existing += 1
            continue
        rows.append((sha1, rel_path))

    total = len(rows)
    print(f"Found {len(all_rows)} active images total")
    print(f"Skipping {skipped_existing} already scored with model {MODEL_VERSION}")
    print(f"Found {total} images to analyze")
    print(f"Detection max dimension: {DETECTION_MAX_DIM}")
    print(f"Upscale small images: {UPSCALE_SMALL_IMAGES}")
    print(f"Score threshold: {SCORE_THRESHOLD}")

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
            INSERT INTO image_faces (
                sha1, face_index,
                img_width, img_height,
                x, y, w, h,
                x_norm, y_norm, w_norm, h_norm,
                area_ratio, confidence,
                right_eye_x, right_eye_y,
                left_eye_x, left_eye_y,
                nose_x, nose_y,
                mouth_right_x, mouth_right_y,
                mouth_left_x, mouth_left_y
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                face["area_ratio"],
                face["confidence"],
                face["right_eye_x"],
                face["right_eye_y"],
                face["left_eye_x"],
                face["left_eye_y"],
                face["nose_x"],
                face["nose_y"],
                face["mouth_right_x"],
                face["mouth_right_y"],
                face["mouth_left_x"],
                face["mouth_left_y"],
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
    print(f"Skipped existing: {skipped_existing}")
    print(f"Duration: {duration/60:.1f} minutes")

if __name__ == "__main__":
    main()