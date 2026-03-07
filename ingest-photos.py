import os
import shutil
import sqlite3
import hashlib
import time
import re
from datetime import datetime
from PIL import Image

# --- STABILITY & ENVIRONMENT ---
# We disable the "Decompression Bomb" protection in the Pillow library.
# This is a critical safety setting that allows the script to process 
# extremely large panorama images or high-resolution scans without crashing.
Image.MAX_IMAGE_PIXELS = None 

# --- CONFIGURATION ---
# Define the source folders to scan and the master archive destination.
SOURCES = [r"C:\test-undated", r"C:\test-images"] 
DEST_ROOT = r"C:\website-test"

# Derived paths for the database and thumbnail storage.
DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")

# Number of files to process between console progress updates.
LOG_INTERVAL = 50 

# Regex pattern for 4-digit years (1950-2026).
# We use 'lookarounds' (?<!\d) and (?!\d) to ensure we catch years even 
# when they are surrounded by underscores (e.g., Trip_2024_Day1).
YEAR_REGEX = r'(?<!\d)(19[5-9]\d|20[0-2]\d)(?!\d)'

# Ensure the destination and thumbnail directories exist.
os.makedirs(THUMB_DIR, exist_ok=True)

### ---------------------------------------------------------------------------
### LAYER 1: IDENTITY & COLLISION LOGIC
### ---------------------------------------------------------------------------

def get_sha1(file_path):
    """
    Creates a 'Digital Fingerprint' of a file's content.
    The 'hashlib.sha1' function reads the file and produces a unique string.
    If the file content changes by even one pixel, the hash will be different.
    """
    h = hashlib.sha1()
    with open(file_path, 'rb') as f:
        # We read the file in 8KB chunks to keep memory usage low.
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def get_unique_dest_path(base_dir, filename, sha1):
    """
    Solves filename collisions. If a different photo is named 'img.jpg',
    this function renames the new one to 'img_1.jpg'. If the content (SHA1)
    is identical, it simply returns the existing path.
    """
    name, ext = os.path.splitext(filename)
    counter = 0
    while True:
        # Construct a candidate path (e.g., photo.jpg, then photo_1.jpg...)
        candidate_name = f"{name}_{counter}{ext}" if counter > 0 else filename
        candidate_path = os.path.join(base_dir, candidate_name)
        
        # If the file doesn't exist, this path is safe!
        if not os.path.exists(candidate_path):
            return candidate_path
        
        # If it DOES exist, check if it's the SAME photo (matching SHA1)
        if get_sha1(candidate_path) == sha1:
            return candidate_path
        
        # If it's a DIFFERENT photo with the same name, increment and try again.
        counter += 1

### ---------------------------------------------------------------------------
### LAYER 2: THE RESOLUTION ENGINE (Dates & Tags)
### ---------------------------------------------------------------------------

def analyze_path_context(full_path):
    """
    The 'Brain' of the system. It explodes the path into components to 
    find 'undated' flags, specific years, and clean tags.
    """
    # Split the path into a list of individual folder names.
    parts = full_path.replace('\\', '/').split('/')
    
    is_undated = False
    detected_year = None
    clean_tag_list = []

    for part in parts:
        # Skip drive letters (like 'C:') or empty strings.
        if not part or ':' in part: continue 
        
        # Check for the 'undated' override (case-insensitive).
        if 'undated' in part.lower():
            is_undated = True
        
        # Scan for a year. The deepest (most specific) folder wins.
        year_match = re.search(YEAR_REGEX, part)
        if year_match:
            detected_year = year_match.group(1)
        
        # Clean the folder name for use as a tag by removing the year.
        tag_part = re.sub(YEAR_REGEX, '', part)
        # Replace underscores/hyphens with spaces and trim.
        tag_part = tag_part.replace('_', ' ').replace('-', ' ').strip()
        if tag_part:
            clean_tag_list.append(tag_part)

    return is_undated, detected_year, ",".join(clean_tag_list)

