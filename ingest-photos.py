import os
import shutil
import sqlite3
import hashlib
import time
import re
from datetime import datetime
from PIL import Image, ImageOps

# --- CONFIGURATION ---
# Add all high-volume source folders to the SOURCES list.
# DEST_ROOT is the target directory for the managed archive.
SOURCES = [r"e:\LegacyTransfer\TransferTest"]
DEST_ROOT = r"C:\LifeArchive"

# Database and Folder paths
DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")
LOG_INTERVAL = 100
COMMIT_INTERVAL = 100

# Minimum image dimensions for accepted source assets.
# This filters out tiny source thumbnails, icons, and other noise.
MIN_WIDTH = 500
MIN_HEIGHT = 300

# GLOBAL SETTINGS
# Disabling the decompression bomb limit allows processing of massive panoramas/scans.
Image.MAX_IMAGE_PIXELS = None

# Ensure environment is ready
os.makedirs(THUMB_DIR, exist_ok=True)


### ---------------------------------------------------------------------------
### LAYER: IO_ENGINE
### ---------------------------------------------------------------------------


def get_sha1(file_path):
    """
    Calculates the SHA1 hash of a file to provide a unique fingerprint.
    Input: file_path (str)
    Output: hex digest string (str)
    """
    h = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()



def get_unique_dest_path(base_dir, filename, sha1):
    """
    Implements the Collision Resolver. If a different file shares a name,
    appends a numerical suffix until a unique slot is found.
    Input: base_dir, filename, sha1
    Output: finalized unique file path
    """
    name, ext = os.path.splitext(filename)
    counter = 0
    while True:
        candidate_name = f"{name}_{counter}{ext}" if counter > 0 else filename
        candidate_path = os.path.join(base_dir, candidate_name)

        if not os.path.exists(candidate_path):
            return candidate_path

        if get_sha1(candidate_path) == sha1:
            return candidate_path

        counter += 1



def generate_thumbnail(image_path, thumb_path):
    """
    Generate a UI thumbnail while honoring EXIF orientation.
    This prevents portrait photos from being baked out sideways/upside-down
    when the original relies on EXIF orientation metadata.
    """
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((400, 400))
        img.convert("RGB").save(thumb_path, "JPEG", quality=85)



def probe_image_dimensions(file_path):
    """
    Returns (width, height) for readable images, otherwise None.
    This is used to reject tiny source thumbnails and to skip unreadable files.
    """
    try:
        with Image.open(file_path) as img:
            return img.size
    except Exception:
        return None


### ---------------------------------------------------------------------------
### LAYER: METADATA_ENGINE
### ---------------------------------------------------------------------------


def get_gps_coordinates(exif_data):
    """
    Verifies and extracts GPS coordinates from EXIF.
    Input: exif_data dictionary
    Output: (lat, lon) tuple if valid, else None
    """
    if not exif_data:
        return None

    gps_info = exif_data.get(34853)
    if not gps_info:
        return None

    def convert_to_degrees(value):
        d = float(value[0])
        m = float(value[1])
        s = float(value[2])
        return d + (m / 60.0) + (s / 3600.0)

    try:
        lat = convert_to_degrees(gps_info[2])
        if gps_info[1] == 'S':
            lat = -lat

        lon = convert_to_degrees(gps_info[4])
        if gps_info[3] == 'W':
            lon = -lon

        if lat == 0.0 and lon == 0.0:
            return None
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    except Exception:
        pass
    return None



def safe_datetime_from_timestamp(ts):
    """
    Convert a filesystem timestamp into a datetime safely.
    Returns None for invalid, zero, negative, or platform-rejected values.
    """
    try:
        if ts is None or ts <= 0:
            return None
        return datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None



def resolve_file_date(file_path, rel_path):
    """
    Implements the Date Resolution Algorithm Hierarchy.
    GPS Golden Signal -> Undated Override -> Year Extraction -> Date Assembly.

    rel_path must be the full archive-relative path context, including the
    source base directory, so that paths like Topaz-Undated/... correctly
    trigger the undated override.
    """
    exif_data = None
    try:
        with Image.open(file_path) as img:
            exif_data = img._getexif()
    except Exception:
        pass

    gps = get_gps_coordinates(exif_data)
    if gps and exif_data:
        dt_str = exif_data.get(36867) or exif_data.get(36868)
        if dt_str:
            try:
                dt = datetime.strptime(dt_str, '%Y:%m:%d %H:%M:%S')
                if 1950 <= dt.year <= 2026:
                    return dt.strftime('%Y-%m-%d %H:%M:%S'), "GPS-Verified EXIF"
            except Exception:
                pass

    if 'undated' in rel_path.lower():
        return "0000-00-00 00:00:00", "Path Override: Undated"

    year_regex = r"(?<!\d)(19[5-9]\d|20[0-2]\d)(?!\d)"
    path_parts = rel_path.split(os.sep)
    target_year = None
    for part in reversed(path_parts):
        match = re.search(year_regex, part)
        if match:
            target_year = int(match.group(1))
            break

    internal_dt = None
    source_label = "Unknown"

    if exif_data and (36867 in exif_data):
        try:
            candidate = datetime.strptime(exif_data[36867], '%Y:%m:%d %H:%M:%S')
            internal_dt = candidate
            source_label = "EXIF"
        except Exception:
            pass

    if internal_dt is None:
        try:
            mtime = os.path.getmtime(file_path)
            internal_dt = safe_datetime_from_timestamp(mtime)
            if internal_dt is not None:
                source_label = "File Modification"
        except OSError:
            internal_dt = None

    if target_year:
        if internal_dt is not None:
            if internal_dt.year == target_year:
                return internal_dt.strftime('%Y-%m-%d %H:%M:%S'), source_label
            return f"{target_year}-01-01 00:00:00", f"Path-Year Overwrite ({source_label} mismatched)"
        return f"{target_year}-01-01 00:00:00", "Path-Year Fallback"

    if internal_dt is not None:
        return internal_dt.strftime('%Y-%m-%d %H:%M:%S'), source_label

    return "0000-00-00 00:00:00", "Date Error Fallback"


