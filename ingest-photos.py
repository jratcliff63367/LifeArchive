import os
import shutil
import sqlite3
import hashlib
import time
import re
from datetime import datetime
from PIL import Image, ImageOps

# --- CONFIGURATION ---
# Add all high-volume source folders to the SOURCES list.
# DEST_ROOT is the target directory for the managed archive.
SOURCES = [r"c:\TerrysBackup", r"e:\Topaz-Undated"] 
DEST_ROOT = r"C:\website-test"

# Database and Folder paths
DB_PATH = os.path.join(DEST_ROOT, "archive_index.db")
THUMB_DIR = os.path.join(DEST_ROOT, "_thumbs")
LOG_INTERVAL = 100 

# GLOBAL SETTINGS
# Disabling the decompression bomb limit allows processing of massive panoramas/scans.
Image.MAX_IMAGE_PIXELS = None 

# Ensure environment is ready
os.makedirs(THUMB_DIR, exist_ok=True)

### ---------------------------------------------------------------------------
### LAYER: IO_ENGINE
### ---------------------------------------------------------------------------

def get_sha1(file_path):
    """
    Calculates the SHA1 hash of a file to provide a unique fingerprint.
    Input: file_path (str)
    Output: hex digest string (str)
    """
    h = hashlib.sha1()
    with open(file_path, 'rb') as f:
        # Read in 8kb chunks to handle large files without memory spikes
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def get_unique_dest_path(base_dir, filename, sha1):
    """
    Implements the Collision Resolver. If a different file shares a name, 
    appends a numerical suffix until a unique slot is found.
    Input: base_dir, filename, sha1
    Output: finalized unique file path
    """
    name, ext = os.path.splitext(filename)
    counter = 0
    while True:
        candidate_name = f"{name}_{counter}{ext}" if counter > 0 else filename
        candidate_path = os.path.join(base_dir, candidate_name)
        
        # Path is free
        if not os.path.exists(candidate_path):
            return candidate_path
        
        # If file exists, check if it's actually the same content (same SHA1)
        # If so, it's not a collision, it's just a duplicate we're aware of.
        if get_sha1(candidate_path) == sha1:
            return candidate_path
        
        counter += 1

def generate_thumbnail(image_path, thumb_path):
    """
    Generate a UI thumbnail while honoring EXIF orientation.
    This prevents portrait photos from being baked out sideways/upside-down
    when the original relies on EXIF orientation metadata.
    """
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((400, 400))
        img.convert("RGB").save(thumb_path, "JPEG", quality=85)

### ---------------------------------------------------------------------------
### LAYER: METADATA_ENGINE
### ---------------------------------------------------------------------------

def get_gps_coordinates(exif_data):
    """
    Verifies and extracts GPS coordinates from EXIF.
    Input: exif_data dictionary
    Output: (lat, lon) tuple if valid, else None
    """
    if not exif_data:
        return None
        
    # GPS data is stored in a nested dictionary under tag 34853 (GPSInfo)
    gps_info = exif_data.get(34853)
    if not gps_info:
        return None

    def convert_to_degrees(value):
        # Coordinates are stored as (degrees, minutes, seconds)
        d = float(value[0])
        m = float(value[1])
        s = float(value[2])
        return d + (m / 60.0) + (s / 3600.0)

    try:
        lat = convert_to_degrees(gps_info[2])
        if gps_info[1] == 'S': lat = -lat
        
        lon = convert_to_degrees(gps_info[4])
        if gps_info[3] == 'W': lon = -lon
        
        # Validity Check: Must not be 0.0 (Null Island) and within global range
        if lat == 0.0 and lon == 0.0:
            return None
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    except:
        pass
    return None

def resolve_file_date(file_path, rel_path):
    """
    Implements the Date Resolution Algorithm Hierarchy (Spec V3.9).
    GPS Golden Signal -> Undated Override -> Year Extraction -> Date Assembly.
    """
    # 1. INITIAL DATA GATHERING
    exif_data = None
    try:
        with Image.open(file_path) as img:
            exif_data = img._getexif()
    except:
        pass

    # --- STEP 0: THE GPS GOLDEN SIGNAL ---
    gps = get_gps_coordinates(exif_data)
    if gps:
        # Trust EXIF tags: 36867 (DateTimeOriginal) or 36868 (CreateDate)
        dt_str = exif_data.get(36867) or exif_data.get(36868)
        if dt_str:
            try:
                dt = datetime.strptime(dt_str, '%Y:%m:%d %H:%M:%S')
                return dt.strftime('%Y-%m-%d %H:%M:%S'), "GPS-Verified EXIF"
            except:
                pass

    # --- STEP A: UNDATED OVERRIDE ---
    if 'undated' in rel_path.lower():
        return "0000-00-00 00:00:00", "Path Override: Undated"

    # --- STEP B: YEAR EXTRACTION ---
    # Regex checks for 1950-2029 while avoiding numbers embedded in larger strings.
    year_regex = r"(?<!\d)(19[5-9]\d|20[0-2]\d)(?!\d)"
    path_parts = rel_path.split(os.sep)
    target_year = None
    # Iterate from deepest folder to shallowest (Spec V3.9 requirement)
    for part in reversed(path_parts):
        match = re.search(year_regex, part)
        if match:
            target_year = int(match.group(1))
            break

    # --- STEP C: DATE ASSEMBLY ---
    # Fallback to File Modification time if EXIF is missing or corrupt.
    mtime = os.path.getmtime(file_path)
    internal_dt = datetime.fromtimestamp(mtime)
    source_label = "File Modification"

    if exif_data and (36867 in exif_data):
        try:
            internal_dt = datetime.strptime(exif_data[36867], '%Y:%m:%d %H:%M:%S')
            source_label = "EXIF"
        except:
            pass

    if target_year:
        if internal_dt.year == target_year:
            return internal_dt.strftime('%Y-%m-%d %H:%M:%S'), source_label
        else:
            # If internal clock is wrong but folder is named, folder wins.
            return f"{target_year}-01-01 00:00:00", f"Path-Year Overwrite ({source_label} mismatched)"
    
    return internal_dt.strftime('%Y-%m-%d %H:%M:%S'), source_label

