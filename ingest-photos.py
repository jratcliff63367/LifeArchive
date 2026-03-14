import os
import sqlite3
import hashlib
import time
import shutil
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ExifTags, ImageOps

# ------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------

SOURCES = [
r"C:\Photos from 2025",

]

DEST_ROOT = r"C:\website-photos"

DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")

MIN_WIDTH = 500
MIN_HEIGHT = 300

PROGRESS_INTERVAL = 10
COMMIT_INTERVAL = 100

MIN_VALID_YEAR = 1950
MAX_VALID_YEAR = 2026

# ------------------------------------------------------------
# UTILITY FUNCTIONS
# ------------------------------------------------------------

EXIF_TAG_NAME_BY_ID = ExifTags.TAGS
EXIF_DATETIME_TAGS = {
    "DateTimeOriginal",
    "DateTimeDigitized",
    "DateTime",
}
XMP_DATE_PATTERNS = [
    r'(?:xmp:CreateDate|photoshop:DateCreated|xmp:ModifyDate|exif:DateTimeOriginal)\s*=\s*"([^"]+)"',
    r'<(?:xmp:CreateDate|photoshop:DateCreated|xmp:ModifyDate|exif:DateTimeOriginal)>([^<]+)</',
]
FILENAME_DATE_PATTERNS = [
    # PXL_20250331_194558354.jpg
    re.compile(r'(?<!\d)(20\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d)'),
    # IMG_2025-03-31_19-45-58
    re.compile(r'(?<!\d)(20\d{2})[-_](\d{2})[-_](\d{2})[-_](\d{2})[-_](\d{2})[-_](\d{2})(?!\d)'),
    # 20250331_194558
    re.compile(r'(?<!\d)(20\d{2})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})(?!\d)'),
]

def compute_sha1(path: str) -> str:
    sha1 = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            data = f.read(65536)
            if not data:
                break
            sha1.update(data)
    return sha1.hexdigest()


def safe_datetime_from_timestamp(ts):
    try:
        if ts is None or ts <= 0:
            return None
        return datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def is_valid_dt(dt):
    return dt is not None and MIN_VALID_YEAR <= dt.year <= MAX_VALID_YEAR


def strip_tz(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def parse_exif_style_datetime(value: str):
    if not value:
        return None
    value = value.strip().replace("\x00", "")
    # Common EXIF style
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if is_valid_dt(dt) else None
        except Exception:
            pass
    # ISO-like forms
    value2 = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value2)
        dt = strip_tz(dt)
        return dt if is_valid_dt(dt) else None
    except Exception:
        return None


def get_pillow_exif_datetimes(path: str):
    found = []
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                for tag_id, value in exif.items():
                    tag_name = EXIF_TAG_NAME_BY_ID.get(tag_id, tag_id)
                    if tag_name in EXIF_DATETIME_TAGS and value:
                        dt = parse_exif_style_datetime(str(value))
                        if is_valid_dt(dt):
                            found.append((tag_name, dt))
    except Exception:
        pass
    return found


def extract_xmp_datetimes(path: str):
    found = []
    try:
        with open(path, "rb") as f:
            data = f.read(512 * 1024)  # enough for APP1/XMP in normal files
        text = data.decode("utf-8", errors="ignore")
        for pattern in XMP_DATE_PATTERNS:
            for match in re.finditer(pattern, text):
                dt = parse_exif_style_datetime(match.group(1))
                if is_valid_dt(dt):
                    found.append(("XMP", dt))
    except Exception:
        pass
    return found


def google_takeout_sidecar_candidates(file_path: str):
    p = Path(file_path)
    candidates = []

    # Most common: IMG_1234.JPG.json
    candidates.append(p.with_name(p.name + ".json"))

    # Some workflows may strip extension before .json
    candidates.append(p.with_suffix(p.suffix + ".json"))
    candidates.append(p.with_suffix(".json"))

    # Deduplicate while preserving order
    seen = set()
    out = []
    for c in candidates:
        s = str(c)
        if s not in seen:
            out.append(c)
            seen.add(s)
    return out


