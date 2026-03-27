import os
import sqlite3
import cv2
import time
from datetime import datetime, timezone

import mediapipe as mp

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

ARCHIVE_DB = r"C:\LifeArchive\archive_index.db"
FACE_DB = r"C:\LifeArchive\face_scores.sqlite"
IMAGE_ROOT = r"C:\LifeArchive"
OUTPUT_DB = r"C:\LifeArchive\face_expression.sqlite"

MODEL_PATH = r"C:\LifeArchive\models\face_landmarker.task"
MODEL_VERSION = "mediapipe_face_landmarker_v1"

PROGRESS_INTERVAL = 5
COMMIT_INTERVAL = 250

# Crop behavior
FACE_PADDING_RATIO = 0.28
MIN_FACE_CROP_SIZE = 224
MAX_FACE_CROP_LONG_EDGE = 1024

# Debug failed crops by writing the cropped face image to disk.
DEBUG_WRITE_FAILED_CROPS = False
DEBUG_FAILED_CROP_DIR = r"C:\LifeArchive\face_expression_debug"
DEBUG_MAX_FAILED_CROPS = 200

# If True, skip images already present in the summary table.
# Good for incremental maintenance runs.
SKIP_ALREADY_SCORED = True

# Optional debug limiter: set > 0 to only process first N images
MAX_IMAGES = 0

# ------------------------------------------------------------
# MEDIAPIPE SETUP
# ------------------------------------------------------------

BaseOptions = mp.tasks.BaseOptions
FaceLandmarker = mp.tasks.vision.FaceLandmarker
FaceLandmarkerOptions = mp.tasks.vision.FaceLandmarkerOptions
VisionRunningMode = mp.tasks.vision.RunningMode


def create_landmarker():
    options = FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=VisionRunningMode.IMAGE,
        output_face_blendshapes=True,
        num_faces=1,  # we are feeding one pre-cropped face at a time
    )
    return FaceLandmarker.create_from_options(options)


# ------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS face_expression (
        sha1 TEXT NOT NULL,
        face_index INTEGER NOT NULL,

        area_ratio REAL,
        confidence REAL,

        smile_left REAL,
        smile_right REAL,
        smile_score REAL,

        blink_left REAL,
        blink_right REAL,
        eyes_open_score REAL,

        squint_left REAL,
        squint_right REAL,
        eye_engagement_score REAL,

        brow_outer_up_left REAL,
        brow_outer_up_right REAL,
        mouth_upper_up_left REAL,
        mouth_upper_up_right REAL,

        smile_asymmetry REAL,
        blink_asymmetry REAL,
        squint_asymmetry REAL,
        asymmetry_score REAL,

        expression_score REAL,

        model_version TEXT,
        scored_at TEXT,

        PRIMARY KEY (sha1, face_index)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS image_face_expression_summary (
        sha1 TEXT PRIMARY KEY,
        face_count_scored INTEGER,
        smiling_face_count INTEGER,
        eyes_open_face_count INTEGER,
        good_expression_face_count INTEGER,
        best_face_expression_score REAL,
        avg_top2_face_expression_score REAL,
        prominent_face_expression_score REAL,
        people_moment_score REAL,
        model_version TEXT,
        scored_at TEXT
    )
    """)

    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_face_expression_sha1
    ON face_expression(sha1)
    """)

    conn.commit()


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def save_failed_crop_image(crop_bgr, sha1, face_index, reason, counter):
    if not DEBUG_WRITE_FAILED_CROPS:
        return
    if crop_bgr is None or crop_bgr.size == 0:
        return
    os.makedirs(DEBUG_FAILED_CROP_DIR, exist_ok=True)
    safe_reason = "".join(ch if ch.isalnum() else "_" for ch in str(reason))[:40]
    filename = f"FACE{counter:04d}_{sha1}_f{face_index}_{safe_reason}.jpg"
    out_path = os.path.join(DEBUG_FAILED_CROP_DIR, filename)
    cv2.imwrite(out_path, crop_bgr)


