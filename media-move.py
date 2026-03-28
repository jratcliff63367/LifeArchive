#!/usr/bin/env python3

import os
import shutil
from pathlib import Path

# =========================
# CONFIG
# =========================
SOURCE_DIR = r"E:\media-move"

DEST_IMAGES = r"E:\media-move-images"
DEST_VIDEOS = r"E:\media-move-videos"

MOVE_IMAGES = False
MOVE_VIDEOS = True

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".webp", ".heic"
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"
}

# =========================
# HELPERS
# =========================

def classify_file(path: Path):
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return None


def get_unique_destination(dest_file: Path) -> Path:
    if not dest_file.exists():
        return dest_file

    counter = 1
    while True:
        candidate = dest_file.with_name(
            f"{dest_file.stem}_{counter}{dest_file.suffix}"
        )
        if not candidate.exists():
            return candidate
        counter += 1


# =========================
# MAIN
# =========================

def main():
    source = Path(SOURCE_DIR)
    dest_images = Path(DEST_IMAGES)
    dest_videos = Path(DEST_VIDEOS)

    if not source.exists():
        print(f"ERROR: Source directory does not exist: {source}")
        return

    if MOVE_IMAGES:
        dest_images.mkdir(parents=True, exist_ok=True)
    if MOVE_VIDEOS:
        dest_videos.mkdir(parents=True, exist_ok=True)

    total_found = 0
    moved_images = 0
    moved_videos = 0

    print(f"Scanning: {source}")
    print(f"Move images: {MOVE_IMAGES} → {dest_images}")
    print(f"Move videos: {MOVE_VIDEOS} → {dest_videos}")
    print("----")

    for root, _, files in os.walk(source):
        root_path = Path(root)

        for file in files:
            src_file = root_path / file
            file_type = classify_file(src_file)

            if file_type is None:
                continue

            total_found += 1

            # Determine destination
            if file_type == "image" and MOVE_IMAGES:
                dest_root = dest_images
            elif file_type == "video" and MOVE_VIDEOS:
                dest_root = dest_videos
            else:
                continue  # Skip based on config

            # Preserve relative path
            rel_path = src_file.relative_to(source)
            dest_file = dest_root / rel_path

            dest_file.parent.mkdir(parents=True, exist_ok=True)

            try:
                final_dest = get_unique_destination(dest_file)
                shutil.move(str(src_file), str(final_dest))

                if file_type == "image":
                    moved_images += 1
                else:
                    moved_videos += 1

                total_moved = moved_images + moved_videos
                if total_moved % 100 == 0:
                    print(f"Moved {total_moved} files...")

            except Exception as e:
                print(f"ERROR moving {src_file}: {e}")

    print("----")
    print(f"Total media files found: {total_found}")
    print(f"Images moved: {moved_images}")
    print(f"Videos moved: {moved_videos}")
    print("Done.")


if __name__ == "__main__":
    main()