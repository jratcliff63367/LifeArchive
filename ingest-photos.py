#!/usr/bin/env python3
"""
Life Archive ingestion script.

Merged, robust version based on the older working ingestor, with later bug fixes retained:

Retained baseline behaviors
- explicit SOURCE_DIRECTORIES inputs
- copy/stage files under DEST_ROOT
- exact duplicate skipping by SHA1
- heartbeat logging during long runs
- checkpoint commits during ingest
- valid partially-populated SQLite DB if interrupted
- robust date extraction from EXIF / XMP / Google Takeout JSON / filename / mtime
- tiny/unreadable image skipping

Retained later fixes
- JPEG-only ingestion (.jpg / .jpeg)
- explicit GPS IFD decoding from Pillow
- expanded media schema:
    latitude, longitude, altitude_meters
    width, height
    extension, file_size, mtime_utc
- single-file INSPECT_FILE mode for diagnostics
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

# ------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------

SOURCE_DIRECTORIES = [
     r"C:\Terry's Photos",
     r"C:\PhotoArchive",
]

DEST_ROOT = r"c:\LifeArchive"

# Mode:
#   "ingest"  = scan SOURCE_DIRECTORIES, copy into DEST_ROOT, update DB incrementally
#   "rebuild" = scan files already under DEST_ROOT, do not copy, rebuild/update metadata in place
MODE = "ingest"   # "ingest" or "rebuild"

# Rebuild options (used only when MODE == "rebuild")
# If True, delete and fully rebuild the SQLite database from files already inside DEST_ROOT.
REBUILD_SQLITE = False
# If True, delete _thumbs and regenerate thumbnails from files already inside DEST_ROOT.
REBUILD_THUMBNAILS = False

DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")

# JPEG-only by design.
ALLOWED_EXTENSIONS = {".jpg", ".jpeg"}

# Filter out tiny junk / UI fragments.
MIN_WIDTH = 500
MIN_HEIGHT = 300

# Logging / checkpointing
PROGRESS_INTERVAL = 10          # seconds
COMMIT_INTERVAL = 100           # processed files between DB commits
DIRECTORY_LOG_INTERVAL = 250    # directories walked between scan logs

MIN_VALID_YEAR = 1950
MAX_VALID_YEAR = 2026

# Set to a single file path to inspect parsing instead of running ingest.
INSPECT_FILE = ""

# ------------------------------------------------------------
# UTILITY CONSTANTS
# ------------------------------------------------------------

Image.MAX_IMAGE_PIXELS = None

EXIF_TAG_NAME_BY_ID = ExifTags.TAGS
EXIF_DATETIME_TAGS = {
    "DateTimeOriginal",
    "DateTimeDigitized",
    "DateTime",
}
GPS_TAG_NAME_BY_ID = ExifTags.GPSTAGS
GPS_IFD_ENUM = getattr(getattr(ExifTags, "IFD", object()), "GPSInfo", 34853)

XMP_DATE_PATTERNS = [
    r'(?:xmp:CreateDate|photoshop:DateCreated|xmp:ModifyDate|exif:DateTimeOriginal)\s*=\s*"([^"]+)"',
    r'<(?:xmp:CreateDate|photoshop:DateCreated|xmp:ModifyDate|exif:DateTimeOriginal)>([^<]+)</',
]

FILENAME_DATE_PATTERNS = [
    re.compile(r'(?<!\d)(20\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d)'),
    re.compile(r'(?<!\d)(20\d{2})[-_](\d{2})[-_](\d{2})[-_](\d{2})[-_](\d{2})[-_](\d{2})(?!\d)'),
    re.compile(r'(?<!\d)(20\d{2})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})(?!\d)'),
]

# ------------------------------------------------------------
# UTILITY FUNCTIONS
# ------------------------------------------------------------

def compute_sha1(path: str) -> str:
    sha1 = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            data = f.read(65536)
            if not data:
                break
            sha1.update(data)
    return sha1.hexdigest()


def file_extension(path: str) -> str:
    return os.path.splitext(path)[1].lower()


NON_JPEG_IMAGE_EXTENSIONS = {
    ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".jfif",
}


def is_non_jpeg_image_file(path: str) -> bool:
    return file_extension(path) in NON_JPEG_IMAGE_EXTENSIONS


def should_skip_dir(dirname: str) -> bool:
    return dirname.startswith("_")


def rebuild_file_iter(dest_root: str):
    """
    In rebuild mode, scan files already under DEST_ROOT.
    Ignore _thumbs, _trash, and any folder beginning with "_".
    """
    for root, dirs, files in os.walk(dest_root):
        dirs[:] = [d for d in dirs if not should_skip_dir(d)]
        yield root, dirs, files


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
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if is_valid_dt(dt) else None
        except Exception:
            pass
    value2 = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value2)
        dt = strip_tz(dt)
        return dt if is_valid_dt(dt) else None
    except Exception:
        return None


def maybe_float(value):
    if value is None:
        return None
    try:
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            den = float(value.denominator)
            if den == 0:
                return None
            return float(value.numerator) / den
        if isinstance(value, tuple) and len(value) == 2:
            den = float(value[1])
            if den == 0:
                return None
            return float(value[0]) / den
        return float(value)
    except Exception:
        return None


def dms_to_deg(values, ref):
    try:
        vals = list(values)
        if len(vals) != 3:
            return None
        deg = maybe_float(vals[0])
        mins = maybe_float(vals[1])
        secs = maybe_float(vals[2])
        if deg is None or mins is None or secs is None:
            return None
        out = deg + (mins / 60.0) + (secs / 3600.0)
        ref_s = str(ref).upper()
        if ref_s in {"S", "W"}:
            out = -out
        return out
    except Exception:
        return None


def get_pillow_metadata(path: str):
    """
    Open once and extract:
    - width/height
    - EXIF datetimes
    - GPS info
    """
    width = height = None
    exif_found = []
    latitude = longitude = altitude_meters = None
    gps_present = False

    try:
        with Image.open(path) as img:
            width, height = img.width, img.height
            exif = img.getexif()
            if exif:
                for tag_id, value in exif.items():
                    tag_name = EXIF_TAG_NAME_BY_ID.get(tag_id, tag_id)
                    if tag_name in EXIF_DATETIME_TAGS and value:
                        dt = parse_exif_style_datetime(str(value))
                        if is_valid_dt(dt):
                            exif_found.append((tag_name, dt))

                gps_ifd = None
                try:
                    gps_ifd = exif.get_ifd(GPS_IFD_ENUM)
                except Exception:
                    gps_ifd = None

                if not gps_ifd:
                    try:
                        raw = exif.get(34853)
                        if isinstance(raw, dict):
                            gps_ifd = raw
                    except Exception:
                        gps_ifd = None

                if isinstance(gps_ifd, dict) and gps_ifd:
                    gps_present = True
                    gps_named = {GPS_TAG_NAME_BY_ID.get(k, k): v for k, v in gps_ifd.items()}
                    latitude = dms_to_deg(gps_named.get("GPSLatitude"), gps_named.get("GPSLatitudeRef"))
                    longitude = dms_to_deg(gps_named.get("GPSLongitude"), gps_named.get("GPSLongitudeRef"))
                    altitude_meters = maybe_float(gps_named.get("GPSAltitude"))
                    alt_ref = gps_named.get("GPSAltitudeRef")
                    try:
                        alt_ref_num = int(alt_ref) if alt_ref is not None else None
                    except Exception:
                        alt_ref_num = 1 if str(alt_ref) == "b'\\x01'" else 0
                    if altitude_meters is not None and alt_ref_num == 1:
                        altitude_meters = -altitude_meters
    except Exception:
        pass

    return {
        "width": width,
        "height": height,
        "exif_found": exif_found,
        "latitude": latitude,
        "longitude": longitude,
        "altitude_meters": altitude_meters,
        "gps_present": gps_present,
    }


def extract_xmp_datetimes(path: str):
    found = []
    try:
        with open(path, "rb") as f:
            data = f.read(512 * 1024)
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
    candidates.append(p.with_name(p.name + ".json"))
    candidates.append(p.with_suffix(p.suffix + ".json"))
    candidates.append(p.with_suffix(".json"))

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
                    if "timestamp" in data and str(data.get("timestamp")).isdigit():
                        ts = data.get("timestamp")

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
    pillow_meta = get_pillow_metadata(file_path)

    exif_found = pillow_meta["exif_found"]
    priority = {
        "DateTimeOriginal": 0,
        "DateTimeDigitized": 1,
        "DateTime": 2,
    }
    if exif_found:
        exif_found.sort(key=lambda x: priority.get(x[0], 99))
        tag_name, dt = exif_found[0]
        return dt, f"EXIF: {tag_name}", pillow_meta

    xmp_found = extract_xmp_datetimes(file_path)
    if xmp_found:
        _, dt = xmp_found[0]
        return dt, "XMP", pillow_meta

    dt, src = get_google_takeout_datetime(file_path)
    if dt is not None:
        return dt, src, pillow_meta

    dt = get_filename_datetime(file_path)
    if dt is not None:
        return dt, "Filename Pattern", pillow_meta

    return None, None, pillow_meta


def resolve_file_date(file_path: str, archive_rel_path: str):
    if "undated" in archive_rel_path.lower():
        return "0000-00-00 00:00:00", "Path Override: Undated", get_pillow_metadata(file_path)

    best_dt, best_source, pillow_meta = get_best_datetime(file_path)
    if is_valid_dt(best_dt):
        return best_dt.strftime("%Y-%m-%d %H:%M:%S"), best_source, pillow_meta

    try:
        mtime = os.path.getmtime(file_path)
        internal_dt = safe_datetime_from_timestamp(mtime)
        if internal_dt is not None:
            return internal_dt.strftime("%Y-%m-%d %H:%M:%S"), "File Modification", pillow_meta
    except OSError:
        pass

    return "0000-00-00 00:00:00", "Fallback", pillow_meta


def ensure_parent_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def generate_thumbnail(image_path: str, thumb_path: str):
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((400, 400))
        img.convert("RGB").save(thumb_path, "JPEG", quality=85)


# ------------------------------------------------------------
# DATABASE HELPERS
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
            custom_tags TEXT DEFAULT '',
            width INTEGER DEFAULT 0,
            height INTEGER DEFAULT 0,
            latitude REAL,
            longitude REAL,
            altitude_meters REAL,
            extension TEXT DEFAULT '',
            file_size INTEGER DEFAULT 0,
            mtime_utc TEXT DEFAULT ''
        )
    """)

    # Upgrade older schemas in place.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(media)").fetchall()}
    needed = {
        "width": "INTEGER DEFAULT 0",
        "height": "INTEGER DEFAULT 0",
        "latitude": "REAL",
        "longitude": "REAL",
        "altitude_meters": "REAL",
        "extension": "TEXT DEFAULT ''",
        "file_size": "INTEGER DEFAULT 0",
        "mtime_utc": "TEXT DEFAULT ''",
    }
    for col, ddl in needed.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE media ADD COLUMN {col} {ddl}")

    conn.commit()


