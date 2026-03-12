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
- Photo-grid multi-select with checkbox overlays, select all, batch rotate
- Move to Trash / Move to Stash
- Empty Trash
- Current curation sidebar shell (filename + notes textarea only)

Not included yet:
- Maps
- Videos
- Tag editing
- Date editing
- AI/geography/face metadata panels
- Day calendar view
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import logging
import os
import re
import shutil
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
        .photo-card.selected {
            border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(187, 134, 252, 0.35);
        }
        .photo-select-wrap {
            position: absolute;
            top: 10px;
            left: 10px;
            z-index: 20;
            width: 26px;
            height: 26px;
            border-radius: 50%;
            background: rgba(0, 0, 0, 0.55);
            border: 1px solid rgba(255,255,255,0.35);
            display: flex;
            align-items: center;
            justify-content: center;
            backdrop-filter: blur(6px);
        }
        .photo-select-checkbox {
            width: 16px;
            height: 16px;
            cursor: pointer;
            accent-color: var(--accent);
        }
        .selection-bar {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: rgba(20, 20, 20, 0.95);
            border: 1px solid #444;
            border-radius: 12px;
            padding: 12px 16px;
            z-index: 10004;
            display: none;
            gap: 12px;
            align-items: center;
            font-weight: 800;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45);
        }
        .selection-bar button {
            background: #222;
            color: #fff;
            border: 1px solid #555;
            border-radius: 8px;
            padding: 8px 12px;
            cursor: pointer;
            font-weight: 800;
        }
        .selection-bar button:hover {
            background: var(--accent);
            color: #000;
            border-color: var(--accent);
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

        .page-actions {
            display: flex;
            gap: 12px;
            margin: -10px 0 24px;
            flex-wrap: wrap;
        }
        .page-action {
            text-decoration: none;
            color: #fff;
            background: #1b1b1b;
            border: 1px solid #3a3a3a;
            border-radius: 10px;
            padding: 10px 14px;
            font-size: 0.85em;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            cursor: pointer;
            font-family: inherit;
        }
        .page-action.active, .page-action:hover {
            border-color: var(--accent);
            color: var(--accent);
        }
        .calendar-shell {
            display: grid;
            grid-template-columns: repeat(7, minmax(0, 1fr));
            border: 1px solid #2f2f2f;
            border-radius: 14px;
            overflow: hidden;
            background: rgba(255,255,255,0.02);
        }
        .calendar-dow {
            padding: 14px 10px;
            text-align: center;
            font-size: 0.8em;
            font-weight: 800;
            color: #aaa;
            background: rgba(255,255,255,0.03);
            border-right: 1px solid #242424;
            border-bottom: 1px solid #242424;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }
        .calendar-dow:nth-child(7) { border-right: 0; }
        .day-cell {
            min-height: 220px;
            border-right: 1px solid #242424;
            border-bottom: 1px solid #242424;
            position: relative;
            padding: 10px;
            background: rgba(255,255,255,0.01);
        }
        .day-cell:nth-child(7n) { border-right: 0; }
        .day-cell.empty {
            background: rgba(0,0,0,0.08);
        }
        .day-number {
            position: absolute;
            top: 10px;
            right: 10px;
            font-weight: 800;
            color: #aaa;
            font-size: 0.9em;
        }
        .day-link {
            display: block;
            text-decoration: none;
            color: inherit;
            height: 100%;
            padding-top: 22px;
        }
        .day-thumb-wrap {
            width: 100%;
            aspect-ratio: 16 / 10;
            overflow: hidden;
            border-radius: 10px;
            background: #000;
            margin-bottom: 10px;
            border: 1px solid #2f2f2f;
        }
        .day-thumb-wrap img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        .day-meta-title {
            font-weight: 800;
            font-size: 0.92em;
            margin-bottom: 6px;
            color: #fff;
        }
        .day-meta-sub {
            color: #9a9a9a;
            font-size: 0.82em;
            font-weight: 700;
            margin-bottom: 8px;
        }
        .day-tag-row {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }
        .day-location {
            display: inline-block;
            margin-bottom: 8px;
            color: var(--accent);
            font-size: 0.8em;
            font-weight: 800;
        }

        .lb-tab-row {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin: 18px 0 16px;
        }
        .lb-tab {
            background: #1b1b1b;
            color: #aaa;
            border: 1px solid #333;
            border-radius: 999px;
            padding: 7px 12px;
            font-size: 0.72em;
            font-weight: 900;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            cursor: pointer;
        }
        .lb-tab.active {
            background: var(--accent);
            color: #000;
            border-color: var(--accent);
        }
        .lb-section {
            display: none;
        }
        .lb-section.active {
            display: block;
        }
        .lb-kv {
            display: grid;
            grid-template-columns: 140px 1fr;
            gap: 8px 12px;
            align-items: start;
            font-size: 0.88em;
        }
        .lb-k {
            color: #8d8d8d;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            font-size: 0.78em;
        }
        .lb-v {
            color: #fff;
            word-break: break-word;
        }
        .lb-empty {
            color: #777;
            font-size: 0.9em;
            font-style: italic;
        }
        #lb-raw {
            width: 100%;
            min-height: 280px;
            background: #171717;
            border: 1px solid #333;
            color: #ddd;
            border-radius: 10px;
            padding: 14px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 12px;
            line-height: 1.45;
            white-space: pre-wrap;
        }

    </style>
</head>
<body>
    <div id="context-menu">
        <div class="menu-item" onclick="rotateImage(90)">Rotate 90° Clockwise ↻</div>
        <div class="menu-item" onclick="rotateImage(270)">Rotate 90° Counter ↺</div>
        <div class="menu-item" onclick="moveContextTo('trash')">Move to Trash</div>
        <div class="menu-item" onclick="moveContextTo('stash')">Move to Stash</div>
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

        {% if action_links %}
        <div class="page-actions">
            {% for action in action_links %}
                {% if action.onclick %}
                <button type="button" class="page-action {{ 'active' if action.active else '' }}" onclick="{{ action.onclick }}">{{ action.label }}</button>
                {% else %}
                <a href="{{ action.url }}" class="page-action {{ 'active' if action.active else '' }}">{{ action.label }}</a>
                {% endif %}
            {% endfor %}
        </div>
        {% endif %}

        {% if photos %}
        <div class="page-actions">
            <button type="button" class="page-action" onclick="selectAllVisible()">Select All</button>
            <button type="button" class="page-action" onclick="emptyTrash()">Empty Trash</button>
        </div>
        {% endif %}

        {% if day_calendar %}
        <div class="calendar-shell">
            {% for dow in day_calendar.headers %}
            <div class="calendar-dow">{{ dow }}</div>
            {% endfor %}
            {% for week in day_calendar.weeks %}
                {% for cell in week %}
                    {% if cell.day == 0 %}
                    <div class="day-cell empty"></div>
                    {% else %}
                    <div class="day-cell {{ '' if cell.has_items else 'empty' }}">
                        <div class="day-number">{{ cell.day }}</div>
                        {% if cell.has_items %}
                        <a class="day-link" href="{{ cell.url }}">
                            <div class="day-thumb-wrap">
                                <img src="/thumbs/{{ cell.thumb_sha1 }}.jpg" loading="lazy">
                            </div>
                            {% if cell.location_label %}<div class="day-location">{{ cell.location_label }}</div>{% endif %}
                            <div class="day-meta-title">{{ cell.count }} photo{{ '' if cell.count == 1 else 's' }}</div>
                            <div class="day-meta-sub">{{ cell.date_label }}</div>
                            <div class="day-tag-row">
                                {% for t in cell.tags %}<span class="tag-pill">{{ t }}</span>{% endfor %}
                            </div>
                        </a>
                        {% endif %}
                    </div>
                    {% endif %}
                {% endfor %}
            {% endfor %}
        </div>
        {% endif %}

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
            <div class="card photo-card" data-sha="{{ p.sha1 }}" oncontextmenu="handleCtx(event, '{{ p.sha1 }}')">
                <div class="photo-select-wrap">
                    <input type="checkbox" class="photo-select-checkbox" data-sha="{{ p.sha1 }}" aria-label="Select image">
                </div>
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

    <div id="selection-bar" class="selection-bar">
        <span id="selection-count">0 selected</span>
        <button type="button" onclick="selectAllVisible()">Select All</button>
        <button type="button" onclick="rotateSelection(90)">Rotate 90° Clockwise ↻</button>
        <button type="button" onclick="rotateSelection(270)">Rotate 90° Counter ↺</button>
        <button type="button" onclick="moveCurrentSelectionTo('trash')">Move to Trash</button>
        <button type="button" onclick="moveCurrentSelectionTo('stash')">Move to Stash</button>
    </div>

    <div id="lightbox" onclick="if(event.target===this) closeLB()">
        <div class="lb-close" onclick="closeLB()">&times;</div>
        <div id="lb-prev" class="lb-nav" onclick="changeImg(-1)">&#10094;</div>
        <img id="lb-img" src="" oncontextmenu="handleCtxFromLightbox(event)">
        <div id="lb-next" class="lb-nav" onclick="changeImg(1)">&#10095;</div>

        <div style="position:absolute; bottom:30px; right:30px; background:#222; padding:12px 24px; border-radius:40px; cursor:pointer; font-weight:800; z-index:10006; border:1px solid #444; color:var(--accent);" onclick="toggleSidebar()">CURATE (E)</div>

        <div id="lb-sidebar" onclick="event.stopPropagation()">
            <h2 style="margin:0; color:var(--accent);">Inspect</h2>
            <div id="meta-file" style="color:#888; font-size:0.8em; margin-top:20px; word-break:break-all; font-weight:700;"></div>
            <div id="lb-tab-row" class="lb-tab-row"></div>

            <div id="lb-section-overview" class="lb-section active">
                <div id="lb-overview" class="lb-kv"></div>
            </div>

            <div id="lb-section-technical" class="lb-section">
                <div id="lb-technical" class="lb-kv"></div>
            </div>

            <div id="lb-section-raw" class="lb-section">
                <pre id="lb-raw"></pre>
            </div>

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
        let selectedSha1s = new Set();
        let currentMeta = null;
        let currentTab = 'overview';

        function renderKV(containerId, rows) {
            const el = document.getElementById(containerId);
            if (!el) return;
            if (!rows || rows.length === 0) {
                el.innerHTML = '<div class="lb-empty">No data available.</div>';
                return;
            }
            el.innerHTML = rows.map(row =>
                `<div class="lb-k">${row[0]}</div><div class="lb-v">${row[1]}</div>`
            ).join('');
        }

        function setActiveMetaTab(tabName) {
            currentTab = tabName;
            document.querySelectorAll('#lb-tab-row .lb-tab').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.tab === tabName);
            });
            document.querySelectorAll('#lb-sidebar .lb-section').forEach(sec => {
                sec.classList.remove('active');
            });
            const target = document.getElementById('lb-section-' + tabName);
            if (target) target.classList.add('active');
        }

        function buildMetaTabs(availableTabs) {
            const row = document.getElementById('lb-tab-row');
            if (!row) return;
            row.innerHTML = '';
            for (const tabName of availableTabs) {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'lb-tab' + (tabName === currentTab ? ' active' : '');
                btn.dataset.tab = tabName;
                btn.textContent = tabName.charAt(0).toUpperCase() + tabName.slice(1);
                btn.onclick = () => setActiveMetaTab(tabName);
                row.appendChild(btn);
            }
            if (!availableTabs.includes(currentTab)) {
                currentTab = availableTabs[0] || 'overview';
            }
            setActiveMetaTab(currentTab);
        }

        async function loadLightboxMetadata() {
            if (!curM || !manifests[curM] || !manifests[curM][curI]) return;
            const item = manifests[curM][curI];
            try {
                const resp = await fetch('/api/lightbox_meta/' + encodeURIComponent(item.sha1));
                const data = await resp.json();
                currentMeta = data;
                const overview = data.overview || {};
                const technical = data.technical || {};
                const raw = data.raw || {};
                const overviewRows = [
                    ['Filename', overview.original_filename || item.filename || ''],
                    ['Path', overview.rel_fqn || item.path || ''],
                    ['Date', overview.final_dt || ''],
                    ['Date Source', overview.dt_source || ''],
                    ['Dimensions', overview.dimensions || ''],
                    ['Tags', overview.tags || ''],
                    ['Deleted', String(overview.is_deleted ?? '')],
                ].filter(row => row[1] !== '' && row[1] !== 'None');
                renderKV('lb-overview', overviewRows);

                const techRows = [
                    ['Technical Score', technical.technical_score || ''],
                    ['Sharpness', technical.sharpness || ''],
                    ['Contrast', technical.contrast || ''],
                    ['Brightness', technical.brightness || ''],
                    ['Edge Density', technical.edge_density || ''],
                    ['Resolution Score', technical.resolution_score || ''],
                    ['Model Version', technical.model_version || ''],
                    ['Scored At', technical.scored_at || ''],
                ].filter(row => row[1] !== '' && row[1] !== 'None');

                renderKV('lb-technical', techRows);
                const rawEl = document.getElementById('lb-raw');
                if (rawEl) rawEl.textContent = JSON.stringify(raw, null, 2);
                const availableTabs = ['overview'];
                if (techRows.length > 0) availableTabs.push('technical');
                availableTabs.push('raw');
                buildMetaTabs(availableTabs);
            } catch (err) {
                renderKV('lb-overview', [['Error', 'Failed to load metadata']]);
                renderKV('lb-technical', []);
                const rawEl = document.getElementById('lb-raw');
                if (rawEl) rawEl.textContent = String(err);
                buildMetaTabs(['overview', 'raw']);
            }
        }

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
            loadLightboxMetadata();
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

        function refreshSelectionUI() {
            document.querySelectorAll('.photo-card').forEach(card => {
                const sha1 = card.dataset.sha;
                const checked = selectedSha1s.has(sha1);
                card.classList.toggle('selected', checked);
                const cb = card.querySelector('.photo-select-checkbox');
                if (cb) cb.checked = checked;
            });
            const bar = document.getElementById('selection-bar');
            const count = selectedSha1s.size;
            if (bar) {
                if (count > 0) {
                    bar.style.display = 'flex';
                    document.getElementById('selection-count').innerText = `${count} selected`;
                } else {
                    bar.style.display = 'none';
                }
            }
        }

        function clearSelection() {
            if (selectedSha1s.size === 0) return;
            selectedSha1s.clear();
            refreshSelectionUI();
        }

        function toggleSelection(sha1) {
            if (selectedSha1s.has(sha1)) selectedSha1s.delete(sha1);
            else selectedSha1s.add(sha1);
            refreshSelectionUI();
        }

        function selectAllVisible() {
            document.querySelectorAll('.photo-card[data-sha]').forEach(card => {
                selectedSha1s.add(card.dataset.sha);
            });
            refreshSelectionUI();
        }

        function getContextTargets(clickedSha1) {
            if (selectedSha1s.size > 1 && selectedSha1s.has(clickedSha1)) {
                return Array.from(selectedSha1s);
            }
            return [clickedSha1];
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
            const targets = getContextTargets(menuSha1);
            if (targets.length > 1) {
                fetch('/api/rotate_batch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sha1_list: targets, degrees })
                })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'ok') {
                        location.reload();
                    }
                });
                return;
            }

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

        function rotateSelection(degrees) {
            if (selectedSha1s.size === 0) return;
            fetch('/api/rotate_batch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sha1_list: Array.from(selectedSha1s), degrees })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'ok') {
                    location.reload();
                }
            });
        }

        function moveSha1List(target, sha1List) {
            if (!sha1List || sha1List.length === 0) return;
            const endpoint = target === 'trash' ? '/api/move_to_trash' : '/api/move_to_stash';
            const label = target === 'trash' ? 'trash' : 'stash';

            fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sha1_list: sha1List })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'ok') {
                    location.reload();
                } else {
                    alert(data.message || `Failed to move image(s) to ${label}.`);
                }
            });
        }

        function moveCurrentSelectionTo(target) {
            moveSha1List(target, Array.from(selectedSha1s));
        }

        function moveContextTo(target) {
            const sha1List = getContextTargets(menuSha1);
            moveSha1List(target, sha1List);
        }

        function emptyTrash() {
            if (!confirm('Permanently delete all files currently in _trash? This cannot be undone.')) return;
            fetch('/api/empty_trash', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'ok') {
                    location.reload();
                } else {
                    alert(data.message || 'Failed to empty trash.');
                }
            });
        }

        window.onclick = (e) => {
            document.getElementById('context-menu').style.display = 'none';
        };

        document.onkeydown = (e) => {
            const targetTag = (e.target && e.target.tagName ? e.target.tagName.toLowerCase() : '');
            const isTextInput = targetTag === 'input' || targetTag === 'textarea';
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a' && !isTextInput && document.querySelectorAll('.photo-card[data-sha]').length > 0) {
                e.preventDefault();
                selectAllVisible();
                return;
            }
            if (e.key === 'Escape') {
                if (selectedSha1s.size > 0) {
                    clearSelection();
                    return;
                }
                closeLB();
            }
            if (document.getElementById('lightbox').classList.contains('active')) {
                if (e.key === 'ArrowRight') changeImg(1);
                if (e.key === 'ArrowLeft') changeImg(-1);
            }
            if (e.key.toLowerCase() === 'e') toggleSidebar();
        };

        document.addEventListener('DOMContentLoaded', () => {
            document.querySelectorAll('.photo-select-checkbox').forEach(cb => {
                cb.addEventListener('click', (e) => {
                    e.stopPropagation();
                    toggleSelection(cb.dataset.sha);
                });
            });

            document.querySelectorAll('.nav-bar a, .tag-pill, .breadcrumb a, .grid a').forEach(link => {
                link.addEventListener('click', () => clearSelection());
            });

            refreshSelectionUI();
        });
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
    technical_db_path: Path
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
                item["_day"] = dt_obj.strftime("%d")
                item["_day_int"] = int(dt_obj.strftime("%d"))
                item["_date_key"] = dt_obj.strftime("%Y-%m-%d")
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

    def get_day_location_label(self, items: Iterable[dict[str, Any]]) -> str | None:
        top_tags = self.get_top_tags(items, limit=3)
        return top_tags[0] if top_tags else None

    def build_month_day_calendar(self, year: str, month: str, month_items: list[dict[str, Any]]) -> dict[str, Any]:
        year_int = int(year)
        month_int = int(month)
        month_name = datetime(year_int, month_int, 1).strftime('%B')
        day_buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in month_items:
            day_buckets[int(item.get('_day_int', 0))].append(item)

        cal = calendar.Calendar(firstweekday=6)
        weeks = []
        for week in cal.monthdayscalendar(year_int, month_int):
            row = []
            for day in week:
                if day == 0:
                    row.append({'day': 0, 'has_items': False})
                    continue
                day_items = sorted(day_buckets.get(day, []), key=lambda x: x.get('final_dt', ''))
                if day_items:
                    top_tags = self.get_top_tags(day_items, limit=3)
                    row.append({
                        'day': day,
                        'has_items': True,
                        'url': f'/timeline/month/{year}/{month}/day/{day:02d}',
                        'thumb_sha1': day_items[0]['sha1'],
                        'count': len(day_items),
                        'tags': top_tags,
                        'location_label': self.get_day_location_label(day_items),
                        'date_label': f'{month_name} {day}, {year}',
                    })
                else:
                    row.append({'day': day, 'has_items': False})
            weeks.append(row)

        return {
            'headers': ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
            'weeks': weeks,
        }

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

    def get_lightbox_metadata(self, sha1: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "overview": {},
            "technical": {},
            "raw": {},
        }

        if not self.config.db_path.exists():
            return result

        with sqlite3.connect(self.config.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM media WHERE sha1=?", (sha1,)).fetchone()

        media_row = dict(row) if row else {}
        if media_row:
            path_tags = str(media_row.get("path_tags") or "")
            custom_tags = str(media_row.get("custom_tags") or "")
            raw_tags = f"{path_tags},{custom_tags}"
            tags = sorted({
                t.strip().title()
                for t in raw_tags.split(",")
                if t.strip() and not self.is_excluded_tag(t)
            })
            dimensions = ""
            full_path = self.config.archive_root / str(media_row.get("rel_fqn", "")).replace("\\", "/")
            try:
                if full_path.exists():
                    with Image.open(full_path) as img:
                        dimensions = f"{img.width} x {img.height}"
            except Exception:
                dimensions = ""

            result["overview"] = {
                "sha1": sha1,
                "original_filename": media_row.get("original_filename") or "",
                "rel_fqn": str(media_row.get("rel_fqn") or "").replace("\\", "/"),
                "final_dt": media_row.get("final_dt") or "",
                "dt_source": media_row.get("dt_source") or "",
                "path_tags": path_tags,
                "custom_tags": custom_tags,
                "tags": ", ".join(tags),
                "custom_notes": media_row.get("custom_notes") or "",
                "is_deleted": media_row.get("is_deleted"),
                "dimensions": dimensions,
            }

        if self.config.technical_db_path.exists():
            try:
                with sqlite3.connect(self.config.technical_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    tech_row = conn.execute("SELECT * FROM image_scores WHERE sha1=?", (sha1,)).fetchone()
                if tech_row:
                    tr = dict(tech_row)
                    result["technical"] = {
                        "width": tr.get("width"),
                        "height": tr.get("height"),
                        "sharpness": f"{float(tr.get('sharpness', 0)):.2f}" if tr.get("sharpness") is not None else "",
                        "contrast": f"{float(tr.get('contrast', 0)):.2f}" if tr.get("contrast") is not None else "",
                        "brightness": f"{float(tr.get('brightness', 0)):.2f}" if tr.get("brightness") is not None else "",
                        "edge_density": f"{float(tr.get('edge_density', 0)):.4f}" if tr.get("edge_density") is not None else "",
                        "resolution_score": f"{float(tr.get('resolution_score', 0)):.4f}" if tr.get("resolution_score") is not None else "",
                        "technical_score": f"{float(tr.get('technical_score', 0)):.4f}" if tr.get("technical_score") is not None else "",
                        "model_version": tr.get("model_version") or "",
                        "scored_at": tr.get("scored_at") or "",
                    }
            except Exception:
                pass

        result["raw"] = {
            "overview": result["overview"],
            "technical": result["technical"],
            "technical_db_path": str(self.config.technical_db_path),
        }
        return result



def make_config(archive_root: str, theme_color: str = DEFAULT_THEME_COLOR) -> ArchiveConfig:
    root = Path(archive_root)
    return ArchiveConfig(
        archive_root=root,
        db_path=root / "archive_index.db",
        assets_dir=root / "_web_layout" / "assets",
        thumb_dir=root / "_thumbs",
        composite_dir=root / "_thumbs" / "_composites",
        technical_db_path=root / "technical_scores.sqlite",
        theme_color=theme_color,
    )


def create_app(config: ArchiveConfig) -> Flask:
    app = Flask(__name__)
    store = ArchiveStore(config)

    def render_page(**kwargs: Any) -> str:
        kwargs.setdefault('manifests', {})
        kwargs.setdefault('action_links', None)
        kwargs.setdefault('day_calendar', None)
        return render_template_string(HTML_TEMPLATE, theme_color=config.theme_color, **kwargs)

    @app.route("/media/<path:relative_path>")
    def serve_media(relative_path: str):
        disk_path = config.archive_root / unquote(relative_path).replace("/", os.sep)
        disk_path = Path(os.path.normpath(str(disk_path)))
        if disk_path.exists():
            return send_file(disk_path)
        return "Not Found", 404

    def invalidate_composites() -> None:
        for comp_file in config.composite_dir.glob("*.jpg"):
            try:
                comp_file.unlink()
            except OSError:
                pass
        if config.db_path.exists():
            with sqlite3.connect(config.db_path) as conn:
                conn.execute("DELETE FROM composite_cache")
                conn.commit()

    def safe_destination_relative(base_dir_name: str, rel_fqn: str, sha1: str) -> Path:
        rel_path = Path(str(rel_fqn).replace("\\", "/"))
        target_rel = Path(base_dir_name) / rel_path
        candidate = config.archive_root / target_rel
        if not candidate.exists():
            return target_rel

        stem = candidate.stem
        suffix = candidate.suffix
        parent = Path(base_dir_name) / rel_path.parent
        return parent / f"{stem}__{sha1[:8]}{suffix}"

    def move_media_records(sha1_list: list[str], target_dir_name: str) -> tuple[bool, str | None]:
        if not sha1_list:
            return False, "sha1_list required"

        target_dir = config.archive_root / target_dir_name
        target_dir.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(config.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT sha1, rel_fqn FROM media WHERE sha1 IN ({','.join('?' for _ in sha1_list)})",
                tuple(sha1_list),
            ).fetchall()

            found = {str(r["sha1"]): r for r in rows}
            for sha1 in sha1_list:
                if str(sha1) not in found:
                    continue
                row = found[str(sha1)]
                old_rel = str(row["rel_fqn"]).replace("\\", "/")
                if old_rel.startswith("_trash/") or old_rel.startswith("_stash/"):
                    continue

                old_full = config.archive_root / old_rel
                if not old_full.exists():
                    continue

                new_rel_path = safe_destination_relative(target_dir_name, old_rel, str(sha1))
                new_full = config.archive_root / new_rel_path
                new_full.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_full), str(new_full))
                conn.execute(
                    "UPDATE media SET rel_fqn=?, is_deleted=1 WHERE sha1=?",
                    (str(new_rel_path).replace('/', '\\'), str(sha1)),
                )

            conn.commit()

        invalidate_composites()
        return True, None

    def empty_trash_impl() -> tuple[bool, str | None]:
        trash_root = config.archive_root / "_trash"
        trash_root.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(config.db_path) as conn:
            conn.row_factory = sqlite3.Row
            trash_rows = conn.execute(
                "SELECT sha1 FROM media WHERE rel_fqn LIKE '_trash/%' OR rel_fqn LIKE '_trash\\%'"
            ).fetchall()
            trash_sha1s = [str(r['sha1']) for r in trash_rows]

            if trash_root.exists():
                shutil.rmtree(trash_root, ignore_errors=True)
            trash_root.mkdir(parents=True, exist_ok=True)

            conn.execute("DELETE FROM media WHERE rel_fqn LIKE '_trash/%' OR rel_fqn LIKE '_trash\\%'")
            conn.commit()

        for sha1 in trash_sha1s:
            thumb = config.thumb_dir / f"{sha1}.jpg"
            if thumb.exists():
                try:
                    thumb.unlink()
                except OSError:
                    pass

        invalidate_composites()
        return True, None

    def rotate_media_by_sha(sha1: str, degrees: int) -> tuple[bool, str | None]:
        with sqlite3.connect(config.db_path) as conn:
            row = conn.execute("SELECT rel_fqn FROM media WHERE sha1=?", (sha1,)).fetchone()

        if not row:
            return False, "not found"

        full_path = config.archive_root / str(row[0])
        if not full_path.exists():
            return False, "file missing"

        try:
            with Image.open(full_path) as img:
                img = ImageOps.exif_transpose(img)
                rotated = img.rotate(-degrees, expand=True)
                rotated.convert("RGB").save(full_path, "JPEG", quality=95)

            thumb_path = config.thumb_dir / f"{sha1}.jpg"
            with Image.open(full_path) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((400, 400))
                img.convert("RGB").save(thumb_path, "JPEG", quality=85)
            return True, None
        except Exception as exc:
            return False, str(exc)

    @app.route("/api/rotate/<sha1>", methods=["POST"])
    def rotate_image(sha1: str):
        degrees = int((request.json or {}).get("degrees", 90))
        ok, message = rotate_media_by_sha(sha1, degrees)
        if not ok:
            return jsonify({"status": "error", "message": message}), 404
        invalidate_composites()
        return jsonify({"status": "ok"})

    @app.route("/api/rotate_batch", methods=["POST"])
    def rotate_batch():
        payload = request.json or {}
        sha1_list = payload.get("sha1_list") or []
        degrees = int(payload.get("degrees", 90))
        if not isinstance(sha1_list, list) or not sha1_list:
            return jsonify({"status": "error", "message": "sha1_list required"}), 400

        results: dict[str, dict[str, Any]] = {}
        any_success = False
        for sha1 in sha1_list:
            ok, message = rotate_media_by_sha(str(sha1), degrees)
            results[str(sha1)] = {"status": "ok" if ok else "error"}
            if message:
                results[str(sha1)]["message"] = message
            if ok:
                any_success = True

        if any_success:
            invalidate_composites()

        return jsonify({"status": "ok", "results": results})

    @app.route("/api/move_to_trash", methods=["POST"])
    def move_to_trash():
        payload = request.json or {}
        sha1_list = payload.get("sha1_list") or []
        if not isinstance(sha1_list, list) or not sha1_list:
            return jsonify({"status": "error", "message": "sha1_list required"}), 400
        ok, message = move_media_records([str(x) for x in sha1_list], "_trash")
        if not ok:
            return jsonify({"status": "error", "message": message}), 400
        return jsonify({"status": "ok"})

    @app.route("/api/move_to_stash", methods=["POST"])
    def move_to_stash():
        payload = request.json or {}
        sha1_list = payload.get("sha1_list") or []
        if not isinstance(sha1_list, list) or not sha1_list:
            return jsonify({"status": "error", "message": "sha1_list required"}), 400
        ok, message = move_media_records([str(x) for x in sha1_list], "_stash")
        if not ok:
            return jsonify({"status": "error", "message": message}), 400
        return jsonify({"status": "ok"})

    @app.route("/api/empty_trash", methods=["POST"])
    def empty_trash():
        ok, message = empty_trash_impl()
        if not ok:
            return jsonify({"status": "error", "message": message}), 400
        return jsonify({"status": "ok"})


    @app.route("/api/lightbox_meta/<sha1>")
    def lightbox_meta(sha1: str):
        store.load_cache()
        return jsonify(store.get_lightbox_metadata(sha1))

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
        month_name = imgs[0]["_month_name"] if imgs else datetime(int(year), int(month), 1).strftime("%B")
        return render_page(
            page_title=f"{month_name} {year}",
            active_tab="timeline",
            banner_img="hero-timeline.png",
            breadcrumb=f"<a href='/timeline'>Timeline</a> / <a href='/timeline/decade/{year[:3]}0s'>{year[:3]}0s</a> / <a href='/timeline/year/{year}'>{year}</a> / {month_name}",
            action_links=[{'label': 'Day View', 'url': f'/timeline/month/{year}/{month}/days', 'active': False}],
            photos=imgs,
            manifests={"main_gallery": store.build_manifest(imgs)},
        )

    @app.route("/timeline/month/<year>/<month>/days")
    def timeline_month_days(year: str, month: str):
        store.load_cache()
        imgs = [item for item in store.db_cache if item["_year"] == year and item["_month"] == month]
        month_name = imgs[0]["_month_name"] if imgs else datetime(int(year), int(month), 1).strftime("%B")
        day_calendar = store.build_month_day_calendar(year, month, imgs)
        return render_page(
            page_title=f"{month_name} {year} · Day View",
            active_tab="timeline",
            banner_img="hero-timeline.png",
            breadcrumb=f"<a href='/timeline'>Timeline</a> / <a href='/timeline/decade/{year[:3]}0s'>{year[:3]}0s</a> / <a href='/timeline/year/{year}'>{year}</a> / <a href='/timeline/month/{year}/{month}'>{month_name}</a> / Day View",
            action_links=[{'label': 'Grid View', 'url': f'/timeline/month/{year}/{month}', 'active': False}],
            day_calendar=day_calendar,
        )

    @app.route("/timeline/month/<year>/<month>/day/<day>")
    def timeline_month_day(year: str, month: str, day: str):
        store.load_cache()
        day_int = int(day)
        imgs = [
            item for item in store.db_cache
            if item["_year"] == year and item["_month"] == month and int(item.get("_day_int", 0)) == day_int
        ]
        month_name = imgs[0]["_month_name"] if imgs else datetime(int(year), int(month), 1).strftime("%B")
        return render_page(
            page_title=f"{month_name} {day_int}, {year}",
            active_tab="timeline",
            banner_img="hero-timeline.png",
            breadcrumb=f"<a href='/timeline'>Timeline</a> / <a href='/timeline/decade/{year[:3]}0s'>{year[:3]}0s</a> / <a href='/timeline/year/{year}'>{year}</a> / <a href='/timeline/month/{year}/{month}'>{month_name}</a> / <a href='/timeline/month/{year}/{month}/days'>Day View</a> / {day_int}",
            action_links=[{'label': 'Day View', 'url': f'/timeline/month/{year}/{month}/days', 'active': False}, {'label': 'Grid View', 'url': f'/timeline/month/{year}/{month}', 'active': False}],
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