def resolve_final_date(file_path, root_path):
    """Implements the Spec's Date Hierarchy (Undated > Year Override > EXIF)."""
    is_undated, folder_year, _ = analyze_path_context(root_path)
    
    # 1. Undated Override
    if is_undated:
        return "0000-00-00 00:00:00", "Path: Undated Override"
    
    # Extract internal file metadata (EXIF or Modification Time).
    internal_dt = None
    try:
        with Image.open(file_path) as img:
            exif = img._getexif()
            if exif and 36867 in exif:
                internal_dt = datetime.strptime(exif[36867], '%Y:%m:%d %H:%M:%S')
    except: pass
    
    if not internal_dt:
        # Fallback to the Operating System's 'Modified' time.
        internal_dt = datetime.fromtimestamp(os.path.getmtime(file_path))

    # 2. Folder Year Override
    if folder_year:
        # If the camera year matches the folder year, trust the full date.
        if str(internal_dt.year) == folder_year:
            return internal_dt.strftime('%Y-%m-%d %H:%M:%S'), "EXIF (Year Match)"
        else:
            # If they conflict, the folder is the 'Source of Truth'. Set Jan 1.
            return f"{folder_year}-01-01 00:00:00", f"Path: {folder_year} Override"

    # 3. Default
    return internal_dt.strftime('%Y-%m-%d %H:%M:%S'), "Default (EXIF/OS)"

### ---------------------------------------------------------------------------
### LAYER 3: MAIN EXECUTION ENGINE
### ---------------------------------------------------------------------------

def run_ingest():
    # Initialize the Database without wiping existing data.
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS media (
        sha1 TEXT PRIMARY KEY, rel_fqn TEXT, original_filename TEXT, 
        path_tags TEXT, final_dt TEXT, dt_source TEXT, 
        is_deleted INTEGER DEFAULT 0, custom_notes TEXT, custom_tags TEXT
    )''')
    
    start_time = time.time()
    stats = {"new": 0, "healed": 0, "skipped": 0, "total": 0}

    print(f"--- STARTING V3.8.1 MASTER INGEST ---")

    for src in SOURCES:
        if not os.path.exists(src): continue
        source_name = os.path.basename(src)

        for root, _, files in os.walk(src):
            rel_dir = os.path.relpath(root, src)
            
            for f in files:
                if not f.lower().endswith(('.jpg', '.jpeg', '.png')): continue
                
                src_path = os.path.join(root, f)
                sha1 = get_sha1(src_path)
                stats["total"] += 1
                
                # Check DB for content identity.
                existing = conn.execute("SELECT rel_fqn FROM media WHERE sha1=?", (sha1,)).fetchone()
                
                if existing:
                    master_path = os.path.join(DEST_ROOT, existing[0])
                    if os.path.exists(master_path):
                        stats["skipped"] += 1
                        continue
                    else:
                        # HEALING: The photo is in the DB but missing from the disk.
                        dest_dir = os.path.join(DEST_ROOT, source_name, rel_dir)
                        os.makedirs(dest_dir, exist_ok=True)
                        dest_path = get_unique_dest_path(dest_dir, f, sha1)
                        # copy2 preserves the original file timestamps.
                        shutil.copy2(src_path, dest_path)
                        
                        new_rel = os.path.relpath(dest_path, DEST_ROOT)
                        # We update the 'rel_fqn' pointer instead of inserting a new row.
                        conn.execute("UPDATE media SET rel_fqn = ? WHERE sha1 = ?", (new_rel, sha1))
                        stats["healed"] += 1
                        continue

                # NEW INGEST
                dest_dir = os.path.join(DEST_ROOT, source_name, rel_dir)
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = get_unique_dest_path(dest_dir, f, sha1)
                
                shutil.copy2(src_path, dest_path)
                
                # Resolve Dates and Tags based on the 'Hardened' spec rules.
                final_dt, source_label = resolve_final_date(dest_path, root)
                _, _, clean_tags = analyze_path_context(root)
                final_tags = f"{source_name},{clean_tags}" if clean_tags else source_name
                
                rel_fqn = os.path.relpath(dest_path, DEST_ROOT)
                
                # INSERT ensures we don't clobber any manually edited notes or tags.
                conn.execute("""INSERT INTO media 
                    (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source) 
                    VALUES (?, ?, ?, ?, ?, ?)""", 
                    (sha1, rel_fqn, f, final_tags, final_dt, source_label))
                
                # Create a 400px thumbnail.
                t_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
                if not os.path.exists(t_path):
                    try:
                        with Image.open(dest_path) as img:
                            img.thumbnail((400, 400))
                            img.convert("RGB").save(t_path, "JPEG", quality=85)
                    except: pass
                
                stats["new"] += 1

                # Progress Display: Calculates real-time files per second.
                if stats["total"] % LOG_INTERVAL == 0:
                    elapsed = time.time() - start_time
                    rate = stats["total"] / elapsed if elapsed > 0 else 0
                    print(f"\r  [Progress] {stats['total']} files | {rate:.1f} files/sec", end="", flush=True)

    conn.commit()
    conn.close()
    
    print(f"\n\n--- INGEST COMPLETE ---")
    print(f"New: {stats['new']} | Healed: {stats['healed']} | Skipped: {stats['skipped']}")

if __name__ == "__main__":
    run_ingest()