def load_existing_sha1s(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("SELECT sha1 FROM media")
    return {row[0] for row in cur.fetchall()}


def insert_media(conn: sqlite3.Connection,
                 sha1: str,
                 rel_fqn: str,
                 original_filename: str,
                 path_tags: str,
                 final_dt: str,
                 dt_source: str,
                 width: int,
                 height: int,
                 latitude,
                 longitude,
                 altitude_meters,
                 extension: str,
                 file_size: int,
                 mtime_utc: str):
    conn.execute("""
        INSERT OR IGNORE INTO media
        (
            sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source,
            is_deleted, custom_notes, custom_tags,
            width, height, latitude, longitude, altitude_meters,
            extension, file_size, mtime_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, '', '', ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source,
        width, height, latitude, longitude, altitude_meters,
        extension, file_size, mtime_utc
    ))


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

    # Backend expects comma-separated tags.
    return ", ".join(cleaned)


# ------------------------------------------------------------
# INSPECT MODE
# ------------------------------------------------------------

def inspect_file(path: str):
    print("=" * 90)
    print(f"Inspecting: {path}")
    print("=" * 90)

    dt, src, pillow_meta = get_best_datetime(path)
    print(f"best_dt: {dt}")
    print(f"best_source: {src}")
    print(f"width: {pillow_meta.get('width')}")
    print(f"height: {pillow_meta.get('height')}")
    print(f"latitude: {pillow_meta.get('latitude')}")
    print(f"longitude: {pillow_meta.get('longitude')}")
    print(f"altitude_meters: {pillow_meta.get('altitude_meters')}")
    print(f"gps_present: {pillow_meta.get('gps_present')}")
    print(f"exif_datetimes: {pillow_meta.get('exif_found')}")
    print(f"xmp_datetimes: {extract_xmp_datetimes(path)}")
    print(f"google_takeout_datetime: {get_google_takeout_datetime(path)}")
    print(f"filename_datetime: {get_filename_datetime(path)}")
    print("=" * 90)


# ------------------------------------------------------------
# INGEST LOGIC
# ------------------------------------------------------------

def run_ingest():
    print(f"--- STARTING ROBUST {MODE.upper()} (Merged GPS + Checkpoint + Dedup) ---")

    os.makedirs(DEST_ROOT, exist_ok=True)

    if MODE == "rebuild":
        print("[Mode] REBUILD MODE ACTIVE")
        if REBUILD_THUMBNAILS and os.path.exists(THUMB_DIR):
            print("[Rebuild] Deleting thumbnails directory")
            shutil.rmtree(THUMB_DIR, ignore_errors=True)
        if REBUILD_SQLITE and os.path.exists(DB_PATH):
            print("[Rebuild] Deleting existing SQLite database")
            os.remove(DB_PATH)

    os.makedirs(THUMB_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    existing_sha1s = load_existing_sha1s(conn)

    examined = 0
    new_added = 0
    healed = 0
    duplicates_skipped = 0
    non_jpeg_skipped = 0
    tiny = 0
    unreadable = 0
    warnings = 0
    dirs_walked = 0

    start_time = time.time()
    last_print = start_time
    since_commit = 0
    current_file = ""

    if MODE == "ingest":
        scan_roots = SOURCE_DIRECTORIES
    elif MODE == "rebuild":
        scan_roots = [DEST_ROOT]
    else:
        raise ValueError(f"Invalid MODE: {MODE}")

    for scan_root in scan_roots:
        scan_root = os.path.normpath(scan_root)
        source_base = os.path.basename(scan_root.rstrip("\\/"))

        print(f"[Source] Scanning: {scan_root}")

        walker = rebuild_file_iter(scan_root) if MODE == "rebuild" else os.walk(scan_root)

        for root, dirs, files in walker:
            if MODE == "ingest":
                dirs[:] = [d for d in dirs if not should_skip_dir(d)]

            dirs_walked += 1
            if DIRECTORY_LOG_INTERVAL and dirs_walked % DIRECTORY_LOG_INTERVAL == 0:
                print(f"[Scan] dirs_walked={dirs_walked:,} examined={examined:,} current_dir={root}")

            for name in files:
                examined += 1
                since_commit += 1

                full_path = os.path.join(root, name)
                current_file = full_path

                # In ingest mode, the source file may be on a different drive than DEST_ROOT.
                # We only want to inspect whether the path under the *scan root* contains private
                # underscore-prefixed folders, not compute a cross-drive relative path to DEST_ROOT.
                rel_base = scan_root if MODE == "ingest" else DEST_ROOT
                rel_check = os.path.relpath(full_path, rel_base)
                parts = Path(rel_check).parts

                if any(p.startswith("_") for p in parts):
                    continue

                if file_extension(full_path) not in ALLOWED_EXTENSIONS:
                    if is_non_jpeg_image_file(full_path):
                        non_jpeg_skipped += 1
                    continue

                if MODE == "ingest":
                    rel_dir = os.path.relpath(root, scan_root)
                    if rel_dir == ".":
                        rel_dir = ""
                    archive_rel_path = os.path.join(source_base, rel_dir) if rel_dir else source_base
                    rel_fqn = os.path.join(source_base, rel_dir, name) if rel_dir else os.path.join(source_base, name)
                    dest_path = os.path.join(DEST_ROOT, rel_fqn)
                    path_tags = make_path_tags(source_base, rel_dir)
                else:
                    rel_fqn = os.path.relpath(full_path, DEST_ROOT)
                    archive_rel_path = os.path.dirname(rel_fqn)
                    dest_path = full_path
                    rel_parts = Path(rel_fqn).parts
                    path_tags = ", ".join(
                        p.replace("_", " ").replace("-", " ")
                        for p in rel_parts[:-1]
                        if p and not p.startswith("_")
                    )

                try:
                    dt_string, source_label, pillow_meta = resolve_file_date(full_path, archive_rel_path)

                    width = pillow_meta.get("width")
                    height = pillow_meta.get("height")
                    if width is None or height is None:
                        unreadable += 1
                        continue

                    if width < MIN_WIDTH or height < MIN_HEIGHT:
                        tiny += 1
                        continue

                    sha1 = compute_sha1(full_path)

                    if MODE == "ingest":
                        if sha1 in existing_sha1s:
                            duplicates_skipped += 1
                            continue

                    if MODE == "ingest":
                        ensure_parent_dir(dest_path)
                        if not os.path.exists(dest_path):
                            shutil.copy2(full_path, dest_path)
                            new_added += 1
                        else:
                            healed += 1

                    stat = os.stat(dest_path)

                    insert_media(
                        conn,
                        sha1=sha1,
                        rel_fqn=rel_fqn.replace("\\", "/"),
                        original_filename=name,
                        path_tags=path_tags,
                        final_dt=dt_string,
                        dt_source=source_label,
                        width=int(width),
                        height=int(height),
                        latitude=pillow_meta.get("latitude"),
                        longitude=pillow_meta.get("longitude"),
                        altitude_meters=pillow_meta.get("altitude_meters"),
                        extension=file_extension(dest_path).upper().lstrip("."),
                        file_size=int(stat.st_size),
                        mtime_utc=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat(),
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
                        f"[Heartbeat] Examined: {examined:,} | "
                        f"New: {new_added:,} | "
                        f"Healed: {healed:,} | "
                        f"Duplicates: {duplicates_skipped:,} | "
                        f"Non-JPEG: {non_jpeg_skipped:,} | "
                        f"Tiny: {tiny:,} | "
                        f"Unreadable: {unreadable:,} | "
                        f"Warnings: {warnings:,} | "
                        f"{rate:.1f} files/sec"
                    )
                    print(f"            Current: {current_file}")
                    last_print = now

                if since_commit >= COMMIT_INTERVAL:
                    conn.commit()
                    print(f"[Checkpoint] committed at examined={examined:,}")
                    since_commit = 0

    conn.commit()
    conn.close()

    duration = time.time() - start_time

    print("\n--- INGEST COMPLETE ---")
    print(f"Duration: {duration:.1f}s")
    print(f"Examined: {examined:,}")
    print(f"New added: {new_added:,}")
    print(f"Files healed: {healed:,}")
    print(f"Duplicates skipped: {duplicates_skipped:,}")
    print(f"Non-JPEG skipped: {non_jpeg_skipped:,}")
    print(f"Tiny images skipped: {tiny:,}")
    print(f"Unreadable images skipped: {unreadable:,}")
    print(f"Warnings: {warnings:,}")
    print(f"Mode: {MODE}")
    if MODE == "rebuild":
        print(f"Rebuild SQLite: {REBUILD_SQLITE}")
        print(f"Rebuild Thumbnails: {REBUILD_THUMBNAILS}")


if __name__ == "__main__":
    if INSPECT_FILE:
        inspect_file(INSPECT_FILE)
    else:
        run_ingest()
