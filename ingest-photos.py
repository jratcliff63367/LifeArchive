#!/usr/bin/env python3
"""
Life Archive ingestor / in-place media table rebuild.

Normal Life Archive ingestor:
- JPEG only (.jpg / .jpeg)
- explicit SOURCE_DIRECTORIES list
- GPS EXIF parsing fixed via explicit GPS IFD decoding
- stages files under ARCHIVE_ROOT before writing media rows
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import ExifTags, Image, ImageOps

# ============================================================================
# CONFIG
# ============================================================================

ARCHIVE_ROOT = Path(r"C:\website-photos")

# Explicit source roots to scan. These may be outside ARCHIVE_ROOT.
SOURCE_DIRECTORIES = [
    Path(r"C:\Photos from 2025"),
    # Path(r"C:\Photos from 2024"),
]

DB_PATH = ARCHIVE_ROOT / "archive_index.db"
THUMB_DIR = ARCHIVE_ROOT / "_thumbs"

ALLOWED_EXTENSIONS = {".jpg", ".jpeg"}

SKIP_DIR_NAMES = {
    "_thumbs",
    "_web_layout",
    "__pycache__",
    ".git",
    ".github",
}

DELETED_PREFIXES = ("_trash/", "_stash/")

THUMB_MAX_DIM = 640
THUMB_QUALITY = 85
WRITE_THUMBS = True

# Set this to a real file to test parsing on one image only.
INSPECT_FILE = ""

REBUILD_MEDIA_TABLE = True

# ============================================================================
# CONSTANTS
# ============================================================================

Image.MAX_IMAGE_PIXELS = None

TAGS = ExifTags.TAGS
GPSTAGS = ExifTags.GPSTAGS
DT_FORMAT = "%Y-%m-%d %H:%M:%S"

GPS_IFD_ENUM = getattr(getattr(ExifTags, "IFD", object()), "GPSInfo", 34853)

IMG_RE = re.compile(r"(?:IMG|PXL)[_-](?P<date>\d{8})[_-](?P<time>\d{6})", re.IGNORECASE)
PIXEL_RE = re.compile(r"(?:^|[_-])(?P<date>\d{8})[_-](?P<time>\d{6})")


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class ParsedMedia:
    sha1: str
    rel_fqn: str
    original_filename: str
    final_dt: str
    dt_source: str
    width: int
    height: int
    latitude: float | None
    longitude: float | None
    altitude_meters: float | None
    path_tags: str
    is_deleted: int
    extension: str
    file_size: int
    mtime_utc: str


# ============================================================================
# HELPERS
# ============================================================================

def utc_now_str() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_sha1(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def file_extension(path: Path) -> str:
    return path.suffix.lower()


def is_allowed_media_file(path: Path) -> bool:
    return path.is_file() and file_extension(path) in ALLOWED_EXTENSIONS


def iter_archive_files(source_dirs: Iterable[Path]) -> Iterable[tuple[Path, Path]]:
    seen: set[Path] = set()
    for root in source_dirs:
        root = Path(root)
        if not root.exists():
            print(f"[WARN] Source directory does not exist, skipping: {root}")
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            for filename in filenames:
                p = Path(dirpath) / filename
                if p in seen:
                    continue
                if is_allowed_media_file(p):
                    seen.add(p)
                    yield root, p


def rel_fqn_for(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("/", "\\")


def archive_dest_for(source_path: Path, source_root: Path, archive_root: Path) -> Path:
    """
    Map an arbitrary source file into the archive tree.

    Example:
        source_root = C:\Photos from 2025
        source_path = C:\Photos from 2025\foo\bar.jpg
        archive_root = C:\website-photos
    becomes:
        C:\website-photos\Photos from 2025\foo\bar.jpg
    """
    relative_inside_source = source_path.relative_to(source_root)
    top_level = source_root.name
    return archive_root / top_level / relative_inside_source


def ensure_archived_copy(source_path: Path, source_root: Path, archive_root: Path) -> Path:
    """
    Ensure the source JPEG exists under ARCHIVE_ROOT, because the rest of the
    system expects media.rel_fqn to resolve under that tree.
    """
    dest_path = archive_dest_for(source_path, source_root, archive_root)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if not dest_path.exists():
        dest_path.write_bytes(source_path.read_bytes())

    return dest_path


def is_deleted_rel(rel_fqn: str) -> int:
    normalized = rel_fqn.replace("\\", "/").lower()
    return 1 if normalized.startswith(DELETED_PREFIXES) else 0


def path_tags_for(rel_fqn: str) -> str:
    parts = [p for p in rel_fqn.replace("\\", "/").split("/")[:-1] if p]
    tags: list[str] = []
    for part in parts:
        low = part.lower()
        if low in {"_trash", "_stash", "_thumbs", "_web_layout"}:
            continue
        tags.append(part)

    seen: set[str] = set()
    ordered: list[str] = []
    for t in tags:
        if t not in seen:
            ordered.append(t)
            seen.add(t)
    return ", ".join(ordered)


def maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            den = float(value.denominator)
            if den == 0:
                return None
            return float(value.numerator) / den
        if isinstance(value, tuple) and len(value) == 2:
            den = float(value[1])
            if den == 0:
                return None
            return float(value[0]) / den
        return float(value)
    except Exception:
        return None


def dms_to_deg(values: Any, ref: Any) -> float | None:
    try:
        vals = list(values)
        if len(vals) != 3:
            return None
        deg = maybe_float(vals[0])
        mins = maybe_float(vals[1])
        secs = maybe_float(vals[2])
        if deg is None or mins is None or secs is None:
            return None
        out = deg + (mins / 60.0) + (secs / 3600.0)
        ref_s = str(ref).upper()
        if ref_s in {"S", "W"}:
            out = -out
        return out
    except Exception:
        return None


def exif_to_named_dict(exif: Any) -> dict[str, Any]:
    named: dict[str, Any] = {}
    if not exif:
        return named
    for key, value in exif.items():
        name = TAGS.get(key, key)
        named[name] = value
    return named


def decode_gps_ifd(exif: Any) -> tuple[dict[str, Any], float | None, float | None, float | None]:
    gps_named: dict[str, Any] = {}
    lat = lon = alt = None

    gps_ifd = None
    try:
        gps_ifd = exif.get_ifd(GPS_IFD_ENUM)
    except Exception:
        gps_ifd = None

    if not gps_ifd:
        try:
            raw = exif.get(34853)
            if isinstance(raw, dict):
                gps_ifd = raw
        except Exception:
            gps_ifd = None

    if isinstance(gps_ifd, dict) and gps_ifd:
        for gk, gv in gps_ifd.items():
            gps_named[GPSTAGS.get(gk, gk)] = gv

        lat = dms_to_deg(gps_named.get("GPSLatitude"), gps_named.get("GPSLatitudeRef"))
        lon = dms_to_deg(gps_named.get("GPSLongitude"), gps_named.get("GPSLongitudeRef"))
        alt = maybe_float(gps_named.get("GPSAltitude"))

        alt_ref = gps_named.get("GPSAltitudeRef")
        try:
            alt_ref_num = int(alt_ref) if alt_ref is not None else None
        except Exception:
            alt_ref_num = 1 if str(alt_ref) == "b'\\x01'" else 0

        if alt is not None and alt_ref_num == 1:
            alt = -alt

    return gps_named, lat, lon, alt


def dt_from_filename(filename: str) -> tuple[str | None, str | None]:
    stem = Path(filename).stem
    for rx in (IMG_RE, PIXEL_RE):
        m = rx.search(stem)
        if m:
            date_s = m.group("date")
            time_s = m.group("time")
            try:
                dt = datetime.strptime(date_s + time_s, "%Y%m%d%H%M%S")
                return dt.strftime(DT_FORMAT), f"Filename {rx.pattern}"
            except Exception:
                continue
    return None, None


def dt_from_mtime(path: Path) -> tuple[str, str]:
    dt = datetime.fromtimestamp(path.stat().st_mtime)
    return dt.strftime(DT_FORMAT), "Filesystem Modified"


def parse_exif_metadata(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "width": 0,
        "height": 0,
        "latitude": None,
        "longitude": None,
        "altitude_meters": None,
        "final_dt": "",
        "dt_source": "",
        "exif_tag_count": 0,
        "gps_present": False,
        "raw_exif_keys": [],
        "raw_gps_keys": [],
    }

    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        result["width"], result["height"] = img.size

        exif = img.getexif()
        result["exif_tag_count"] = len(exif) if exif else 0
        named = exif_to_named_dict(exif)
        result["raw_exif_keys"] = sorted(str(k) for k in named.keys())

        for key_name, source_name in (
            ("DateTimeOriginal", "EXIF DateTimeOriginal"),
            ("DateTimeDigitized", "EXIF DateTimeDigitized"),
            ("DateTime", "EXIF DateTime"),
        ):
            raw_dt = named.get(key_name)
            if raw_dt:
                try:
                    dt = datetime.strptime(str(raw_dt), "%Y:%m:%d %H:%M:%S")
                    result["final_dt"] = dt.strftime(DT_FORMAT)
                    result["dt_source"] = source_name
                    break
                except Exception:
                    pass

        gps_named, lat, lon, alt = decode_gps_ifd(exif)
        result["raw_gps_keys"] = sorted(str(k) for k in gps_named.keys())
        result["gps_present"] = bool(gps_named)
        result["latitude"] = lat
        result["longitude"] = lon
        result["altitude_meters"] = alt

    if not result["final_dt"]:
        dt_value, dt_source = dt_from_filename(path.name)
        if dt_value:
            result["final_dt"] = dt_value
            result["dt_source"] = dt_source

    if not result["final_dt"]:
        dt_value, dt_source = dt_from_mtime(path)
        result["final_dt"] = dt_value
        result["dt_source"] = dt_source

    return result


def ensure_thumb(parsed: ParsedMedia, source_path: Path) -> None:
    if not WRITE_THUMBS:
        return
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = THUMB_DIR / f"{parsed.sha1}.jpg"
    if thumb_path.exists():
        return
    try:
        with Image.open(source_path) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM))
            img.save(thumb_path, "JPEG", quality=THUMB_QUALITY)
    except Exception as exc:
        print(f"[WARN] thumb failed for {source_path}: {exc}")


def ensure_db_extensions(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS composite_cache (
            path_key TEXT PRIMARY KEY,
            sha1_list TEXT,
            composite_hash TEXT,
            candidate_hash TEXT DEFAULT '',
            selection_version TEXT DEFAULT ''
        )
    """)
    conn.commit()


