import os
import shutil
import sqlite3
import hashlib
import time
import re
from datetime import datetime
from PIL import Image, ImageOps, UnidentifiedImageError

# --- CONFIGURATION ---
SOURCES = [r"F:\Terry's Pictures for website", r"F:\GoogleTakeout\jratcliffscarab\Takeout\Google Photos"]
DEST_ROOT = r"C:\website-test"

DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")

# Progress / durability tuning
LOG_INTERVAL = 100
HEARTBEAT_SECONDS = 10.0
COMMIT_INTERVAL = 100
+
# Minimum source image size to accept into the archive
MIN_WIDTH = 500
MIN_HEIGHT = 300

# GLOBAL SETTINGS
Image.MAX_IMAGE_PIXELS = None

os.makedirs(THUMB_DIR, exist_ok=True)


def get_sha1(file_path: str) -> str:
    h = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def get_unique_dest_path(base_dir: str, filename: str, sha1: str) -> str:
    name, ext = os.path.splitext(filename)
    counter = 0
    while True:
        candidate_name = f"{name}_{counter}{ext}" if counter > 0 else filename
        candidate_path = os.path.join(base_dir, candidate_name)

        if not os.path.exists(candidate_path):
            return candidate_path

        try:
            if get_sha1(candidate_path) == sha1:
                return candidate_path
        except Exception:
            pass

        counter += 1


def generate_thumbnail(image_path: str, thumb_path: str) -> None:
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((400, 400))
        img.convert("RGB").save(thumb_path, "JPEG", quality=85)


def safe_datetime_from_timestamp(ts):
    try:
        if ts is None or ts <= 0:
            return None
        return datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def get_gps_coordinates(exif_data):
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


def resolve_file_date(file_path: str, archive_rel_path: str):
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

    if 'undated' in archive_rel_path.lower():
        return "0000-00-00 00:00:00", "Path Override: Undated"

    year_regex = r"(?<!\d)(19[5-9]\d|20[0-2]\d)(?!\d)"
    path_parts = archive_rel_path.split(os.sep)
    target_year = None
    for part in reversed(path_parts):
        match = re.search(year_regex, part)
        if match:
            target_year = int(match.group(1))
            break

    internal_dt = None
    source_label = "Unknown"

    try:
        mtime = os.path.getmtime(file_path)
        internal_dt = safe_datetime_from_timestamp(mtime)
        if internal_dt is not None:
            source_label = "File Modification"
    except OSError:
        internal_dt = None

    if exif_data and (36867 in exif_data):
        try:
            exif_dt = datetime.strptime(exif_data[36867], '%Y:%m:%d %H:%M:%S')
            if 1950 <= exif_dt.year <= 2026:
                internal_dt = exif_dt
                source_label = "EXIF"
        except Exception:
            pass

    if target_year:
        if internal_dt is not None and internal_dt.year == target_year:
            return internal_dt.strftime('%Y-%m-%d %H:%M:%S'), source_label
        return f"{target_year}-01-01 00:00:00", f"Path-Year Overwrite ({source_label} mismatched)"

    if internal_dt is not None:
        return internal_dt.strftime('%Y-%m-%d %H:%M:%S'), source_label

    return "0000-00-00 00:00:00", "Date Error Fallback"


def init_db():
    conn = sqlite3.connect(DB_PATH)
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


def format_elapsed(seconds: float) -> str:
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def is_supported_image_extension(filename: str) -> bool:
    return filename.lower().endswith((".jpg", ".jpeg", ".png"))


def inspect_source_image(src_path: str):
    try:
        with Image.open(src_path) as img:
            width, height = img.size
            return True, width, height, None
    except (UnidentifiedImageError, OSError, ValueError) as e:
        return False, None, None, str(e)


def print_progress(start_time, total_seen, total_new, total_healed, skipped, tiny_skipped,
                   unreadable_skipped, warnings_count, current_path=None, checkpoint=False):
    elapsed = time.time() - start_time
    rate = total_seen / elapsed if elapsed > 0 else 0.0
    prefix = "[Checkpoint]" if checkpoint else "[Progress]"
    msg = (
        f"\r  {prefix} Elapsed: {format_elapsed(elapsed)} | "
        f"Seen: {total_seen} | New: {total_new} | Healed: {total_healed} | "
        f"Skipped: {skipped} | Tiny: {tiny_skipped} | Unreadable: {unreadable_skipped} | "
        f"Warnings: {warnings_count} | {rate:.1f} files/sec"
    )
    if current_path:
        trimmed = current_path
        if len(trimmed) > 110:
            trimmed = "..." + trimmed[-107:]
        msg += f" | Current: {trimmed}"
    print(msg, end="", flush=True)
    if checkpoint:
        print("", flush=True)


