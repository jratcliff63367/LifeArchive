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
import math
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
            padding: 20px;
            box-sizing: border-box;
        }
        #lightbox.active { display: flex; }
        #lb-shell {
            position: relative;
            display: flex;
            align-items: stretch;
            width: min(1800px, calc(100vw - 40px));
            height: calc(100vh - 40px);
            min-width: 0;
            min-height: 0;
        }
        #lb-stage {
            position: relative;
            flex: 1;
            min-width: 0;
            min-height: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }
        #lb-img {
            max-width: 100%;
            max-height: 85vh;
            object-fit: contain;
            box-shadow: 0 0 80px rgba(0, 0, 0, 0.8);
        }
        #lb-face-overlay {
            position: absolute;
            inset: 0;
            pointer-events: none;
            z-index: 10003;
        }
        #lb-ai-summary-overlay {
            position: absolute;
            left: 50%;
            transform: translateX(-50%);
            bottom: 26px;
            max-width: min(72%, 900px);
            padding: 14px 20px;
            border-radius: 16px;
            background: rgba(0, 0, 0, 0.64);
            backdrop-filter: blur(6px);
            color: #fff;
            font-size: 28px;
            line-height: 1.3;
            font-weight: 600;
            letter-spacing: 0.01em;
            text-align: center;
            pointer-events: none;
            display: none;
            box-shadow: 0 10px 28px rgba(0, 0, 0, 0.32);
            text-shadow: 0 2px 8px rgba(0, 0, 0, 0.45);
            z-index: 10004;
        }
        #lb-ai-summary-overlay.show {
            display: block;
        }
        .lb-inline-toggle {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 14px;
            font-size: 12px;
            color: #ddd;
            font-weight: 700;
        }
        .lb-inline-toggle input {
            accent-color: var(--accent);
        }
        .lb-face-box {
            position: absolute;
            border: 2px solid rgba(183, 119, 255, 0.95);
            box-shadow: 0 0 0 1px rgba(0,0,0,0.5) inset;
            border-radius: 4px;
            background: rgba(183, 119, 255, 0.08);
        }
        .lb-face-label {
            position: absolute;
            top: -22px;
            left: 0;
            background: rgba(183, 119, 255, 0.95);
            color: #111;
            font-size: 11px;
            font-weight: 800;
            padding: 2px 6px;
            border-radius: 999px;
            white-space: nowrap;
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
            width: 90px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: 0.3s;
            font-size: 3em;
            color: rgba(255, 255, 255, 0.14);
            z-index: 10002;
        }
        .lb-nav:hover {
            background: rgba(255, 255, 255, 0.05);
            color: var(--accent);
        }
        #lb-prev { left: 0; }
        #lb-next { right: 0; }
        #lb-curate-btn {
            position: absolute;
            bottom: 30px;
            right: 30px;
            background: #222;
            padding: 12px 24px;
            border-radius: 40px;
            cursor: pointer;
            font-weight: 800;
            z-index: 10006;
            border: 1px solid #444;
            color: var(--accent);
        }
        #lb-sidebar {
            position: relative;
            width: 0;
            height: 100%;
            background: #111;
            border-left: 0 solid #333;
            padding: 60px 0;
            transition: width 0.35s cubic-bezier(0.16, 1, 0.3, 1),
                        padding 0.35s cubic-bezier(0.16, 1, 0.3, 1),
                        border-color 0.35s cubic-bezier(0.16, 1, 0.3, 1);
            z-index: 10005;
            box-shadow: none;
            overflow-y: auto;
            overflow-x: hidden;
            flex-shrink: 0;
            box-sizing: border-box;
        }
        #lb-sidebar.visible {
            width: 400px;
            padding: 60px 30px;
            border-left-width: 1px;
            box-shadow: -20px 0 50px rgba(0, 0, 0, 0.5);
        }
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
            user-select: text;
            -webkit-user-select: text;
        }
        .lb-k {
            color: #8d8d8d;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            font-size: 0.78em;
            user-select: text;
            -webkit-user-select: text;
        }
        .lb-v {
            color: #fff;
            word-break: break-word;
            user-select: text;
            -webkit-user-select: text;
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
            user-select: text;
            -webkit-user-select: text;
        }

    </style>
