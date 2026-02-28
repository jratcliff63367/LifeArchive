import os
import hashlib
import sqlite3
import re
from datetime import datetime
from PIL import Image, ImageFile

# --- CONFIGURATION ---
# Add your test folder or scan folder here
SOURCE_PATHS = [r"c:\topaz-undated"] 
DEST_ROOT = r"C:\website-test"

# Internal paths
DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")

# EXPLICIT ALLOW-LIST
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg'}
VALID_YEAR_RANGE = range(1950, 2027)
ImageFile.LOAD_TRUNCATED_IMAGES = True

def get_sha1(filepath):
    hasher = hashlib.sha1()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def determine_best_date(filepath, rel_path, folder_year):
    # TRIGGER: The 'undated' rule
    if "undated" in rel_path.lower():
        return "0000-00-00 00:00:00", "Manual Override (Undated)"

    exif_dt = None
    try:
        with Image.open(filepath) as img:
            exif = img._getexif()
            if exif and 36867 in exif:
                exif_dt = datetime.strptime(str(exif[36867])[:19], "%Y:%m:%d %H:%M:%S")
    except: pass

    # Fallback to file system time
    creation_dt = datetime.fromtimestamp(os.path.getmtime(filepath))

    if folder_year:
        folder_year_int = int(folder_year)
        if exif_dt and exif_dt.year == folder_year_int: return exif_dt, "EXIF (Corroborated)"
        return datetime(folder_year_int, 1, 1), "Folder Year Override"
    
    if exif_dt and exif_dt.year in VALID_YEAR_RANGE: return exif_dt, "EXIF"
    return creation_dt, "File Creation Fallback"

def run_ingestor():
    os.makedirs(THUMB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    
    # Clean up non-JPEG pollution from previous runs
    print("Purging non-JPEG artifacts from database...")
    conn.execute("UPDATE media SET is_deleted = 1 WHERE lower(original_filename) NOT LIKE '%.jpg' AND lower(original_filename) NOT LIKE '%.jpeg'")
    conn.commit()

    for source_root in SOURCE_PATHS:
        print(f"\n>>> SCANNING SOURCE: {source_root}")
        if not os.path.exists(source_root):
            print(f" ! Path not found: {source_root}")
            continue

        for root, _, files in os.walk(source_root):
            if any(x in root for x in ['_thumbs', '_web_layout', '.git']): continue
            
            # Extract folder year if present
            year_match = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', root)
            folder_year = year_match.group(1) if year_match else None

            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext not in ALLOWED_EXTENSIONS: continue 

                src_path = os.path.join(root, file)
                # We store the path RELATIVE to the source_root
                rel_path = os.path.relpath(src_path, source_root)
                
                # Check for existing record
                cursor = conn.execute("SELECT sha1 FROM media WHERE rel_fqn = ?", (rel_path,))
                if cursor.fetchone(): continue

                print(f" Ingesting: {file}")
                file_hash = get_sha1(src_path)
                dt, source = determine_best_date(src_path, rel_path, folder_year)
                
                # Path-based tags
                path_tags = ",".join(rel_path.split(os.sep)[:-1])
                if "0000" in str(dt):
                    path_tags += ",Undated"

                conn.execute('''INSERT INTO media 
                    (sha1, rel_fqn, original_filename, media_type, final_dt, dt_source, path_tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sha1) DO UPDATE SET rel_fqn=excluded.rel_fqn''',
                    (file_hash, rel_path, file, 'image/jpeg', str(dt), source, path_tags))
                
                # Thumbnailing
                thumb_path = os.path.join(THUMB_DIR, f"{file_hash}.jpg")
                if not os.path.exists(thumb_path):
                    try:
                        with Image.open(src_path) as img:
                            img.thumbnail((400, 400))
                            img.convert("RGB").save(thumb_path, "JPEG")
                    except: pass
        conn.commit()
    conn.close()
    print("\nDone. Enjoy the walk!")

if __name__ == "__main__":
    run_ingestor()