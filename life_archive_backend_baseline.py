"""
Life Archive baseline backend.

This script is intentionally scoped to replicate the CURRENT working backend,
not the aspirational design document. It is designed to work against the SQLite
archive database populated by the existing ingest-photos.py pipeline.

Baseline behavior reproduced here:
- Timeline views: decade -> year -> month -> lightbox/gallery
- Undated view grouped by parent folder
- Explorer view grouped by filesystem hierarchy
- Tags index and per-tag gallery view
- 4x4 composite cards cached on disk and in SQLite
- Thumbnail serving and full-media serving
- Lightbox with keyboard navigation
- Context menu with 90 degree rotation operations
- Current curation sidebar shell (filename + notes textarea only)

Not included yet:
- Maps
- Videos
- Multi-select
- Tag editing
- Date editing
- Delete operations
- AI/geography/face metadata panels
- Day calendar view
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

from flask import Flask, jsonify, render_template_string, request, send_file, send_from_directory
from PIL import Image, ImageOps


# ---------------------------------------------------------------------------
# Pillow safety / behavior
# ---------------------------------------------------------------------------
# The existing archive contains some very large scans and panoramas. The
# currently working backend disables the decompression bomb warning entirely.
Image.MAX_IMAGE_PIXELS = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
werkzeug_log = logging.getLogger("werkzeug")
werkzeug_log.setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Defaults / constants
# ---------------------------------------------------------------------------
DEFAULT_THEME_COLOR = "#bb86fc"
TAG_EXCLUSIONS = {
    "pictures",
    "photos",
    "photographs",
    "media",
    "images",
    "terrysbackup",
    "topaz-undated",
}

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{{ page_title }} | Archive</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap" rel="stylesheet">
    <style>
        :root {
            --accent: {{ theme_color }};
            --bg: #0d0d0d;
            --card-bg: #1a1a1a;
        }
        * { box-sizing: border-box; }
        body {
            font-family: 'Inter', sans-serif;
            background: var(--bg);
            color: #fff;
            margin: 0;
            overflow-x: hidden;
        }
        .nav-bar {
            background: rgba(10, 10, 10, 0.9);
            backdrop-filter: blur(15px);
            padding: 15px 40px;
            border-bottom: 1px solid #333;
            display: flex;
            gap: 30px;
            position: sticky;
            top: 0;
            z-index: 1000;
        }
        .nav-bar a {
            color: #888;
            text-decoration: none;
            font-weight: 700;
            font-size: 0.8em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .nav-bar a.active {
            color: var(--accent);
            border-bottom: 2px solid var(--accent);
            padding-bottom: 5px;
        }
        .hero-banner {
            height: 200px;
            width: 100%;
            overflow: hidden;
            position: relative;
            border-bottom: 1px solid #333;
        }
        .hero-banner img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            opacity: 0.4;
        }
        .content {
            padding: 40px;
            max-width: 1600px;
            margin: 0 auto;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 30px;
        }
        .photo-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 12px;
        }
        .card {
            background: var(--card-bg);
            border-radius: 12px;
            border: 1px solid #333;
            overflow: hidden;
            cursor: pointer;
            transition: 0.2s;
            position: relative;
            display: flex;
            flex-direction: column;
        }
        .card:hover {
            border-color: var(--accent);
            transform: translateY(-5px);
        }
        .hero-preview {
            width: 100%;
            aspect-ratio: 1 / 1;
            background: #000;
            overflow: hidden;
        }
        .hero-preview img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        .tag-container {
            padding: 10px 15px 15px;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }
        .tag-pill {
            font-size: 0.65em;
            background: #333;
            color: #aaa;
            padding: 4px 10px;
            border-radius: 4px;
            font-weight: 800;
            text-transform: uppercase;
            text-decoration: none;
            border: 1px solid transparent;
        }
        .tag-pill:hover {
            background: var(--accent);
            color: #000;
            border-color: #fff;
        }
        .breadcrumb {
            font-weight: 800;
            color: #666;
            margin-bottom: 30px;
            text-transform: uppercase;
            font-size: 0.8em;
        }
        .breadcrumb a {
            color: var(--accent);
            text-decoration: none;
        }
        #lightbox {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.98);
            z-index: 9999;
            align-items: center;
            justify-content: center;
            user-select: none;
        }
        #lightbox.active { display: flex; }
        #lb-img {
            max-width: 85%;
            max-height: 85vh;
            object-fit: contain;
            box-shadow: 0 0 80px rgba(0, 0, 0, 0.8);
        }
        .lb-close {
            position: absolute;
            top: 20px;
            right: 30px;
            font-size: 40px;
            color: #fff;
            cursor: pointer;
            z-index: 10007;
            opacity: 0.5;
            transition: 0.2s;
        }
        .lb-close:hover {
            opacity: 1;
            color: var(--accent);
            transform: scale(1.1);
        }
        .lb-nav {
            position: absolute;
            top: 0;
            bottom: 0;
            width: 12%;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: 0.3s;
            font-size: 3em;
            color: rgba(255, 255, 255, 0.1);
            z-index: 10002;
        }
        .lb-nav:hover {
            background: rgba(255, 255, 255, 0.05);
            color: var(--accent);
        }
        #lb-prev { left: 0; }
        #lb-next { right: 0; }
        #lb-sidebar {
            position: fixed;
            top: 0;
            right: -450px;
            width: 400px;
            height: 100vh;
            background: #111;
            border-left: 1px solid #333;
            padding: 60px 30px;
            transition: 0.4s cubic-bezier(0.16, 1, 0.3, 1);
            z-index: 10005;
            box-shadow: -20px 0 50px rgba(0, 0, 0, 0.5);
            overflow-y: auto;
        }
        #lb-sidebar.visible { right: 0; }
        #context-menu {
            display: none;
            position: fixed;
            background: #222;
            border: 1px solid #444;
            z-index: 100000;
            width: 220px;
            border-radius: 8px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
        }
        .menu-item {
            padding: 12px 20px;
            cursor: pointer;
            font-size: 0.9em;
            font-weight: 700;
            transition: 0.1s;
        }
        .menu-item:hover {
            background: var(--accent);
            color: #000;
        }
    </style>
</head>
<body>
    <div id="context-menu">
        <div class="menu-item" onclick="rotateImage(90)">Rotate 90° Clockwise ↻</div>
        <div class="menu-item" onclick="rotateImage(270)">Rotate 90° Counter ↺</div>
    </div>

    <div class="nav-bar">
        <a href="/timeline" class="{{ 'active' if active_tab == 'timeline' else '' }}">Timeline</a>
        <a href="/undated" class="{{ 'active' if active_tab == 'undated' else '' }}">Undated</a>
        <a href="/folder" class="{{ 'active' if active_tab == 'file' else '' }}">Explorer</a>
        <a href="/tags" class="{{ 'active' if active_tab == 'tags' else '' }}">Tags</a>
    </div>

    <div class="hero-banner">
        <img src="/assets/{{ banner_img }}" onerror="this.src='/assets/hero-timeline.png'">
    </div>

    <div class="content">
        <h1>{{ page_title }}</h1>
        <div class="breadcrumb">{{ breadcrumb | safe }}</div>

        {% if cards %}
        <div class="grid" style="margin-bottom: 50px;">
            {% for c in cards %}
            <div class="card" onclick="handleGridClick(event, '{{ c.id }}')">
                <div class="hero-preview">
                    {% if c.comp_hash %}
                    <img src="/composite/{{ c.comp_hash }}.jpg" loading="lazy">
                    {% endif %}
                </div>
                <div style="padding:20px 20px 5px;">
                    <a href="{{ c.url }}" style="text-decoration:none; color:#fff;"><h3 style="margin:0;">{{ c.title }}</h3></a>
                    <div style="color:#666; font-size:0.85em; font-weight:700;">{{ c.subtitle }}</div>
                </div>
                <div class="tag-container">
                    {% for t in c.tags %}
                    <a href="/tags/{{ t }}" class="tag-pill">{{ t }}</a>
                    {% endfor %}
                </div>
            </div>
            {% endfor %}
        </div>
        {% endif %}

        {% if photos %}
        <div class="photo-grid">
            {% for p in photos %}
            <div class="card" oncontextmenu="handleCtx(event, '{{ p.sha1 }}')">
                <div class="hero-preview" onclick="openLB({{ loop.index0 }}, 'main_gallery')">
                    <img id="thumb-{{ p.sha1 }}" src="/thumbs/{{ p.sha1 }}.jpg" loading="lazy">
                </div>
                <div class="tag-container">
                    {% for t in p._tags_list[:3] %}
                    <a href="/tags/{{ t }}" class="tag-pill">{{ t }}</a>
                    {% endfor %}
                    {% if p._tags_list|length > 3 %}
                    <span class="tag-pill">+{{ p._tags_list|length - 3 }}</span>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        </div>
        {% endif %}
    </div>

    <div id="lightbox" onclick="if(event.target===this) closeLB()">
        <div class="lb-close" onclick="closeLB()">&times;</div>
        <div id="lb-prev" class="lb-nav" onclick="changeImg(-1)">&#10094;</div>
        <img id="lb-img" src="" oncontextmenu="handleCtxFromLightbox(event)">
        <div id="lb-next" class="lb-nav" onclick="changeImg(1)">&#10095;</div>

        <div style="position:absolute; bottom:30px; right:30px; background:#222; padding:12px 24px; border-radius:40px; cursor:pointer; font-weight:800; z-index:10006; border:1px solid #444; color:var(--accent);" onclick="toggleSidebar()">CURATE (E)</div>

        <div id="lb-sidebar" onclick="event.stopPropagation()">
            <h2 style="margin:0; color:var(--accent);">Curation</h2>
            <div id="meta-file" style="color:#888; font-size:0.8em; margin-top:20px; word-break:break-all; font-weight:700;"></div>
            <hr style="border:0; border-top:1px solid #333; margin:30px 0;">
            <label style="font-size:0.7em; color:#666; font-weight:900; letter-spacing:1px;">NOTES</label>
            <textarea id="input-notes" style="width:100%; background:#222; border:1px solid #444; color:#fff; padding:15px; margin-top:10px; border-radius:8px; resize:none;" rows="8" placeholder="Notes..."></textarea>
        </div>
    </div>

    <script>
        const manifests = {{ manifests | tojson | safe }};
        let curM = null;
        let curI = 0;
        let menuSha1 = '';

        function handleGridClick(e, key) {
            if (e.target.closest('a') || e.target.closest('.tag-pill')) return;
            const rect = e.currentTarget.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const cell = rect.width / 4;
            const idx = Math.floor(y / cell) * 4 + Math.floor(x / cell);
            openLB(idx, key);
        }

        function openLB(index, key) {
            if (!manifests[key] || manifests[key].length === 0) return;
            curM = key;
            curI = Math.min(index, manifests[key].length - 1);
            updateLB();
            document.getElementById('lightbox').classList.add('active');
        }

        function updateLB() {
            const item = manifests[curM][curI];
            const path = item.path.split('/').map(encodeURIComponent).join('/');
            document.getElementById('lb-img').src = '/media/' + path + '?t=' + Date.now();
            document.getElementById('meta-file').innerText = item.filename;
        }

        function changeImg(step) {
            if (!curM) return;
            curI = (curI + step + manifests[curM].length) % manifests[curM].length;
            updateLB();
        }

        function toggleSidebar() {
            document.getElementById('lb-sidebar').classList.toggle('visible');
        }

        function closeLB() {
            document.getElementById('lightbox').classList.remove('active');
            document.getElementById('lb-sidebar').classList.remove('visible');
        }

        function handleCtx(e, sha1) {
            e.preventDefault();
            menuSha1 = sha1;
            const menu = document.getElementById('context-menu');
            menu.style.display = 'block';
            menu.style.left = e.clientX + 'px';
            menu.style.top = e.clientY + 'px';
        }

        function handleCtxFromLightbox(e) {
            if (!curM) return;
            handleCtx(e, manifests[curM][curI].sha1);
        }

        function rotateImage(degrees) {
            fetch('/api/rotate/' + menuSha1, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ degrees })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'ok') {
                    const ts = Date.now();
                    if (document.getElementById('lightbox').classList.contains('active')) {
                        updateLB();
                    }
                    const thumb = document.getElementById('thumb-' + menuSha1);
                    if (thumb) thumb.src = '/thumbs/' + menuSha1 + '.jpg?t=' + ts;
                }
            });
        }

        window.onclick = () => {
            document.getElementById('context-menu').style.display = 'none';
        };

        document.onkeydown = (e) => {
            if (e.key === 'Escape') closeLB();
            if (document.getElementById('lightbox').classList.contains('active')) {
                if (e.key === 'ArrowRight') changeImg(1);
                if (e.key === 'ArrowLeft') changeImg(-1);
            }
            if (e.key.toLowerCase() === 'e') toggleSidebar();
        };
    </script>
</body>
</html>
"""


