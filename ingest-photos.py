import os
import shutil
import sqlite3
import hashlib
import time
from datetime import datetime
from PIL import Image

# --- CONFIGURATION ---
SOURCES = [r"C:\test-undated", r"C:\test-images"] 
DEST_ROOT = r"C:\website-test"
DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")
LOG_INTERVAL = 100 

os.makedirs(THUMB_DIR, exist_ok=True)

def get_sha1(file_path):
    h = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def get_unique_dest_path(base_dir, filename, sha1):
    """
    Prevents different photos with the same filename from overwriting each other.
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

def init_db():
    """Ensures the DB and essential columns exist without wiping data."""
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
    conn.close()

def parse_date(file_path):
    fname = os.path.basename(file_path)
    if "OBI_" in fname:
        return "0000-00-00 00:00:00", "Undated Fallback"
    try:
        with Image.open(file_path) as img:
            exif = img._getexif()
            if exif and 36867 in exif:
                dt = datetime.strptime(exif[36867], '%Y:%m:%d %H:%M:%S')
                return dt.strftime('%Y-%m-%d %H:%M:%S'), "EXIF"
    except: pass
    return datetime.fromtimestamp(os.path.getmtime(file_path)).strftime('%Y-%m-%d %H:%M:%S'), "File MTime"

def run_ingest():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    
    total_new = 0
    total_healed = 0
    skipped = 0
    
    print(f"--- STARTING ROBUST INGEST (V3.5) ---")
    
    for src in SOURCES:
        if not os.path.exists(src): continue
        source_name = os.path.basename(src)
            
        for root, _, files in os.walk(src):
            for f in files:
                if not f.lower().endswith(('.jpg', '.jpeg', '.png')): continue
                
                src_path = os.path.join(root, f)
                sha1 = get_sha1(src_path)
                
                # Check DB for existing identity
                existing = conn.execute("SELECT rel_fqn FROM media WHERE sha1=?", (sha1,)).fetchone()
                
                if existing:
                    # Identity is known. Check if the physical file is where it should be.
                    if os.path.exists(os.path.join(DEST_ROOT, existing[0])):
                        skipped += 1
                        continue
                    else:
                        # SELF-HEALING: The photo is in the DB but missing from the disk.
                        # We will copy it to the new destination and update the DB pointer.
                        rel_dir = os.path.relpath(root, src)
                        dest_dir = os.path.join(DEST_ROOT, source_name, rel_dir)
                        os.makedirs(dest_dir, exist_ok=True)
                        dest_path = get_unique_dest_path(dest_dir, f, sha1)
                        
                        shutil.copy2(src_path, dest_path)
                        new_rel_fqn = os.path.relpath(dest_path, DEST_ROOT)
                        
                        conn.execute("UPDATE media SET rel_fqn = ? WHERE sha1 = ?", (new_rel_fqn, sha1))
                        total_healed += 1
                        continue

                # NEW PHOTO LOGIC
                rel_dir = os.path.relpath(root, src)
                dest_dir = os.path.join(DEST_ROOT, source_name, rel_dir)
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = get_unique_dest_path(dest_dir, f, sha1)
                
                shutil.copy2(src_path, dest_path)
                
                final_dt, source_label = parse_date(dest_path)
                rel_fqn = os.path.relpath(dest_path, DEST_ROOT)
                
                # Combine source folder and relative path for richer tags
                path_parts = [source_name] + rel_dir.split(os.sep)
                path_tags = ",".join([p for p in path_parts if p and p != "."])
                
                conn.execute("""INSERT INTO media 
                    (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source) 
                    VALUES (?, ?, ?, ?, ?, ?)""", 
                    (sha1, rel_fqn, os.path.basename(dest_path), path_tags, final_dt, source_label))
                
                # Thumbnail creation
                t_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
                if not os.path.exists(t_path):
                    try:
                        with Image.open(dest_path) as img:
                            img.thumbnail((400, 400))
                            img.convert("RGB").save(t_path, "JPEG", quality=85)
                    except: pass
                
                total_new += 1
                if (total_new + skipped + total_healed) % LOG_INTERVAL == 0:
                    print(f"\r  [Progress] New: {total_new} | Healed: {total_healed} | Skipped: {skipped}", end="", flush=True)

    conn.commit()
    conn.close()
    print(f"\n--- INGEST COMPLETE ---")
    print(f"New added: {total_new}\nFiles healed: {total_healed}\nDuplicates skipped: {skipped}")

if __name__ == "__main__":
    run_ingest()