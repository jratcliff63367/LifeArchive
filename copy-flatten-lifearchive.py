#!/usr/bin/env python3

import shutil
from pathlib import Path

# =========================
# CONFIG
# =========================
TRASH_SOURCE_DIR = r"C:\LifeArchive\trash"
STASH_SOURCE_DIR = r"C:\LifeArchive\_stash"
DEST_ROOT        = r"C:\LifeFlatten"

# JPEG only, matching the current utility script
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


def copy_flattened(source_dir: Path, dest_dir: Path) -> tuple[int, int]:
    if not source_dir.exists():
        print(f"WARNING: Source directory does not exist: {source_dir}")
        return 0, 0

    dest_dir.mkdir(parents=True, exist_ok=True)

    total_found = 0
    total_copied = 0

    print(f"Scanning: {source_dir}")
    print(f"Destination (flat): {dest_dir}")
    print("----")

    for file in source_dir.rglob("*"):
        if not file.is_file():
            continue

        if not is_jpeg(file):
            continue

        total_found += 1

        try:
            dest_file = get_unique_filename(dest_dir, file.name)
            shutil.copy2(file, dest_file)
            total_copied += 1

            if total_copied % 100 == 0:
                print(f"Copied {total_copied} files...")

        except Exception as e:
            print(f"ERROR copying {file}: {e}")

    print(f"Total JPEGs found: {total_found}")
    print(f"Total copied: {total_copied}")
    print()
    return total_found, total_copied


def process_trash(dest_root: Path) -> tuple[int, int]:
    trash_source = Path(TRASH_SOURCE_DIR)
    trash_dest = dest_root / "trash"
    return copy_flattened(trash_source, trash_dest)


def process_stash(dest_root: Path) -> tuple[int, int]:
    stash_source = Path(STASH_SOURCE_DIR)

    if not stash_source.exists():
        print(f"WARNING: Stash source directory does not exist: {stash_source}")
        return 0, 0

    total_found = 0
    total_copied = 0

    subdirs = sorted([p for p in stash_source.iterdir() if p.is_dir()])

    if not subdirs:
        print(f"No subdirectories found under: {stash_source}")
        print()
        return 0, 0

    for subdir in subdirs:
        dest_dir = dest_root / subdir.name
        found, copied = copy_flattened(subdir, dest_dir)
        total_found += found
        total_copied += copied

    return total_found, total_copied


def main():
    dest_root = Path(DEST_ROOT)
    dest_root.mkdir(parents=True, exist_ok=True)

    print("========================================")
    print("Flatten-copy utility for LifeArchive")
    print("========================================")
    print(f"Trash source: {TRASH_SOURCE_DIR}")
    print(f"Stash source: {STASH_SOURCE_DIR}")
    print(f"Destination root: {DEST_ROOT}")
    print()

    trash_found, trash_copied = process_trash(dest_root)
    stash_found, stash_copied = process_stash(dest_root)

    print("========================================")
    print("Summary")
    print("========================================")
    print(f"Trash JPEGs found:   {trash_found}")
    print(f"Trash JPEGs copied:  {trash_copied}")
    print(f"Stash JPEGs found:   {stash_found}")
    print(f"Stash JPEGs copied:  {stash_copied}")
    print(f"Grand total found:   {trash_found + stash_found}")
    print(f"Grand total copied:  {trash_copied + stash_copied}")
    print("Done.")


if __name__ == "__main__":
    main()
