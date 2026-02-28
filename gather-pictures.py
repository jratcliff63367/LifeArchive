import os
import re
import sys
import shutil
import sqlite3
import hashlib
import signal
from datetime import datetime

# Third-party libraries
import exifread
from PIL import Image
import imagehash

# --- CONFIGURATION ---
SOURCE_DIR = r"C:\LegacyTransfer"
DEST_DIR = r"C:\GatherPictures"
DB_NAME = "archive_index.sqlite"

# Toggles
APPEND_FOLDER_NAME = True  # Append the parent folder name to the base file name
VERBOSE_LOGGING = False    # Set to True for extreme per-file debug spew
PROGRESS_INTERVAL = 100    # Print a heartbeat message every X files processed

# Global flag for clean interrupt
stop_requested = False

def signal_handler(sig, frame):
    global stop_requested
    print("\n[!] Ctrl+C detected. Finishing current file and shutting down safely...")
    stop_requested = True

signal.signal(signal.SIGINT, signal_handler)

def debug_print(msg):
    if VERBOSE_LOGGING:
        print(msg)

# --- DATABASE SETUP ---
def setup_database(dest_dir):
    print(f"[*] Setting up/Connecting to database at: {dest_dir}")
    db_path = os.path.join(dest_dir, DB_NAME)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dest_files (
            dest_path TEXT PRIMARY KEY,
            base_name TEXT,
            size INTEGER,
            sha256 TEXT,
            dhash TEXT,
            file_date TEXT
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sha_size ON dest_files(sha256, size)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_dhash ON dest_files(dhash)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_basename ON dest_files(base_name)')
    conn.commit()
    return conn

# --- HASHING & METADATA FUNCTIONS ---
def get_sha256(file_path):
    debug_print(f"[DEBUG] Computing SHA256 for: {os.path.basename(file_path)}")
    sha = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            while chunk := f.read(8192):
                sha.update(chunk)
        return sha.hexdigest()
    except Exception as e:
        print(f"[ERROR] SHA256 Error on {file_path}: {e}")
        return "ERROR"

def get_dhash(file_path):
    debug_print(f"[DEBUG] Computing dHash for: {os.path.basename(file_path)}")
    try:
        with Image.open(file_path) as img:
            return str(imagehash.dhash(img))
    except Exception as e:
        debug_print(f"[DEBUG] dHash Error: {e}")
        return "ERROR"

def _convert_gps_to_degrees(value):
    try:
        d, m, s = value.values
        degrees = float(d.num) / float(d.den)
        minutes = float(m.num) / float(m.den)
        seconds = float(s.num) / float(s.den)
        return degrees + (minutes / 60.0) + (seconds / 3600.0)
    except Exception:
        return None

def extract_exif_data(file_path):
    exif_date = None
    lat_deg = None
    lon_deg = None
    
    debug_print(f"[DEBUG] Extracting EXIF from: {os.path.basename(file_path)}")
    try:
        with open(file_path, 'rb') as f:
            tags = exifread.process_file(f, details=False)
            
            if 'EXIF DateTimeOriginal' in tags:
                date_str = str(tags['EXIF DateTimeOriginal'])
                try:
                    exif_date = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
                    debug_print(f"[DEBUG]   -> EXIF Date: {exif_date}")
                except ValueError as ve:
                    debug_print(f"[DEBUG]   -> Date parse error: {ve}")
            else:
                debug_print("[DEBUG]   -> No EXIF DateTimeOriginal found.")

            if 'GPS GPSLatitude' in tags and 'GPS GPSLongitude' in tags:
                try:
                    lat = _convert_gps_to_degrees(tags['GPS GPSLatitude'])
                    lon = _convert_gps_to_degrees(tags['GPS GPSLongitude'])
                    lat_ref = str(tags.get('GPS GPSLatitudeRef', 'N'))
                    lon_ref = str(tags.get('GPS GPSLongitudeRef', 'E'))
                    
                    if lat is not None and lon is not None:
                        lat_deg = lat if lat_ref == 'N' else -lat
                        lon_deg = lon if lon_ref == 'E' else -lon
                        debug_print(f"[DEBUG]   -> EXIF GPS: {lat_deg:.4f}, {lon_deg:.4f}")
                except Exception as e:
                    debug_print(f"[DEBUG]   -> GPS math error: {e}")
            else:
                debug_print("[DEBUG]   -> No GPS data found.")
                
    except Exception as e:
        debug_print(f"[DEBUG]   -> EXIF read failed entirely: {e}")
        
    return exif_date, lat_deg, lon_deg

def format_gps(lat, lon):
    if lat is None or lon is None:
        return "+00.0000+000.0000"
    return f"{lat:+08.4f}{lon:+09.4f}"

# --- CORE LOGIC ---
def process_file(source_path, conn):
    global stop_requested
    
    debug_print(f"\n[DEBUG] === Processing: {source_path} ===")
    raw_base_name = os.path.basename(source_path)
    
    # 1. Sanitize the base name (Converts "photo(1).jpg" to "photo_1.jpg")
    base_name = re.sub(r'\((\d+)\)', r'_\1', raw_base_name)
    
    # 2. Append Folder Name Logic
    if APPEND_FOLDER_NAME:
        parent_folder = os.path.basename(os.path.dirname(source_path))
        if parent_folder and not re.match(r'^(19|20)\d{2}$', parent_folder):
            clean_folder = parent_folder.replace(" ", "_")
            base_name = f"{clean_folder}_{base_name}"
            debug_print(f"[DEBUG] Appended folder name. New base: {base_name}")
    
    try:
        size = os.path.getsize(source_path)
        fs_date = datetime.fromtimestamp(os.path.getmtime(source_path))
    except Exception as e:
        print(f"[ERROR] Failed to read file properties for {source_path}: {e}")
        return "ERRORS"
    
    cursor = conn.cursor()
    sha256 = get_sha256(source_path)
    cursor.execute('SELECT dest_path FROM dest_files WHERE sha256=? AND size=?', (sha256, size))
    if cursor.fetchone():
        debug_print("[DEBUG] -> Found exact match in DB. Skipping.")
        return "SKIPPED_EXACT_DUPLICATE"

    exif_date, lat, lon = extract_exif_data(source_path)
    dhash = get_dhash(source_path)
    
    match = re.search(r'(?:^|[\\/])(19[5-9]\d|20[0-2]\d)(?:[\\/]|$)', source_path)
    path_year = int(match.group(1)) if match else None

    has_gps = (lat is not None)
    if path_year and exif_date and not has_gps:
        final_date = datetime(path_year, 1, 1)
        debug_print("[DEBUG] Applied Scanner Edge Case logic (Path Year + Exif Date, No GPS)")
    elif exif_date:
        final_date = exif_date
    elif path_year:
        final_date = datetime(path_year, 1, 1)
    else:
        final_date = fs_date

    year_str = final_date.strftime('%Y')
    month_str = final_date.strftime('%m')
    date_prefix = final_date.strftime('%Y-%m-%d')
    gps_prefix = format_gps(lat, lon)
    
    target_dir = os.path.join(DEST_DIR, f"Y{year_str}", f"Y{year_str}M{month_str}")
    new_base = f"P{date_prefix}-{gps_prefix}-{base_name}"
    target_path = os.path.join(target_dir, new_base)
    
    counter = 1
    while True:
        cursor.execute('SELECT size FROM dest_files WHERE dest_path=?', (target_path,))
        row = cursor.fetchone()
        if not row:
            break 
            
        dest_size = row[0]
        if size == dest_size:
            debug_print(f"[DEBUG] Name and Size collision at {target_path}. Skipping.")
            return "SKIPPED_EXISTING_NAME_AND_SIZE"
            
        name_only, ext = os.path.splitext(base_name)
        new_base = f"P{date_prefix}-{gps_prefix}-{name_only}_{counter}{ext}"
        target_path = os.path.join(target_dir, new_base)
        counter += 1

    os.makedirs(target_dir, exist_ok=True)
    try:
        shutil.copy2(source_path, target_path)
    except Exception as e:
        print(f"[ERROR] Copy failed for {source_path}: {e}")
        return "ERRORS"
    
    cursor.execute('''
        INSERT INTO dest_files (dest_path, base_name, size, sha256, dhash, file_date)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (target_path, base_name, size, sha256, dhash, final_date.strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    
    return "COPIED"

def main():
    print(f"\n{'='*40}")
    print("=== LIFE ARCHIVE BUILDER STARTING ===")
    print(f"SOURCE: {SOURCE_DIR}")
    print(f"DEST:   {DEST_DIR}")
    print(f"{'='*40}\n")

    if not os.path.exists(SOURCE_DIR):
        print(f"[FATAL] Source directory {SOURCE_DIR} does not exist!")
        sys.exit(1)

    if not os.path.exists(DEST_DIR):
        print(f"[*] Creating destination directory: {DEST_DIR}")
        os.makedirs(DEST_DIR)
        
    conn = setup_database(DEST_DIR)
    
    stats = {"COPIED": 0, "SKIPPED_EXACT_DUPLICATE": 0, "SKIPPED_EXISTING_NAME_AND_SIZE": 0, "ERRORS": 0}
    total_processed = 0
    
    for root, dirs, files in os.walk(SOURCE_DIR):
        if stop_requested:
            break
            
        jpg_files = [f for f in files if f.lower().endswith(('.jpg', '.jpeg'))]
        if jpg_files:
            print(f"\n[>] Scanning folder: {root} ({len(jpg_files)} photos)")
        
        for file in jpg_files:
            if stop_requested:
                break
                
            source_path = os.path.join(root, file)
            try:
                status = process_file(source_path, conn)
                stats[status] += 1
                total_processed += 1
                
                # Heartbeat feedback
                if total_processed % PROGRESS_INTERVAL == 0:
                    print(f"    ... processed {total_processed} total files so far ...")
                    
            except Exception as e:
                print(f"[ERROR] Fatal error processing {file}: {e}")
                stats["ERRORS"] += 1

    conn.close()
    
    print("\n" + "="*40)
    print("ARCHIVE RUN COMPLETE")
    print(f"Files Copied: {stats['COPIED']}")
    print(f"Duplicates Skipped: {stats['SKIPPED_EXACT_DUPLICATE']}")
    print(f"Collisions Skipped: {stats['SKIPPED_EXISTING_NAME_AND_SIZE']}")
    print(f"Errors: {stats['ERRORS']}")
    print("="*40)

if __name__ == "__main__":
    main()