def get_google_takeout_datetime(file_path: str):
    for candidate in google_takeout_sidecar_candidates(file_path):
        if not candidate.exists():
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Common Google Photos Takeout schema
            ts = None
            if isinstance(data, dict):
                photo_taken = data.get("photoTakenTime")
                if isinstance(photo_taken, dict):
                    ts = photo_taken.get("timestamp")

                if ts is None:
                    creation_time = data.get("creationTime")
                    if isinstance(creation_time, dict):
                        ts = creation_time.get("timestamp")

                if ts is None:
                    # Sometimes timestamp-like values may be present in nested metadata
                    for key in ("timestamp",):
                        if key in data and str(data.get(key)).isdigit():
                            ts = data.get(key)
                            break

            if ts is not None:
                try:
                    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().replace(tzinfo=None)
                    if is_valid_dt(dt):
                        return dt, f"Google Takeout JSON: {candidate.name}"
                except Exception:
                    pass
        except Exception:
            continue
    return None, None


def get_filename_datetime(file_path: str):
    name = Path(file_path).name
    for pattern in FILENAME_DATE_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        try:
            parts = [int(x) for x in m.groups()]
            dt = datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5])
            if is_valid_dt(dt):
                return dt
        except Exception:
            continue
    return None


def get_best_datetime(file_path: str):
    # 1) Standard EXIF date fields
    exif_found = get_pillow_exif_datetimes(file_path)
    priority = {
        "DateTimeOriginal": 0,
        "DateTimeDigitized": 1,
        "DateTime": 2,
    }
    if exif_found:
        exif_found.sort(key=lambda x: priority.get(x[0], 99))
        tag_name, dt = exif_found[0]
        return dt, f"EXIF: {tag_name}"

    # 2) XMP metadata embedded in the file
    xmp_found = extract_xmp_datetimes(file_path)
    if xmp_found:
        _, dt = xmp_found[0]
        return dt, "XMP"

    # 3) Google Takeout sidecar JSON
    dt, src = get_google_takeout_datetime(file_path)
    if dt is not None:
        return dt, src

    # 4) Filename-derived datetime (useful for Pixel / phone exports)
    dt = get_filename_datetime(file_path)
    if dt is not None:
        return dt, "Filename Pattern"

    return None, None


def get_image_size(path: str):
    try:
        with Image.open(path) as img:
            return img.width, img.height
    except Exception:
        return None, None


def generate_thumbnail(image_path: str, thumb_path: str):
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((400, 400))
        img.convert("RGB").save(thumb_path, "JPEG", quality=85)


def resolve_file_date(file_path: str, archive_rel_path: str):
    if "undated" in archive_rel_path.lower():
        return "0000-00-00 00:00:00", "Path Override: Undated"

    best_dt, best_source = get_best_datetime(file_path)
    if is_valid_dt(best_dt):
        return best_dt.strftime("%Y-%m-%d %H:%M:%S"), best_source

    try:
        mtime = os.path.getmtime(file_path)
        internal_dt = safe_datetime_from_timestamp(mtime)
        if internal_dt is not None:
            return internal_dt.strftime("%Y-%m-%d %H:%M:%S"), "File Modification"
    except OSError:
        pass

    return "0000-00-00 00:00:00", "Fallback"


def ensure_parent_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