def run_ingest():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    start_time = time.time()
    last_log_time = start_time
    since_commit = 0

    total_new = 0
    total_healed = 0
    skipped = 0
    tiny_skipped = 0
    unreadable_skipped = 0
    warnings_count = 0
    total_seen = 0

    print("--- STARTING ROBUST INGEST (V4.2 Heartbeat + Checkpointed + Size Filter) ---")

    try:
        for src in SOURCES:
            if not os.path.exists(src):
                continue

            source_base = os.path.basename(src)

            for root, _, files in os.walk(src):
                for f in files:
                    src_path = os.path.join(root, f)

                    if not is_supported_image_extension(f):
                        continue

                    total_seen += 1
                    now = time.time()
                    if now - last_log_time >= HEARTBEAT_SECONDS:
                        print_progress(start_time, total_seen, total_new, total_healed, skipped,
                                       tiny_skipped, unreadable_skipped, warnings_count, src_path)
                        last_log_time = now

                    readable, width, height, err = inspect_source_image(src_path)
                    if not readable:
                        unreadable_skipped += 1
                        warnings_count += 1
                        continue

                    if width < MIN_WIDTH or height < MIN_HEIGHT:
                        tiny_skipped += 1
                        continue

                    sha1 = get_sha1(src_path)
                    existing = conn.execute("SELECT rel_fqn FROM media WHERE sha1=?", (sha1,)).fetchone()
                    if existing:
                        existing_full_path = os.path.join(DEST_ROOT, existing[0])
                        if os.path.exists(existing_full_path):
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
                            since_commit += 1
                        if total_seen % LOG_INTERVAL == 0:
                            print_progress(start_time, total_seen, total_new, total_healed, skipped,
                                           tiny_skipped, unreadable_skipped, warnings_count, src_path)
                            last_log_time = time.time()
                        if since_commit >= COMMIT_INTERVAL:
                            conn.commit()
                            since_commit = 0
                            print_progress(start_time, total_seen, total_new, total_healed, skipped,
                                           tiny_skipped, unreadable_skipped, warnings_count, src_path, checkpoint=True)
                            last_log_time = time.time()
                        continue

                    rel_dir = os.path.relpath(root, src)
                    dest_dir = os.path.join(DEST_ROOT, source_base, rel_dir)
                    os.makedirs(dest_dir, exist_ok=True)
                    dest_path = get_unique_dest_path(dest_dir, f, sha1)

                    shutil.copy2(src_path, dest_path)

                    archive_rel_path = os.path.join(source_base, rel_dir)
                    try:
                        final_dt, source_label = resolve_file_date(dest_path, archive_rel_path)
                    except Exception:
                        final_dt, source_label = "0000-00-00 00:00:00", "Date Error Fallback"
                        warnings_count += 1

                    rel_fqn = os.path.relpath(dest_path, DEST_ROOT)

                    path_parts = [p for p in rel_fqn.split(os.sep)[:-1] if p not in ('.', '')]
                    path_tags_list = []
                    for part in path_parts:
                        if not re.fullmatch(r"(?<!\d)(19[5-9]\d|20[0-2]\d)(?!\d)", part):
                            path_tags_list.append(part)
                    path_tags = ",".join(path_tags_list)

                    conn.execute(
                        """INSERT INTO media
                        (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (sha1, rel_fqn, os.path.basename(dest_path), path_tags, final_dt, source_label),
                    )
                    since_commit += 1

                    t_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
                    if not os.path.exists(t_path):
                        try:
                            generate_thumbnail(dest_path, t_path)
                        except Exception:
                            warnings_count += 1

                    total_new += 1
                    if total_seen % LOG_INTERVAL == 0:
                        print_progress(start_time, total_seen, total_new, total_healed, skipped,
                                       tiny_skipped, unreadable_skipped, warnings_count, src_path)
                        last_log_time = time.time()

                    if since_commit >= COMMIT_INTERVAL:
                        conn.commit()
                        since_commit = 0
                        print_progress(start_time, total_seen, total_new, total_healed, skipped,
                                       tiny_skipped, unreadable_skipped, warnings_count, src_path, checkpoint=True)
                        last_log_time = time.time()

        if since_commit > 0:
            conn.commit()
    finally:
        conn.close()

    duration = time.time() - start_time
    print(f"\n\n--- INGEST COMPLETE ---")
    print(f"Duration: {duration:.1f}s")
    print(f"New added: {total_new}")
    print(f"Files healed: {total_healed}")
    print(f"Duplicates skipped: {skipped}")
    print(f"Tiny images skipped: {tiny_skipped}")
    print(f"Unreadable images skipped: {unreadable_skipped}")
    print(f"Warnings: {warnings_count}")


if __name__ == "__main__":
    run_ingest()
