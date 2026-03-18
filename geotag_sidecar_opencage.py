#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EPSILON_GPS = 0.000001
DEFAULT_ARCHIVE_ROOT = r"C:\website-photos"
DEFAULT_LOOKUP_LIMIT = 10  # conservative first-run safety limit
DEFAULT_ROUND_DECIMALS = 3
DEFAULT_REQUEST_DELAY_SECONDS = 1.1
OPENCAGE_URL = "https://api.opencagedata.com/geocode/v1/json"
OPENCAGE_API_KEY_LENGTH = 32


@dataclass(frozen=True)
class Config:
    archive_root: Path
    archive_db: Path
    geo_db: Path
    api_key_file: Path
    round_decimals: int
    request_delay_seconds: float
    max_live_lookups: int
    verbose: bool


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="Reverse-geocode GPS photos using OpenCage with aggressive local caching.")
    parser.add_argument("--archive-root", default=DEFAULT_ARCHIVE_ROOT)
    parser.add_argument("--archive-db", default=None, help="Path to archive_index.db (defaults to <archive-root>/archive_index.db)")
    parser.add_argument("--geo-db", default=None, help="Path to geo_tags.sqlite (defaults to <archive-root>/geo_tags.sqlite)")
    parser.add_argument("--api-key-file", default=None, help="Path to opencage.txt (defaults to <archive-root>/opencage.txt)")
    parser.add_argument("--round-decimals", type=int, default=DEFAULT_ROUND_DECIMALS)
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument(
        "--max-live-lookups",
        type=int,
        default=DEFAULT_LOOKUP_LIMIT,
        help="Maximum number of live API lookups this run. Default is intentionally conservative for first-run safety.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    archive_root = Path(args.archive_root)
    archive_db = Path(args.archive_db) if args.archive_db else archive_root / "archive_index.db"
    geo_db = Path(args.geo_db) if args.geo_db else archive_root / "geo_tags.sqlite"
    api_key_file = Path(args.api_key_file) if args.api_key_file else archive_root / "opencage.txt"

    return Config(
        archive_root=archive_root,
        archive_db=archive_db,
        geo_db=geo_db,
        api_key_file=api_key_file,
        round_decimals=args.round_decimals,
        request_delay_seconds=args.request_delay,
        max_live_lookups=max(0, int(args.max_live_lookups)),
        verbose=bool(args.verbose),
    )


def read_api_key(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"API key file not found: {path}")

    raw = path.read_text(encoding="utf-8-sig")
    lines = [line.strip() for line in raw.splitlines()]

    key = ""
    for line in lines:
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" in line:
            _, value = line.split("=", 1)
            line = value.strip()
        key = line.strip().strip('"').strip("'")
        break

    if not key:
        raise ValueError(f"API key file is empty or does not contain a usable key: {path}")
    return key


def open_archive_conn(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"Archive DB not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def open_geo_conn(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geo_cache (
            coord_key TEXT PRIMARY KEY,
            lat_rounded REAL NOT NULL,
            lon_rounded REAL NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_at TEXT,
            looked_up_at TEXT,
            updated_at TEXT,
            http_status INTEGER,
            confidence INTEGER,
            formatted TEXT,
            result_type TEXT,
            country TEXT,
            country_code TEXT,
            state TEXT,
            state_code TEXT,
            county TEXT,
            city TEXT,
            town TEXT,
            village TEXT,
            hamlet TEXT,
            suburb TEXT,
            postcode TEXT,
            road TEXT,
            house_number TEXT,
            place_name TEXT,
            raw_json TEXT NOT NULL,
            components_json TEXT,
            annotations_json TEXT,
            results_count INTEGER,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_geo (
            sha1 TEXT PRIMARY KEY,
            coord_key TEXT NOT NULL,
            source_lat REAL,
            source_lon REAL,
            linked_at TEXT NOT NULL,
            FOREIGN KEY (coord_key) REFERENCES geo_cache(coord_key)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_geo_coord_key ON photo_geo(coord_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_geo_cache_status ON geo_cache(status)")
    conn.commit()
    return conn


def has_valid_gps(row: sqlite3.Row) -> bool:
    try:
        lat = float(row["latitude"])
        lon = float(row["longitude"])
        return abs(lat) > EPSILON_GPS or abs(lon) > EPSILON_GPS
    except Exception:
        return False


def rounded_coord_key(lat: float, lon: float, decimals: int) -> tuple[str, float, float]:
    lat_r = round(lat, decimals)
    lon_r = round(lon, decimals)
    return (f"{lat_r:.{decimals}f},{lon_r:.{decimals}f}", lat_r, lon_r)


def _sqlite_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        if not value:
            return ""
        if all(isinstance(x, (str, int, float, bool)) or x is None for x in value):
            return ", ".join("" if x is None else str(x) for x in value)
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def load_photos_with_gps(archive_conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = archive_conn.execute(
        """
        SELECT sha1, rel_fqn, original_filename, final_dt, latitude, longitude
        FROM media
        WHERE is_deleted = 0
        ORDER BY final_dt, sha1
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not has_valid_gps(row):
            continue
        out.append(dict(row))
    return out


def group_by_coord(photos: list[dict[str, Any]], decimals: int) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for photo in photos:
        lat = float(photo["latitude"])
        lon = float(photo["longitude"])
        coord_key, lat_r, lon_r = rounded_coord_key(lat, lon, decimals)
        entry = grouped.setdefault(
            coord_key,
            {
                "coord_key": coord_key,
                "lat_rounded": lat_r,
                "lon_rounded": lon_r,
                "photos": [],
            },
        )
        entry["photos"].append(photo)
    return grouped


def cache_row_exists(geo_conn: sqlite3.Connection, coord_key: str) -> bool:
    row = geo_conn.execute("SELECT 1 FROM geo_cache WHERE coord_key = ?", (coord_key,)).fetchone()
    return row is not None


def upsert_photo_mapping(geo_conn: sqlite3.Connection, sha1: str, coord_key: str, source_lat: float, source_lon: float) -> None:
    geo_conn.execute(
        """
        INSERT INTO photo_geo (sha1, coord_key, source_lat, source_lon, linked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(sha1) DO UPDATE SET
            coord_key=excluded.coord_key,
            source_lat=excluded.source_lat,
            source_lon=excluded.source_lon,
            linked_at=excluded.linked_at
        """,
        (sha1, coord_key, source_lat, source_lon, utc_now_iso()),
    )


def parse_best_result(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results") or []
    best = results[0] if results else {}
    components = best.get("components") or {}
    annotations = best.get("annotations") or {}

    return {
        "confidence": _sqlite_scalar(best.get("confidence")),
        "formatted": _sqlite_scalar(best.get("formatted") or ""),
        "result_type": _sqlite_scalar(best.get("_type") or best.get("type") or ""),
        "country": _sqlite_scalar(components.get("country") or ""),
        "country_code": _sqlite_scalar(components.get("country_code") or ""),
        "state": _sqlite_scalar(components.get("state") or ""),
        "state_code": _sqlite_scalar(components.get("state_code") or components.get("ISO_3166-2") or ""),
        "county": _sqlite_scalar(components.get("county") or ""),
        "city": _sqlite_scalar(components.get("city") or components.get("city_district") or ""),
        "town": _sqlite_scalar(components.get("town") or ""),
        "village": _sqlite_scalar(components.get("village") or ""),
        "hamlet": _sqlite_scalar(components.get("hamlet") or ""),
        "suburb": _sqlite_scalar(components.get("suburb") or components.get("neighbourhood") or ""),
        "postcode": _sqlite_scalar(components.get("postcode") or ""),
        "road": _sqlite_scalar(components.get("road") or ""),
        "house_number": _sqlite_scalar(components.get("house_number") or ""),
        "place_name": _sqlite_scalar(
            components.get("attraction")
            or components.get("building")
            or components.get("amenity")
            or components.get("park")
            or components.get("natural")
            or ""
        ),
        "components_json": json.dumps(components, ensure_ascii=False),
        "annotations_json": json.dumps(annotations, ensure_ascii=False),
        "results_count": len(results),
    }


def store_cache_success(
    geo_conn: sqlite3.Connection,
    coord_key: str,
    lat_rounded: float,
    lon_rounded: float,
    http_status: int,
    payload: dict[str, Any],
) -> None:
    parsed = parse_best_result(payload)
    now = utc_now_iso()
    geo_conn.execute(
        """
        INSERT INTO geo_cache (
            coord_key, lat_rounded, lon_rounded, provider, status,
            requested_at, looked_up_at, updated_at, http_status,
            confidence, formatted, result_type,
            country, country_code, state, state_code, county,
            city, town, village, hamlet, suburb, postcode, road,
            house_number, place_name, raw_json, components_json,
            annotations_json, results_count, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(coord_key) DO UPDATE SET
            lat_rounded=excluded.lat_rounded,
            lon_rounded=excluded.lon_rounded,
            provider=excluded.provider,
            status=excluded.status,
            requested_at=excluded.requested_at,
            looked_up_at=excluded.looked_up_at,
            updated_at=excluded.updated_at,
            http_status=excluded.http_status,
            confidence=excluded.confidence,
            formatted=excluded.formatted,
            result_type=excluded.result_type,
            country=excluded.country,
            country_code=excluded.country_code,
            state=excluded.state,
            state_code=excluded.state_code,
            county=excluded.county,
            city=excluded.city,
            town=excluded.town,
            village=excluded.village,
            hamlet=excluded.hamlet,
            suburb=excluded.suburb,
            postcode=excluded.postcode,
            road=excluded.road,
            house_number=excluded.house_number,
            place_name=excluded.place_name,
            raw_json=excluded.raw_json,
            components_json=excluded.components_json,
            annotations_json=excluded.annotations_json,
            results_count=excluded.results_count,
            error_message=excluded.error_message
        """,
        (
            coord_key, lat_rounded, lon_rounded, "OpenCage", "ok",
            now, now, now, http_status,
            parsed["confidence"], parsed["formatted"], parsed["result_type"],
            parsed["country"], parsed["country_code"], parsed["state"], parsed["state_code"], parsed["county"],
            parsed["city"], parsed["town"], parsed["village"], parsed["hamlet"], parsed["suburb"], parsed["postcode"], parsed["road"],
            parsed["house_number"], parsed["place_name"], json.dumps(payload, ensure_ascii=False), parsed["components_json"],
            parsed["annotations_json"], parsed["results_count"], None,
        ),
    )
    geo_conn.commit()


def store_cache_error(
    geo_conn: sqlite3.Connection,
    coord_key: str,
    lat_rounded: float,
    lon_rounded: float,
    http_status: int,
    payload: dict[str, Any] | None,
    error_message: str,
) -> None:
    now = utc_now_iso()
    geo_conn.execute(
        """
        INSERT INTO geo_cache (
            coord_key, lat_rounded, lon_rounded, provider, status,
            requested_at, looked_up_at, updated_at, http_status,
            raw_json, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(coord_key) DO UPDATE SET
            lat_rounded=excluded.lat_rounded,
            lon_rounded=excluded.lon_rounded,
            provider=excluded.provider,
            status=excluded.status,
            requested_at=excluded.requested_at,
            looked_up_at=excluded.looked_up_at,
            updated_at=excluded.updated_at,
            http_status=excluded.http_status,
            raw_json=excluded.raw_json,
            error_message=excluded.error_message
        """,
        (
            coord_key,
            lat_rounded,
            lon_rounded,
            "OpenCage",
            "error",
            now,
            now,
            now,
            http_status,
            json.dumps(payload, ensure_ascii=False) if payload is not None else "",
            error_message,
        ),
    )
    geo_conn.commit()


def opencage_reverse_geocode(api_key: str, lat: float, lon: float) -> tuple[int, dict[str, Any]]:
    params = {
        "q": f"{lat:.8f},{lon:.8f}",
        "key": api_key,
        "no_annotations": 0,
        "language": "en",
        "pretty": 0,
        "limit": 1,
    }
    query = urllib.parse.urlencode(params)
    url = f"{OPENCAGE_URL}?{query}"
    safe_params = dict(params)
    safe_params["key"] = "***REDACTED***"
    logging.debug("Request URL: %s?%s", OPENCAGE_URL, urllib.parse.urlencode(safe_params))
    req = urllib.request.Request(url, headers={"User-Agent": "life-archive-geotag-sidecar/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        status = resp.getcode()
        body = resp.read().decode("utf-8")
        return status, json.loads(body)


def maybe_sleep_after_lookup(delay_seconds: float) -> None:
    if delay_seconds <= 0:
        return
    time.sleep(delay_seconds)


def main() -> int:
    cfg = parse_args()
    setup_logging(cfg.verbose)

    logging.info("Archive root: %s", cfg.archive_root)
    logging.info("Archive DB:   %s", cfg.archive_db)
    logging.info("Geo DB:       %s", cfg.geo_db)
    logging.info("API key file: %s", cfg.api_key_file)
    logging.info("Rounded GPS precision: %d decimals", cfg.round_decimals)
    logging.info("Max live lookups this run: %d", cfg.max_live_lookups)

    api_key = read_api_key(cfg.api_key_file)
    logging.info("Loaded OpenCage API key from file (length=%d)", len(api_key))
    archive_conn = open_archive_conn(cfg.archive_db)
    geo_conn = open_geo_conn(cfg.geo_db)

    photos = load_photos_with_gps(archive_conn)
    grouped = group_by_coord(photos, cfg.round_decimals)
    total_unique_coords = len(grouped)
    logging.info("Photos with valid GPS: %d", len(photos))
    logging.info("Unique normalized coordinates: %d", total_unique_coords)

    cache_hits = 0
    live_lookups = 0
    mappings_written = 0

    try:
        for coord_idx, coord_entry in enumerate(sorted(grouped.values(), key=lambda x: x["coord_key"]), start=1):
            coord_key = coord_entry["coord_key"]
            lat_r = coord_entry["lat_rounded"]
            lon_r = coord_entry["lon_rounded"]
            photos_for_coord = coord_entry["photos"]

            if cache_row_exists(geo_conn, coord_key):
                cache_hits += 1
            else:
                if cfg.max_live_lookups and live_lookups >= cfg.max_live_lookups:
                    logging.warning(
                        "Reached max live lookup limit (%d). Stopping cleanly. Run again later.",
                        cfg.max_live_lookups,
                    )
                    break

                logging.info(
                    "[%d/%d] OpenCage lookup for %s (%d photo%s)",
                    coord_idx,
                    total_unique_coords,
                    coord_key,
                    len(photos_for_coord),
                    "" if len(photos_for_coord) == 1 else "s",
                )
                try:
                    status, payload = opencage_reverse_geocode(api_key, lat_r, lon_r)
                    if status != 200:
                        message = f"Unexpected HTTP status {status}"
                        store_cache_error(geo_conn, coord_key, lat_r, lon_r, status, payload, message)
                        logging.error("%s. Exiting cleanly.", message)
                        return 1

                    store_cache_success(geo_conn, coord_key, lat_r, lon_r, status, payload)
                    live_lookups += 1
                except urllib.error.HTTPError as exc:
                    body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                    try:
                        payload = json.loads(body_text) if body_text else {}
                    except Exception:
                        payload = {"raw_body": body_text}
                    store_cache_error(geo_conn, coord_key, lat_r, lon_r, exc.code, payload, str(exc))
                    logging.error("HTTP error %s for %s. Exiting cleanly.", exc.code, coord_key)
                    if body_text:
                        logging.error("OpenCage response body: %s", body_text)
                    return 1
                except Exception as exc:
                    store_cache_error(geo_conn, coord_key, lat_r, lon_r, 0, None, str(exc))
                    logging.error("Lookup failed for %s: %s. Exiting cleanly.", coord_key, exc)
                    return 1

                maybe_sleep_after_lookup(cfg.request_delay_seconds)

            for photo in photos_for_coord:
                upsert_photo_mapping(
                    geo_conn,
                    sha1=str(photo["sha1"]),
                    coord_key=coord_key,
                    source_lat=float(photo["latitude"]),
                    source_lon=float(photo["longitude"]),
                )
                mappings_written += 1
            geo_conn.commit()

        logging.info("Done.")
        logging.info("Cache hits this run: %d", cache_hits)
        logging.info("Live lookups this run: %d", live_lookups)
        logging.info("Photo geo mappings written: %d", mappings_written)
        logging.info("Geo sidecar DB preserved at: %s", cfg.geo_db)
        return 0
    finally:
        archive_conn.close()
        geo_conn.close()


if __name__ == "__main__":
    sys.exit(main())
