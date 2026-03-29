#!/usr/bin/env python3

import shutil
from pathlib import Path

# =========================
# CONFIG
# =========================
SOURCE_DIR = r"C:\GameArt"
DEST_DIR   = r"C:\GameArt-Flat"

# =========================
# SCRIPT
# =========================

JPEG_EXTENSIONS = {".jpg", ".jpeg"}


def is_jpeg(path: Path) -> bool:
    return path.suffix.lower() in JPEG_EXTENSIONS


def get_unique_filename(dest_dir: Path, filename: str) -> Path:
    base = Path(filename).stem
    ext = Path(filename).suffix

    candidate = dest_dir / filename
    counter = 1

    while candidate.exists():
        candidate = dest_dir / f"{base}({counter}){ext}"
        counter += 1

    return candidate


def main():
    source = Path(SOURCE_DIR)
    dest = Path(DEST_DIR)

    if not source.exists():
        print(f"ERROR: Source directory does not exist: {source}")
        return

    dest.mkdir(parents=True, exist_ok=True)

    total_found = 0
    total_copied = 0

    print(f"Scanning: {source}")
    print(f"Destination (flat): {dest}")
    print("----")

    for file in source.rglob("*"):
        if not file.is_file():
            continue

        if not is_jpeg(file):
            continue

        total_found += 1

        try:
            dest_file = get_unique_filename(dest, file.name)
            shutil.copy2(file, dest_file)
            total_copied += 1

            if total_copied % 100 == 0:
                print(f"Copied {total_copied} files...")

        except Exception as e:
            print(f"ERROR copying {file}: {e}")

    print("----")
    print(f"Total JPEGs found: {total_found}")
    print(f"Total copied: {total_copied}")
    print("Done.")


if __name__ == "__main__":
    main()