import os
import shutil
import sqlite3
import hashlib
import time
from datetime import datetime
from PIL import Image

# --- CONFIGURATION (EDIT THESE FOR THE FULL RUN) ---
# Add all your high-volume source folders here
SOURCES = [r"C:\test-undated", r"C:\test-images"] 
DEST_ROOT = r"C:\website-test"

DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")
LOG_INTERVAL = 100  # Only update the console every 100 files

# Ensure environment is ready
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

def create_thumbnail(src_path, sha1):
    t_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
    if os.path.exists(t_path): return
    try:
        with Image.open(src_path) as img:
            img.thumbnail((400, 400))
            img.convert("RGB").save(t_path, "JPEG", quality=85)
    except: pass # Skip bad files silently during massive runs

### ---------------------------------------------------------------------------
### LAYER: DATA_PERSISTENCE
### ---------------------------------------------------------------------------

def init_db():
    """Nukes the existing DB and cache for a clean stress-test foundation."""
    if os.path.exists(DB_PATH): os.remove(DB_PATH)
    # Also nuke the composite cache folder to fix the 'whack' thumbnails
    comp_dir = os.path.join(THUMB_DIR, "_composites")
    if os.path.isdir(comp_dir):
        shutil.rmtree(comp_dir)
        os.makedirs(comp_dir, exist_ok=True)
        
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE media (
        sha1 TEXT PRIMARY KEY, rel_fqn TEXT, original_filename TEXT, 
        path_tags TEXT, final_dt TEXT, dt_source TEXT, 
        is_deleted INTEGER DEFAULT 0, custom_notes TEXT, custom_tags TEXT
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
    mtime = os.path.getmtime(file_path)
    dt = datetime.fromtimestamp(mtime)
    return dt.strftime('%Y-%m-%d %H:%M:%S'), "File Creation"

### ---------------------------------------------------------------------------
### LAYER: WORKFLOW_CONTROLLER
### ---------------------------------------------------------------------------

def run_ingest():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    
    start_time = time.time()
    total_files = 0
    
    print(f"--- STARTING MASSIVE INGEST (V3.1) ---")
    print(f"Destination: {DEST_ROOT}")
    
    for src in SOURCES:
        if not os.path.exists(src):
            print(f"Skipping missing source: {src}")
            continue
            
        print(f"\nScanning: {src}")
        
        for root, _, files in os.walk(src):
            for f in files:
                if not f.lower().endswith(('.jpg', '.jpeg', '.png')): continue
                
                src_path = os.path.join(root, f)
                sha1 = get_sha1(src_path)
                
                rel_dir = os.path.relpath(root, src)
                dest_dir = os.path.join(DEST_ROOT, os.path.basename(src), rel_dir)
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = os.path.join(dest_dir, f)
                
                # PHYSICAL COPY
                shutil.copy2(src_path, dest_path)
                
                # METADATA & THUMB
                final_dt, source_label = parse_date(dest_path)
                rel_fqn = os.path.relpath(dest_path, DEST_ROOT)
                path_tags = rel_dir.replace(os.sep, ',')
                
                conn.execute("""INSERT OR REPLACE INTO media 
                    (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source) 
                    VALUES (?, ?, ?, ?, ?, ?)""", 
                    (sha1, rel_fqn, f, path_tags, final_dt, source_label))
                
                create_thumbnail(dest_path, sha1)
                
                total_files += 1
                if total_files % LOG_INTERVAL == 0:
                    elapsed = time.time() - start_time
                    rate = total_files / elapsed if elapsed > 0 else 0
                    print(f"\r  [Progress] Processed {total_files} files... ({rate:.1f} files/sec)", end="", flush=True)

    conn.commit()
    conn.close()
    
    duration = time.time() - start_time
    print(f"\n\n--- INGEST COMPLETE ---")
    print(f"Total Files Indexed: {total_files}")
    print(f"Total Duration: {duration/60:.2f} minutes")
    print(f"Final DB Size: {os.path.getsize(DB_PATH)/1024/1024:.2f} MB")

if __name__ == "__main__":
    run_ingest()