### ---------------------------------------------------------------------------
### LAYER: DATA_PERSISTENCE
### ---------------------------------------------------------------------------

def init_db():
    """Ensures the DB and essential columns exist without wiping data."""
    conn = sqlite3.connect(DB_PATH)
    # Using IF NOT EXISTS protects existing manual curation.
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
    
    total_new, total_healed, skipped = 0, 0, 0
    year_regex = r"(?<!\d)(19[5-9]\d|20[0-2]\d)(?!\d)"
    
    print(f"--- STARTING ROBUST INGEST (V3.9 GPS-Enabled) ---")
    
    for src in SOURCES:
        if not os.path.exists(src): continue
        source_base = os.path.basename(src)
            
        for root, _, files in os.walk(src):
            for f in files:
                # Extension filter
                if not f.lower().endswith(('.jpg', '.jpeg', '.png')): continue
                
                src_path = os.path.join(root, f)
                sha1 = get_sha1(src_path)
                
                # --- IDENTITY GATE ---
                existing = conn.execute("SELECT rel_fqn FROM media WHERE sha1=?", (sha1,)).fetchone()
                if existing:
                    # If file exists at its registered path, skip.
                    if os.path.exists(os.path.join(DEST_ROOT, existing[0])):
                        skipped += 1
                        continue
                    else:
                        # HEAL Logic: Update DB to point to the new location.
                        rel_dir = os.path.relpath(root, src)
                        dest_dir = os.path.join(DEST_ROOT, source_base, rel_dir)
                        os.makedirs(dest_dir, exist_ok=True)
                        dest_path = get_unique_dest_path(dest_dir, f, sha1)
                        
                        shutil.copy2(src_path, dest_path)
                        new_rel_fqn = os.path.relpath(dest_path, DEST_ROOT)
                        conn.execute("UPDATE media SET rel_fqn = ? WHERE sha1 = ?", (new_rel_fqn, sha1))
                        total_healed += 1
                        continue

                # --- NEW PHOTO PROCESSING ---
                rel_dir = os.path.relpath(root, src)
                dest_dir = os.path.join(DEST_ROOT, source_base, rel_dir)
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = get_unique_dest_path(dest_dir, f, sha1)
                
                # shutil.copy2 preserves the OS 'Modified' time.
                shutil.copy2(src_path, dest_path)
                
                # Metadata extraction
                final_dt, source_label = resolve_file_date(dest_path, rel_dir)
                rel_fqn = os.path.relpath(dest_path, DEST_ROOT)
                
                # --- TAGGING ALGORITHM ---
                # Clean up folder names to create human-readable tags
                path_parts = [source_base] + rel_dir.split(os.sep)
                clean_tags = []
                for part in path_parts:
                    if not part or part == ".": continue
                    # Remove years, replace punctuation with spaces, trim.
                    clean = re.sub(year_regex, "", part)
                    clean = clean.replace("_", " ").replace("-", " ").strip()
                    if clean: clean_tags.append(clean.title())
                path_tags = ",".join(clean_tags)
                
                # Persist to database
                conn.execute("""INSERT INTO media 
                    (sha1, rel_fqn, original_filename, path_tags, final_dt, dt_source) 
                    VALUES (?, ?, ?, ?, ?, ?)""", 
                    (sha1, rel_fqn, os.path.basename(dest_path), path_tags, final_dt, source_label))
                
                # Generate UI thumbnail
                t_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
                if not os.path.exists(t_path):
                    try:
                        generate_thumbnail(dest_path, t_path)
                    except:
                        pass
                
                total_new += 1
                if (total_new + skipped + total_healed) % LOG_INTERVAL == 0:
                    rate = (total_new + skipped + total_healed) / (time.time() - start_time)
                    print(f"\r  [Progress] New: {total_new} | Healed: {total_healed} | Skipped: {skipped} | {rate:.1f} files/sec", end="", flush=True)

    conn.commit()
    conn.close()
    
    # End Summary
    duration = time.time() - start_time
    print(f"\n\n--- INGEST COMPLETE ---")
    print(f"Duration: {duration:.1f}s")
    print(f"New added: {total_new}\nFiles healed: {total_healed}\nDuplicates skipped: {skipped}")

if __name__ == "__main__":
    run_ingest()