# ------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media (
            sha1 TEXT PRIMARY KEY,
            rel_fqn TEXT NOT NULL,
            original_filename TEXT,
            path_tags TEXT,
            final_dt TEXT,
            dt_source TEXT,
            is_deleted INTEGER DEFAULT 0,
            custom_notes TEXT DEFAULT '',
            custom_tags TEXT DEFAULT ''
        )
    """)
    conn.commit()


def load_existing_sha1s(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("SELECT sha1 FROM media")
    return {row[0] for row in cur.fetchall()}


def insert_media(conn: sqlite3.Connection, sha1: str, rel_fqn: str,
                 original_filename: str, path_tags: str,
                 final_dt: str, dt_source: str):
    conn.execute("""
        INSERT OR IGNORE INTO media
        (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source, is_deleted, custom_notes, custom_tags)
        VALUES (?, ?, ?, ?, ?, ?, 0, '', '')
    """, (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source))


# ------------------------------------------------------------
# TAG HELPERS
# ------------------------------------------------------------

def make_path_tags(source_base: str, rel_dir: str) -> str:
    parts = []

    if source_base and source_base != ".":
        parts.append(source_base)

    if rel_dir and rel_dir != ".":
        parts.extend(Path(rel_dir).parts)

    cleaned = []
    for p in parts:
        p = p.strip().replace("_", " ").replace("-", " ")
        if p:
            cleaned.append(p)

    return "|".join(cleaned)


# ------------------------------------------------------------
# INGEST LOGIC
# ------------------------------------------------------------

def run_ingest():
    print("--- STARTING ROBUST INGEST (V4.5 Robust Date Extraction) ---")

    os.makedirs(DEST_ROOT, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    existing_sha1s = load_existing_sha1s(conn)

    examined = 0
    new_added = 0
    healed = 0
    skipped = 0
    tiny = 0
    unreadable = 0
    warnings = 0

    start_time = time.time()
    last_print = start_time
    since_commit = 0
    current_file = ""

    for source_root in SOURCES:
        source_root = os.path.normpath(source_root)
        source_base = os.path.basename(source_root.rstrip("\\/"))

        print(f"[Source] Scanning: {source_root}")

        for root, dirs, files in os.walk(source_root):
            for name in files:
                examined += 1
                since_commit += 1

                full_path = os.path.join(root, name)
                current_file = full_path

                rel_dir = os.path.relpath(root, source_root)
                if rel_dir == ".":
                    rel_dir = ""

                archive_rel_path = os.path.join(source_base, rel_dir) if rel_dir else source_base
                rel_fqn = os.path.join(source_base, rel_dir, name) if rel_dir else os.path.join(source_base, name)

                dest_path = os.path.join(DEST_ROOT, rel_fqn)

                try:
                    width, height = get_image_size(full_path)

                    if width is None or height is None:
                        unreadable += 1
                        continue

                    if width < MIN_WIDTH or height < MIN_HEIGHT:
                        tiny += 1
                        continue

                    sha1 = compute_sha1(full_path)

                    if sha1 in existing_sha1s:
                        skipped += 1
                        continue

                    try:
                        final_dt, source_label = resolve_file_date(full_path, archive_rel_path)
                    except Exception:
                        warnings += 1
                        final_dt, source_label = "0000-00-00 00:00:00", "Date Error Fallback"

                    path_tags = make_path_tags(source_base, rel_dir)

                    ensure_parent_dir(dest_path)

                    if not os.path.exists(dest_path):
                        shutil.copy2(full_path, dest_path)
                        new_added += 1
                    else:
                        healed += 1

                    insert_media(
                        conn,
                        sha1=sha1,
                        rel_fqn=rel_fqn.replace("\\", "/"),
                        original_filename=name,
                        path_tags=path_tags,
                        final_dt=final_dt,
                        dt_source=source_label,
                    )
                    existing_sha1s.add(sha1)

                    thumb_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
                    if not os.path.exists(thumb_path):
                        try:
                            generate_thumbnail(dest_path, thumb_path)
                        except Exception:
                            warnings += 1

                except Exception:
                    warnings += 1

                now = time.time()

                if now - last_print >= PROGRESS_INTERVAL:
                    elapsed = now - start_time
                    rate = examined / elapsed if elapsed > 0 else 0.0

                    print(
                        f"[Heartbeat] Examined: {examined} | "
                        f"New: {new_added} | "
                        f"Healed: {healed} | "
                        f"Skipped: {skipped} | "
                        f"Tiny: {tiny} | "
                        f"Unreadable: {unreadable} | "
                        f"Warnings: {warnings} | "
                        f"{rate:.1f} files/sec"
                    )
                    print(f"            Current: {current_file}")

                    last_print = now

                if since_commit >= COMMIT_INTERVAL:
                    conn.commit()
                    print(f"[Checkpoint] committed at examined={examined}")
                    since_commit = 0

    conn.commit()
    conn.close()

    duration = time.time() - start_time

    print("\n--- INGEST COMPLETE ---")
    print(f"Duration: {duration:.1f}s")
    print(f"Examined: {examined}")
    print(f"New added: {new_added}")
    print(f"Files healed: {healed}")
    print(f"Duplicates skipped: {skipped}")
    print(f"Tiny images skipped: {tiny}")
    print(f"Unreadable images skipped: {unreadable}")
    print(f"Warnings: {warnings}")


if __name__ == "__main__":
    run_ingest()