</head>
<body>
    <div id="context-menu">
        <div class="menu-item" onclick="showClusters()">Show Clusters</div>
        <div class="menu-item" onclick="selectByFormula('interesting', 1)">Select Most Interesting Picture</div>
        <div class="menu-item" onclick="selectByFormula('cull', 1)">Select Best Picture for Culling</div>
        <div class="menu-item" onclick="selectByFormula('cull', 2)">Select Best Two Pictures for Culling</div>
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
                <div class="cluster-badge"></div>
                <div class="keeper-badge"></div>
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
        <div id="lb-shell" onclick="event.stopPropagation()">
            <div class="lb-close" onclick="closeLB()">&times;</div>
            <div id="lb-stage">
                <div id="lb-prev" class="lb-nav" onclick="changeImg(-1)">&#10094;</div>
                <img id="lb-img" src="" oncontextmenu="handleCtxFromLightbox(event)">
                <div id="lb-face-overlay"></div>
                <div id="lb-ai-summary-overlay"></div>
                <div id="lb-next" class="lb-nav" onclick="changeImg(1)">&#10095;</div>
                <div id="lb-curate-btn" onclick="toggleSidebar()">CURATE (E)</div>
            </div>

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

            <div id="lb-section-faces" class="lb-section">
                <label style="display:flex; align-items:center; gap:8px; margin-bottom:14px; font-size:12px; color:#ddd; font-weight:700;">
                    <input id="lb-show-faces" type="checkbox" style="accent-color: var(--accent);">
                    Show face boxes on image
                </label>
                <div id="lb-faces-summary" class="lb-kv"></div>
                <div id="lb-faces-boxes" style="margin-top:16px;"></div>
            </div>

            <div id="lb-section-face-expression" class="lb-section">
                <div id="lb-face-expression-summary" class="lb-kv"></div>
                <div id="lb-face-expression-faces" style="margin-top:16px;"></div>
            </div>

            <div id="lb-section-aesthetic" class="lb-section">
                <div id="lb-aesthetic" class="lb-kv"></div>
            </div>

            <div id="lb-section-semantic" class="lb-section">
                <div id="lb-semantic" class="lb-kv"></div>
            </div>

            <div id="lb-section-ai-summary" class="lb-section">
                <label class="lb-inline-toggle">
                    <input id="lb-show-ai-summary" type="checkbox">
                    Show AI summary on image
                </label>
                <div id="lb-ai-summary" class="lb-kv"></div>
            </div>

            <div id="lb-section-raw" class="lb-section">
                <pre id="lb-raw"></pre>
            </div>

            <hr style="border:0; border-top:1px solid #333; margin:30px 0;">
            <label style="font-size:0.7em; color:#666; font-weight:900; letter-spacing:1px;">NOTES</label>
            <textarea id="input-notes" style="width:100%; background:#222; border:1px solid #444; color:#fff; padding:15px; margin-top:10px; border-radius:8px; resize:none;" rows="8" placeholder="Notes..."></textarea>
            </div>
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
        let showFaceOverlay = false;

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
                const tabLabelMap = {
                    'overview': 'Overview',
                    'technical': 'Technical',
                    'faces': 'Faces',
                    'face-expression': 'Face Expression',
                    'aesthetic': 'Aesthetic',
                    'semantic': 'Semantic',
                    'ai-summary': 'AI Summary',
                    'raw': 'Raw',
                };
                btn.textContent = tabLabelMap[tabName] || (tabName.charAt(0).toUpperCase() + tabName.slice(1));
                btn.onclick = () => setActiveMetaTab(tabName);
                row.appendChild(btn);
            }
            if (!availableTabs.includes(currentTab)) {
                currentTab = availableTabs[0] || 'overview';
            }
            setActiveMetaTab(currentTab);
        }

        function renderFaceBoxes(boxes) {
            const el = document.getElementById('lb-faces-boxes');
            if (!el) return;
            if (!boxes || boxes.length === 0) {
                el.innerHTML = '<div class="lb-empty">No face boxes available.</div>';
                return;
            }
            const html = boxes.map((box, idx) => {
                const conf = box.confidence != null ? Number(box.confidence).toFixed(3) : '';
                const rows = [
                    ['Face', idx + 1],
                    ['Abs Box', `${box.x}, ${box.y}, ${box.w}, ${box.h}`],
                    ['Norm Box', `${Number(box.x_norm).toFixed(4)}, ${Number(box.y_norm).toFixed(4)}, ${Number(box.w_norm).toFixed(4)}, ${Number(box.h_norm).toFixed(4)}`],
                    ['Area Ratio', Number(box.area_ratio).toFixed(4)],
                    ['Confidence', conf],
                ];
                return `<div style="margin-bottom:14px; padding:12px; border:1px solid #333; border-radius:10px; background:#171717;">${rows.map(r => `<div class="lb-kv" style="grid-template-columns:110px 1fr; margin-bottom:6px;"><div class="lb-k">${r[0]}</div><div class="lb-v">${r[1]}</div></div>`).join('')}</div>`;
            }).join('');
            el.innerHTML = html;
        }

        function renderFaceExpressionFaces(faces) {
            const el = document.getElementById('lb-face-expression-faces');
            if (!el) return;
            if (!faces || faces.length === 0) {
                el.innerHTML = '<div class="lb-empty">No face expression data available.</div>';
                return;
            }
            const html = faces.map((face, idx) => {
                const rows = [
                    ['Face', face.face_index != null ? Number(face.face_index) + 1 : (idx + 1)],
                    ['Area Ratio', face.area_ratio != null ? Number(face.area_ratio).toFixed(4) : ''],
                    ['Confidence', face.confidence != null ? Number(face.confidence).toFixed(3) : ''],
                    ['Smile Score', face.smile_score != null ? Number(face.smile_score).toFixed(4) : ''],
                    ['Eyes Open', face.eyes_open_score != null ? Number(face.eyes_open_score).toFixed(4) : ''],
                    ['Eye Engage', face.eye_engagement_score != null ? Number(face.eye_engagement_score).toFixed(4) : ''],
                    ['Asymmetry', face.asymmetry_score != null ? Number(face.asymmetry_score).toFixed(4) : ''],
                    ['Expression', face.expression_score != null ? Number(face.expression_score).toFixed(4) : ''],
                    ['Smile L/R', `${face.smile_left != null ? Number(face.smile_left).toFixed(3) : ''} / ${face.smile_right != null ? Number(face.smile_right).toFixed(3) : ''}`],
                    ['Blink L/R', `${face.blink_left != null ? Number(face.blink_left).toFixed(3) : ''} / ${face.blink_right != null ? Number(face.blink_right).toFixed(3) : ''}`],
                    ['Squint L/R', `${face.squint_left != null ? Number(face.squint_left).toFixed(3) : ''} / ${face.squint_right != null ? Number(face.squint_right).toFixed(3) : ''}`],
                ].filter(row => row[1] !== '' && row[1] !== ' / ');
                return `<div style="margin-bottom:14px; padding:12px; border:1px solid #333; border-radius:10px; background:#171717;">${rows.map(r => `<div class="lb-kv" style="grid-template-columns:120px 1fr; margin-bottom:6px;"><div class="lb-k">${r[0]}</div><div class="lb-v">${r[1]}</div></div>`).join('')}</div>`;
            }).join('');
            el.innerHTML = html;
        }


        function clearFaceOverlay() {
            const overlay = document.getElementById('lb-face-overlay');
            if (overlay) overlay.innerHTML = '';
        }

        function updateAiSummaryOverlay() {
            const overlay = document.getElementById('lb-ai-summary-overlay');
            const cb = document.getElementById('lb-show-ai-summary');
            if (!overlay) return;
            const summary = (((currentMeta || {}).ai_summary || {}).summary_text || '').trim();
            const shouldShow = !!(cb && cb.checked && summary.length > 0);
            overlay.textContent = summary;
            overlay.classList.toggle('show', shouldShow);
        }

        function renderFaceOverlay() {
            const overlay = document.getElementById('lb-face-overlay');
            const img = document.getElementById('lb-img');
            const stage = document.getElementById('lb-stage');
            if (!overlay || !img || !stage) return;
            overlay.innerHTML = '';

            const boxes = (((currentMeta || {}).faces || {}).boxes) || [];
            if (!showFaceOverlay || boxes.length === 0) return;
            if (!img.complete || !img.naturalWidth || !img.naturalHeight) return;

            const imgRect = img.getBoundingClientRect();
            const stageRect = stage.getBoundingClientRect();
            const left0 = imgRect.left - stageRect.left;
            const top0 = imgRect.top - stageRect.top;
            const displayW = imgRect.width;
            const displayH = imgRect.height;

            boxes.forEach((box, idx) => {
                const left = left0 + (Number(box.x_norm) * displayW);
                const top = top0 + (Number(box.y_norm) * displayH);
                const width = Math.max(2, Number(box.w_norm) * displayW);
                const height = Math.max(2, Number(box.h_norm) * displayH);

                const div = document.createElement('div');
                div.className = 'lb-face-box';
                div.style.left = `${left}px`;
                div.style.top = `${top}px`;
                div.style.width = `${width}px`;
                div.style.height = `${height}px`;

                const label = document.createElement('div');
                label.className = 'lb-face-label';
                const conf = box.confidence != null ? ` ${Number(box.confidence).toFixed(2)}` : '';
                label.textContent = `Face ${idx + 1}${conf}`;
                div.appendChild(label);

                overlay.appendChild(div);
            });
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
                const faces = data.faces || {};
                const faceSummary = faces.summary || {};
                const faceBoxes = faces.boxes || [];
                const aesthetic = data.aesthetic || {};
                const semantic = data.semantic || {};
                const aiSummary = data.ai_summary || {};
                const faceExpression = data.face_expression || {};
                const faceExpressionSummary = faceExpression.summary || {};
                const faceExpressionFaces = faceExpression.faces || [];
                const raw = data.raw || {};
                const overviewRows = [
                    ['Filename', overview.original_filename || item.filename || ''],
                    ['Path', overview.rel_fqn || item.path || ''],
                    ['Date', overview.final_dt || ''],
                    ['Date Source', overview.dt_source || ''],
                    ['Dimensions', overview.dimensions || ''],
                    ['Latitude', overview.latitude || ''],
                    ['Longitude', overview.longitude || ''],
                    ['Altitude (m)', overview.altitude_meters || ''],
                    ['Has Valid GPS', overview.has_valid_gps || ''],
                    ['Extension', overview.file_extension || ''],
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

                const faceRows = [
                    ['Face Count', faceSummary.face_count ?? ''],
                    ['Prominent Faces', faceSummary.prominent_face_count ?? ''],
                    ['Largest Face Ratio', faceSummary.largest_face_area_ratio ?? ''],
                    ['Has Prominent Face', faceSummary.has_prominent_face ?? ''],
                    ['Model Version', faceSummary.model_version || ''],
                    ['Scored At', faceSummary.scored_at || ''],
                ].filter(row => row[1] !== '' && row[1] !== 'None');

                const faceExpressionRows = [
                    ['Faces Scored', faceExpressionSummary.face_count_scored ?? ''],
                    ['Smiling Faces', faceExpressionSummary.smiling_face_count ?? ''],
                    ['Eyes Open Faces', faceExpressionSummary.eyes_open_face_count ?? ''],
                    ['Good Expression Faces', faceExpressionSummary.good_expression_face_count ?? ''],
                    ['Best Face Expression', faceExpressionSummary.best_face_expression_score ?? ''],
                    ['Avg Top2 Expression', faceExpressionSummary.avg_top2_face_expression_score ?? ''],
                    ['Prominent Face Expr', faceExpressionSummary.prominent_face_expression_score ?? ''],
                    ['People Moment Score', faceExpressionSummary.people_moment_score ?? ''],
                    ['Model Version', faceExpressionSummary.model_version || ''],
                    ['Scored At', faceExpressionSummary.scored_at || ''],
                ].filter(row => row[1] !== '' && row[1] !== 'None');

                const aestheticRows = [
                    ['Overall Aesthetic Score', aesthetic.overall_aesthetic_score || ''],
                    ['Aesthetic Score', aesthetic.aesthetic_score || ''],
                    ['Composition Score', aesthetic.composition_score || ''],
                    ['Subject Prominence', aesthetic.subject_prominence_score || ''],
                    ['Interest Score', aesthetic.interest_score || ''],
                    ['Saliency Score', aesthetic.saliency_score || ''],
                    ['Scorer Name', aesthetic.scorer_name || ''],
                    ['Model Name', aesthetic.model_name || ''],
                    ['Model Version', aesthetic.model_version || ''],
                    ['Scored At', aesthetic.scored_at || ''],
                    ['Warnings', aesthetic.warnings || ''],
                ].filter(row => row[1] !== '' && row[1] !== 'None');

                const semanticRows = [
                    ['Semantic Score', semantic.semantic_score || ''],
                    ['Scene Type', semantic.scene_type || ''],
                    ['Top Labels', semantic.top_labels_display || ''],
                    ['AI Tags', semantic.ai_tags_display || ''],
                    ['Contains People', semantic.contains_people ?? ''],
                    ['Contains Animals', semantic.contains_animals ?? ''],
                    ['Contains Text', semantic.contains_text ?? ''],
                    ['Document Like', semantic.is_document_like ?? ''],
                    ['Screenshot Like', semantic.is_screenshot_like ?? ''],
                    ['Landscape Like', semantic.is_landscape_like ?? ''],
                    ['Food Like', semantic.is_food_like ?? ''],
                    ['Indoor Like', semantic.is_indoor_like ?? ''],
                    ['Outdoor Like', semantic.is_outdoor_like ?? ''],
                    ['Scorer Name', semantic.scorer_name || ''],
                    ['Model Name', semantic.model_name || ''],
                    ['Model Version', semantic.model_version || ''],
                    ['Scored At', semantic.scored_at || ''],
                    ['Warnings', semantic.warnings || ''],
                ].filter(row => row[1] !== '' && row[1] !== 'None');

                const aiSummaryRows = [
                    ['Summary', aiSummary.summary_text || ''],
                    ['Model Name', aiSummary.model_name || ''],
                    ['Model Version', aiSummary.model_version || ''],
                    ['Scored At', aiSummary.scored_at || ''],
                    ['Warnings', aiSummary.warnings || ''],
                ].filter(row => row[1] !== '' && row[1] !== 'None');

                renderKV('lb-technical', techRows);
                renderKV('lb-faces-summary', faceRows);
                renderKV('lb-face-expression-summary', faceExpressionRows);
                renderFaceExpressionFaces(faceExpressionFaces);
                renderKV('lb-aesthetic', aestheticRows);
                renderKV('lb-semantic', semanticRows);
                renderKV('lb-ai-summary', aiSummaryRows);
                renderFaceBoxes(faceBoxes);

                const showFacesCb = document.getElementById('lb-show-faces');
                if (showFacesCb) {
                    showFacesCb.checked = showFaceOverlay;
                    showFacesCb.onchange = () => {
                        showFaceOverlay = !!showFacesCb.checked;
                        renderFaceOverlay();
                    };
                }

                const showAiSummaryCb = document.getElementById('lb-show-ai-summary');
                if (showAiSummaryCb) {
                    showAiSummaryCb.onchange = () => updateAiSummaryOverlay();
                }

                renderFaceOverlay();
                updateAiSummaryOverlay();

                const rawEl = document.getElementById('lb-raw');
                if (rawEl) rawEl.textContent = JSON.stringify(raw, null, 2);
                const availableTabs = ['overview'];
                if (techRows.length > 0) availableTabs.push('technical');
                if (faceRows.length > 0 || faceBoxes.length > 0) availableTabs.push('faces');
                if (faceExpressionRows.length > 0 || faceExpressionFaces.length > 0) availableTabs.push('face-expression');
                if (aestheticRows.length > 0) availableTabs.push('aesthetic');
                if (semanticRows.length > 0) availableTabs.push('semantic');
                if (aiSummaryRows.length > 0) availableTabs.push('ai-summary');
                availableTabs.push('raw');
                buildMetaTabs(availableTabs);
            } catch (err) {
                renderKV('lb-overview', [['Error', 'Failed to load metadata']]);
                renderKV('lb-technical', []);
                renderKV('lb-faces-summary', []);
                renderKV('lb-face-expression-summary', []);
                renderFaceExpressionFaces([]);
                renderKV('lb-aesthetic', []);
                renderKV('lb-semantic', []);
                renderKV('lb-ai-summary', []);
                renderFaceBoxes([]);
                clearFaceOverlay();
                const showAiSummaryCb = document.getElementById('lb-show-ai-summary');
                    updateAiSummaryOverlay();
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
            const lbImg = document.getElementById('lb-img');
            lbImg.onload = () => renderFaceOverlay();
            lbImg.src = '/media/' + path + '?t=' + Date.now();
            document.getElementById('meta-file').innerText = item.filename;
            clearFaceOverlay();
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
            clearFaceOverlay();
            const showAiSummaryCb = document.getElementById('lb-show-ai-summary');
            currentMeta = null;
            updateAiSummaryOverlay();
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
            clearClusterVisuals();
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

        async function selectByFormula(mode, keepCount) {
            const targets = getContextTargets(menuSha1);
            document.getElementById('context-menu').style.display = 'none';
            if (!targets || targets.length === 0) return;
            if (targets.length <= keepCount) {
                selectedSha1s = new Set(targets);
                refreshSelectionUI();
                return;
            }

            try {
                const resp = await fetch('/api/select_by_formula', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        sha1_list: targets,
                        mode,
                        keep_count: keepCount
                    })
                });
                const data = await resp.json();
                if (!resp.ok || data.status !== 'ok') {
                    alert(data.message || 'Selection formula failed.');
                    return;
                }

                clearClusterVisuals();
                selectedSha1s = new Set(data.winners || []);
                refreshSelectionUI();

                if (Array.isArray(data.ranked)) {
                    console.table(data.ranked.map(item => ({
                        sha1: item.sha1,
                        filename: item.filename || '',
                        score: item.score,
                        face_score: item.breakdown?.face_score ?? '',
                        technical: item.breakdown?.technical_score ?? '',
                        aesthetic: item.breakdown?.aesthetic_score ?? '',
                        prominence: item.breakdown?.subject_prominence_score ?? '',
                        semantic: item.breakdown?.semantic_score ?? '',
                        face_expression: item.breakdown?.face_expression_score ?? '',
                        people_weight: item.breakdown?.people_weight ?? '',
                        note: item.breakdown?.note ?? ''
                    })));
                }
            } catch (err) {
                console.error(err);
                alert('Selection formula request failed.');
            }
        }

        var clusterMembership = new Map();

        function clearClusterVisuals() {
            clusterMembership = new Map();
            document.querySelectorAll('.photo-card').forEach(card => {
                card.classList.remove('clustered', 'keeper1', 'keeper2');
                for (let i = 0; i < 8; i++) {
                    card.classList.remove(`cluster-${i}`);
                }
                const badge = card.querySelector('.cluster-badge');
                if (badge) badge.textContent = '';
                const keeperBadge = card.querySelector('.keeper-badge');
                if (keeperBadge) keeperBadge.textContent = '';
            });
        }

        function applyClusterVisuals(clusters) {
            clearClusterVisuals();
            if (!Array.isArray(clusters)) return;
            clusters.forEach((cluster, idx) => {
                const label = String(idx + 1);
                const colorClass = `cluster-${idx % 8}`;
                const primary = String(cluster.primary_sha1 || '');
                const secondary = String(cluster.secondary_sha1 || '');
                (cluster.sha1s || []).forEach(sha => {
                    const shaStr = String(sha);
                    clusterMembership.set(shaStr, idx + 1);
                    const card = document.querySelector(`.photo-card[data-sha="${shaStr}"]`);
                    if (!card) return;
                    card.classList.add('clustered', colorClass);
                    if (shaStr === primary) card.classList.add('keeper1');
                    else if (shaStr === secondary) card.classList.add('keeper2');

                    const badge = card.querySelector('.cluster-badge');
                    if (badge) badge.textContent = label;
                    const keeperBadge = card.querySelector('.keeper-badge');
                    if (keeperBadge) {
                        if (shaStr === primary) keeperBadge.textContent = '1';
                        else if (shaStr === secondary) keeperBadge.textContent = '2';
                        else keeperBadge.textContent = '';
                    }
                });
            });
        }

        async function showClusters() {
            const targets = getContextTargets(menuSha1);
            document.getElementById('context-menu').style.display = 'none';
            if (!targets || targets.length === 0) return;

            try {
                const resp = await fetch('/api/select_clusters', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        sha1_list: targets
                    })
                });
                const data = await resp.json();
                if (!resp.ok || data.status !== 'ok') {
                    alert(data.message || 'Cluster selection failed.');
                    return;
                }

                selectedSha1s = new Set(data.clustered_sha1s || []);
                applyClusterVisuals(data.clusters || []);
                refreshSelectionUI();

                if (Array.isArray(data.clusters)) {
                    console.table(data.clusters.map(cluster => ({
                        cluster_id: cluster.cluster_id,
                        size: cluster.size,
                        gps_bucket: cluster.gps_bucket,
                        start_time: cluster.start_time,
                        end_time: cluster.end_time,
                        primary_sha1: cluster.primary_sha1 || '',
                        secondary_sha1: cluster.secondary_sha1 || '',
                        sha1s: (cluster.sha1s || []).join(', ')
                    })));
                }
            } catch (err) {
                console.error(err);
                alert('Cluster selection request failed.');
            }
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
    face_db_path: Path
    aesthetic_db_path: Path
    semantic_db_path: Path
    ai_summary_db_path: Path
    face_expression_db_path: Path
    hero_db_path: Path
    theme_color: str = DEFAULT_THEME_COLOR


