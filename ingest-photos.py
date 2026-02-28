# --- CONFIGURATION ---
SOURCE_PATHS = [r"c:\TerrysBackup\pictures"]
DEST_ROOT = r"C:\website-test"

import os
import hashlib
import sqlite3
import shutil
import json
import re
import time
from datetime import datetime
from PIL import Image, ImageFile
from PIL.ExifTags import TAGS

# --- CONFIGURATION ---
SOURCE_PATHS = [r"c:\TerrysBackup"]
DEST_ROOT = r"C:\website-test"
SAVE_DEBUG_JSON = False

# Database Version Control
CURRENT_SCHEMA_VERSION = 1

# Internal paths
DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")
META_DIR = os.path.join(DEST_ROOT, "_metadata")
JSONL_PATH = os.path.join(META_DIR, "debug_archive.jsonl")

VALID_YEAR_RANGE = range(1950, 2027)
ImageFile.LOAD_TRUNCATED_IMAGES = True

def get_sha1(filepath):
    hasher = hashlib.sha1()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def extract_path_tags(rel_path):
    folders = re.split(r'[\\/]', rel_path)
    tags = set()
    ignore_phrases = {
        'photos from', 'camera roll', 'exported photos', 'takeout', 
        'original images', 'saved pictures', 'dcim', 'uploads', 'images', 'pictures'
    }
    for folder in folders:
        clean_name = re.sub(r'\b(19[5-9]\d|20[0-2]\d)\b', '', folder).strip()
        if len(clean_name) >= 4 and clean_name.lower() not in ignore_phrases:
            tags.add(clean_name.title())
    return ", ".join(sorted(list(tags)))

def determine_best_date(filepath, folder_year):
    exif_dt = None
    try:
        with Image.open(filepath) as img:
            exif = img._getexif()
            if exif:
                for tag, val in exif.items():
                    if TAGS.get(tag) == "DateTimeOriginal":
                        exif_dt = datetime.strptime(str(val)[:19], "%Y:%m:%d %H:%M:%S")
                        break
    except: pass

    creation_ts = os.path.getmtime(filepath)
    creation_dt = datetime.fromtimestamp(creation_ts)

    if folder_year:
        folder_year_int = int(folder_year)
        if exif_dt and exif_dt.year == folder_year_int:
            return exif_dt, "EXIF (Corroborated by Folder)"
        elif creation_dt.year == folder_year_int:
            return creation_dt, "File Creation (Corroborated by Folder)"
        else:
            return datetime(folder_year_int, 1, 1), "Folder Override"
    else:
        if exif_dt and exif_dt.year in VALID_YEAR_RANGE:
            return exif_dt, "EXIF (Unverified)"
        elif creation_dt.year in VALID_YEAR_RANGE:
            return creation_dt, "File Creation (Unverified)"
        else:
            return datetime(1900, 1, 1), "Invalid/Fallback"

def setup_directories():
    os.makedirs(THUMB_DIR, exist_ok=True)
    if SAVE_DEBUG_JSON:
        os.makedirs(META_DIR, exist_ok=True)