### ---------------------------------------------------------------------------
### LAYER: DATA_PERSISTENCE
### ---------------------------------------------------------------------------


def init_db():
    """Ensures the DB and essential columns exist without wiping data."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS media (
        sha1 TEXT PRIMARY KEY,
        rel_fqn TEXT,
        original_filename TEXT,
        path_tags TEXT,
        final_dt TEXT,
        dt_source TEXT,
        is_deleted INTEGER DEFAULT 0,
        custom_notes TEXT,
        custom_tags TEXT
    )''')
    conn.commit()
    conn.close()



def run_ingest():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL')
    start_time = time.time()

    total_new, total_healed, skipped, warnings = 0, 0, 0, 0
    tiny_skipped, unreadable_skipped = 0, 0
    year_regex = r"(?<!\d)(19[5-9]\d|20[0-2]\d)(?!\d)"
    pending_writes = 0

    print("--- STARTING ROBUST INGEST (V4.1 Checkpointed + Size Filter) ---")

    for src in SOURCES:
        if not os.path.exists(src):
            continue
        source_base = os.path.basename(src)

        for root, _, files in os.walk(src):
            for f in files:
                if not f.lower().endswith(('.jpg', '.jpeg', '.png')):
                    continue

                src_path = os.path.join(root, f)

                dims = probe_image_dimensions(src_path)
                if dims is None:
                    warnings += 1
                    unreadable_skipped += 1
                    continue

                width, height = dims
                if width < MIN_WIDTH or height < MIN_HEIGHT:
                    tiny_skipped += 1
                    continue

                sha1 = get_sha1(src_path)

                existing = conn.execute("SELECT rel_fqn FROM media WHERE sha1=?", (sha1,)).fetchone()
                if existing:
                    if os.path.exists(os.path.join(DEST_ROOT, existing[0])):
                        skipped += 1
                    else:
                        rel_dir = os.path.relpath(root, src)
                        dest_dir = os.path.join(DEST_ROOT, source_base, rel_dir)
                        os.makedirs(dest_dir, exist_ok=True)
                        dest_path = get_unique_dest_path(dest_dir, f, sha1)
                        shutil.copy2(src_path, dest_path)
                        new_rel_fqn = os.path.relpath(dest_path, DEST_ROOT)
                        conn.execute("UPDATE media SET rel_fqn = ? WHERE sha1 = ?", (new_rel_fqn, sha1))
                        total_healed += 1
                        pending_writes += 1
                else:
                    rel_dir = os.path.relpath(root, src)
                    dest_dir = os.path.join(DEST_ROOT, source_base, rel_dir)
                    os.makedirs(dest_dir, exist_ok=True)
                    dest_path = get_unique_dest_path(dest_dir, f, sha1)

                    if not os.path.exists(dest_path):
                        shutil.copy2(src_path, dest_path)

                    archive_rel_path = os.path.join(source_base, rel_dir)
                    try:
                        final_dt, source_label = resolve_file_date(dest_path, archive_rel_path)
                    except Exception as e:
                        warnings += 1
                        print(f"\n[WARN] Date resolution failed for {dest_path}: {e}")
                        final_dt, source_label = "0000-00-00 00:00:00", "Date Error Fallback"

                    rel_fqn = os.path.relpath(dest_path, DEST_ROOT)

                    path_parts = [source_base] + rel_dir.split(os.sep)
                    clean_tags = []
                    for part in path_parts:
                        if not part or part == ".":
                            continue
                        clean = re.sub(year_regex, "", part)
                        clean = clean.replace("_", " ").replace("-", " ").strip()
                        if clean:
                            clean_tags.append(clean.title())
                    path_tags = ",".join(clean_tags)

                    conn.execute(
                        """INSERT OR IGNORE INTO media
                        (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (sha1, rel_fqn, os.path.basename(dest_path), path_tags, final_dt, source_label)
                    )

                    t_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
                    if not os.path.exists(t_path):
                        try:
                            generate_thumbnail(dest_path, t_path)
                        except Exception:
                            warnings += 1

                    total_new += 1
                    pending_writes += 1

                processed = total_new + skipped + total_healed + tiny_skipped + unreadable_skipped
                if pending_writes >= COMMIT_INTERVAL:
                    conn.commit()
                    pending_writes = 0

                if processed % LOG_INTERVAL == 0:
                    if pending_writes > 0:
                        conn.commit()
                        pending_writes = 0
                    rate = processed / max(0.001, (time.time() - start_time))
                    print(
                        f"\r  [Progress] New: {total_new} | Healed: {total_healed} | "
                        f"Skipped: {skipped} | Tiny: {tiny_skipped} | Unreadable: {unreadable_skipped} | "
                        f"Warnings: {warnings} | {rate:.1f} files/sec",
                        end="",
                        flush=True,
                    )

    conn.commit()
    conn.close()

    duration = time.time() - start_time
    print("\n\n--- INGEST COMPLETE ---")
    print(f"Duration: {duration:.1f}s")
    print(f"New added: {total_new}")
    print(f"Files healed: {total_healed}")
    print(f"Duplicates skipped: {skipped}")
    print(f"Tiny images skipped: {tiny_skipped}")
    print(f"Unreadable images skipped: {unreadable_skipped}")
    print(f"Warnings: {warnings}")


if __name__ == "__main__":
    run_ingest()
