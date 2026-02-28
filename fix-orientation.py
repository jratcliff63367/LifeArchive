import os
import sys
from PIL import Image, ImageOps
import piexif

# --- CONFIGURATION ---
TARGET_DIR = r"C:\GatherPictures"

def fix_orientations(directory):
    print(f"\n{'='*40}")
    print("=== EXIF ORIENTATION FIXER ===")
    print(f"TARGET: {directory}")
    print(f"{'='*40}\n")

    if not os.path.exists(directory):
        print(f"[FATAL] Directory {directory} does not exist.")
        sys.exit(1)

    fixed_count = 0
    error_count = 0

    for root, _, files in os.walk(directory):
        jpg_files = [f for f in files if f.lower().endswith(('.jpg', '.jpeg'))]
        
        for file in jpg_files:
            file_path = os.path.join(root, file)
            
            try:
                # Open image without loading pixels into memory yet
                img = Image.open(file_path)
                
                if "exif" in img.info:
                    exif_dict = piexif.load(img.info["exif"])
                    
                    # 274 is the standard EXIF tag ID for Orientation
                    if piexif.ImageIFD.Orientation in exif_dict["0th"]:
                        orientation = exif_dict["0th"][piexif.ImageIFD.Orientation]
                        
                        # 1 means Normal (Upright). Anything else needs fixing.
                        if orientation != 1:
                            print(f"[FIXING] Rotating sideways photo: {os.path.basename(file_path)}")
                            
                            # 1. Rotate the actual pixels based on the EXIF tag
                            fixed_img = ImageOps.exif_transpose(img)
                            
                            # 2. Reset the EXIF tag to 1 (Normal) so it doesn't get double-rotated
                            exif_dict["0th"][piexif.ImageIFD.Orientation] = 1
                            exif_bytes = piexif.dump(exif_dict)
                            
                            # 3. Save the permanently upright image
                            fixed_img.save(file_path, "JPEG", exif=exif_bytes, quality=95)
                            fixed_count += 1
                            
            except Exception as e:
                # Usually triggers on truncated/corrupted files
                print(f"[ERROR] Could not process {os.path.basename(file_path)}: {e}")
                error_count += 1

    print("\n" + "="*40)
    print("ROTATION RUN COMPLETE")
    print(f"Files Fixed: {fixed_count}")
    print(f"Errors: {error_count}")
    print("="*40)

if __name__ == "__main__":
    fix_orientations(TARGET_DIR)