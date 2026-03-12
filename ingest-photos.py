
import os
import sqlite3
import hashlib
import time
import shutil
from datetime import datetime
from pathlib import Path
from PIL import Image, ExifTags, ImageOps

# ------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------

SOURCES = [
r"F:\Construction",
r"F:\CriticalBackup",
r"F:\facebook",
r"F:\FacebookData",
r"F:\FileHistory",
r"F:\ForceFieldVideos",
r"F:\OldMediaTransfer",
r"F:\photographs",
r"F:\Pictures",
]

DEST_ROOT = r"C:\website-test"

DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")

MIN_WIDTH = 500
MIN_HEIGHT = 300

PROGRESS_INTERVAL = 10
COMMIT_INTERVAL = 100


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


def safe_datetime_from_timestamp(ts):
    try:
        if ts is None or ts <= 0:
            return None
        return datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def get_exif_datetime(path: str):
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None

            for tag_id, value in exif.items():
                tag_name = ExifTags.TAGS.get(tag_id, tag_id)
                if tag_name == "DateTimeOriginal" and value:
                    try:
                        return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
                    except Exception:
                        return None
    except Exception:
        return None

    return None


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

    exif_dt = get_exif_datetime(file_path)
    if exif_dt is not None:
        year = exif_dt.year
        if 1950 <= year <= 2026:
            return exif_dt.strftime("%Y-%m-%d %H:%M:%S"), "EXIF"

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
    print("--- STARTING ROBUST INGEST (V4.4 Rerun Feedback + Restored Layout) ---")

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

                # IMPORTANT: restore old layout, copy directly under DEST_ROOT
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