def crop_face(img, x, y, w, h):
    img_h, img_w = img.shape[:2]

    pad_x = int(round(w * FACE_PADDING_RATIO))
    pad_y = int(round(h * FACE_PADDING_RATIO))

    x0 = clamp(x - pad_x, 0, img_w - 1)
    y0 = clamp(y - pad_y, 0, img_h - 1)
    x1 = clamp(x + w + pad_x, x0 + 1, img_w)
    y1 = clamp(y + h + pad_y, y0 + 1, img_h)

    crop = img[y0:y1, x0:x1]
    if crop is None or crop.size == 0:
        return None

    crop_h, crop_w = crop.shape[:2]
    long_edge = max(crop_w, crop_h)

    # Upscale tiny face crops a little so the model has enough detail.
    if long_edge < MIN_FACE_CROP_SIZE:
        scale = MIN_FACE_CROP_SIZE / float(long_edge)
        new_w = max(1, int(round(crop_w * scale)))
        new_h = max(1, int(round(crop_h * scale)))
        crop = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        crop_h, crop_w = crop.shape[:2]
        long_edge = max(crop_w, crop_h)

    # Downscale absurdly large crops, e.g. Topaz upscaled images.
    if long_edge > MAX_FACE_CROP_LONG_EDGE:
        scale = MAX_FACE_CROP_LONG_EDGE / float(long_edge)
        new_w = max(1, int(round(crop_w * scale)))
        new_h = max(1, int(round(crop_h * scale)))
        crop = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_AREA)

    return crop


def get_blend(blend_dict, name):
    return float(blend_dict.get(name, 0.0))


def analyze_face_crop(landmarker, crop_bgr):
    crop_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=crop_rgb)
    result = landmarker.detect(mp_image)

    if not result.face_blendshapes:
        return None

    # We expect one face because we crop one face at a time.
    face = result.face_blendshapes[0]
    blend = {b.category_name: float(b.score) for b in face}

    smile_left = get_blend(blend, "mouthSmileLeft")
    smile_right = get_blend(blend, "mouthSmileRight")
    smile_score = (smile_left + smile_right) / 2.0

    blink_left = get_blend(blend, "eyeBlinkLeft")
    blink_right = get_blend(blend, "eyeBlinkRight")
    eyes_open_score = 1.0 - ((blink_left + blink_right) / 2.0)
    eyes_open_score = clamp(eyes_open_score, 0.0, 1.0)

    squint_left = get_blend(blend, "eyeSquintLeft")
    squint_right = get_blend(blend, "eyeSquintRight")
    eye_engagement_score = (squint_left + squint_right) / 2.0

    brow_outer_up_left = get_blend(blend, "browOuterUpLeft")
    brow_outer_up_right = get_blend(blend, "browOuterUpRight")

    mouth_upper_up_left = get_blend(blend, "mouthUpperUpLeft")
    mouth_upper_up_right = get_blend(blend, "mouthUpperUpRight")

    smile_asymmetry = abs(smile_left - smile_right)
    blink_asymmetry = abs(blink_left - blink_right)
    squint_asymmetry = abs(squint_left - squint_right)
    asymmetry_score = max(smile_asymmetry, blink_asymmetry, squint_asymmetry)

    # Weighted face-expression score.
    # Discussion-driven goals:
    # - strong smile matters most
    # - eyes-open is hygiene, but should not kill "cheeky" expressions
    # - eye engagement (crow's feet / squint) helps reward authentic smiles
    # - asymmetry adds personality but only as a bonus
    expression_score = (
        0.50 * smile_score +
        0.18 * eyes_open_score +
        0.17 * eye_engagement_score +
        0.10 * asymmetry_score +
        0.05 * ((brow_outer_up_left + brow_outer_up_right + mouth_upper_up_left + mouth_upper_up_right) / 4.0)
    )

    return {
        "smile_left": smile_left,
        "smile_right": smile_right,
        "smile_score": smile_score,
        "blink_left": blink_left,
        "blink_right": blink_right,
        "eyes_open_score": eyes_open_score,
        "squint_left": squint_left,
        "squint_right": squint_right,
        "eye_engagement_score": eye_engagement_score,
        "brow_outer_up_left": brow_outer_up_left,
        "brow_outer_up_right": brow_outer_up_right,
        "mouth_upper_up_left": mouth_upper_up_left,
        "mouth_upper_up_right": mouth_upper_up_right,
        "smile_asymmetry": smile_asymmetry,
        "blink_asymmetry": blink_asymmetry,
        "squint_asymmetry": squint_asymmetry,
        "asymmetry_score": asymmetry_score,
        "expression_score": expression_score,
    }


