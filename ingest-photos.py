import os
import shutil
import sqlite3
import hashlib
from datetime import datetime
from PIL import Image

# --- CONFIGURATION ---
SOURCES = [r"C:\test-undated", r"C:\test-images"]
DEST_ROOT = r"C:\website-test"
DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")

# Ensure environment is ready
os.makedirs(THUMB_DIR, exist_ok=True)

### ---------------------------------------------------------------------------
### LAYER: IO_ENGINE (File Handling & Hashing)
### ---------------------------------------------------------------------------

def get_sha1(file_path):
    """Generates unique ID for deduplication and thumbnails."""
    h = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def create_thumbnail(src_path, sha1):
    """Generates the 400px thumb used by the UI and 4x4 composites."""
    t_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
    if os.path.exists(t_path): return
    try:
        with Image.open(src_path) as img:
            img.thumbnail((400, 400))
            img.convert("RGB").save(t_path, "JPEG", quality=85)
    except Exception as e:
        print(f"  [!] Thumb Error: {e}")

### ---------------------------------------------------------------------------
### LAYER: DATA_PERSISTENCE (Metadata & Date Logic)
### ---------------------------------------------------------------------------

def init_db():
    """Wipes and recreates the index for a clean test-bench run."""
    if os.path.exists(DB_PATH): os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE media (
        sha1 TEXT PRIMARY KEY, rel_fqn TEXT, original_filename TEXT, 
        path_tags TEXT, final_dt TEXT, dt_source TEXT, 
        is_deleted INTEGER DEFAULT 0, custom_notes TEXT, custom_tags TEXT
    )''')
    conn.close()

def parse_date(file_path):
    """
    STRICT INTERFACE: 
    1. If filename contains 'OBI_', return '0000-00-00 00:00:00'.
    2. Else, try EXIF.
    3. Fallback to File Creation.
    """
    fname = os.path.basename(file_path)
    if "OBI_" in fname:
        return "0000-00-00 00:00:00", "Undated Fallback"
    
    # Try EXIF
    try:
        with Image.open(file_path) as img:
            exif = img._getexif()
            if exif and 36867 in exif:
                dt = datetime.strptime(exif[36867], '%Y:%m:%d %H:%M:%S')
                return dt.strftime('%Y-%m-%d %H:%M:%S'), "EXIF"
    except: pass
    
    # Fallback
    mtime = os.path.getmtime(file_path)
    dt = datetime.fromtimestamp(mtime)
    return dt.strftime('%Y-%m-%d %H:%M:%S'), "File Creation"

### ---------------------------------------------------------------------------
### LAYER: WORKFLOW_CONTROLLER (The 'Copy & Index' Loop)
### ---------------------------------------------------------------------------

def run_ingest():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    
    print(f"--- STARTING GOLDEN INGEST (V3.0) ---")
    
    for src in SOURCES:
        if not os.path.exists(src):
            print(f"Skipping missing source: {src}")
            continue
            
        print(f"Processing Source: {src}")
        
        for root, _, files in os.walk(src):
            for f in files:
                if not f.lower().endswith(('.jpg', '.jpeg', '.png')): continue
                
                src_path = os.path.join(root, f)
                sha1 = get_sha1(src_path)
                
                # Determine Destination Path (maintain relative folder structure)
                rel_dir = os.path.relpath(root, src)
                dest_dir = os.path.join(DEST_ROOT, os.path.basename(src), rel_dir)
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = os.path.join(dest_dir, f)
                
                # PHYSICAL COPY
                shutil.copy2(src_path, dest_path)
                
                # METADATA
                final_dt, source_label = parse_date(dest_path)
                rel_fqn = os.path.relpath(dest_path, DEST_ROOT)
                path_tags = rel_dir.replace(os.sep, ',')
                
                # INDEX
                conn.execute("""INSERT OR REPLACE INTO media 
                    (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source) 
                    VALUES (?, ?, ?, ?, ?, ?)""", 
                    (sha1, rel_fqn, f, path_tags, final_dt, source_label))
                
                # THUMBNAIL
                create_thumbnail(dest_path, sha1)
                print(f"  [+] Indexed: {f} ({source_label})")

    conn.commit()
    conn.close()
    print(f"--- INGEST COMPLETE. Database created at {DB_PATH} ---")

if __name__ == "__main__":
    run_ingest()