import os
import shutil
import sqlite3
import hashlib
import time
from datetime import datetime
from PIL import Image

# --- STABILITY FIX ---
# Prevents "DecompressionBombError" for very large images/panoramas
Image.MAX_IMAGE_PIXELS = None 

# --- CONFIGURATION ---
SOURCES = [r"C:\test-undated", r"C:\test-images"] 
DEST_ROOT = r"C:\website-test"
DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")
LOG_INTERVAL = 100 

os.makedirs(THUMB_DIR, exist_ok=True)

### ---------------------------------------------------------------------------
### LAYER: IO_ENGINE
### ---------------------------------------------------------------------------

def get_sha1(file_path):
    h = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def get_unique_dest_path(base_dir, filename, sha1):
    """Handles collisions: ensures unique files get unique names (e.g. photo_1.jpg)"""
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

def create_thumbnail(src_path, sha1):
    t_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
    if os.path.exists(t_path): return
    try:
        with Image.open(src_path) as img:
            img.thumbnail((400, 400))
            img.convert("RGB").save(t_path, "JPEG", quality=85)
    except Exception:
        pass # Silent skip for corrupted files

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

### ---------------------------------------------------------------------------
### LAYER: DATA_MANAGEMENT
### ---------------------------------------------------------------------------

def init_db():
    """Initializes schema if missing. Does NOT delete existing data."""
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

def run_ingest():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    
    start_time = time.time()
    total_new = 0
    total_healed = 0
    total_skipped = 0
    total_processed = 0
    
    print(f"--- STARTING V3.6 MASTER INGEST ---")
    
    for src in SOURCES:
        if not os.path.exists(src):
            print(f" ! Source not found: {src}")
            continue
            
        source_name = os.path.basename(src)
        print(f" > Scanning: {source_name}")
            
        for root, _, files in os.walk(src):
            for f in files:
                if not f.lower().endswith(('.jpg', '.jpeg', '.png')): continue
                
                src_path = os.path.join(root, f)
                sha1 = get_sha1(src_path)
                total_processed += 1
                
                # GATE 1: Check Database Identity
                existing = conn.execute("SELECT rel_fqn FROM media WHERE sha1=?", (sha1,)).fetchone()
                
                if existing:
                    # Check if the physical file is still there
                    if os.path.exists(os.path.join(DEST_ROOT, existing[0])):
                        total_skipped += 1
                    else:
                        # SELF-HEALING: Record exists but file is missing
                        rel_dir = os.path.relpath(root, src)
                        dest_dir = os.path.join(DEST_ROOT, source_name, rel_dir)
                        os.makedirs(dest_dir, exist_ok=True)
                        dest_path = get_unique_dest_path(dest_dir, f, sha1)
                        
                        shutil.copy2(src_path, dest_path)
                        new_rel_fqn = os.path.relpath(dest_path, DEST_ROOT)
                        
                        conn.execute("UPDATE media SET rel_fqn = ? WHERE sha1 = ?", (new_rel_fqn, sha1))
                        create_thumbnail(dest_path, sha1)
                        total_healed += 1
                else:
                    # NEW FILE: Standard Ingest
                    rel_dir = os.path.relpath(root, src)
                    dest_dir = os.path.join(DEST_ROOT, source_name, rel_dir)
                    os.makedirs(dest_dir, exist_ok=True)
                    dest_path = get_unique_dest_path(dest_dir, f, sha1)
                    
                    shutil.copy2(src_path, dest_path)
                    
                    final_dt, source_label = parse_date(dest_path)
                    rel_fqn = os.path.relpath(dest_path, DEST_ROOT)
                    
                    # Rich Tagging: [Source Name] + [Subfolders]
                    path_parts = [source_name] + rel_dir.split(os.sep)
                    path_tags = ",".join([p for p in path_parts if p and p != "."])
                    
                    conn.execute("""INSERT INTO media 
                        (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source) 
                        VALUES (?, ?, ?, ?, ?, ?)""", 
                        (sha1, rel_fqn, os.path.basename(dest_path), path_tags, final_dt, source_label))
                    
                    create_thumbnail(dest_path, sha1)
                    total_new += 1

                # Progress Reporting
                if total_processed % LOG_INTERVAL == 0:
                    elapsed = time.time() - start_time
                    rate = total_processed / elapsed if elapsed > 0 else 0
                    print(f"\r  [Progress] {total_processed} files scanned... ({rate:.1f} files/sec)", end="", flush=True)

    conn.commit()
    conn.close()
    
    duration = time.time() - start_time
    db_size = os.path.getsize(DB_PATH) / (1024 * 1024)
    
    print(f"\n\n--- INGEST COMPLETE ---")
    print(f"Duration:       {duration:.1f} seconds")
    print(f"Total Scanned:  {total_processed}")
    print(f"New Items:      {total_new}")
    print(f"Files Restored: {total_healed} (Self-Healed)")
    print(f"Skipped:        {total_skipped} (Existing)")
    print(f"Final DB Size:  {db_size:.2f} MB")

if __name__ == "__main__":
    run_ingest()