def setup_db():
    """Initializes the database and handles schema versioning/migrations."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Ensure the schema tracking table exists
    cursor.execute('''CREATE TABLE IF NOT EXISTS system_settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT
    )''')
    
    # 2. Check current version
    cursor.execute("SELECT setting_value FROM system_settings WHERE setting_key = 'schema_version'")
    row = cursor.fetchone()
    db_version = int(row[0]) if row else 0
    
    # 3. Apply Migrations if needed
    if db_version == 0:
        print("[DB] Initializing new database (Version 1)...")
        cursor.execute('''CREATE TABLE media (
            sha1 TEXT PRIMARY KEY,
            rel_fqn TEXT,
            original_filename TEXT,
            media_type TEXT,
            path_tags TEXT,
            final_dt TIMESTAMP,
            dt_source TEXT,
            takeout_notes TEXT,
            has_thumb BOOLEAN DEFAULT 0,
            custom_notes TEXT DEFAULT '',
            custom_tags TEXT DEFAULT '',
            is_deleted BOOLEAN DEFAULT 0,
            is_favorite BOOLEAN DEFAULT 0
        )''')
        cursor.execute("INSERT INTO system_settings (setting_key, setting_value) VALUES ('schema_version', ?)", (CURRENT_SCHEMA_VERSION,))
        conn.commit()
    
    elif db_version < CURRENT_SCHEMA_VERSION:
        # Future migration block (e.g., ALTER TABLE media ADD COLUMN ...)
        print(f"[DB] Upgrading database from v{db_version} to v{CURRENT_SCHEMA_VERSION}...")
        # (Migration logic would go here)
        cursor.execute("UPDATE system_settings SET setting_value = ? WHERE setting_key = 'schema_version'", (CURRENT_SCHEMA_VERSION,))
        conn.commit()
        
    return conn

def run_ingestor():
    start_time = time.time()
    setup_directories()
    conn = setup_db()
    cursor = conn.cursor()
    
    stats = {
        "new_ingested": 0,
        "duplicates_skipped": 0,
        "metadata_updates": 0,
        "errors": 0
    }
    
    jsonl_file = open(JSONL_PATH, 'a', encoding='utf-8') if SAVE_DEBUG_JSON else None
    
    try:
        for source_root in SOURCE_PATHS:
            source_name = os.path.basename(os.path.normpath(source_root))
            print(f"\n>>> SCANNING SOURCE: {source_name}")
            
            for root, _, files in os.walk(source_root):
                year_match = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', root)
                folder_year = year_match.group(1) if year_match else None

                for file in files:
                    if not file.lower().endswith(('.jpg', '.jpeg', '.png', '.mp4', '.mkv', '.mov')): continue
                    
                    is_video = file.lower().endswith(('.mp4', '.mkv', '.mov'))
                    media_type = 'video' if is_video else 'image'
                    
                    try:
                        src_path = os.path.join(root, file)
                        file_hash = get_sha1(src_path)

                        json_path = src_path + ".json"
                        json_data = None
                        if os.path.exists(json_path):
                            try:
                                with open(json_path, 'r') as f: json_data = json.load(f)
                            except: pass

                        cursor.execute("SELECT rel_fqn, takeout_notes FROM media WHERE sha1=?", (file_hash,))
                        existing = cursor.fetchone()

                        if existing:
                            stats["duplicates_skipped"] += 1
                            if json_data and json_data.get('description') and not existing[1]:
                                cursor.execute("UPDATE media SET takeout_notes=? WHERE sha1=?", (json_data.get('description'), file_hash))
                                conn.commit()
                                stats["metadata_updates"] += 1
                            continue

                        dt, source = determine_best_date(src_path, folder_year)
                        
                        rel_from_root = os.path.relpath(root, source_root)
                        virt_dir = source_name if rel_from_root == "." else os.path.join(source_name, rel_from_root)
                        
                        dest_dir = os.path.join(DEST_ROOT, virt_dir)
                        os.makedirs(dest_dir, exist_ok=True)
                        dest_path = os.path.join(dest_dir, file)
                        
                        shutil.copy2(src_path, dest_path)
                        
                        rel_fqn = os.path.join(virt_dir, file)
                        tags = extract_path_tags(virt_dir)
                        notes = json_data.get('description') if json_data else None

                        # Thumbnail Handling
                        thumb_path = os.path.join(THUMB_DIR, f"{file_hash}.jpg")
                        has_thumb = 0
                        if not is_video:
                            try:
                                with Image.open(dest_path) as img:
                                    img.thumbnail((400, 400))
                                    img.save(thumb_path, "JPEG")
                                has_thumb = 1
                            except: pass
                        else:
                            # Look for a manually created poster image for the video
                            poster_path = os.path.splitext(dest_path)[0] + '.jpg'
                            if os.path.exists(poster_path):
                                shutil.copy2(poster_path, thumb_path)
                                has_thumb = 1

                        cursor.execute('''INSERT INTO media 
                            (sha1, rel_fqn, original_filename, media_type, path_tags, final_dt, dt_source, takeout_notes, has_thumb)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (file_hash, rel_fqn, file, media_type, tags, dt, source, notes, has_thumb))
                        conn.commit()
                        
                        if SAVE_DEBUG_JSON and jsonl_file:
                            debug_record = {
                                "sha1": file_hash, "rel_fqn": rel_fqn, "original_filename": file,
                                "media_type": media_type, "final_dt": dt.isoformat() if isinstance(dt, datetime) else str(dt),
                                "dt_source": source, "path_tags": tags, "takeout_notes": notes
                            }
                            jsonl_file.write(json.dumps(debug_record) + '\n')
                            jsonl_file.flush()

                        stats["new_ingested"] += 1
                        print(f" Ingested: {file[:25]:<25} | {media_type.upper():<5} | {dt.year}", end='\r')
                    
                    except Exception as e:
                        stats["errors"] += 1
                        print(f"\n Error processing {file}: {e}")

    finally:
        if jsonl_file: jsonl_file.close()

    elapsed = time.time() - start_time
    print("\n" + "="*50)
    print(" INGESTION ENGINE V2 SUMMARY")
    print("="*50)
    print(f" Total Time:          {elapsed:.2f} seconds")
    print(f" New Media Ingested:  {stats['new_ingested']}")
    print(f" Duplicates Skipped:  {stats['duplicates_skipped']}")
    print(f" Metadata Merges:     {stats['metadata_updates']}")
    print(f" Errors Encountered:  {stats['errors']}")
    print("="*50)
    print(f" Master Database:     {DB_PATH} (Version {CURRENT_SCHEMA_VERSION})")
    print("="*50 + "\n")

if __name__ == "__main__":
    run_ingestor()