#!/usr/bin/env python3
"""
Seed a local raster tile cache for the Life Archive Places map.

This script downloads z/x/y PNG map tiles into:
    <archive_root>/_web_layout/map_tiles/{z}/{x}/{y}.png

It is intentionally conservative:
- skips tiles already present
- rate limits requests
- retries gently
- writes a descriptive User-Agent
- is meant for bounded regions and testing, not bulk planet mirroring

Default tile source:
    https://tile.openstreetmap.org/{z}/{x}/{y}.png

Edit the REGION_PRESETS and SEED_PLAN sections below to control coverage.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TILE_URL_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "LifeArchiveTileSeeder/1.0 (personal local archive map cache)"
REQUEST_DELAY_SECONDS = 0.25
MAX_RETRIES = 3
TIMEOUT_SECONDS = 20.0

# Bounding boxes: (min_lon, min_lat, max_lon, max_lat)
REGION_PRESETS: dict[str, tuple[float, float, float, float]] = {
    "world": (-180.0, -85.0, 180.0, 85.0),
    "usa": (-125.0, 24.0, -66.0, 50.0),
    "hawaii": (-161.0, 18.5, -154.0, 22.8),
    "europe": (-12.0, 34.0, 32.0, 72.0),
    "italy": (6.0, 36.0, 19.5, 47.5),
    "france": (-5.8, 41.0, 9.8, 51.5),
    "spain": (-10.5, 35.0, 4.8, 44.5),
    "japan": (129.0, 30.0, 146.5, 46.5),
    "missouri": (-95.9, 35.9, -89.0, 40.8),
    "ohio": (-84.9, 38.2, -80.3, 42.3),
}

# Conservative default seed plan for your current travel-heavy archive.
# Format: (preset_name, min_zoom, max_zoom)
SEED_PLAN: list[tuple[str, int, int]] = [
    ("world", 0, 2),
    ("usa", 3, 5),
    ("europe", 3, 5),
    ("japan", 3, 5),
    ("hawaii", 6, 8),
    ("italy", 6, 8),
    ("france", 6, 8),
    ("spain", 6, 8),
    ("missouri", 6, 8),
    ("ohio", 6, 8),
]


def lon_to_tile_x(lon: float, z: int) -> int:
    n = 2 ** z
    return int((lon + 180.0) / 360.0 * n)


def lat_to_tile_y(lat: float, z: int) -> int:
    lat = max(min(lat, 85.05112878), -85.05112878)
    lat_rad = math.radians(lat)
    n = 2 ** z
    return int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)


def tile_range_for_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float, z: int) -> tuple[range, range]:
    n = 2 ** z
    x0 = max(0, min(n - 1, lon_to_tile_x(min_lon, z)))
    x1 = max(0, min(n - 1, lon_to_tile_x(max_lon, z)))
    y0 = max(0, min(n - 1, lat_to_tile_y(max_lat, z)))  # north edge
    y1 = max(0, min(n - 1, lat_to_tile_y(min_lat, z)))  # south edge

    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0

    return range(x0, x1 + 1), range(y0, y1 + 1)


def iter_seed_tiles(plan: Iterable[tuple[str, int, int]]) -> Iterable[tuple[int, int, int]]:
    seen: set[tuple[int, int, int]] = set()
    for preset_name, zmin, zmax in plan:
        if preset_name not in REGION_PRESETS:
            raise KeyError(f"Unknown region preset: {preset_name}")
        bbox = REGION_PRESETS[preset_name]
        for z in range(zmin, zmax + 1):
            xr, yr = tile_range_for_bbox(*bbox, z)
            for x in xr:
                for y in yr:
                    tile = (z, x, y)
                    if tile not in seen:
                        seen.add(tile)
                        yield tile


def download_tile(url: str, dest: Path) -> tuple[bool, str]:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                data = resp.read()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return True, "downloaded"
        except HTTPError as exc:
            if exc.code == 404:
                return False, "missing"
            if attempt == MAX_RETRIES:
                return False, f"http {exc.code}"
            time.sleep(REQUEST_DELAY_SECONDS * attempt)
        except URLError as exc:
            if attempt == MAX_RETRIES:
                return False, f"url error: {exc.reason}"
            time.sleep(REQUEST_DELAY_SECONDS * attempt)
        except Exception as exc:
            if attempt == MAX_RETRIES:
                return False, f"error: {exc}"
            time.sleep(REQUEST_DELAY_SECONDS * attempt)
    return False, "unknown"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed local map tiles for Life Archive")
    parser.add_argument(
        "--archive-root",
        default=r"C:\website-photos",
        help="Archive root containing _web_layout",
    )
    parser.add_argument(
        "--list-presets",
        action="store_true",
        help="List built-in region presets and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be downloaded without downloading",
    )
    parser.add_argument(
        "--max-tiles",
        type=int,
        default=0,
        help="Optional safety cap. 0 means no cap.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_presets:
        print("Region presets:")
        for name, bbox in REGION_PRESETS.items():
            print(f"  {name:10s} -> {bbox}")
        return 0

    archive_root = Path(args.archive_root)
    tile_root = archive_root / "_web_layout" / "map_tiles"
    tile_root.mkdir(parents=True, exist_ok=True)

    tiles = list(iter_seed_tiles(SEED_PLAN))
    if args.max_tiles > 0:
        tiles = tiles[: args.max_tiles]

    total = len(tiles)
    print(f"[plan] {total} tile(s) to consider")
    print(f"[dest] {tile_root}")

    downloaded = 0
    skipped = 0
    failed = 0

    for idx, (z, x, y) in enumerate(tiles, start=1):
        dest = tile_root / str(z) / str(x) / f"{y}.png"
        if dest.exists():
            skipped += 1
            if idx % 50 == 0 or idx == total:
                print(f"[{idx}/{total}] skip existing z={z} x={x} y={y}")
            continue

        url = TILE_URL_TEMPLATE.format(z=z, x=x, y=y)

        if args.dry_run:
            print(f"[{idx}/{total}] would fetch {url}")
            continue

        ok, detail = download_tile(url, dest)
        if ok:
            downloaded += 1
            print(f"[{idx}/{total}] ok   z={z} x={x} y={y}")
        else:
            failed += 1
            print(f"[{idx}/{total}] fail z={z} x={x} y={y} -> {detail}")

        time.sleep(REQUEST_DELAY_SECONDS)

    print()
    print("Done.")
    print(f"  downloaded: {downloaded}")
    print(f"  skipped:    {skipped}")
    print(f"  failed:     {failed}")
    print(f"  tile root:  {tile_root}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