@dataclass(frozen=True)
class ArchiveConfig:
    archive_root: Path
    db_path: Path
    assets_dir: Path
    thumb_dir: Path
    composite_dir: Path
    theme_color: str = DEFAULT_THEME_COLOR


class ArchiveStore:
    def __init__(self, config: ArchiveConfig) -> None:
        self.config = config
        self.db_cache: list[dict[str, Any]] = []
        self.undated_cache: list[dict[str, Any]] = []
        self.global_tags: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.config.composite_dir.mkdir(parents=True, exist_ok=True)

    def is_excluded_tag(self, tag: str) -> bool:
        clean = tag.strip().lower()
        if clean in TAG_EXCLUSIONS:
            return True
        return bool(re.fullmatch(r"\d{4}", clean))

    def init_db_extensions(self) -> None:
        if not self.config.db_path.exists():
            return
        with sqlite3.connect(self.config.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS composite_cache (path_key TEXT PRIMARY KEY, sha1_list TEXT, composite_hash TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_dt ON media(final_dt)")

    def load_cache(self) -> None:
        if not self.config.db_path.exists():
            self.db_cache = []
            self.undated_cache = []
            self.global_tags = defaultdict(list)
            return

        self.init_db_extensions()

        with sqlite3.connect(self.config.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM media WHERE is_deleted = 0 ORDER BY final_dt DESC"
            ).fetchall()

        self.db_cache = []
        self.undated_cache = []
        self.global_tags = defaultdict(list)

        for row in rows:
            item = dict(row)
            dt_str = str(item["final_dt"])
            item["_web_path"] = str(item["rel_fqn"]).replace("\\", "/")

            raw_tags = f"{item.get('path_tags') or ''},{item.get('custom_tags') or ''}"
            normalized_tags = {
                t.strip().title()
                for t in raw_tags.split(",")
                if t.strip() and not self.is_excluded_tag(t)
            }
            item["_tags_list"] = sorted(normalized_tags)

            if dt_str.startswith("0000"):
                parts = item["_web_path"].split("/")
                item["_folder_group"] = parts[-2] if len(parts) > 1 else "Root"
                self.undated_cache.append(item)
            else:
                dt_obj = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                item["_year"] = dt_str[:4]
                item["_month"] = dt_obj.strftime("%m")
                item["_month_name"] = dt_obj.strftime("%B")
                item["_decade"] = dt_str[:3] + "0s"
                self.db_cache.append(item)

            for tag in item["_tags_list"]:
                self.global_tags[tag].append(item)

    @staticmethod
    def get_top_tags(items: Iterable[dict[str, Any]], limit: int = 3) -> list[str]:
        counts: Counter[str] = Counter()
        for item in items:
            counts.update(item.get("_tags_list", []))
        return [tag for tag, _ in counts.most_common(limit)]

    def get_composite_hash(self, path_key: str, media_list: list[dict[str, Any]]) -> str | None:
        if not media_list:
            return None

        with sqlite3.connect(self.config.db_path) as conn:
            cached = conn.execute(
                "SELECT composite_hash FROM composite_cache WHERE path_key=?",
                (path_key,),
            ).fetchone()
            if cached:
                candidate = self.config.composite_dir / f"{cached[0]}.jpg"
                if candidate.exists():
                    return str(cached[0])

        heroes = media_list[:16]
        sha1s = [str(hero["sha1"]) for hero in heroes]
        composite_hash = hashlib.md5("".join(sha1s).encode("utf-8")).hexdigest()
        composite_path = self.config.composite_dir / f"{composite_hash}.jpg"

        if not composite_path.exists():
            canvas = Image.new("RGB", (400, 400), (13, 13, 13))
            for idx, hero in enumerate(heroes):
                thumb_path = self.config.thumb_dir / f"{hero['sha1']}.jpg"
                media_path = self.config.archive_root / str(hero["rel_fqn"])
                source_path = thumb_path if thumb_path.exists() else media_path

                if not source_path.exists():
                    continue

                try:
                    with Image.open(source_path) as img:
                        img = ImageOps.exif_transpose(img)
                        img.thumbnail((100, 100))
                        width, height = img.size
                        x = (idx % 4) * 100 + (100 - width) // 2
                        y = (idx // 4) * 100 + (100 - height) // 2
                        canvas.paste(img, (x, y))
                except Exception:
                    continue

            canvas.save(composite_path, "JPEG", quality=85)

        with sqlite3.connect(self.config.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO composite_cache (path_key, sha1_list, composite_hash) VALUES (?, ?, ?)",
                (path_key, ",".join(sha1s), composite_hash),
            )
            conn.commit()

        return composite_hash

    @staticmethod
    def build_manifest(media_list: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "sha1": str(item["sha1"]),
                "path": str(item["_web_path"]),
                "filename": str(item["original_filename"]),
            }
            for item in media_list
        ]


def make_config(archive_root: str, theme_color: str = DEFAULT_THEME_COLOR) -> ArchiveConfig:
    root = Path(archive_root)
    return ArchiveConfig(
        archive_root=root,
        db_path=root / "archive_index.db",
        assets_dir=root / "_web_layout" / "assets",
        thumb_dir=root / "_thumbs",
        composite_dir=root / "_thumbs" / "_composites",
        theme_color=theme_color,
    )


def create_app(config: ArchiveConfig) -> Flask:
    app = Flask(__name__)
    store = ArchiveStore(config)

    def render_page(**kwargs: Any) -> str:
        return render_template_string(HTML_TEMPLATE, theme_color=config.theme_color, **kwargs)

    @app.route("/media/<path:relative_path>")
    def serve_media(relative_path: str):
        disk_path = config.archive_root / unquote(relative_path).replace("/", os.sep)
        disk_path = Path(os.path.normpath(str(disk_path)))
        if disk_path.exists():
            return send_file(disk_path)
        return "Not Found", 404

    @app.route("/api/rotate/<sha1>", methods=["POST"])
    def rotate_image(sha1: str):
        degrees = int((request.json or {}).get("degrees", 90))

        with sqlite3.connect(config.db_path) as conn:
            row = conn.execute("SELECT rel_fqn FROM media WHERE sha1=?", (sha1,)).fetchone()

        if not row:
            return jsonify({"status": "error"}), 404

        full_path = config.archive_root / str(row[0])
        if not full_path.exists():
            return jsonify({"status": "error", "message": "file missing"}), 404

        with Image.open(full_path) as img:
            exif = img.info.get("exif")
            method = Image.ROTATE_270 if degrees == 90 else Image.ROTATE_90
            rotated = img.transpose(method)
            rotated.save(full_path, "JPEG", exif=exif, quality=95)

        thumb_path = config.thumb_dir / f"{sha1}.jpg"
        with Image.open(full_path) as img:
            img.thumbnail((400, 400))
            img.convert("RGB").save(thumb_path, "JPEG")

        return jsonify({"status": "ok"})

    @app.route("/")
    @app.route("/timeline")
    def timeline():
        store.load_cache()
        decades = sorted({item["_decade"] for item in store.db_cache}, reverse=True)
        cards: list[dict[str, Any]] = []

        for decade in decades:
            items = [item for item in store.db_cache if item["_decade"] == decade]
            cards.append(
                {
                    "id": f"d_{decade}",
                    "title": decade,
                    "subtitle": f"{len(items)} items",
                    "url": f"/timeline/decade/{decade}",
                    "heroes": items,
                    "tags": store.get_top_tags(items),
                }
            )

        for card in cards:
            card["comp_hash"] = store.get_composite_hash(card["id"], card["heroes"])

        manifests = {card["id"]: store.build_manifest(card["heroes"]) for card in cards}
        return render_page(
            page_title="Timeline",
            active_tab="timeline",
            banner_img="hero-timeline.png",
            breadcrumb="Decades",
            cards=cards,
            manifests=manifests,
        )

    @app.route("/timeline/decade/<decade>")
    def timeline_decade(decade: str):
        store.load_cache()
        years = sorted({item["_year"] for item in store.db_cache if item["_decade"] == decade}, reverse=True)
        cards: list[dict[str, Any]] = []

        for year in years:
            items = [item for item in store.db_cache if item["_year"] == year]
            cards.append(
                {
                    "id": f"y_{year}",
                    "title": year,
                    "subtitle": f"{len(items)} items",
                    "url": f"/timeline/year/{year}",
                    "heroes": items,
                    "tags": store.get_top_tags(items),
                }
            )

        for card in cards:
            card["comp_hash"] = store.get_composite_hash(card["id"], card["heroes"])

        manifests = {card["id"]: store.build_manifest(card["heroes"]) for card in cards}
        return render_page(
            page_title=f"The {decade}",
            active_tab="timeline",
            banner_img="hero-timeline.png",
            breadcrumb=f"<a href='/timeline'>Timeline</a> / {decade}",
            cards=cards,
            manifests=manifests,
        )

    @app.route("/timeline/year/<year>")
    def timeline_year(year: str):
        store.load_cache()
        month_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in [i for i in store.db_cache if i["_year"] == year]:
            month_map[item["_month"]].append(item)

        cards: list[dict[str, Any]] = []
        for month_code in sorted(month_map.keys()):
            imgs = month_map[month_code]
            month_name = imgs[0]["_month_name"]
            cards.append(
                {
                    "id": f"m_{year}_{month_code}",
                    "title": month_name,
                    "subtitle": f"{len(imgs)} items",
                    "url": f"/timeline/month/{year}/{month_code}",
                    "heroes": imgs,
                    "tags": store.get_top_tags(imgs),
                }
            )

        for card in cards:
            card["comp_hash"] = store.get_composite_hash(card["id"], card["heroes"])

        manifests = {card["id"]: store.build_manifest(card["heroes"]) for card in cards}
        return render_page(
            page_title=year,
            active_tab="timeline",
            banner_img="hero-timeline.png",
            breadcrumb=f"<a href='/timeline'>Timeline</a> / <a href='/timeline/decade/{year[:3]}0s'>{year[:3]}0s</a> / {year}",
            cards=cards,
            manifests=manifests,
        )

    @app.route("/timeline/month/<year>/<month>")
    def timeline_month(year: str, month: str):
        store.load_cache()
        imgs = [item for item in store.db_cache if item["_year"] == year and item["_month"] == month]
        month_name = imgs[0]["_month_name"] if imgs else "Month"
        return render_page(
            page_title=f"{month_name} {year}",
            active_tab="timeline",
            banner_img="hero-timeline.png",
            breadcrumb=f"<a href='/timeline'>Timeline</a> / <a href='/timeline/decade/{year[:3]}0s'>{year[:3]}0s</a> / <a href='/timeline/year/{year}'>{year}</a> / {month_name}",
            photos=imgs,
            manifests={"main_gallery": store.build_manifest(imgs)},
        )

    @app.route("/undated")
    def undated():
        store.load_cache()
        folder_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in store.undated_cache:
            folder_map[item["_folder_group"]].append(item)

        cards = []
        for folder_name, imgs in sorted(folder_map.items()):
            cards.append(
                {
                    "id": f"u_{folder_name}",
                    "title": folder_name,
                    "subtitle": f"{len(imgs)} items",
                    "url": f"/undated/{folder_name}",
                    "heroes": imgs,
                    "tags": store.get_top_tags(imgs),
                }
            )

        for card in cards:
            card["comp_hash"] = store.get_composite_hash(card["id"], card["heroes"])

        manifests = {card["id"]: store.build_manifest(card["heroes"]) for card in cards}
        return render_page(
            page_title="Undated Archive",
            active_tab="undated",
            banner_img="hero-undated.png",
            breadcrumb="Undated",
            cards=cards,
            manifests=manifests,
        )

    @app.route("/undated/<folder>")
    def undated_folder(folder: str):
        store.load_cache()
        imgs = [item for item in store.undated_cache if item["_folder_group"] == folder]
        return render_page(
            page_title=folder,
            active_tab="undated",
            banner_img="hero-undated.png",
            breadcrumb=f"<a href='/undated'>Undated</a> / {folder}",
            photos=imgs,
            manifests={"main_gallery": store.build_manifest(imgs)},
        )

    @app.route("/folder")
    @app.route("/folder/<path:subpath>")
    def explorer(subpath: str = ""):
        store.load_cache()
        all_media = store.db_cache + store.undated_cache
        prefix = f"{subpath}/" if subpath else ""
        unique_subfolders: set[str] = set()
        direct_files: list[dict[str, Any]] = []

        for item in all_media:
            web_path = item["_web_path"]
            if not web_path.startswith(prefix):
                continue
            remainder = web_path[len(prefix):]
            if "/" in remainder:
                unique_subfolders.add(remainder.split("/")[0])
            else:
                direct_files.append(item)

        cards = []
        for folder_name in sorted(unique_subfolders):
            items = [
                item
                for item in all_media
                if item["_web_path"].startswith(prefix + folder_name + "/")
            ]
            cards.append(
                {
                    "id": f"f_{folder_name}",
                    "title": folder_name,
                    "subtitle": f"{len(items)} items",
                    "url": f"/folder/{prefix + folder_name}",
                    "heroes": items,
                    "tags": store.get_top_tags(items),
                }
            )

        for card in cards:
            card["comp_hash"] = store.get_composite_hash(card["id"], card["heroes"])

        manifests = {card["id"]: store.build_manifest(card["heroes"]) for card in cards}
        manifests["main_gallery"] = store.build_manifest(direct_files)

        crumb = "Root" if not subpath else f"<a href='/folder'>Root</a> / {subpath.replace('/', ' / ')}"
        return render_page(
            page_title="Explorer",
            active_tab="file",
            banner_img="hero-files.png",
            breadcrumb=crumb,
            cards=cards,
            photos=direct_files,
            manifests=manifests,
        )

    @app.route("/tags")
    @app.route("/tags/<tag>")
    def tags(tag: str | None = None):
        store.load_cache()

        if tag is None:
            cards = []
            for tag_name in sorted(store.global_tags.keys()):
                imgs = store.global_tags[tag_name]
                cards.append(
                    {
                        "id": f"t_{tag_name}",
                        "title": f"#{tag_name}",
                        "subtitle": f"{len(imgs)} items",
                        "url": f"/tags/{tag_name}",
                        "heroes": imgs,
                        "tags": [],
                    }
                )

            for card in cards:
                card["comp_hash"] = store.get_composite_hash(card["id"], card["heroes"])

            manifests = {card["id"]: store.build_manifest(card["heroes"]) for card in cards}
            return render_page(
                page_title="Tags",
                active_tab="tags",
                banner_img="hero-tags.png",
                breadcrumb="All Tags",
                cards=cards,
                manifests=manifests,
            )

        imgs = store.global_tags.get(tag, [])
        return render_page(
            page_title=f"#{tag}",
            active_tab="tags",
            banner_img="hero-tags.png",
            breadcrumb=f"<a href='/tags'>Tags</a> / {tag}",
            photos=imgs,
            manifests={"main_gallery": store.build_manifest(imgs)},
        )

    @app.route("/composite/<composite_name>.jpg")
    def serve_composite(composite_name: str):
        return send_from_directory(config.composite_dir, f"{composite_name}.jpg")

    @app.route("/thumbs/<filename>")
    def serve_thumb(filename: str):
        return send_from_directory(config.thumb_dir, filename)

    @app.route("/assets/<path:filename>")
    def serve_assets(filename: str):
        return send_from_directory(config.assets_dir, filename)

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Life Archive baseline backend")
    parser.add_argument(
        "--archive-root",
        default=os.environ.get("LIFE_ARCHIVE_ROOT", r"C:\website-test"),
        help="Archive root containing archive_index.db, _thumbs, and _web_layout/assets",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("LIFE_ARCHIVE_HOST", "0.0.0.0"),
        help="Flask bind host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LIFE_ARCHIVE_PORT", "5000")),
        help="Flask bind port",
    )
    parser.add_argument(
        "--theme-color",
        default=os.environ.get("LIFE_ARCHIVE_THEME", DEFAULT_THEME_COLOR),
        help="Accent color for UI",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = make_config(args.archive_root, theme_color=args.theme_color)
    app = create_app(config)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