def fetch_rows(archive_conn, face_conn, out_conn):
    out_cur = out_conn.cursor()
    already_scored = set()
    if SKIP_ALREADY_SCORED:
        out_cur.execute("SELECT sha1 FROM image_face_expression_summary")
        already_scored = {row[0] for row in out_cur.fetchall()}

    archive_cur = archive_conn.cursor()
    archive_cur.execute("SELECT sha1, rel_fqn FROM media WHERE is_deleted = 0 ORDER BY final_dt, rel_fqn")
    media_rows = archive_cur.fetchall()

    face_cur = face_conn.cursor()

    work = []
    for sha1, rel_fqn in media_rows:
        if SKIP_ALREADY_SCORED and sha1 in already_scored:
            continue

        face_cur.execute("""
            SELECT face_index, x, y, w, h, area_ratio, confidence
            FROM image_faces
            WHERE sha1 = ?
            ORDER BY area_ratio DESC, face_index ASC
        """, (sha1,))
        faces = face_cur.fetchall()
        if not faces:
            continue

        work.append((sha1, rel_fqn, faces))

        if MAX_IMAGES and len(work) >= MAX_IMAGES:
            break

    return work


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    if not os.path.exists(MODEL_PATH):
        raise RuntimeError(f"Model not found: {MODEL_PATH}")

    print("Opening archive database...")
    archive_conn = sqlite3.connect(ARCHIVE_DB)

    print("Opening face detection database...")
    face_conn = sqlite3.connect(FACE_DB)

    print("Opening output database...")
    out_conn = sqlite3.connect(OUTPUT_DB)
    init_db(out_conn)

    print("Reading candidate images from archive + face DB...")
    work = fetch_rows(archive_conn, face_conn, out_conn)

    total = len(work)
    print(f"Found {total} images with detected faces to score")
    print(f"Model: {MODEL_VERSION}")
    print(f"Face padding ratio: {FACE_PADDING_RATIO}")
    print(f"Min face crop size: {MIN_FACE_CROP_SIZE}")
    print(f"Max face crop long edge: {MAX_FACE_CROP_LONG_EDGE}")
    print(f"Skip already scored: {SKIP_ALREADY_SCORED}")

    start_time = time.time()
    last_print = start_time

    processed = 0
    unreadable = 0
    faces_scored = 0
    faces_failed = 0
    current_file = ""
    failed_crop_dump_count = 0

    if DEBUG_WRITE_FAILED_CROPS:
        os.makedirs(DEBUG_FAILED_CROP_DIR, exist_ok=True)
        print(f"Failed face crop debug dir: {DEBUG_FAILED_CROP_DIR}")
        print(f"Max failed crops to dump: {DEBUG_MAX_FAILED_CROPS}")

    landmarker = create_landmarker()

    try:
        for sha1, rel_path, faces in work:
            processed += 1
            current_file = os.path.join(IMAGE_ROOT, rel_path)

            img = cv2.imread(current_file)
            if img is None:
                unreadable += 1
                continue

            out_conn.execute("DELETE FROM face_expression WHERE sha1 = ?", (sha1,))

            face_records = []

            for face in faces:
                face_index, x, y, w, h, area_ratio, confidence = face
                crop = crop_face(img, int(x), int(y), int(w), int(h))
                if crop is None:
                    faces_failed += 1
                    continue

                try:
                    result = analyze_face_crop(landmarker, crop)
                except Exception:
                    result = None

                if result is None:
                    faces_failed += 1
                    if DEBUG_WRITE_FAILED_CROPS and failed_crop_dump_count < DEBUG_MAX_FAILED_CROPS:
                        failed_crop_dump_count += 1
                        save_failed_crop_image(crop, sha1, face_index, "mediapipe_failed", failed_crop_dump_count)
                        print(
                            f"[FaceFail] dump={failed_crop_dump_count} "
                            f"sha1={sha1} face_index={face_index} "
                            f"area_ratio={float(area_ratio):.6f} "
                            f"box=({int(x)},{int(y)},{int(w)},{int(h)}) "
                            f"file={current_file}"
                        )
                    continue

                faces_scored += 1
                face_records.append({
                    "sha1": sha1,
                    "face_index": int(face_index),
                    "area_ratio": float(area_ratio),
                    "confidence": float(confidence),
                    **result
                })

            # Store per-face rows
            scored_at = datetime.now(timezone.utc).isoformat()

            for rec in face_records:
                out_conn.execute("""
                    INSERT OR REPLACE INTO face_expression (
                        sha1, face_index,
                        area_ratio, confidence,
                        smile_left, smile_right, smile_score,
                        blink_left, blink_right, eyes_open_score,
                        squint_left, squint_right, eye_engagement_score,
                        brow_outer_up_left, brow_outer_up_right,
                        mouth_upper_up_left, mouth_upper_up_right,
                        smile_asymmetry, blink_asymmetry, squint_asymmetry, asymmetry_score,
                        expression_score,
                        model_version, scored_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rec["sha1"], rec["face_index"],
                    rec["area_ratio"], rec["confidence"],
                    rec["smile_left"], rec["smile_right"], rec["smile_score"],
                    rec["blink_left"], rec["blink_right"], rec["eyes_open_score"],
                    rec["squint_left"], rec["squint_right"], rec["eye_engagement_score"],
                    rec["brow_outer_up_left"], rec["brow_outer_up_right"],
                    rec["mouth_upper_up_left"], rec["mouth_upper_up_right"],
                    rec["smile_asymmetry"], rec["blink_asymmetry"], rec["squint_asymmetry"], rec["asymmetry_score"],
                    rec["expression_score"],
                    MODEL_VERSION, scored_at
                ))

            # Summary row
            if face_records:
                ranked = sorted(
                    face_records,
                    key=lambda r: (r["expression_score"] * max(r["area_ratio"], 0.000001)),
                    reverse=True
                )

                expression_scores = [r["expression_score"] for r in ranked]
                weighted_scores = [r["expression_score"] * r["area_ratio"] for r in ranked]

                best_face_expression_score = expression_scores[0]
                avg_top2_face_expression_score = (
                    sum(expression_scores[:2]) / min(2, len(expression_scores))
                )
                prominent_face_expression_score = weighted_scores[0]

                smiling_face_count = sum(1 for r in face_records if r["smile_score"] >= 0.60)
                eyes_open_face_count = sum(1 for r in face_records if r["eyes_open_score"] >= 0.55)
                good_expression_face_count = sum(1 for r in face_records if r["expression_score"] >= 0.65)

                # Main per-image signal used later by culling.
                # Top faces matter most; tiny background faces should not dominate.
                people_moment_score = 0.0
                for i, r in enumerate(ranked[:3]):
                    rank_weight = 1.00 if i == 0 else (0.70 if i == 1 else 0.45)
                    prominence = min(1.0, max(0.10, r["area_ratio"] / 0.05))
                    people_moment_score += r["expression_score"] * rank_weight * prominence

                out_conn.execute("""
                    INSERT OR REPLACE INTO image_face_expression_summary (
                        sha1,
                        face_count_scored,
                        smiling_face_count,
                        eyes_open_face_count,
                        good_expression_face_count,
                        best_face_expression_score,
                        avg_top2_face_expression_score,
                        prominent_face_expression_score,
                        people_moment_score,
                        model_version,
                        scored_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sha1,
                    len(face_records),
                    smiling_face_count,
                    eyes_open_face_count,
                    good_expression_face_count,
                    best_face_expression_score,
                    avg_top2_face_expression_score,
                    prominent_face_expression_score,
                    people_moment_score,
                    MODEL_VERSION,
                    scored_at
                ))
            else:
                out_conn.execute("DELETE FROM image_face_expression_summary WHERE sha1 = ?", (sha1,))

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
                    f"Faces scored: {faces_scored} | "
                    f"Face failures: {faces_failed} | "
                    f"Unreadable: {unreadable}"
                )
                print(f"Current: {current_file}")
                last_print = now

            if processed % COMMIT_INTERVAL == 0:
                out_conn.commit()
                print("[Checkpoint] committed")

        out_conn.commit()

    finally:
        landmarker.close()
        archive_conn.close()
        face_conn.close()
        out_conn.close()

    duration = time.time() - start_time

    print("\n--- COMPLETE ---")
    print(f"Processed images: {processed}")
    print(f"Faces scored: {faces_scored}")
    print(f"Face scoring failures: {faces_failed}")
    print(f"Failed crops dumped: {failed_crop_dump_count}")
    print(f"Unreadable images: {unreadable}")
    print(f"Duration: {duration/60:.1f} minutes")


if __name__ == "__main__":
    main()