def read_preserved_fields(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    try:
        rows = conn.execute("""
            SELECT sha1, custom_tags, custom_notes
            FROM media
        """).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for sha1, custom_tags, custom_notes in rows:
            out[str(sha1)] = {
                "custom_tags": custom_tags or "",
                "custom_notes": custom_notes or "",
            }
        return out
    except sqlite3.OperationalError:
        return {}


def rebuild_media_table(conn: sqlite3.Connection, rows: list[ParsedMedia]) -> None:
    preserved = read_preserved_fields(conn)

    conn.execute("DROP TABLE IF EXISTS media_new")
    conn.execute("""
        CREATE TABLE media_new (
            sha1 TEXT PRIMARY KEY,
            rel_fqn TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            final_dt TEXT NOT NULL,
            dt_source TEXT NOT NULL,
            width INTEGER DEFAULT 0,
            height INTEGER DEFAULT 0,
            latitude REAL,
            longitude REAL,
            altitude_meters REAL,
            path_tags TEXT DEFAULT '',
            custom_tags TEXT DEFAULT '',
            custom_notes TEXT DEFAULT '',
            is_deleted INTEGER DEFAULT 0,
            extension TEXT DEFAULT '',
            file_size INTEGER DEFAULT 0,
            mtime_utc TEXT DEFAULT ''
        )
    """)

    for row in rows:
        keep = preserved.get(row.sha1, {})
        conn.execute("""
            INSERT INTO media_new (
                sha1, rel_fqn, original_filename, final_dt, dt_source,
                width, height, latitude, longitude, altitude_meters,
                path_tags, custom_tags, custom_notes, is_deleted,
                extension, file_size, mtime_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row.sha1,
            row.rel_fqn,
            row.original_filename,
            row.final_dt,
            row.dt_source,
            row.width,
            row.height,
            row.latitude,
            row.longitude,
            row.altitude_meters,
            row.path_tags,
            keep.get("custom_tags", ""),
            keep.get("custom_notes", ""),
            row.is_deleted,
            row.extension,
            row.file_size,
            row.mtime_utc,
        ))

    conn.execute("DROP TABLE IF EXISTS media")
    conn.execute("ALTER TABLE media_new RENAME TO media")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_dt ON media(final_dt)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_rel ON media(rel_fqn)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_deleted ON media(is_deleted)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_lat_lon ON media(latitude, longitude)")
    conn.commit()


def inspect_file(path_str: str) -> int:
    path = Path(path_str)
    if not path.exists():
        print(f"[ERROR] File not found: {path}")
        return 1

    print("=" * 90)
    print(f"Inspecting: {path}")
    print("=" * 90)
    parsed = parse_exif_metadata(path)
    for k, v in parsed.items():
        print(f"{k}: {v}")
    return 0


def rebuild_ingest() -> int:
    if not ARCHIVE_ROOT.exists():
        print(f"[ERROR] ARCHIVE_ROOT not found: {ARCHIVE_ROOT}")
        return 1

    files = list(iter_archive_files(SOURCE_DIRECTORIES))
    skipped_non_jpeg = 0

    for source_root in SOURCE_DIRECTORIES:
        source_root = Path(source_root)
        if not source_root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(source_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            for filename in filenames:
                p = Path(dirpath) / filename
                if p.is_file() and file_extension(p) not in ALLOWED_EXTENSIONS:
                    skipped_non_jpeg += 1

    print(f"Archive root: {ARCHIVE_ROOT}")
    print("Source directories:")
    for source_root in SOURCE_DIRECTORIES:
        print(f"  - {source_root}")
    print(f"JPEG files to ingest: {len(files)}")
    print(f"Non-JPEG files skipped: {skipped_non_jpeg}")
    print(f"Started: {utc_now_str()}")

    rows: list[ParsedMedia] = []
    exif_gps_count = 0
    no_gps_count = 0
    exif_time_count = 0
    filename_time_count = 0
    fs_time_count = 0

    for idx, (source_root, source_path) in enumerate(files, start=1):
        try:
            archived_path = ensure_archived_copy(source_path, source_root, ARCHIVE_ROOT)
            meta = parse_exif_metadata(archived_path)

            if meta.get("latitude") is not None and meta.get("longitude") is not None:
                exif_gps_count += 1
            else:
                no_gps_count += 1

            dt_source = str(meta.get("dt_source") or "")
            if dt_source.startswith("EXIF"):
                exif_time_count += 1
            elif dt_source.startswith("Filename"):
                filename_time_count += 1
            else:
                fs_time_count += 1

            archived_rel = rel_fqn_for(archived_path, ARCHIVE_ROOT)
            stat = archived_path.stat()
            parsed = ParsedMedia(
                sha1=file_sha1(archived_path),
                rel_fqn=archived_rel,
                original_filename=archived_path.name,
                final_dt=str(meta.get("final_dt") or ""),
                dt_source=dt_source,
                width=int(meta.get("width") or 0),
                height=int(meta.get("height") or 0),
                latitude=meta.get("latitude"),
                longitude=meta.get("longitude"),
                altitude_meters=meta.get("altitude_meters"),
                path_tags=path_tags_for(archived_rel),
                is_deleted=is_deleted_rel(archived_rel),
                extension=file_extension(archived_path).upper().lstrip("."),
                file_size=int(stat.st_size),
                mtime_utc=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat(),
            )
            rows.append(parsed)
            ensure_thumb(parsed, archived_path)

            if idx % 500 == 0:
                print(f"[Progress] {idx}/{len(files)}")
        except Exception as exc:
            print(f"[WARN] Failed to ingest {source_path}: {exc}")

    print(f"Rows parsed: {len(rows)}")
    print(f"Images with GPS: {exif_gps_count}")
    print(f"Images without GPS: {no_gps_count}")
    print(f"Time from EXIF: {exif_time_count}")
    print(f"Time from filename: {filename_time_count}")
    print(f"Time from filesystem: {fs_time_count}")

    with sqlite3.connect(DB_PATH) as conn:
        ensure_db_extensions(conn)
        if REBUILD_MEDIA_TABLE:
            rebuild_media_table(conn, rows)
        else:
            print("[WARN] REBUILD_MEDIA_TABLE is False, nothing written.")

    print(f"Completed: {utc_now_str()}")
    return 0


def main() -> int:
    if INSPECT_FILE:
        return inspect_file(INSPECT_FILE)
    return rebuild_ingest()


if __name__ == "__main__":
    raise SystemExit(main())
