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
SOURCE_PATHS = [r"F:\GoogleTakeout\jratcliffscarab\Takeout\Google Photos"]
DEST_ROOT = r"C:\Photo-Website"

# Internal paths
DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")
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

def get_validated_date(filepath, folder_year, json_data):
    target_year = int(folder_year) if folder_year and int(folder_year) in VALID_YEAR_RANGE else None
    if json_data:
        try:
            ts = int(json_data.get('photoTakenTime', {}).get('timestamp', 0))
            dt = datetime.fromtimestamp(ts)
            if dt.year in VALID_YEAR_RANGE:
                if not target_year or dt.year == target_year: return dt, "JSON"
        except: pass
    try:
        with Image.open(filepath) as img:
            exif = img._getexif()
            if exif:
                for tag, val in exif.items():
                    if TAGS.get(tag) == "DateTimeOriginal":
                        dt = datetime.strptime(str(val)[:19], "%Y:%m:%d %H:%M:%S")
                        if dt.year in VALID_YEAR_RANGE:
                            if not target_year or dt.year == target_year: return dt, "EXIF"
    except: pass
    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
    if target_year:
        try: return mtime.replace(year=target_year), "Folder-Override"
        except: return datetime(target_year, mtime.month, 1), "Folder-Override"
    return (mtime, "FileSystem") if mtime.year in VALID_YEAR_RANGE else (datetime(1900,1,1), "Invalid")

def setup_db():
    os.makedirs(THUMB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS photos (
        sha1 TEXT PRIMARY KEY,
        rel_fqn TEXT,
        original_filename TEXT,
        path_tags TEXT,
        final_dt TIMESTAMP,
        dt_source TEXT,
        notes TEXT,
        has_thumb BOOLEAN DEFAULT 0
    )''')
    return conn

def run_ingestor():
    start_time = time.time()
    conn = setup_db()
    cursor = conn.cursor()
    
    stats = {
        "new_ingested": 0,
        "duplicates_skipped": 0,
        "metadata_updates": 0,
        "errors": 0
    }
    
    for source_root in SOURCE_PATHS:
        source_name = os.path.basename(os.path.normpath(source_root))
        print(f"\n>>> SCANNING SOURCE: {source_name}")
        
        for root, _, files in os.walk(source_root):
            year_match = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', root)
            folder_year = year_match.group(1) if year_match else None

            for file in files:
                if not file.lower().endswith(('.jpg', '.jpeg', '.png')): continue
                
                try:
                    src_path = os.path.join(root, file)
                    file_hash = get_sha1(src_path)

                    # Check for Takeout JSON
                    json_path = src_path + ".json"
                    json_data = None
                    if os.path.exists(json_path):
                        try:
                            with open(json_path, 'r') as f: json_data = json.load(f)
                        except: pass

                    # DUPLICATE CHECK
                    cursor.execute("SELECT rel_fqn, notes FROM photos WHERE sha1=?", (file_hash,))
                    existing = cursor.fetchone()

                    if existing:
                        stats["duplicates_skipped"] += 1
                        # METADATA FUSION: If duplicate, check if this source has new notes
                        if json_data and json_data.get('description') and not existing[1]:
                            cursor.execute("UPDATE photos SET notes=? WHERE sha1=?", (json_data.get('description'), file_hash))
                            conn.commit()
                            stats["metadata_updates"] += 1
                        continue

                    # NEW FILE PROCESSING
                    dt, source = get_validated_date(src_path, folder_year, json_data)
                    rel_from_root = os.path.relpath(root, source_root)
                    virt_dir = source_name if rel_from_root == "." else os.path.join(source_name, rel_from_root)
                    
                    dest_dir = os.path.join(DEST_ROOT, virt_dir)
                    os.makedirs(dest_dir, exist_ok=True)
                    dest_path = os.path.join(dest_dir, file)
                    
                    shutil.copy2(src_path, dest_path)
                    
                    rel_fqn = os.path.join(virt_dir, file)
                    tags = extract_path_tags(virt_dir)

                    # Thumbnail
                    thumb_path = os.path.join(THUMB_DIR, f"{file_hash}.jpg")
                    try:
                        with Image.open(dest_path) as img:
                            img.thumbnail((400, 400))
                            img.save(thumb_path, "JPEG")
                        has_thumb = 1
                    except: has_thumb = 0

                    cursor.execute('''INSERT INTO photos 
                        (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source, notes, has_thumb)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                        (file_hash, rel_fqn, file, tags, dt, source, json_data.get('description') if json_data else None, has_thumb))
                    
                    conn.commit()
                    stats["new_ingested"] += 1
                    print(f" Ingested: {file[:25]:<25} | {dt.year} | {tags[:20]}...", end='\r')
                
                except Exception as e:
                    stats["errors"] += 1
                    print(f"\n Error processing {file}: {e}")

    # FINAL SUMMARY OUTPUT
    elapsed = time.time() - start_time
    print("\n" + "="*50)
    print(" INGESTION SUMMARY")
    print("="*50)
    print(f" Total Time:          {elapsed:.2f} seconds")
    print(f" New Photos Ingested: {stats['new_ingested']}")
    print(f" Duplicates Skipped:  {stats['duplicates_skipped']}")
    print(f" Metadata Merges:     {stats['metadata_updates']}")
    print(f" Errors Encountered:  {stats['errors']}")
    print("="*50)
    print(f" Archive Database:    {DB_PATH}")
    print("="*50 + "\n")

if __name__ == "__main__":
    run_ingestor()