class ArchiveStore:
    def __init__(self, config: ArchiveConfig) -> None:
        self.config = config
        self.db_cache: list[dict[str, Any]] = []
        self.undated_cache: list[dict[str, Any]] = []
        self.global_tags: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.hero_score_map: dict[str, float] = {}
        self.hero_db_mtime: float = 0.0
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
            cols = {row[1] for row in conn.execute("PRAGMA table_info(composite_cache)").fetchall()}
            if "candidate_hash" not in cols:
                conn.execute("ALTER TABLE composite_cache ADD COLUMN candidate_hash TEXT DEFAULT ''")
            if "selection_version" not in cols:
                conn.execute("ALTER TABLE composite_cache ADD COLUMN selection_version TEXT DEFAULT ''")
            conn.commit()


    def load_hero_score_map(self) -> None:
        self.hero_score_map = {}
        self.hero_db_mtime = 0.0
        if not self.config.hero_db_path.exists():
            return
        try:
            self.hero_db_mtime = self.config.hero_db_path.stat().st_mtime
        except Exception:
            self.hero_db_mtime = 0.0
        try:
            with sqlite3.connect(self.config.hero_db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT sha1, hero_score FROM hero_scores").fetchall()
            self.hero_score_map = {str(r["sha1"]): float(r["hero_score"] or 0.0) for r in rows}
        except Exception:
            self.hero_score_map = {}

    def choose_best_interesting_item(self, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not items:
            return None
        def sort_key(item: dict[str, Any]):
            return (
                float(self.hero_score_map.get(str(item.get("sha1")), -1.0)),
                str(item.get("final_dt") or ""),
            )
        best = max(items, key=sort_key)
        return best

    def load_cache(self) -> None:
        if not self.config.db_path.exists():
            self.db_cache = []
            self.undated_cache = []
            self.global_tags = defaultdict(list)
            return

        self.init_db_extensions()
        self.load_hero_score_map()

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
                    best_item = self.choose_best_interesting_item(day_items) or day_items[0]
                    row.append({
                        'day': day,
                        'has_items': True,
                        'url': f'/timeline/month/{year}/{month}/day/{day:02d}',
                        'thumb_sha1': best_item['sha1'],
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


    def get_hero_score(self, item: dict[str, Any]) -> float:
        return float(self.hero_score_map.get(str(item.get("sha1")), 0.0))

    @staticmethod
    def has_valid_gps(item: dict[str, Any]) -> bool:
        try:
            lat = float(item.get("latitude"))
            lon = float(item.get("longitude"))
            return abs(lat) > 0.000001 or abs(lon) > 0.000001
        except Exception:
            return False

    @staticmethod
    def _gps_bucket_key(item: dict[str, Any], precision: int = 3) -> tuple[float, float]:
        return (
            round(float(item.get("latitude") or 0.0), precision),
            round(float(item.get("longitude") or 0.0), precision),
        )

    @staticmethod
    def _candidate_hash(media_list: list[dict[str, Any]]) -> str:
        sig = "|".join(sorted(str(item.get("sha1")) for item in media_list))
        return hashlib.md5(sig.encode("utf-8")).hexdigest()

    def _selection_version(self) -> str:
        return f"geohero_v2_timecluster|hero_mtime={int(self.hero_db_mtime)}"

    def _choose_representative(self, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not items:
            return None
        return max(
            items,
            key=lambda item: (
                self.get_hero_score(item),
                str(item.get("final_dt") or ""),
                str(item.get("sha1") or ""),
            ),
        )

    def _gps_distance_sq(self, a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    def _kmeans_pp_clusters(self, buckets: list[dict[str, Any]], k: int, iterations: int = 12) -> list[list[dict[str, Any]]]:
        if not buckets:
            return []
        if len(buckets) <= k:
            return [[b] for b in buckets]

        points = [(float(b["centroid_lat"]), float(b["centroid_lon"])) for b in buckets]
        weighted = sorted(
            enumerate(buckets),
            key=lambda pair: (
                self.get_hero_score(pair[1].get("best_item") or {}),
                pair[1].get("count", 0),
            ),
            reverse=True,
        )
        first_idx = weighted[0][0]
        centers = [points[first_idx]]

        while len(centers) < k:
            dists = []
            total = 0.0
            for pt in points:
                d2 = min(self._gps_distance_sq(pt, c) for c in centers)
                dists.append(d2)
                total += d2
            if total <= 1e-12:
                break
            threshold = total / 2.0
            accum = 0.0
            chosen_idx = 0
            for idx, d2 in enumerate(dists):
                accum += d2
                if accum >= threshold:
                    chosen_idx = idx
                    break
            candidate = points[chosen_idx]
            if candidate not in centers:
                centers.append(candidate)
            else:
                for pt in points:
                    if pt not in centers:
                        centers.append(pt)
                        break
            if len(centers) >= len(points):
                break

        if not centers:
            centers = [points[0]]

        for _ in range(iterations):
            assignments = [[] for _ in centers]
            for bucket, pt in zip(buckets, points):
                best_i = min(range(len(centers)), key=lambda i: self._gps_distance_sq(pt, centers[i]))
                assignments[best_i].append(bucket)

            new_centers = []
            for idx, cluster in enumerate(assignments):
                if not cluster:
                    new_centers.append(centers[idx])
                    continue
                total_w = sum(max(1, int(b.get("count", 1))) for b in cluster)
                lat = sum(float(b["centroid_lat"]) * max(1, int(b.get("count", 1))) for b in cluster) / total_w
                lon = sum(float(b["centroid_lon"]) * max(1, int(b.get("count", 1))) for b in cluster) / total_w
                new_centers.append((lat, lon))
            if new_centers == centers:
                break
            centers = new_centers

        final_assignments = [[] for _ in centers]
        for bucket, pt in zip(buckets, points):
            best_i = min(range(len(centers)), key=lambda i: self._gps_distance_sq(pt, centers[i]))
            final_assignments[best_i].append(bucket)

        return [cluster for cluster in final_assignments if cluster]


    @staticmethod
    def _parse_final_dt(item: dict[str, Any]) -> datetime | None:
        try:
            dt_str = str(item.get("final_dt") or "")
            if not dt_str or dt_str.startswith("0000"):
                return None
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    def _cluster_items_by_time(self, items: list[dict[str, Any]], threshold_seconds: int = 15) -> list[list[dict[str, Any]]]:
        if not items:
            return []
        decorated = []
        for item in items:
            dt = self._parse_final_dt(item)
            if dt is None:
                # Put undated/invalid items into their own singleton cluster.
                decorated.append((None, item))
            else:
                decorated.append((dt, item))

        # Sort valid timestamps first, then invalids at end as singletons.
        valid = sorted([(dt, item) for dt, item in decorated if dt is not None], key=lambda pair: pair[0])
        invalid = [item for dt, item in decorated if dt is None]

        clusters: list[list[dict[str, Any]]] = []
        if valid:
            current: list[dict[str, Any]] = [valid[0][1]]
            prev_dt = valid[0][0]
            for dt, item in valid[1:]:
                delta = (dt - prev_dt).total_seconds()
                if delta <= threshold_seconds:
                    current.append(item)
                else:
                    clusters.append(current)
                    current = [item]
                prev_dt = dt
            if current:
                clusters.append(current)

        for item in invalid:
            clusters.append([item])

        return clusters

    def _reduce_candidates_by_time_clusters(self, media_list: list[dict[str, Any]], threshold_seconds: int = 15) -> list[dict[str, Any]]:
        if not media_list:
            return []

        gps_bucket_map: dict[tuple[float, float], list[dict[str, Any]]] = defaultdict(list)
        non_gps_items: list[dict[str, Any]] = []

        for item in media_list:
            if self.has_valid_gps(item):
                gps_bucket_map[self._gps_bucket_key(item)].append(item)
            else:
                non_gps_items.append(item)

        reduced: list[dict[str, Any]] = []

        # GPS items: cluster within each place by time adjacency, then keep best representative.
        for _, bucket_items in gps_bucket_map.items():
            time_clusters = self._cluster_items_by_time(bucket_items, threshold_seconds=threshold_seconds)
            for cluster in time_clusters:
                rep = self._choose_representative(cluster)
                if rep is not None:
                    reduced.append(rep)

        # Non-GPS items: leave as-is for now. They may still represent important legacy material.
        reduced.extend(non_gps_items)

        # De-duplicate by sha1 just in case.
        seen: set[str] = set()
        unique_reduced: list[dict[str, Any]] = []
        for item in reduced:
            sha1 = str(item.get("sha1") or "")
            if not sha1 or sha1 in seen:
                continue
            seen.add(sha1)
            unique_reduced.append(item)

        return unique_reduced

    def _select_composite_heroes(self, media_list: list[dict[str, Any]], max_images: int = 16) -> list[dict[str, Any]]:
        if not media_list:
            return []

        # Step 1: reduce burst/near-duplicate groups first, within each GPS location bucket,
        # by clustering images taken within a short time window.
        reduced_candidates = self._reduce_candidates_by_time_clusters(media_list, threshold_seconds=15)
        if not reduced_candidates:
            reduced_candidates = list(media_list)

        gps_items = [item for item in reduced_candidates if self.has_valid_gps(item)]
        non_gps_items = [item for item in reduced_candidates if not self.has_valid_gps(item)]

        selected: list[dict[str, Any]] = []
        selected_sha1s: set[str] = set()

        non_gps_slots = 1 if non_gps_items else 0
        gps_slots = max(0, max_images - non_gps_slots)

        # Reserve one slot for non-GPS material if present.
        if non_gps_slots:
            non_gps_best = self._choose_representative(non_gps_items)
            if non_gps_best:
                selected.append(non_gps_best)
                selected_sha1s.add(str(non_gps_best["sha1"]))

        # Step 2: build GPS buckets from the reduced representative set only.
        bucket_map: dict[tuple[float, float], list[dict[str, Any]]] = defaultdict(list)
        for item in gps_items:
            bucket_map[self._gps_bucket_key(item)].append(item)

        buckets: list[dict[str, Any]] = []
        for key, items in bucket_map.items():
            best_item = self._choose_representative(items)
            buckets.append({
                "bucket_key": key,
                "centroid_lat": sum(float(it.get("latitude") or 0.0) for it in items) / max(1, len(items)),
                "centroid_lon": sum(float(it.get("longitude") or 0.0) for it in items) / max(1, len(items)),
                "items": items,
                "count": len(items),
                "best_item": best_item,
            })

        # Step 3: geographic diversity selection.
        gps_reps: list[dict[str, Any]] = []
        if gps_slots > 0 and buckets:
            if len(buckets) <= gps_slots:
                # Few unique locations: take the best rep from each bucket,
                # then filler later can add additional reduced representatives.
                for bucket in buckets:
                    rep = bucket.get("best_item")
                    if rep is not None:
                        gps_reps.append(rep)
            else:
                # Many unique locations: cluster bucket centroids into gps_slots regions.
                clusters = self._kmeans_pp_clusters(buckets, gps_slots)
                for cluster in clusters:
                    cluster_items: list[dict[str, Any]] = []
                    for bucket in cluster:
                        cluster_items.extend(bucket["items"])
                    rep = self._choose_representative(cluster_items)
                    if rep is not None:
                        gps_reps.append(rep)

        gps_reps = sorted(
            gps_reps,
            key=lambda item: (self.get_hero_score(item), str(item.get("final_dt") or "")),
            reverse=True,
        )

        for rep in gps_reps:
            sha1 = str(rep["sha1"])
            if sha1 not in selected_sha1s and len(selected) < max_images:
                selected.append(rep)
                selected_sha1s.add(sha1)

        # Step 4: fill remaining slots ONLY from the reduced representative pool,
        # not the raw original media list. This prevents the filler phase from
        # reintroducing burst duplicates that the clustering step removed.
        if len(selected) < max_images:
            remaining = sorted(
                reduced_candidates,
                key=lambda item: (self.get_hero_score(item), str(item.get("final_dt") or "")),
                reverse=True,
            )
            for item in remaining:
                sha1 = str(item["sha1"])
                if sha1 in selected_sha1s:
                    continue
                selected.append(item)
                selected_sha1s.add(sha1)
                if len(selected) >= max_images:
                    break

        return selected[:max_images]


    def get_composite_payload(self, path_key: str, media_list: list[dict[str, Any]], max_images: int = 16) -> tuple[str | None, list[dict[str, Any]]]:
        if not media_list:
            return None, []

        candidate_hash = self._candidate_hash(media_list)
        selection_version = self._selection_version()
        heroes = self._select_composite_heroes(media_list, max_images=max_images)
        if not heroes:
            heroes = media_list[:max_images]

        sha1s = [str(hero["sha1"]) for hero in heroes]
        composite_hash = hashlib.md5(("|".join(sha1s) + "|" + selection_version).encode("utf-8")).hexdigest()
        composite_path = self.config.composite_dir / f"{composite_hash}.jpg"

        # Reuse cached composite file if all cache keys still match and file exists.
        with sqlite3.connect(self.config.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cached = conn.execute(
                "SELECT composite_hash, candidate_hash, selection_version, sha1_list FROM composite_cache WHERE path_key=?",
                (path_key,),
            ).fetchone()
            if cached:
                cached_hash = str(cached["composite_hash"] or "")
                cached_candidate_hash = str(cached["candidate_hash"] or "")
                cached_selection_version = str(cached["selection_version"] or "")
                cached_sha1s = str(cached["sha1_list"] or "")
                candidate = self.config.composite_dir / f"{cached_hash}.jpg"
                if (
                    candidate.exists()
                    and cached_candidate_hash == candidate_hash
                    and cached_selection_version == selection_version
                    and cached_sha1s == ",".join(sha1s)
                ):
                    return cached_hash, heroes

        if not composite_path.exists():
            canvas = Image.new("RGB", (400, 400), (13, 13, 13))
            for idx, hero in enumerate(heroes[:max_images]):
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
                "INSERT OR REPLACE INTO composite_cache (path_key, sha1_list, composite_hash, candidate_hash, selection_version) VALUES (?, ?, ?, ?, ?)",
                (path_key, ",".join(sha1s), composite_hash, candidate_hash, selection_version),
            )
            conn.commit()

        return composite_hash, heroes

    def get_composite_hash(self, path_key: str, media_list: list[dict[str, Any]]) -> str | None:
        composite_hash, _heroes = self.get_composite_payload(path_key, media_list, max_images=16)
        return composite_hash

        heroes = self._select_composite_heroes(media_list, max_images=16)
        if not heroes:
            heroes = media_list[:16]

        sha1s = [str(hero["sha1"]) for hero in heroes]
        composite_hash = hashlib.md5(("|".join(sha1s) + "|" + selection_version).encode("utf-8")).hexdigest()
        composite_path = self.config.composite_dir / f"{composite_hash}.jpg"

        if not composite_path.exists():
            canvas = Image.new("RGB", (400, 400), (13, 13, 13))
            for idx, hero in enumerate(heroes[:16]):
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
                "INSERT OR REPLACE INTO composite_cache (path_key, sha1_list, composite_hash, candidate_hash, selection_version) VALUES (?, ?, ?, ?, ?)",
                (path_key, ",".join(sha1s), composite_hash, candidate_hash, selection_version),
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
            "faces": {"summary": {}, "boxes": []},
            "aesthetic": {},
            "semantic": {},
            "ai_summary": {},
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

            lat_val = media_row.get("latitude")
            lon_val = media_row.get("longitude")
            alt_val = media_row.get("altitude_meters")
            file_size_val = media_row.get("file_size")
            ext_val = media_row.get("extension") or ""
            try:
                has_valid_gps = (
                    lat_val is not None and lon_val is not None and
                    (abs(float(lat_val)) > 0.000001 or abs(float(lon_val)) > 0.000001)
                )
            except Exception:
                has_valid_gps = False

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
                "latitude": "" if lat_val is None else f"{float(lat_val):.6f}",
                "longitude": "" if lon_val is None else f"{float(lon_val):.6f}",
                "altitude_meters": "" if alt_val is None else f"{float(alt_val):.1f}",
                "has_valid_gps": "Yes" if has_valid_gps else "No",
                "file_extension": str(ext_val),
                "file_size": "" if file_size_val is None else str(file_size_val),
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

        if self.config.face_db_path.exists():
            try:
                with sqlite3.connect(self.config.face_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    face_summary_row = conn.execute("SELECT * FROM image_face_summary WHERE sha1=?", (sha1,)).fetchone()
                    face_box_rows = conn.execute("SELECT * FROM image_faces WHERE sha1=? ORDER BY face_index", (sha1,)).fetchall()

                if face_summary_row:
                    fs = dict(face_summary_row)
                    result["faces"]["summary"] = {
                        "width": fs.get("width"),
                        "height": fs.get("height"),
                        "face_count": fs.get("face_count"),
                        "prominent_face_count": fs.get("prominent_face_count"),
                        "largest_face_area_ratio": f"{float(fs.get('largest_face_area_ratio', 0)):.4f}" if fs.get("largest_face_area_ratio") is not None else "",
                        "has_prominent_face": fs.get("has_prominent_face"),
                        "model_version": fs.get("model_version") or "",
                        "scored_at": fs.get("scored_at") or "",
                    }

                if face_box_rows:
                    result["faces"]["boxes"] = []
                    for r in face_box_rows:
                        fr = dict(r)
                        result["faces"]["boxes"].append({
                            "face_index": fr.get("face_index"),
                            "x": fr.get("x"),
                            "y": fr.get("y"),
                            "w": fr.get("w"),
                            "h": fr.get("h"),
                            "x_norm": fr.get("x_norm"),
                            "y_norm": fr.get("y_norm"),
                            "w_norm": fr.get("w_norm"),
                            "h_norm": fr.get("h_norm"),
                            "area_ratio": fr.get("area_ratio"),
                            "img_width": fr.get("img_width"),
                            "img_height": fr.get("img_height"),
                        })
            except Exception:
                pass
        if self.config.face_expression_db_path.exists():
            try:
                with sqlite3.connect(self.config.face_expression_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    summary_row = conn.execute(
                        "SELECT * FROM image_face_expression_summary WHERE sha1 = ?",
                        (sha1,),
                    ).fetchone()
                    face_rows = conn.execute(
                        "SELECT * FROM face_expression WHERE sha1 = ? ORDER BY face_index",
                        (sha1,),
                    ).fetchall()
                result["face_expression"] = {
                    "summary": dict(summary_row) if summary_row else {},
                    "faces": [dict(r) for r in face_rows],
                }
            except Exception:
                pass

        if self.config.aesthetic_db_path.exists():
            try:
                with sqlite3.connect(self.config.aesthetic_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    aesthetic_row = conn.execute("SELECT * FROM aesthetic_scores WHERE sha1=?", (sha1,)).fetchone()
                if aesthetic_row:
                    ar = dict(aesthetic_row)
                    result["aesthetic"] = {
                        "aesthetic_score": f"{float(ar.get('aesthetic_score', 0)):.4f}" if ar.get("aesthetic_score") is not None else "",
                        "composition_score": f"{float(ar.get('composition_score', 0)):.4f}" if ar.get("composition_score") is not None else "",
                        "subject_prominence_score": f"{float(ar.get('subject_prominence_score', 0)):.4f}" if ar.get("subject_prominence_score") is not None else "",
                        "interest_score": f"{float(ar.get('interest_score', 0)):.4f}" if ar.get("interest_score") is not None else "",
                        "saliency_score": f"{float(ar.get('saliency_score', 0)):.4f}" if ar.get("saliency_score") is not None else "",
                        "overall_aesthetic_score": f"{float(ar.get('overall_aesthetic_score', 0)):.4f}" if ar.get("overall_aesthetic_score") is not None else "",
                        "scorer_name": ar.get("scorer_name") or "",
                        "model_name": ar.get("model_name") or "",
                        "model_version": ar.get("model_version") or "",
                        "scored_at": ar.get("scored_at") or "",
                        "warnings": ar.get("warnings") or "",
                    }
            except Exception:
                pass

        if self.config.semantic_db_path.exists():
            try:
                with sqlite3.connect(self.config.semantic_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    semantic_row = conn.execute("SELECT * FROM semantic_scores WHERE sha1=?", (sha1,)).fetchone()
                if semantic_row:
                    sr = dict(semantic_row)
                    top_labels_raw = sr.get("top_labels_json") or "[]"
                    ai_tags_raw = sr.get("ai_tags_json") or "[]"
                    try:
                        top_labels = json.loads(top_labels_raw) if isinstance(top_labels_raw, str) else top_labels_raw
                    except Exception:
                        top_labels = []
                    try:
                        ai_tags = json.loads(ai_tags_raw) if isinstance(ai_tags_raw, str) else ai_tags_raw
                    except Exception:
                        ai_tags = []
                    result["semantic"] = {
                        "semantic_score": f"{float(sr.get('semantic_score', 0)):.4f}" if sr.get("semantic_score") is not None else "",
                        "scene_type": sr.get("scene_type") or "",
                        "top_labels_json": top_labels_raw,
                        "ai_tags_json": ai_tags_raw,
                        "top_labels_display": ", ".join(
                            f"{item.get('label')} ({float(item.get('score', 0)):.2f})"
                            for item in top_labels[:6]
                            if isinstance(item, dict) and item.get('label')
                        ),
                        "ai_tags_display": ", ".join(str(tag) for tag in ai_tags),
                        "contains_people": sr.get("contains_people"),
                        "contains_animals": sr.get("contains_animals"),
                        "contains_text": sr.get("contains_text"),
                        "is_document_like": sr.get("is_document_like"),
                        "is_screenshot_like": sr.get("is_screenshot_like"),
                        "is_landscape_like": sr.get("is_landscape_like"),
                        "is_food_like": sr.get("is_food_like"),
                        "is_indoor_like": sr.get("is_indoor_like"),
                        "is_outdoor_like": sr.get("is_outdoor_like"),
                        "scorer_name": sr.get("scorer_name") or "",
                        "model_name": sr.get("model_name") or "",
                        "model_version": sr.get("model_version") or "",
                        "scored_at": sr.get("scored_at") or "",
                        "warnings": sr.get("warnings") or "",
                    }
            except Exception:
                pass

        if self.config.ai_summary_db_path.exists():
            try:
                with sqlite3.connect(self.config.ai_summary_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    summary_row = conn.execute("SELECT * FROM ai_summaries WHERE sha1=?", (sha1,)).fetchone()
                if summary_row:
                    sr = dict(summary_row)
                    result["ai_summary"] = {
                        "summary_text": sr.get("summary_text") or "",
                        "model_name": sr.get("model_name") or "",
                        "model_version": sr.get("model_version") or "",
                        "scored_at": sr.get("scored_at") or "",
                        "warnings": sr.get("warnings") or "",
                    }
            except Exception:
                pass

        result["raw"] = {
            "overview": result["overview"],
            "technical": result["technical"],
            "faces": result["faces"],
            "aesthetic": result["aesthetic"],
            "semantic": result["semantic"],
            "ai_summary": result["ai_summary"],
            "technical_db_path": str(self.config.technical_db_path),
            "face_db_path": str(self.config.face_db_path),
            "aesthetic_db_path": str(self.config.aesthetic_db_path),
            "semantic_db_path": str(self.config.semantic_db_path),
            "ai_summary_db_path": str(self.config.ai_summary_db_path),
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
        face_db_path=root / "face_scores.sqlite",
        aesthetic_db_path=root / "aesthetic_scores.sqlite",
        semantic_db_path=root / "semantic_scores.sqlite",
        ai_summary_db_path=root / "ai_summaries.sqlite",
        face_expression_db_path=root / "face_expression.sqlite",
        hero_db_path=root / "hero_scores.sqlite",
        theme_color=theme_color,
    )



def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        return float(value)
    except Exception:
        return default


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _face_score_from_meta(meta: dict[str, Any]) -> float:
    faces = (meta.get("faces") or {}).get("summary") or {}
    face_count = int(faces.get("face_count") or 0)
    prominent_count = int(faces.get("prominent_face_count") or 0)
    largest_ratio = _safe_float(faces.get("largest_face_area_ratio"), 0.0)

    score = 0.0
    if face_count > 0:
        score += 0.30 * min(1.0, math.log1p(face_count))
        score += 0.45 * min(1.0, largest_ratio / 0.18) if largest_ratio > 0 else 0.0
        score += 0.15 * min(1.0, prominent_count / 2.0)
        score += 0.10
    return _clamp01(score)


def _face_expression_summary_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return (meta.get("face_expression") or {}).get("summary") or {}


def _face_expression_signal_from_meta(meta: dict[str, Any]) -> float:
    summary = _face_expression_summary_from_meta(meta)
    best_face = _safe_float(summary.get("best_face_expression_score"), 0.0)
    avg_top2 = _safe_float(summary.get("avg_top2_face_expression_score"), 0.0)
    prominent_expr = _safe_float(summary.get("prominent_face_expression_score"), 0.0)
    people_moment = _safe_float(summary.get("people_moment_score"), 0.0)
    good_count = int(summary.get("good_expression_face_count") or 0)
    smiling_count = int(summary.get("smiling_face_count") or 0)
    eyes_open_count = int(summary.get("eyes_open_face_count") or 0)

    count_bonus = min(1.0, (0.45 * good_count + 0.35 * smiling_count + 0.20 * eyes_open_count) / 3.0)

    signal = (
        best_face * 0.36 +
        avg_top2 * 0.24 +
        prominent_expr * 0.20 +
        people_moment * 0.14 +
        count_bonus * 0.06
    )
    return _clamp01(signal)


def _people_weight_from_meta(meta: dict[str, Any]) -> float:
    faces = (meta.get("faces") or {}).get("summary") or {}
    face_count = int(faces.get("face_count") or 0)
    prominent_count = int(faces.get("prominent_face_count") or 0)
    largest_ratio = _safe_float(faces.get("largest_face_area_ratio"), 0.0)

    if face_count <= 0:
        return 0.0

    # Small background faces should contribute very little.
    largest_component = _clamp01((largest_ratio - 0.018) / 0.10)
    prominent_component = _clamp01(prominent_count / 2.0)

    if prominent_count <= 0 and largest_ratio < 0.018:
        return 0.06

    weight = 0.12 + (largest_component * 0.68) + (prominent_component * 0.20)
    return _clamp01(weight)


def _interesting_score(meta: dict[str, Any]) -> dict[str, Any]:
    technical = _safe_float((meta.get("technical") or {}).get("technical_score"), 0.0)
    aesthetic_meta = meta.get("aesthetic") or {}
    aesthetic = _safe_float(aesthetic_meta.get("overall_aesthetic_score"), 0.0)
    if aesthetic <= 0:
        aesthetic = _safe_float(aesthetic_meta.get("aesthetic_score"), 0.0)
    subject_prominence = _safe_float(aesthetic_meta.get("subject_prominence_score"), 0.0)
    semantic = _safe_float((meta.get("semantic") or {}).get("semantic_score"), 0.0)
    face_score = _face_score_from_meta(meta)
    face_expression_score = _face_expression_signal_from_meta(meta)
    people_weight = _people_weight_from_meta(meta)

    sem = meta.get("semantic") or {}
    contains_people = int(sem.get("contains_people") or 0)
    contains_animals = int(sem.get("contains_animals") or 0)
    is_document = int(sem.get("is_document_like") or 0)
    is_screenshot = int(sem.get("is_screenshot_like") or 0)
    is_landscape = int(sem.get("is_landscape_like") or 0)
    is_food = int(sem.get("is_food_like") or 0)

    ai_summary = ((meta.get("ai_summary") or {}).get("summary_text") or "").lower()
    dog_bonus = 0.0
    if any(term in ai_summary for term in ("dog", "puppy", "golden retriever", "great pyrenees", "pet")):
        dog_bonus = 0.05

    # Base score favors composition / quality when faces do not matter.
    scene_score = (
        technical * 0.16 +
        aesthetic * 0.26 +
        subject_prominence * 0.26 +
        semantic * 0.18 +
        face_score * 0.14
    )

    # People-photo mode heavily favors expressions once faces are prominent.
    people_score = (
        technical * 0.14 +
        aesthetic * 0.12 +
        subject_prominence * 0.08 +
        semantic * 0.06 +
        face_score * 0.08 +
        face_expression_score * 0.52
    )

    score = (scene_score * (1.0 - people_weight)) + (people_score * people_weight)

    note_parts = []
    if contains_people:
        score += 0.03
        note_parts.append("people")
    if contains_animals:
        score += 0.06
        score += subject_prominence * 0.10
        note_parts.append("animals")
        note_parts.append("animal_prominence")
    if dog_bonus > 0:
        score += dog_bonus
        note_parts.append("dog_summary_bonus")
    if is_landscape and people_weight < 0.25:
        score += 0.03
        note_parts.append("landscape")
    if is_food:
        score += 0.02
        note_parts.append("food")
    if people_weight >= 0.30:
        note_parts.append("people_mode")

    if technical < 0.25:
        score *= 0.55
        note_parts.append("low_tech_penalty")
    if is_document:
        score *= 0.45
        note_parts.append("document_penalty")
    if is_screenshot:
        score *= 0.35
        note_parts.append("screenshot_penalty")

    return {
        "score": round(_clamp01(score), 6),
        "technical_score": round(technical, 6),
        "aesthetic_score": round(aesthetic, 6),
        "subject_prominence_score": round(subject_prominence, 6),
        "semantic_score": round(semantic, 6),
        "face_score": round(face_score, 6),
        "face_expression_score": round(face_expression_score, 6),
        "people_weight": round(people_weight, 6),
        "note": ",".join(note_parts),
    }


def _cull_score(meta: dict[str, Any]) -> dict[str, Any]:
    technical = _safe_float((meta.get("technical") or {}).get("technical_score"), 0.0)
    aesthetic_meta = meta.get("aesthetic") or {}
    aesthetic = _safe_float(aesthetic_meta.get("overall_aesthetic_score"), 0.0)
    if aesthetic <= 0:
        aesthetic = _safe_float(aesthetic_meta.get("aesthetic_score"), 0.0)
    subject_prominence = _safe_float(aesthetic_meta.get("subject_prominence_score"), 0.0)
    semantic = _safe_float((meta.get("semantic") or {}).get("semantic_score"), 0.0)
    face_score = _face_score_from_meta(meta)
    face_expression_score = _face_expression_signal_from_meta(meta)
    people_weight = _people_weight_from_meta(meta)

    sem = meta.get("semantic") or {}
    contains_people = int(sem.get("contains_people") or 0)
    contains_animals = int(sem.get("contains_animals") or 0)
    is_document = int(sem.get("is_document_like") or 0)
    is_screenshot = int(sem.get("is_screenshot_like") or 0)

    ai_summary = ((meta.get("ai_summary") or {}).get("summary_text") or "").lower()
    dog_bonus = 0.0
    if contains_animals and any(term in ai_summary for term in ("dog", "puppy", "pet")):
        dog_bonus = 0.03

    # For culling, composition matters unless prominent faces exist.
    scene_score = (
        technical * 0.34 +
        aesthetic * 0.22 +
        subject_prominence * 0.24 +
        face_score * 0.12 +
        semantic * 0.08
    )

    # When prominent faces exist, the better expression should dominate.
    people_score = (
        technical * 0.18 +
        aesthetic * 0.12 +
        subject_prominence * 0.08 +
        semantic * 0.04 +
        face_score * 0.08 +
        face_expression_score * 0.50
    )

    score = (scene_score * (1.0 - people_weight)) + (people_score * people_weight)

    note_parts = []
    if contains_people:
        score += 0.02
        note_parts.append("people")
    if contains_animals:
        score += 0.04
        score += subject_prominence * 0.08
        note_parts.append("animals")
        note_parts.append("animal_prominence")
    if dog_bonus > 0:
        score += dog_bonus
        note_parts.append("dog_summary_bonus")
    if people_weight >= 0.30:
        note_parts.append("people_mode")

    if technical < 0.20:
        score *= 0.45
        note_parts.append("low_tech_penalty")
    if is_document:
        score *= 0.70
        note_parts.append("document_penalty")
    if is_screenshot:
        score *= 0.60
        note_parts.append("screenshot_penalty")

    return {
        "score": round(_clamp01(score), 6),
        "technical_score": round(technical, 6),
        "aesthetic_score": round(aesthetic, 6),
        "subject_prominence_score": round(subject_prominence, 6),
        "semantic_score": round(semantic, 6),
        "face_score": round(face_score, 6),
        "face_expression_score": round(face_expression_score, 6),
        "people_weight": round(people_weight, 6),
        "note": ",".join(note_parts),
    }

def _rank_sha1s_for_mode(store: "ArchiveStore", sha1_list: list[str], mode: str) -> list[dict[str, Any]]:

    technical = _safe_float((meta.get("technical") or {}).get("technical_score"), 0.0)
    aesthetic = _safe_float((meta.get("aesthetic") or {}).get("overall_aesthetic_score"), 0.0)
    if aesthetic <= 0:
        aesthetic = _safe_float((meta.get("aesthetic") or {}).get("aesthetic_score"), 0.0)
    semantic = _safe_float((meta.get("semantic") or {}).get("semantic_score"), 0.0)
    face_score = _face_score_from_meta(meta)

    sem = meta.get("semantic") or {}
    is_document = int(sem.get("is_document_like") or 0)
    is_screenshot = int(sem.get("is_screenshot_like") or 0)
    contains_people = int(sem.get("contains_people") or 0)

    score = (
        technical * 0.42 +
        aesthetic * 0.28 +
        face_score * 0.22 +
        semantic * 0.08
    )

    note_parts = []
    if contains_people:
        score += 0.03
        note_parts.append("people")
    if technical < 0.20:
        score *= 0.45
        note_parts.append("low_tech_penalty")
    if is_document:
        score *= 0.70
        note_parts.append("document_penalty")
    if is_screenshot:
        score *= 0.60
        note_parts.append("screenshot_penalty")

    return {
        "score": round(_clamp01(score), 6),
        "technical_score": round(technical, 6),
        "aesthetic_score": round(aesthetic, 6),
        "semantic_score": round(semantic, 6),
        "face_score": round(face_score, 6),
        "note": ",".join(note_parts),
    }


def _rank_sha1s_for_mode(store: "ArchiveStore", sha1_list: list[str], mode: str) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    seen = set()
    for sha1 in sha1_list:
        sha1 = str(sha1)
        if not sha1 or sha1 in seen:
            continue
        seen.add(sha1)
        meta = store.get_lightbox_metadata(sha1)
        if mode == "cull":
            breakdown = _cull_score(meta)
        else:
            breakdown = _interesting_score(meta)

        overview = meta.get("overview") or {}
        ranked.append({
            "sha1": sha1,
            "filename": overview.get("original_filename") or "",
            "score": breakdown["score"],
            "breakdown": breakdown,
        })

    ranked.sort(
        key=lambda item: (
            item["score"],
            item["breakdown"].get("face_score", 0.0),
            item["breakdown"].get("aesthetic_score", 0.0),
            item["breakdown"].get("technical_score", 0.0),
        ),
        reverse=True,
    )
    return ranked


def _parse_dt_for_cluster(value: Any) -> datetime | None:
    try:
        dt_str = str(value or "")
        if not dt_str or dt_str.startswith("0000"):
            return None
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _gps_bucket_key_for_cluster(item: dict[str, Any], precision: int = 4) -> tuple[float, float] | None:
    try:
        lat = float(item.get("latitude"))
        lon = float(item.get("longitude"))
        if abs(lat) <= 0.000001 and abs(lon) <= 0.000001:
            return None
        return (round(lat, precision), round(lon, precision))
    except Exception:
        return None



def _gps_distance_meters(item_a: dict[str, Any], item_b: dict[str, Any]) -> float | None:
    try:
        lat1 = float(item_a.get("latitude"))
        lon1 = float(item_a.get("longitude"))
        lat2 = float(item_b.get("latitude"))
        lon2 = float(item_b.get("longitude"))
    except Exception:
        return None

    # Equirectangular approximation; accurate enough at short distances.
    lat1r = math.radians(lat1)
    lon1r = math.radians(lon1)
    lat2r = math.radians(lat2)
    lon2r = math.radians(lon2)
    x = (lon2r - lon1r) * math.cos((lat1r + lat2r) / 2.0)
    y = (lat2r - lat1r)
    return 6371000.0 * math.sqrt(x * x + y * y)


def _find_clusters_in_items(
    items: list[dict[str, Any]],
    time_threshold_seconds: int = 15,
    gps_threshold_meters: float = 15.0,
    bridge_time_seconds: int = 5,
    bridge_cluster_span_seconds: int = 10,
) -> list[list[dict[str, Any]]]:
    # Current design:
    # 1) Only GPS-tagged photos participate in clustering debug mode.
    # 2) Sort by time.
    # 3) Same cluster if time delta <= threshold and distance <= threshold.
    # 4) Outlier bridge rule: if a photo is only a few seconds away from a very
    #    short existing cluster, allow one obvious GPS outlier to join.
    decorated: list[tuple[datetime, dict[str, Any]]] = []
    for item in items:
        if _gps_bucket_key_for_cluster(item) is None:
            continue
        dt = _parse_dt_for_cluster(item.get("final_dt"))
        if dt is None:
            continue
        decorated.append((dt, item))

    if not decorated:
        return []

    decorated.sort(key=lambda pair: pair[0])

    clusters: list[list[dict[str, Any]]] = []
    current = [decorated[0][1]]
    current_start_dt = decorated[0][0]
    prev_dt = decorated[0][0]

    for dt, item in decorated[1:]:
        prev_item = current[-1]
        delta = (dt - prev_dt).total_seconds()
        dist_m = _gps_distance_meters(prev_item, item)

        same_time = delta <= time_threshold_seconds
        same_place = (dist_m is not None and dist_m <= gps_threshold_meters)

        # GPS outlier bridge:
        # If we already have a tiny valid short-time run, allow one near-adjacent
        # outlier frame to join even when its GPS is bad.
        cluster_span = (prev_dt - current_start_dt).total_seconds()
        bridge_ok = (
            len(current) >= 2
            and delta <= bridge_time_seconds
            and cluster_span <= bridge_cluster_span_seconds
        )

        if same_time and (same_place or bridge_ok):
            current.append(item)
        else:
            if len(current) >= 2:
                clusters.append(current)
            current = [item]
            current_start_dt = dt

        prev_dt = dt

    if len(current) >= 2:
        clusters.append(current)

    return clusters

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


    @app.route("/api/select_by_formula", methods=["POST"])
    def select_by_formula():
        payload = request.json or {}
        sha1_list = payload.get("sha1_list") or []
        mode = str(payload.get("mode") or "interesting").strip().lower()
        keep_count = int(payload.get("keep_count") or 1)

        if not isinstance(sha1_list, list) or not sha1_list:
            return jsonify({"status": "error", "message": "sha1_list required"}), 400
        if mode not in {"interesting", "cull"}:
            return jsonify({"status": "error", "message": "invalid mode"}), 400
        if keep_count < 1:
            keep_count = 1

        store.load_cache()
        ranked = _rank_sha1s_for_mode(store, [str(x) for x in sha1_list], mode)
        winners = [item["sha1"] for item in ranked[:keep_count]]

        return jsonify({
            "status": "ok",
            "mode": mode,
            "keep_count": keep_count,
            "winners": winners,
            "ranked": ranked,
        })


    @app.route("/api/select_clusters", methods=["POST"])
    def select_clusters():
        payload = request.json or {}
        sha1_list = payload.get("sha1_list") or []
        if not isinstance(sha1_list, list) or not sha1_list:
            return jsonify({"status": "error", "message": "sha1_list required"}), 400

        store.load_cache()
        all_items = store.db_cache + store.undated_cache
        item_map = {str(item.get("sha1")): item for item in all_items}
        items = [item_map[str(sha1)] for sha1 in sha1_list if str(sha1) in item_map]

        clusters = _find_clusters_in_items(items, time_threshold_seconds=15)
        clustered_sha1s: list[str] = []
        cluster_debug: list[dict[str, Any]] = []

        for idx, cluster in enumerate(clusters, start=1):
            sha1s = [str(item.get("sha1")) for item in cluster]
            clustered_sha1s.extend(sha1s)
            first = cluster[0]
            last = cluster[-1]

            ranked = _rank_sha1s_for_mode(store, sha1s, "cull")
            primary_sha1 = ranked[0]["sha1"] if len(ranked) >= 1 else ""
            secondary_sha1 = ranked[1]["sha1"] if len(ranked) >= 2 else ""

            cluster_debug.append({
                "cluster_id": idx,
                "size": len(cluster),
                "gps_bucket": _gps_bucket_key_for_cluster(first),
                "start_time": str(first.get("final_dt") or ""),
                "end_time": str(last.get("final_dt") or ""),
                "primary_sha1": primary_sha1,
                "secondary_sha1": secondary_sha1,
                "sha1s": sha1s,
            })

        return jsonify({
            "status": "ok",
            "clustered_sha1s": clustered_sha1s,
            "clusters": cluster_debug,
        })

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
            card["comp_hash"], card["selected_heroes"] = store.get_composite_payload(card["id"], card["heroes"], max_images=16)

        manifests = {card["id"]: store.build_manifest(card.get("selected_heroes") or card["heroes"]) for card in cards}
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
            card["comp_hash"], card["selected_heroes"] = store.get_composite_payload(card["id"], card["heroes"], max_images=16)

        manifests = {card["id"]: store.build_manifest(card.get("selected_heroes") or card["heroes"]) for card in cards}
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
            card["comp_hash"], card["selected_heroes"] = store.get_composite_payload(card["id"], card["heroes"], max_images=16)

        manifests = {card["id"]: store.build_manifest(card.get("selected_heroes") or card["heroes"]) for card in cards}
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
            card["comp_hash"], card["selected_heroes"] = store.get_composite_payload(card["id"], card["heroes"], max_images=16)

        manifests = {card["id"]: store.build_manifest(card.get("selected_heroes") or card["heroes"]) for card in cards}
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
            card["comp_hash"], card["selected_heroes"] = store.get_composite_payload(card["id"], card["heroes"], max_images=16)

        manifests = {card["id"]: store.build_manifest(card.get("selected_heroes") or card["heroes"]) for card in cards}
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
        default=os.environ.get("LIFE_ARCHIVE_ROOT", r"C:\website-photos"),
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
