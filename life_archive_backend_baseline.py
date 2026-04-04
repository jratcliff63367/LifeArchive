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
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

from flask import Flask, jsonify, render_template_string, request, send_file, send_from_directory
from PIL import Image, ImageOps

from places_service import PlacesContext, PlacesService
from places_map_service import PlacesMapService


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

STASH_CATEGORIES = [
    {"key": "general", "label": "General", "dir": "_stash"},
    {"key": "game-art", "label": "Old Game Art", "dir": "_stash/game-art"},
    {"key": "screenshots", "label": "Screenshots", "dir": "_stash/screenshots"},
    {"key": "documents", "label": "Documents", "dir": "_stash/documents"},
    {"key": "zentangle", "label": "Zentangle", "dir": "_stash/zentangle"},
    {"key": "personal-artwork", "label": "Personal Artwork", "dir": "_stash/personal-artwork"},
    {"key": "museum", "label": "museum", "dir": "_stash/museum"},
    {"key": "Movies16mm", "label": "Moviews16mm", "dir": "_stash/movies-16mm"},
    {"key": "CarShow", "label": "CarShow", "dir": "_stash/carshow"},
    {"key": "449-Oxford-Lane", "label": "449-Oxford-Lane", "dir": "_stash/449-Oxford-Lane"},
    {"key": "445-Oxford-Lane", "label": "445-Oxford-Lane", "dir": "_stash/445-Oxford-Lane"},
    {"key": "Terrys", "label": "Terrys", "dir": "_stash/terrys"},
    {"key": "models", "label": "Models", "dir": "_stash/models"},
]
STASH_CATEGORY_MAP = {item["key"]: item["dir"] for item in STASH_CATEGORIES}

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
        .photo-card.keyboard-focus {
            border-color: #ffffff;
            box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.55), 0 0 0 5px rgba(187, 134, 252, 0.28);
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
            min-width: 250px;
            max-height: calc(100vh - 16px);
            border-radius: 8px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            padding: 6px 0;
            overflow: visible;
        }
        .menu-item {
            position: relative;
            padding: 12px 20px;
            cursor: pointer;
            font-size: 0.9em;
            font-weight: 700;
            transition: 0.1s;
            white-space: nowrap;
        }
        .menu-item:hover {
            background: var(--accent);
            color: #000;
        }
        .menu-item.hidden {
            display: none;
        }
        .menu-arrow {
            float: right;
            opacity: 0.8;
        }
        .submenu {
            display: none;
            position: absolute;
            top: 0;
            left: 100%;
            min-width: 220px;
            max-height: calc(100vh - 16px);
            overflow-y: auto;
            overflow-x: hidden;
            background: #222;
            border: 1px solid #444;
            border-radius: 8px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            padding: 6px 0;
            color: #e5e7eb;
        }

        .submenu .menu-item {
            color: #e5e7eb;
        }
        .menu-item.has-submenu:hover > .submenu {
            display: block;
        }
        .selection-menu-wrap {
            position: relative;
            display: inline-flex;
        }
        .selection-pop-menu {
            display: none;
            position: absolute;
            right: 0;
            bottom: calc(100% + 8px);
            min-width: 220px;
            background: rgba(20, 20, 20, 0.98);
            border: 1px solid #444;
            border-radius: 10px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            padding: 6px 0;
            z-index: 10005;
        }
        .selection-pop-menu.open {
            display: block;
        }
        .selection-pop-menu .menu-item {
            padding: 10px 14px;
        }
        .progress-dialog {
            position: fixed;
            right: 20px;
            bottom: 96px;
            width: 380px;
            background: rgba(20, 20, 20, 0.98);
            border: 1px solid #444;
            border-radius: 14px;
            box-shadow: 0 18px 46px rgba(0, 0, 0, 0.55);
            z-index: 10008;
            padding: 16px;
            display: none;
        }
        .progress-dialog.visible {
            display: block;
        }
        .progress-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 10px;
        }
        .progress-title {
            font-size: 0.95em;
            font-weight: 900;
            color: #fff;
            margin: 0;
        }
        .progress-close {
            background: transparent;
            border: 0;
            color: #aaa;
            font-size: 22px;
            cursor: pointer;
            line-height: 1;
        }
        .progress-close:hover {
            color: var(--accent);
        }
        .progress-subtitle {
            color: #cfcfcf;
            font-size: 0.85em;
            font-weight: 700;
            margin-bottom: 8px;
        }
        .progress-status {
            color: #999;
            font-size: 0.82em;
            min-height: 1.3em;
            margin-bottom: 12px;
        }
        .progress-bar-shell {
            width: 100%;
            height: 12px;
            border-radius: 999px;
            background: #101010;
            border: 1px solid #333;
            overflow: hidden;
            margin-bottom: 10px;
        }
        .progress-bar-fill {
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, var(--accent), #ffffff);
            transition: width 0.2s ease;
        }
        .progress-meta {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            color: #bbb;
            font-size: 0.78em;
            font-weight: 800;
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
        .page-action.places-action {
            border-color: rgba(187, 134, 252, 0.55);
            color: #e4d2ff;
            background: linear-gradient(180deg, rgba(120,70,170,0.35), rgba(35,25,50,0.85));
        }
        .page-action.places-action:hover, .page-action.places-action.active {
            border-color: #d7b9ff;
            color: #fff;
            box-shadow: 0 0 0 1px rgba(215,185,255,0.18) inset;
        }
        .places-layout {
            display: grid;
            grid-template-columns: 320px minmax(0, 1fr);
            gap: 22px;
            align-items: start;
            margin-bottom: 28px;
        }
        .places-sidebar, .places-main-panel {
            background: rgba(255,255,255,0.03);
            border: 1px solid #2f2f2f;
            border-radius: 18px;
        }
        .places-sidebar {
            padding: 18px 14px;
            position: sticky;
            top: 88px;
            max-height: calc(100vh - 120px);
            overflow: auto;
        }
        .places-sidebar-title {
            font-size: 0.82em;
            color: #b6b6b6;
            font-weight: 900;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin: 0 0 14px;
        }
        .places-tree { display: flex; flex-direction: column; gap: 6px; }
        .places-node { margin-left: calc(var(--depth, 0) * 10px); }
        .places-node-link {
            display: flex; align-items: center; gap: 8px;
            text-decoration: none; color: #ddd; padding: 9px 10px;
            border-radius: 10px; border: 1px solid transparent;
            background: rgba(255,255,255,0.01);
        }
        .places-node-link:hover { border-color: #3d3d3d; background: rgba(255,255,255,0.04); }
        .places-node.active > .places-node-link { border-color: var(--accent); color: #fff; background: rgba(187,134,252,0.13); }
        .places-node-icon { width: 18px; text-align: center; font-size: 0.95em; opacity: 0.9; }
        .places-node-label { flex: 1; font-weight: 700; font-size: 0.92em; }
        .places-node-count { color: #8e8e8e; font-size: 0.83em; font-weight: 700; }
        .places-node-children { margin-top: 6px; display: flex; flex-direction: column; gap: 4px; }
        .places-main-panel { padding: 18px; }
        .places-topbar { display:flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 18px; flex-wrap: wrap; }
        .places-kicker { font-size: 0.78em; color: #aaa; font-weight: 900; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 6px; }
        .places-selected-title { font-size: 1.6em; font-weight: 900; margin: 0 0 6px; }
        .places-selected-sub { color: #aaa; font-weight: 700; }
        .places-breadcrumb { color: #9e9e9e; font-size: 0.9em; display:flex; gap:8px; flex-wrap:wrap; }
        .places-breadcrumb span.sep { color:#666; }
        .places-stats { display:flex; gap: 12px; flex-wrap: wrap; }
        .places-stat { background: rgba(255,255,255,0.04); border:1px solid #313131; border-radius: 12px; padding: 10px 12px; min-width: 120px; }
        .places-stat .n { display:block; font-size:1.15em; font-weight:900; color:#fff; }
        .places-stat .l { display:block; color:#999; font-size:0.78em; font-weight:800; text-transform:uppercase; letter-spacing:0.06em; }
        .places-gallery { display:grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 14px; margin-bottom: 18px; }
        .places-gallery-card { display:block; text-decoration:none; color:inherit; background: rgba(0,0,0,0.18); border:1px solid #2f2f2f; border-radius: 14px; overflow:hidden; }
.places-gallery-card:hover { border-color: var(--accent); }
        .places-gallery-card img { width:100%; aspect-ratio: 4/3; object-fit: cover; display:block; background:#080808; }
        .places-gallery-meta { padding: 10px 12px 12px; }
        .places-gallery-title { font-size:0.82em; font-weight:800; color:#cfcfcf; text-transform: uppercase; letter-spacing:0.04em; }
        .places-gallery-sub { margin-top: 4px; color:#8f8f8f; font-size:0.8em; font-weight:700; }
        .places-all-card { margin-bottom: 24px; border: 4px solid rgba(225,198,255,0.98); border-radius: 18px; overflow: hidden; background: linear-gradient(180deg, rgba(122,92,255,0.22), rgba(18,14,28,0.97)); box-shadow: 0 24px 52px rgba(0,0,0,0.44), 0 0 0 1px rgba(255,255,255,0.06) inset; }
        .places-all-card-head { display:flex; justify-content: space-between; align-items:flex-start; gap: 16px; padding: 16px 18px 12px; flex-wrap: wrap; }
        .places-all-kicker { color: var(--accent); font-size: 0.72em; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 6px; }
        .places-all-title { color:#fff; font-size:1.12em; font-weight:900; text-decoration:none; display:inline-block; }
        .places-all-sub { color:#a8a8a8; font-size:0.92em; font-weight:700; margin-top: 4px; }
        .places-all-actions { display:flex; gap: 10px; flex-wrap: wrap; }
        .places-all-btn { display:inline-flex; align-items:center; gap:8px; padding: 10px 14px; border-radius: 12px; text-decoration:none; font-weight:800; font-size:0.84em; }
        .places-all-btn.primary { background: var(--accent); color:#fff; }
        .places-all-btn.secondary { background: rgba(255,255,255,0.04); color:#d9d9d9; border:1px solid #2f2f2f; }
        .places-all-grid-shell { display:block; width:100%; max-width:100%; margin: 0 18px 18px; padding: 14px; border-radius: 16px; border: 4px solid rgba(230,210,255,0.98); background: #303030; box-shadow: 0 0 0 2px rgba(0,0,0,0.46) inset, 0 18px 36px rgba(0,0,0,0.34); }
        .places-all-grid { display:grid; grid-template-columns: repeat(var(--places-all-cols, 4), minmax(0, 1fr)); gap: 8px; width:100%; }
        .places-all-grid-link { display:block; text-decoration:none; }
        .places-all-grid img { width:100%; aspect-ratio: 1/1; object-fit: cover; display:block; border-radius: 10px; background:#101010; border: 2px solid rgba(255,255,255,0.20); box-shadow: 0 0 0 1px rgba(255,255,255,0.06) inset; transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease; }
        .places-all-grid-link:hover img { border-color: rgba(215,185,255,0.98); box-shadow: 0 10px 24px rgba(0,0,0,0.40), 0 0 0 1px rgba(255,255,255,0.10) inset; transform: translateY(-1px); }
        @media (max-width: 900px) { .places-all-grid { width:100%; } }
        @media (max-width: 640px) { .places-all-grid { grid-template-columns: repeat(min(var(--places-all-cols, 4), 2), minmax(0, 1fr)); width: 100%; } .places-all-grid-shell { display:block; } }
        .places-leaf-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 14px; margin-bottom: 18px; }
        .places-leaf-card { text-decoration:none; color:inherit; background: rgba(255,255,255,0.03); border:1px solid #313131; border-radius:14px; overflow:hidden; }
        .places-leaf-card:hover { border-color: var(--accent); }
        .places-leaf-card img { width:100%; aspect-ratio: 16/10; object-fit: cover; display:block; background:#0a0a0a; }
        .places-leaf-body { padding: 12px; }
        .places-leaf-title { font-weight:800; margin-bottom:4px; }
        .places-leaf-sub { color:#999; font-size:0.84em; font-weight:700; }
        .places-map-card { position: relative; border-radius: 16px; overflow: hidden; border: 1px solid #31405f; background: #08101d; min-height: 460px; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.03); }
        .places-map-surface { position: relative; width: 100%; min-height: 460px; background: #0b1220; }
        .places-real-map { position: relative; width: 100%; height: 460px; overflow: hidden; background: #0c1422; cursor: grab; }
        .places-map-tile-layer { position: absolute; inset: 0; overflow: hidden; }
        .places-map-tile { position: absolute; width: 256px; height: 256px; user-select: none; -webkit-user-drag: none; }
        .places-map-grid-fallback {
            position: absolute; inset: 0;
            background:
                linear-gradient(rgba(255,255,255,0.06) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,255,255,0.06) 1px, transparent 1px),
                linear-gradient(180deg, #15263f 0%, #0a1425 100%);
            background-size: 64px 64px, 64px 64px, 100% 100%;
        }
        .places-map-no-tiles {
            position: absolute; top: 18px; left: 18px; z-index: 6;
            background: rgba(8,12,18,0.88); color: #d8e5ff;
            border: 1px solid rgba(97,121,164,0.65);
            border-radius: 12px; padding: 10px 12px; max-width: 360px;
            font-size: 0.86em; line-height: 1.35;
            box-shadow: 0 10px 28px rgba(0,0,0,0.28);
        }
        .places-map-no-tiles code { color: #fff; }
        .places-map-marker-layer { position: absolute; inset: 0; pointer-events: none; }
        .places-map-marker { position: absolute; transform: translate(-50%, -50%); border-radius: 999px; }
        .places-map-marker.child {
            width: 12px; height: 12px;
            background: rgba(185, 214, 255, 0.92);
            border: 2px solid rgba(11,16,24,0.95);
            box-shadow: 0 0 0 1px rgba(255,255,255,0.12);
        }
        .places-map-marker.selected {
            width: 18px; height: 18px;
            background: #ff6a6a;
            border: 4px solid #fff;
            box-shadow: 0 0 0 10px rgba(255,106,106,0.20), 0 0 0 22px rgba(255,106,106,0.08);
        }
        .places-map-mini { position:absolute; top:18px; left:18px; width: 180px; height: 110px; border-radius: 12px; overflow: hidden; border: 1px solid rgba(97,121,164,0.65); background: rgba(6,10,16,0.55); box-shadow: 0 10px 28px rgba(0,0,0,0.28); z-index: 4; backdrop-filter: blur(4px); pointer-events: none; }
        .places-map-mini svg { width:100%; height:100%; display:block; }
        .places-map-badge { position:absolute; left:20px; top:136px; background: rgba(9,12,18,0.84); border: 1px solid rgba(97,121,164,0.55); color:#cfe0ff; border-radius: 999px; padding: 7px 11px; font-size: 0.78em; font-weight: 800; z-index:6; }
        .places-map-controls { position: absolute; top: 16px; right: 16px; display: flex; gap: 8px; z-index: 6; }
        .places-map-btn { border: 1px solid #516791; background: rgba(10,16,27,0.90); color: #fff; border-radius: 10px; width: 40px; height: 40px; font-size: 1.2em; font-weight: 800; cursor: pointer; box-shadow: 0 8px 20px rgba(0,0,0,0.28); }
        .places-map-btn:hover { background: rgba(24,30,42,0.95); }
        .places-map-overlay { position:absolute; left:20px; bottom:20px; background: rgba(9,12,18,0.92); border:1px solid #3d4e70; border-radius: 14px; padding: 16px 18px; min-width: 250px; z-index: 6; box-shadow: 0 14px 40px rgba(0,0,0,0.30); }
        .places-map-title { font-size:1.45em; font-weight:900; margin:0 0 4px; }
        .places-map-sub { color:#b0b0b0; font-size:0.95em; font-weight:700; }
        .places-map-meta { margin-top: 8px; color: #9eb4d8; font-size: 0.82em; font-weight: 700; letter-spacing: 0.01em; }
        .places-map-hint { position:absolute; right:20px; bottom:18px; color: rgba(255,255,255,0.65); font-size: 0.78em; font-weight:700; z-index:6; text-shadow: 0 1px 1px rgba(0,0,0,0.35); }
        .places-mini-world-bg { fill: url(#places-mini-ocean); }
        .places-mini-land { fill: rgba(118,165,102,0.78); stroke: rgba(255,255,255,0.12); stroke-width: 1; }
        .places-mini-marker { fill: #ff7c7c; stroke: #fff; stroke-width: 3; }
        .places-empty { color:#aaa; padding: 18px; background: rgba(255,255,255,0.03); border: 1px dashed #383838; border-radius: 14px; }
        @media (max-width: 1100px) {
            .places-layout { grid-template-columns: 1fr; }
            .places-sidebar { position: static; max-height: none; }
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
        <div class="menu-item" data-role="cull-select" onclick="startCullSelectFromContext()">Cull Select</div>
        <div class="menu-item" data-role="cull-move" onclick="startCullMoveFromContext()">Cull Move</div>
        <div class="menu-item" data-role="trash" onclick="moveContextTo('trash')">Move to Trash</div>
        <div class="menu-item has-submenu" data-role="stash">Stash <span class="menu-arrow">▸</span>
            <div class="submenu">
                {% for stash in stash_categories %}
                <div class="menu-item" onclick="moveContextToStash('{{ stash.key }}')">{{ stash.label }}</div>
                {% endfor %}
            </div>
        </div>
        <div class="menu-item hidden" data-role="rotate-cw" onclick="rotateImage(90)">Rotate 90° Clockwise ↻</div>
        <div class="menu-item hidden" data-role="rotate-ccw" onclick="rotateImage(270)">Rotate 90° Counter ↺</div>
        <div class="menu-item has-submenu hidden" data-role="debug">Debug <span class="menu-arrow">▸</span>
            <div class="submenu">
                <div class="menu-item" onclick="showClusters()">Show Clusters</div>
                <div class="menu-item" onclick="selectByFormula('interesting', 1)">Select Most Interesting Picture</div>
                <div class="menu-item" onclick="selectByFormula('cull', 1)">Select Best Picture for Culling</div>
                <div class="menu-item" onclick="selectByFormula('cull', 2)">Select Best Two Pictures for Culling</div>
            </div>
        </div>
    </div>

    <div class="nav-bar">
        <a href="/timeline" class="{{ 'active' if active_tab == 'timeline' else '' }}">Timeline</a>
        <a href="/undated" class="{{ 'active' if active_tab == 'undated' else '' }}">Undated</a>
        <a href="/folder" class="{{ 'active' if active_tab == 'file' else '' }}">Explorer</a>
        <a href="/tags" class="{{ 'active' if active_tab == 'tags' else '' }}">Tags</a>
        <a href="/places" class="{{ 'active' if active_tab == 'places' else '' }}">Places</a>
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
                <button type="button" class="page-action {{ action.classes or '' }} {{ 'active' if action.active else '' }}" onclick="{{ action.onclick }}">{{ action.label }}</button>
                {% else %}
                <a href="{{ action.url }}" class="page-action {{ action.classes or '' }} {{ 'active' if action.active else '' }}">{{ action.label }}</a>
                {% endif %}
            {% endfor %}
        </div>
        {% endif %}

        {% if places_view %}
        <div class="places-layout">
            <div class="places-sidebar">
                <div class="places-sidebar-title">Places in this context</div>
                {{ places_view.sidebar_html | safe }}
            </div>
            <div class="places-main-panel">
                <div class="places-topbar">
                    <div>
                        <div class="places-kicker">{{ places_view.context.title }}</div>
                        {% if places_view.selected_node %}
                        <h2 class="places-selected-title">{{ places_view.selected_node.label }}</h2>
                        <div class="places-selected-sub">{{ places_view.selected_node.photo_count }} geotagged photo{{ '' if places_view.selected_node.photo_count == 1 else 's' }}</div>
                        {% else %}
                        <h2 class="places-selected-title">No geotagged places found</h2>
                        {% endif %}
                        {% if places_view.selected_path %}
                        <div class="places-breadcrumb">
                            {% for node in places_view.selected_path %}
                                {% if not loop.first %}<span class="sep">/</span>{% endif %}
                                <span>{{ node.label }}</span>
                            {% endfor %}
                        </div>
                        {% endif %}
                    </div>
                    <div class="places-stats">
                        <div class="places-stat"><span class="n">{{ places_view.stats.geotagged_count }}</span><span class="l">Geotagged</span></div>
                        <div class="places-stat"><span class="n">{{ places_view.stats.leaf_count }}</span><span class="l">Leaf places</span></div>
                    </div>
                </div>

                {% if places_view.leaf_cards %}
                <div class="places-leaf-grid">
                    {% for leaf in places_view.leaf_cards %}
                    <a class="places-leaf-card" href="?node={{ leaf.node_q }}">
                        {% if leaf.cover_sha1 %}<img src="/thumbs/{{ leaf.cover_sha1 }}.jpg" loading="lazy">{% endif %}
                        <div class="places-leaf-body">
                            <div class="places-leaf-title">{{ leaf.label }}</div>
                            <div class="places-leaf-sub">{{ leaf.photo_count }} photo{{ '' if leaf.photo_count == 1 else 's' }}</div>
                        </div>
                    </a>
                    {% endfor %}
                </div>
                {% endif %}

                {% if places_view.all_place_card %}
                <div class="places-all-card">
                    <div class="places-all-card-head">
                        <div>
                            <div class="places-all-kicker">All Photos</div>
                            <a class="places-all-title" href="{{ places_view.all_place_card.primary_href }}">{{ places_view.all_place_card.title }}</a>
                            <div class="places-all-sub">{{ places_view.all_place_card.subtitle }}</div>
                        </div>
                        <div class="places-all-actions">
                            <a class="places-all-btn primary" href="{{ places_view.all_place_card.thumb_href }}">Browse Thumbnails</a>
                        </div>
                    </div>
                    {% if places_view.all_place_card.cover_items %}
                    <div class="places-all-grid-shell" style="--places-all-cols: {{ 4 if (places_view.all_place_card.cover_items|length) >= 4 else (places_view.all_place_card.cover_items|length if (places_view.all_place_card.cover_items|length) > 0 else 1) }}; {% set _cover_len = places_view.all_place_card.cover_items|length %}{% if _cover_len < 4 %}max-width: calc({{ _cover_len if _cover_len > 0 else 1 }} * 150px + {{ (_cover_len - 1) if _cover_len > 0 else 0 }} * 8px + 28px);{% endif %}">
                        <div class="places-all-grid">
                            {% for cp in places_view.all_place_card.cover_items[:16] %}
                            <a class="places-all-grid-link" href="#" onclick="event.preventDefault(); openLB({{ cp.lightbox_index if cp.lightbox_index is defined else loop.index0 }}, '{{ places_view.all_place_card.manifest_key }}');">
                                <img src="/thumbs/{{ cp.sha1 }}.jpg" loading="lazy">
                            </a>
                            {% endfor %}
                        </div>
                    </div>
                    {% endif %}
                </div>
                {% endif %}

                {% if places_view.gallery_items %}
                <div class="places-gallery">
                    {% for p in places_view.gallery_items %}
                    <div class="places-gallery-card">
                        <a class="places-gallery-image-link" href="{{ p._places_href if p._places_href is defined else ('/thumbs/' ~ p.sha1 ~ '.jpg') }}">
                            <img src="/thumbs/{{ p.sha1 }}.jpg" loading="lazy">
                        </a>
                        <div class="places-gallery-meta">
                            <a class="places-gallery-title" href="{{ p._places_group_href if p._places_group_href is defined else (p._places_href if p._places_href is defined else ('/thumbs/' ~ p.sha1 ~ '.jpg')) }}">{{ p._places_title if p._places_title is defined else (p._month_name if p._month_name is defined else 'Photo') }}</a>
                            {% if p._places_subtitle is defined %}<div class="places-gallery-sub">{{ p._places_subtitle }}</div>{% endif %}
                        </div>
                    </div>
                    {% endfor %}
                </div>
                {% endif %}

                <div class="places-map-card">
                    <div class="places-map-surface">
                        <div class="places-real-map" id="places-real-map">
                            <div class="places-map-grid-fallback"></div>
                            <div class="places-map-tile-layer" id="places-map-tile-layer"></div>
                            <div class="places-map-marker-layer" id="places-map-marker-layer"></div>
                            {% if not places_map_view.tiles_available %}
                            <div class="places-map-no-tiles">
                                <strong>Local map tiles not found.</strong><br>
                                Put tiles under <code>_web_layout/map_tiles/{z}/{x}/{y}.png</code> inside your archive root.
                            </div>
                            {% endif %}
                        </div>
                        <div class="places-map-mini"><svg id="places-map-mini" viewBox="0 0 1000 500" preserveAspectRatio="xMidYMid meet"></svg></div>
                        <div class="places-map-badge">{{ places_map_view.level if places_map_view else 'place' }}</div>
                        <div class="places-map-controls">
                            <button type="button" class="places-map-btn" data-map-action="zoom-in" aria-label="Zoom in">+</button>
                            <button type="button" class="places-map-btn" data-map-action="zoom-out" aria-label="Zoom out">−</button>
                            <button type="button" class="places-map-btn" data-map-action="reset" aria-label="Reset map">⟳</button>
                        </div>
                        <div class="places-map-hint">Drag to pan • Wheel to zoom{% if places_map_view and places_map_view.child_markers %} • {{ places_map_view.child_markers|length }} child markers{% endif %}</div>
                    </div>
                    <div class="places-map-overlay">
                        <div class="places-map-title">{{ places_map_view.title if places_map_view else (places_view.selected_node.label if places_view.selected_node else 'Places') }}</div>
                        <div class="places-map-sub">{{ places_map_view.subtitle if places_map_view else (places_view.selected_node.photo_count if places_view.selected_node else 0) }}</div>
                        {% if places_map_view and places_map_view.coord_text %}
                        <div class="places-map-meta">{{ places_map_view.coord_text }}</div>
                        {% endif %}
                    </div>
                </div>
                {% if places_map_view %}
                <script>
                (function() {
                    if (window.__placesRealLocalMapInit) return;
                    window.__placesRealLocalMapInit = true;

                    const cfg = {{ places_map_view | tojson | safe }};
                    const mapEl = document.getElementById('places-real-map');
                    const tileLayer = document.getElementById('places-map-tile-layer');
                    const markerLayer = document.getElementById('places-map-marker-layer');
                    const miniSvg = document.getElementById('places-map-mini');
                    if (!mapEl || !tileLayer || !markerLayer || !cfg) return;

                    const TILE_SIZE = 256;
                    const MIN_ZOOM = 1;
                    const MAX_ZOOM = 15;

                    function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
                    function lonLatToGlobalPixels(lon, lat, zoom) {
                        const scale = TILE_SIZE * Math.pow(2, zoom);
                        const x = (Number(lon) + 180) / 360 * scale;
                        const sin = Math.sin(Number(lat) * Math.PI / 180);
                        const y = (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * scale;
                        return { x, y };
                    }
                    let zoom = clamp(Math.round(Number(cfg.zoom || 3)), MIN_ZOOM, MAX_ZOOM);
                    let center = lonLatToGlobalPixels(cfg.center_lon || 0, cfg.center_lat || 0, zoom);
                    const initialState = { zoom, centerX: center.x, centerY: center.y };
                    let dragging = false;
                    let lastX = 0;
                    let lastY = 0;

                    function tileUrl(z, x, y) {
                        return String(cfg.tile_url_template || '/map_tiles/{z}/{x}/{y}.png').replace('{z}', String(z)).replace('{x}', String(x)).replace('{y}', String(y));
                    }
                    function drawMini() {
                        if (!miniSvg) return;
                        while (miniSvg.firstChild) miniSvg.removeChild(miniSvg.firstChild);
                        const NS = 'http://www.w3.org/2000/svg';
                        const make = (tag, attrs) => {
                            const el = document.createElementNS(NS, tag);
                            Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, String(v)));
                            return el;
                        };
                        const defs = make('defs', {});
                        const grad = make('linearGradient', { id: 'places-mini-ocean', x1: '0%', y1: '0%', x2: '0%', y2: '100%' });
                        grad.appendChild(make('stop', { offset: '0%', 'stop-color': '#183459' }));
                        grad.appendChild(make('stop', { offset: '100%', 'stop-color': '#0a1425' }));
                        defs.appendChild(grad);
                        miniSvg.appendChild(defs);
                        miniSvg.appendChild(make('rect', { x: 0, y: 0, width: 1000, height: 500, class: 'places-mini-world-bg' }));
                        const land = [[[-168,72],[-150,68],[-140,60],[-130,55],[-126,49],[-123,42],[-118,34],[-111,29],[-104,24],[-97,20],[-90,18],[-84,20],[-80,25],[-75,33],[-69,44],[-61,49],[-57,54],[-63,60],[-78,70],[-110,72],[-135,73]],[[-82,12],[-76,8],[-72,0],[-70,-10],[-66,-20],[-62,-31],[-58,-41],[-52,-50],[-44,-54],[-40,-45],[-44,-30],[-50,-18],[-58,-5],[-66,3],[-74,9]],[[-10,35],[-6,43],[2,49],[10,55],[20,60],[35,63],[50,64],[65,62],[85,58],[105,54],[122,48],[135,42],[145,35],[150,24],[144,15],[130,8],[118,12],[108,18],[96,22],[82,22],[70,18],[60,14],[48,18],[38,24],[28,30],[18,38],[8,44],[-2,43],[-8,39]],[[-18,36],[-6,37],[6,35],[18,30],[27,23],[33,12],[37,2],[36,-10],[30,-20],[24,-28],[16,-34],[8,-35],[0,-30],[-6,-18],[-10,-5],[-13,8],[-16,20]],[[112,-11],[120,-18],[132,-24],[142,-30],[151,-33],[154,-40],[147,-43],[136,-39],[126,-33],[118,-26],[113,-18]],[[47,-13],[50,-18],[49,-23],[46,-25],[43,-20],[44,-15]]];
                        function proj(lon, lat) { return [((Number(lon)+180)/360)*1000, ((90-Number(lat))/180)*500]; }
                        function path(points) { const c = points.map(([lon, lat]) => proj(lon, lat)); return 'M ' + c.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(' L ') + ' Z'; }
                        land.forEach(shape => miniSvg.appendChild(make('path', { d: path(shape), class: 'places-mini-land' })));
                        if (cfg.marker_lat != null && cfg.marker_lon != null) {
                            const [x, y] = proj(cfg.marker_lon, cfg.marker_lat);
                            miniSvg.appendChild(make('circle', { cx: x, cy: y, r: 8, class: 'places-mini-marker' }));
                        }
                    }
                    function render() {
                        const width = mapEl.clientWidth;
                        const height = mapEl.clientHeight;
                        const worldTiles = Math.pow(2, zoom);
                        const worldPx = TILE_SIZE * worldTiles;
                        center.x = ((center.x % worldPx) + worldPx) % worldPx;
                        center.y = clamp(center.y, 0, worldPx);
                        const leftPx = center.x - width / 2;
                        const topPx = center.y - height / 2;
                        const startX = Math.floor(leftPx / TILE_SIZE);
                        const endX = Math.floor((leftPx + width) / TILE_SIZE);
                        const startY = Math.floor(topPx / TILE_SIZE);
                        const endY = Math.floor((topPx + height) / TILE_SIZE);
                        tileLayer.innerHTML = '';
                        for (let tx = startX; tx <= endX; tx++) {
                            for (let ty = startY; ty <= endY; ty++) {
                                if (ty < 0 || ty >= worldTiles) continue;
                                const wrappedX = ((tx % worldTiles) + worldTiles) % worldTiles;
                                const img = document.createElement('img');
                                img.className = 'places-map-tile';
                                img.alt = '';
                                img.draggable = false;
                                img.loading = 'lazy';
                                img.src = tileUrl(zoom, wrappedX, ty);
                                img.style.left = `${tx * TILE_SIZE - leftPx}px`;
                                img.style.top = `${ty * TILE_SIZE - topPx}px`;
                                tileLayer.appendChild(img);
                            }
                        }
                        markerLayer.innerHTML = '';
                        const children = Array.isArray(cfg.child_markers) ? cfg.child_markers : [];
                        for (const child of children) {
                            const pt = lonLatToGlobalPixels(child.lon, child.lat, zoom);
                            let px = pt.x - leftPx; let py = pt.y - topPx;
                            while (px < -TILE_SIZE) px += worldPx;
                            while (px > width + TILE_SIZE) px -= worldPx;
                            if (py < -24 || py > height + 24) continue;
                            const el = document.createElement('div');
                            el.className = 'places-map-marker child';
                            el.title = `${child.label} (${child.photo_count})`;
                            el.style.left = `${px}px`; el.style.top = `${py}px`;
                            markerLayer.appendChild(el);
                        }
                        if (cfg.marker_lat != null && cfg.marker_lon != null) {
                            const pt = lonLatToGlobalPixels(cfg.marker_lon, cfg.marker_lat, zoom);
                            let px = pt.x - leftPx; let py = pt.y - topPx;
                            while (px < -TILE_SIZE) px += worldPx;
                            while (px > width + TILE_SIZE) px -= worldPx;
                            const el = document.createElement('div');
                            el.className = 'places-map-marker selected';
                            el.style.left = `${px}px`; el.style.top = `${py}px`;
                            markerLayer.appendChild(el);
                        }
                    }
                    function reset() { zoom = initialState.zoom; center = { x: initialState.centerX, y: initialState.centerY }; render(); }
                    function zoomBy(delta) {
                        const oldZoom = zoom; const newZoom = clamp(oldZoom + delta, MIN_ZOOM, MAX_ZOOM);
                        if (newZoom === oldZoom) return;
                        const factor = Math.pow(2, newZoom - oldZoom);
                        center = { x: center.x * factor, y: center.y * factor };
                        zoom = newZoom; render();
                    }
                    mapEl.addEventListener('mousedown', (ev) => { dragging = true; lastX = ev.clientX; lastY = ev.clientY; });
                    window.addEventListener('mousemove', (ev) => {
                        if (!dragging) return;
                        center.x -= (ev.clientX - lastX); center.y -= (ev.clientY - lastY);
                        lastX = ev.clientX; lastY = ev.clientY; render();
                    });
                    window.addEventListener('mouseup', () => { dragging = false; });
                    mapEl.addEventListener('wheel', (ev) => { ev.preventDefault(); zoomBy(ev.deltaY < 0 ? 1 : -1); }, { passive: false });
                    document.querySelectorAll('[data-map-action]').forEach((btn) => {
                        btn.addEventListener('click', () => {
                            const action = btn.getAttribute('data-map-action');
                            if (action === 'zoom-in') zoomBy(1);
                            else if (action === 'zoom-out') zoomBy(-1);
                            else if (action === 'reset') reset();
                        });
                    });
                    drawMini(); render(); window.addEventListener('resize', render);
                })();
                </script>
                {% endif %}
                    </div>
                </div>
                {% if places_map_view %}
                <script>
                (function() {
                    if (window.__placesMapInitV2) return;
                    window.__placesMapInitV2 = true;
                    const cfg = {{ places_map_view | tojson | safe }};
                    const svg = document.getElementById('places-map-canvas');
                    const mini = document.getElementById('places-map-mini');
                    if (!svg || !cfg) return;
                    const NS = 'http://www.w3.org/2000/svg';
                    const WIDTH = 1000;
                    const HEIGHT = 500;
                    const FOCAL_X = WIDTH * 0.50;
                    const FOCAL_Y = HEIGHT * 0.46;
                    function project(lon, lat) {
                        const x = ((Number(lon) + 180) / 360) * WIDTH;
                        const y = ((90 - Number(lat)) / 180) * HEIGHT;
                        return [x, y];
                    }
                    function make(tag, attrs) {
                        const el = document.createElementNS(NS, tag);
                        for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
                        return el;
                    }
                    function pathFrom(points) {
                        const coords = points.map(([lon, lat]) => project(lon, lat));
                        let d = `M ${coords[0][0].toFixed(1)} ${coords[0][1].toFixed(1)}`;
                        for (let i = 1; i < coords.length; i++) d += ` L ${coords[i][0].toFixed(1)} ${coords[i][1].toFixed(1)}`;
                        return d + ' Z';
                    }
                    function addLabel(layer, text, lon, lat, cls='places-map-label') {
                        const [x, y] = project(lon, lat);
                        const t = make('text', { x, y, class: cls, 'text-anchor': 'middle' });
                        t.textContent = text;
                        layer.appendChild(t);
                    }
                    const landShapes = [
                        [[-168,72],[-150,68],[-140,60],[-130,55],[-126,49],[-123,42],[-118,34],[-111,29],[-104,24],[-97,20],[-90,18],[-84,20],[-80,25],[-75,33],[-69,44],[-61,49],[-57,54],[-63,60],[-78,70],[-110,72],[-135,73]],
                        [[-82,12],[-76,8],[-72,0],[-70,-10],[-66,-20],[-62,-31],[-58,-41],[-52,-50],[-44,-54],[-40,-45],[-44,-30],[-50,-18],[-58,-5],[-66,3],[-74,9]],
                        [[-10,35],[-6,43],[2,49],[10,55],[20,60],[35,63],[50,64],[65,62],[85,58],[105,54],[122,48],[135,42],[145,35],[150,24],[144,15],[130,8],[118,12],[108,18],[96,22],[82,22],[70,18],[60,14],[48,18],[38,24],[28,30],[18,38],[8,44],[-2,43],[-8,39]],
                        [[-18,36],[-6,37],[6,35],[18,30],[27,23],[33,12],[37,2],[36,-10],[30,-20],[24,-28],[16,-34],[8,-35],[0,-30],[-6,-18],[-10,-5],[-13,8],[-16,20]],
                        [[112,-11],[120,-18],[132,-24],[142,-30],[151,-33],[154,-40],[147,-43],[136,-39],[126,-33],[118,-26],[113,-18]],
                        [[-54,60],[-46,64],[-38,68],[-30,72],[-24,76],[-26,80],[-40,82],[-50,78],[-56,72]],
                        [[47,-13],[50,-18],[49,-23],[46,-25],[43,-20],[44,-15]],
                        [[-180,-72],[-150,-74],[-120,-76],[-90,-77],[-60,-78],[-30,-79],[0,-79],[30,-79],[60,-78],[90,-77],[120,-76],[150,-74],[180,-72],[180,-85],[-180,-85]],
                    ];
                    const continentLabels = [['North America', -106, 47],['South America', -60, -18],['Europe', 16, 52],['Africa', 18, 10],['Asia', 92, 42],['Australia', 134, -26]];
                    const waterLabels = [['Atlantic Ocean', -32, 12],['Pacific Ocean', -150, 4],['Pacific Ocean', 156, 0],['Indian Ocean', 82, -18]];
                    function drawWorld(targetSvg, includeLabels) {
                        while (targetSvg.firstChild) targetSvg.removeChild(targetSvg.firstChild);
                        const defs = make('defs', {});
                        const oceanGrad = make('linearGradient', { id: targetSvg.id + '-ocean', x1: '0%', y1: '0%', x2: '0%', y2: '100%' });
                        oceanGrad.appendChild(make('stop', { offset: '0%', 'stop-color': '#183459' }));
                        oceanGrad.appendChild(make('stop', { offset: '100%', 'stop-color': '#0a1425' }));
                        defs.appendChild(oceanGrad);
                        targetSvg.appendChild(defs);
                        const root = make('g', {});
                        const viewport = make('g', {});
                        root.appendChild(viewport);
                        viewport.appendChild(make('rect', {x:0,y:0,width:WIDTH,height:HEIGHT,fill:'url(#' + targetSvg.id + '-ocean)'}));
                        for (let lon = -150; lon <= 180; lon += 30) {
                            const p1 = project(lon, -85), p2 = project(lon, 85);
                            viewport.appendChild(make('line', {x1:p1[0],y1:p1[1],x2:p2[0],y2:p2[1],stroke:'rgba(255,255,255,0.10)','stroke-width':1}));
                        }
                        for (let lat = -60; lat <= 60; lat += 30) {
                            const p1 = project(-180, lat), p2 = project(180, lat);
                            viewport.appendChild(make('line', {x1:p1[0],y1:p1[1],x2:p2[0],y2:p2[1],stroke:'rgba(255,255,255,0.10)','stroke-width':1}));
                        }
                        const landLayer = make('g', {});
                        viewport.appendChild(landLayer);
                        for (const shape of landShapes) landLayer.appendChild(make('path', {d:pathFrom(shape), fill:'#3e6743', stroke:'rgba(255,255,255,0.22)', 'stroke-width':1.4}));
                        if (includeLabels) {
                            const labelLayer = make('g', {});
                            viewport.appendChild(labelLayer);
                            continentLabels.forEach(v => addLabel(labelLayer, v[0], v[1], v[2]));
                            waterLabels.forEach(v => addLabel(labelLayer, v[0], v[1], v[2], 'places-map-label water'));
                        }
                        const crosshair = make('g', {});
                        crosshair.appendChild(make('line', {x1:FOCAL_X - 18, y1:FOCAL_Y, x2:FOCAL_X + 18, y2:FOCAL_Y, class:'places-map-crosshair'}));
                        crosshair.appendChild(make('line', {x1:FOCAL_X, y1:FOCAL_Y - 18, x2:FOCAL_X, y2:FOCAL_Y + 18, class:'places-map-crosshair'}));
                        crosshair.appendChild(make('circle', {cx:FOCAL_X, cy:FOCAL_Y, r:46, class:'places-map-focus-ring'}));
                        root.appendChild(crosshair);
                        const markerLayer = make('g', {});
                        viewport.appendChild(markerLayer);

                        const children = Array.isArray(cfg.child_markers) ? cfg.child_markers : [];
                        for (const child of children) {
                            const pt = project(child.lon, child.lat);
                            markerLayer.appendChild(make('circle', {
                                cx:pt[0], cy:pt[1], r:5.5,
                                fill:'rgba(185, 214, 255, 0.88)',
                                stroke:'rgba(11,16,24,0.92)',
                                'stroke-width':2
                            }));
                        }

                        if (cfg.marker_lat !== null && cfg.marker_lon !== null) {
                            const pt = project(cfg.marker_lon, cfg.marker_lat);
                            markerLayer.appendChild(make('circle', {cx:pt[0], cy:pt[1], r:16, class:'places-map-marker-pulse'}));
                            markerLayer.appendChild(make('circle', {cx:pt[0], cy:pt[1], r:8, class:'places-map-marker-core'}));
                        }
                        targetSvg.appendChild(root);
                        return { root, viewport };
                    }
                    const mainLayers = drawWorld(svg, true);
                    drawWorld(mini, false);
                    let scale = Number(cfg.zoom || 2.5), offsetX = 0, offsetY = 0, dragging = false, lastX = 0, lastY = 0, resetState = null;
                    function applyTransform() { mainLayers.viewport.setAttribute('transform', 'translate(' + offsetX.toFixed(2) + ' ' + offsetY.toFixed(2) + ') scale(' + scale.toFixed(4) + ')'); }
                    function centerOn(lon, lat) {
                        const pt = project(lon, lat);
                        offsetX = FOCAL_X - pt[0] * scale;
                        offsetY = FOCAL_Y - pt[1] * scale;
                        applyTransform();
                    }
                    function zoomBy(multiplier) {
                        scale = Math.max(1.1, Math.min(18, scale * multiplier));
                        if (cfg.marker_lat !== null && cfg.marker_lon !== null) centerOn(cfg.marker_lon, cfg.marker_lat);
                        else applyTransform();
                    }
                    if (cfg.marker_lat !== null && cfg.marker_lon !== null) centerOn(cfg.marker_lon, cfg.marker_lat);
                    else { scale = 1.45; centerOn(cfg.center_lon || 0, cfg.center_lat || 20); }
                    resetState = { scale, offsetX, offsetY };
                    svg.addEventListener('mousedown', ev => { dragging = true; lastX = ev.clientX; lastY = ev.clientY; svg.classList.add('dragging'); });
                    window.addEventListener('mousemove', ev => { if (!dragging) return; offsetX += (ev.clientX - lastX); offsetY += (ev.clientY - lastY); lastX = ev.clientX; lastY = ev.clientY; applyTransform(); });
                    window.addEventListener('mouseup', () => { dragging = false; svg.classList.remove('dragging'); });
                    svg.addEventListener('wheel', ev => { ev.preventDefault(); zoomBy(ev.deltaY < 0 ? 1.15 : 0.87); }, { passive:false });
                    document.querySelectorAll('[data-map-action]').forEach(btn => btn.addEventListener('click', () => {
                        const action = btn.getAttribute('data-map-action');
                        if (action === 'zoom-in') zoomBy(1.2);
                        else if (action === 'zoom-out') zoomBy(0.83);
                        else if (action === 'reset' && resetState) { scale = resetState.scale; offsetX = resetState.offsetX; offsetY = resetState.offsetY; applyTransform(); }
                    }));
                })();
                </script>
                {% endif %}
            </div>
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
            <div class="card" onclick="handleGridClick(event, '{{ c.id }}')" oncontextmenu="handleHeroCtx(event, '{{ c.id }}')">
                <div class="hero-preview">
                    {% if c.comp_hash %}
                    <img src="/composite/{{ c.comp_hash }}.jpg" loading="lazy">
                    {% endif %}
                </div>
                <div style="padding:20px 20px 5px;">
                    <a href="{{ c.url }}" style="text-decoration:none; color:#fff;"><h3 style="margin:0;">{{ c.title }}</h3></a>
                    <div style="color:#666; font-size:0.85em; font-weight:700;">{{ c.subtitle }}</div>
                </div>
                {% if c.places_url %}
                <div class="tag-container" style="padding-top:0; padding-bottom:8px;">
                    <a href="{{ c.places_url }}" class="page-action places-action" style="font-size:0.72em; padding:8px 12px;">📍 Places</a>
                </div>
                {% endif %}
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
        <button type="button" onclick="startCullSelectSelection()">Cull Select</button>
        <button type="button" onclick="startCullMoveSelection()">Cull Move</button>
        <button type="button" onclick="rotateSelection(90)">Rotate 90° Clockwise ↻</button>
        <button type="button" onclick="rotateSelection(270)">Rotate 90° Counter ↺</button>
        <button type="button" onclick="moveCurrentSelectionTo('trash')">Move to Trash</button>
        <div class="selection-menu-wrap">
            <button type="button" onclick="toggleSelectionStashMenu(event)">Stash ▾</button>
            <div id="selection-stash-menu" class="selection-pop-menu">
                {% for stash in stash_categories %}
                <div class="menu-item" onclick="moveCurrentSelectionToStash('{{ stash.key }}')">{{ stash.label }}</div>
                {% endfor %}
            </div>
        </div>
    </div>

    <div id="progress-dialog" class="progress-dialog">
        <div class="progress-header">
            <h3 id="progress-title" class="progress-title">Working…</h3>
            <button type="button" class="progress-close" onclick="closeProgressDialog()">×</button>
        </div>
        <div id="progress-subtitle" class="progress-subtitle">Preparing operation…</div>
        <div id="progress-status" class="progress-status"></div>
        <div class="progress-bar-shell">
            <div id="progress-bar-fill" class="progress-bar-fill"></div>
        </div>
        <div class="progress-meta">
            <span id="progress-counts">0 / 0</span>
            <span id="progress-phase">queued</span>
        </div>
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

            <div id="lb-section-place" class="lb-section">
                <div id="lb-place" class="lb-kv"></div>
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
        const cardScopes = {{ card_scopes | tojson | safe }};
        const stashCategories = {{ stash_categories | tojson | safe }};
        let curM = null;
        let curI = 0;
        let menuSha1 = '';
        let menuContext = { kind: 'photo', cardId: null };
        let selectedSha1s = new Set();
        let currentMeta = null;
        let currentTab = 'overview';
        let showFaceOverlay = false;
        let activeJobId = null;
        let activeJobPollTimer = null;
        let focusedPhotoSha1 = null;

        function getVisiblePhotoCards() {
            return Array.from(document.querySelectorAll('.photo-card[data-sha]'));
        }

        function getPhotoCardBySha1(sha1) {
            if (!sha1) return null;
            return document.querySelector(`.photo-card[data-sha="${sha1}"]`);
        }

        function refreshKeyboardFocusUI() {
            const cards = getVisiblePhotoCards();
            let hasFocusedCard = false;
            cards.forEach(card => {
                const isFocused = focusedPhotoSha1 && card.dataset.sha === focusedPhotoSha1;
                card.classList.toggle('keyboard-focus', !!isFocused);
                if (isFocused) hasFocusedCard = true;
            });
            if (!hasFocusedCard) {
                focusedPhotoSha1 = null;
            }
        }

        function setFocusedPhoto(sha1, shouldScroll = true) {
            const card = getPhotoCardBySha1(sha1);
            if (!card) return;
            focusedPhotoSha1 = sha1;
            refreshKeyboardFocusUI();
            if (shouldScroll) {
                card.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            }
        }

        function ensureFocusedPhoto() {
            const cards = getVisiblePhotoCards();
            if (cards.length === 0) return null;
            const existing = focusedPhotoSha1 ? getPhotoCardBySha1(focusedPhotoSha1) : null;
            if (existing) return existing;
            focusedPhotoSha1 = cards[0].dataset.sha;
            refreshKeyboardFocusUI();
            return cards[0];
        }

        function groupPhotoCardsIntoRows() {
            const cards = getVisiblePhotoCards();
            const rows = [];
            const tolerance = 24;
            cards.forEach(card => {
                const rect = card.getBoundingClientRect();
                const top = rect.top;
                let row = rows.find(r => Math.abs(r.top - top) <= tolerance);
                if (!row) {
                    row = { top, cards: [] };
                    rows.push(row);
                }
                row.cards.push({
                    card,
                    left: rect.left,
                    centerX: rect.left + (rect.width / 2),
                });
            });
            rows.sort((a, b) => a.top - b.top);
            rows.forEach(row => row.cards.sort((a, b) => a.left - b.left));
            return rows;
        }

        function moveKeyboardFocus(direction) {
            const current = ensureFocusedPhoto();
            if (!current) return;
            const rows = groupPhotoCardsIntoRows();
            if (!rows.length) return;

            let rowIndex = -1;
            let colIndex = -1;
            for (let i = 0; i < rows.length; i++) {
                const j = rows[i].cards.findIndex(entry => entry.card.dataset.sha === current.dataset.sha);
                if (j >= 0) {
                    rowIndex = i;
                    colIndex = j;
                    break;
                }
            }
            if (rowIndex < 0 || colIndex < 0) return;

            let target = null;
            if (direction === 'left') {
                target = rows[rowIndex].cards[Math.max(0, colIndex - 1)]?.card || null;
            } else if (direction === 'right') {
                target = rows[rowIndex].cards[Math.min(rows[rowIndex].cards.length - 1, colIndex + 1)]?.card || null;
            } else {
                const currentCenter = rows[rowIndex].cards[colIndex].centerX;
                const targetRowIndex = direction === 'up' ? rowIndex - 1 : rowIndex + 1;
                if (targetRowIndex >= 0 && targetRowIndex < rows.length) {
                    const targetRow = rows[targetRowIndex].cards;
                    let bestIdx = 0;
                    let bestDist = Infinity;
                    for (let i = 0; i < targetRow.length; i++) {
                        const dist = Math.abs(targetRow[i].centerX - currentCenter);
                        if (dist < bestDist) {
                            bestDist = dist;
                            bestIdx = i;
                        }
                    }
                    target = targetRow[bestIdx]?.card || null;
                }
            }

            if (target) {
                setFocusedPhoto(target.dataset.sha, true);
            }
        }

        function toggleFocusedPhotoSelection() {
            const current = ensureFocusedPhoto();
            if (!current) return;
            toggleSelection(current.dataset.sha);
            setFocusedPhoto(current.dataset.sha, false);
        }

        function hideContextMenu() {
            const menu = document.getElementById('context-menu');
            if (menu) menu.style.display = 'none';
        }

        function hideSelectionStashMenu() {
            const menu = document.getElementById('selection-stash-menu');
            if (menu) menu.classList.remove('open');
        }

        function clampContextMenuToViewport(menu, clientX, clientY) {
            if (!menu) return;
            const margin = 8;
            menu.style.left = `${clientX}px`;
            menu.style.top = `${clientY}px`;

            const rect = menu.getBoundingClientRect();
            let left = clientX;
            let top = clientY;

            if (rect.right > window.innerWidth - margin) {
                left = Math.max(margin, window.innerWidth - rect.width - margin);
            }
            if (rect.bottom > window.innerHeight - margin) {
                top = Math.max(margin, window.innerHeight - rect.height - margin);
            }

            menu.style.left = `${left}px`;
            menu.style.top = `${top}px`;
        }

        function adjustContextSubmenus() {
            const menu = document.getElementById('context-menu');
            if (!menu) return;
            const margin = 8;
            menu.querySelectorAll('.submenu').forEach(submenu => {
                submenu.style.top = '0px';
                submenu.style.left = '100%';
                submenu.style.right = 'auto';

                const parent = submenu.parentElement;
                if (!parent) return;
                const parentRect = parent.getBoundingClientRect();

                submenu.style.display = 'block';
                submenu.style.visibility = 'hidden';

                const rect = submenu.getBoundingClientRect();

                let top = 0;
                if (rect.bottom > window.innerHeight - margin) {
                    top = Math.min(0, (window.innerHeight - margin) - rect.bottom);
                }
                if (parentRect.top + top < margin) {
                    top = margin - parentRect.top;
                }

                if (rect.right > window.innerWidth - margin) {
                    submenu.style.left = 'auto';
                    submenu.style.right = '100%';
                }

                submenu.style.top = `${top}px`;
                submenu.style.visibility = '';
                submenu.style.display = '';
            });
        }

        function getStashCategoryDir(categoryKey) {
            const item = stashCategories.find(entry => entry.key === categoryKey);
            return item ? item.dir : '_stash';
        }

        function getExplicitContextTargets() {
            if (menuContext.kind === 'hero' && menuContext.cardId && cardScopes[menuContext.cardId]) {
                return Array.from(cardScopes[menuContext.cardId]);
            }
            return null;
        }

        function configureContextMenuFor(kind) {
            const menu = document.getElementById('context-menu');
            if (!menu) return;
            const photoOnly = ['rotate-cw', 'rotate-ccw', 'debug'];
            photoOnly.forEach(role => {
                const el = menu.querySelector(`[data-role="${role}"]`);
                if (!el) return;
                el.classList.toggle('hidden', kind !== 'photo');
            });
        }

        function getJobPhaseLabel(status) {
            if (!status) return 'queued';
            return String(status).toLowerCase();
        }

        function closeProgressDialog() {
            const dialog = document.getElementById('progress-dialog');
            if (dialog) dialog.classList.remove('visible');
            if (activeJobPollTimer) {
                clearTimeout(activeJobPollTimer);
                activeJobPollTimer = null;
            }
            activeJobId = null;
        }

        function updateProgressDialog(data) {
            const dialog = document.getElementById('progress-dialog');
            if (!dialog) return;
            dialog.classList.add('visible');
            const title = document.getElementById('progress-title');
            const subtitle = document.getElementById('progress-subtitle');
            const status = document.getElementById('progress-status');
            const counts = document.getElementById('progress-counts');
            const phase = document.getElementById('progress-phase');
            const barFill = document.getElementById('progress-bar-fill');
            if (title) title.textContent = data.title || 'Working…';
            if (subtitle) subtitle.textContent = data.subtitle || '';
            if (status) status.textContent = data.detail || data.message || '';
            const completed = Number(data.completed || 0);
            const total = Number(data.total || 0);
            const percent = total > 0 ? Math.max(0, Math.min(100, (completed / total) * 100)) : (data.status === 'completed' ? 100 : 0);
            if (barFill) barFill.style.width = `${percent}%`;
            if (counts) counts.textContent = total > 0 ? `${completed} / ${total}` : `${completed}`;
            if (phase) phase.textContent = getJobPhaseLabel(data.status);
        }

        async function pollJob(jobId) {
            if (!jobId) return;
            try {
                const resp = await fetch('/api/job_status/' + encodeURIComponent(jobId));
                const data = await resp.json();
                if (!resp.ok || data.status === 'missing') {
                    throw new Error(data.message || 'Job not found');
                }
                updateProgressDialog(data);
                if (data.status === 'completed') {
                    activeJobId = null;
                    activeJobPollTimer = null;
                    setTimeout(() => location.reload(), 700);
                    return;
                }
                if (data.status === 'error') {
                    activeJobId = null;
                    activeJobPollTimer = null;
                    return;
                }
                activeJobPollTimer = setTimeout(() => pollJob(jobId), 400);
            } catch (err) {
                console.error(err);
                updateProgressDialog({
                    title: 'Operation Failed',
                    subtitle: '',
                    detail: String(err),
                    completed: 0,
                    total: 0,
                    status: 'error',
                });
                activeJobId = null;
                activeJobPollTimer = null;
            }
        }

        async function startBackgroundOperation(payload) {
            hideContextMenu();
            hideSelectionStashMenu();
            if (!payload || !payload.sha1_list || payload.sha1_list.length === 0) return;
            updateProgressDialog({
                title: payload.title || 'Working…',
                subtitle: payload.subtitle || '',
                detail: 'Queued…',
                completed: 0,
                total: payload.sha1_list.length,
                status: 'queued',
            });
            try {
                const resp = await fetch('/api/start_operation', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await resp.json();
                if (!resp.ok || data.status !== 'ok') {
                    throw new Error(data.message || 'Failed to start operation');
                }
                activeJobId = data.job_id;
                if (activeJobPollTimer) clearTimeout(activeJobPollTimer);
                pollJob(activeJobId);
            } catch (err) {
                console.error(err);
                updateProgressDialog({
                    title: payload.title || 'Operation Failed',
                    subtitle: payload.subtitle || '',
                    detail: String(err),
                    completed: 0,
                    total: 0,
                    status: 'error',
                });
            }
        }

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
                    'place': 'Place',
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
                const place = data.place || {};
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

                const placeRows = [
                    ['Formatted', place.formatted || ''],
                    ['Place Name', place.place_name || ''],
                    ['Road', place.road || ''],
                    ['Suburb', place.suburb || ''],
                    ['Locality', place.locality || ''],
                    ['City', place.city || ''],
                    ['Town', place.town || ''],
                    ['Village', place.village || ''],
                    ['Hamlet', place.hamlet || ''],
                    ['County', place.county || ''],
                    ['State', place.state || ''],
                    ['Country', place.country || ''],
                    ['Coord Key', place.coord_key || ''],
                    ['Source Lat', place.source_lat || ''],
                    ['Source Lon', place.source_lon || ''],
                    ['Rounded Lat', place.lat_rounded || ''],
                    ['Rounded Lon', place.lon_rounded || ''],
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
                renderKV('lb-place', placeRows);
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
                if (placeRows.length > 0) availableTabs.push('place');
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
                renderKV('lb-place', []);
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
            refreshKeyboardFocusUI();
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
            const explicit = getExplicitContextTargets();
            if (explicit && explicit.length > 0) {
                return explicit;
            }
            if (selectedSha1s.size > 1 && selectedSha1s.has(clickedSha1)) {
                return Array.from(selectedSha1s);
            }
            return clickedSha1 ? [clickedSha1] : [];
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


        function openContextMenuAt(e, kind) {
            e.preventDefault();
            e.stopPropagation();
            hideSelectionStashMenu();
            configureContextMenuFor(kind);
            const menu = document.getElementById('context-menu');
            menu.style.display = 'block';
            clampContextMenuToViewport(menu, e.clientX, e.clientY);
            adjustContextSubmenus();
        }

        function handleCtx(e, sha1) {
            menuSha1 = sha1;
            menuContext = { kind: 'photo', cardId: null };
            openContextMenuAt(e, 'photo');
        }

        function handleHeroCtx(e, cardId) {
            menuSha1 = '';
            menuContext = { kind: 'hero', cardId };
            openContextMenuAt(e, 'hero');
        }

        function handleCtxFromLightbox(e) {
            if (!curM) return;
            handleCtx(e, manifests[curM][curI].sha1);
        }

        function rotateImage(degrees) {
            hideContextMenu();
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

        function moveSha1List(target, sha1List, options = {}) {
            if (!sha1List || sha1List.length === 0) return;
            const title = options.title || (target === 'trash' ? 'Moving to Trash' : 'Moving to Stash');
            const subtitle = options.subtitle || `${sha1List.length} photo${sha1List.length === 1 ? '' : 's'}`;
            const payload = {
                operation: 'move',
                target_dir: target,
                sha1_list: sha1List,
                title,
                subtitle,
            };
            startBackgroundOperation(payload);
        }

        function moveCurrentSelectionTo(target) {
            moveSha1List(target, Array.from(selectedSha1s));
        }

        function moveCurrentSelectionToStash(categoryKey) {
            hideSelectionStashMenu();
            moveSha1List(getStashCategoryDir(categoryKey), Array.from(selectedSha1s), {
                title: 'Moving to Stash',
                subtitle: categoryKey === 'general' ? 'General' : stashCategories.find(x => x.key === categoryKey)?.label || 'Stash',
            });
        }

        function moveContextTo(target) {
            const sha1List = getContextTargets(menuSha1);
            moveSha1List(target, sha1List);
        }

        function moveContextToStash(categoryKey) {
            const sha1List = getContextTargets(menuSha1);
            moveSha1List(getStashCategoryDir(categoryKey), sha1List, {
                title: 'Moving to Stash',
                subtitle: stashCategories.find(x => x.key === categoryKey)?.label || 'Stash',
            });
        }

        async function startCullSelectForShaList(sha1List, subtitle) {
            if (!sha1List || sha1List.length === 0) return;
            hideContextMenu();
            hideSelectionStashMenu();
            try {
                const resp = await fetch('/api/cull_select', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sha1_list: sha1List }),
                });
                const data = await resp.json();
                if (!resp.ok || data.status !== 'ok') {
                    alert(data.message || 'Cull select failed.');
                    return;
                }

                clearClusterVisuals();
                selectedSha1s = new Set(data.selected_sha1s || []);
                applyClusterVisuals(data.clusters || []);
                refreshSelectionUI();

                if (Array.isArray(data.clusters)) {
                    console.table(data.clusters.map(cluster => ({
                        cluster_id: cluster.cluster_id,
                        size: cluster.size,
                        keep_count: cluster.keep_count,
                        primary_sha1: cluster.primary_sha1 || '',
                        secondary_sha1: cluster.secondary_sha1 || '',
                        selected_for_cull: (cluster.selected_sha1s || []).join(', '),
                        sha1s: (cluster.sha1s || []).join(', ')
                    })));
                }
            } catch (err) {
                console.error(err);
                alert('Cull select request failed.');
            }
        }

        function startCullMoveForShaList(sha1List, subtitle) {
            if (!sha1List || sha1List.length === 0) return;
            moveSha1List('_cull', sha1List, {
                title: 'Moving to Cull',
                subtitle: subtitle || `${sha1List.length} photo${sha1List.length === 1 ? '' : 's'}`,
            });
        }

        function startCullSelectFromContext() {
            const sha1List = getContextTargets(menuSha1);
            const subtitle = menuContext.kind === 'hero' ? 'Current card scope' : `${sha1List.length} photo${sha1List.length === 1 ? '' : 's'}`;
            startCullSelectForShaList(sha1List, subtitle);
        }

        function startCullMoveFromContext() {
            const sha1List = getContextTargets(menuSha1);
            const subtitle = menuContext.kind === 'hero' ? 'Current card scope' : `${sha1List.length} photo${sha1List.length === 1 ? '' : 's'}`;
            startCullMoveForShaList(sha1List, subtitle);
        }

        function startCullSelectSelection() {
            startCullSelectForShaList(Array.from(selectedSha1s), `${selectedSha1s.size} selected`);
        }

        function startCullMoveSelection() {
            startCullMoveForShaList(Array.from(selectedSha1s), `${selectedSha1s.size} selected`);
        }

        function toggleSelectionStashMenu(event) {
            event.preventDefault();
            event.stopPropagation();
            const menu = document.getElementById('selection-stash-menu');
            if (!menu) return;
            menu.classList.toggle('open');
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
            const ctx = document.getElementById('context-menu');
            if (ctx && !ctx.contains(e.target)) {
                hideContextMenu();
            }
            const stashMenu = document.getElementById('selection-stash-menu');
            const stashWrap = e.target.closest ? e.target.closest('.selection-menu-wrap') : null;
            if (stashMenu && !stashWrap) {
                hideSelectionStashMenu();
            }
        };

        document.addEventListener('keydown', (e) => {
            const activeEl = document.activeElement;
            const targetTag = (activeEl && activeEl.tagName ? activeEl.tagName.toLowerCase() : '');
            const activeIsCheckbox = !!(activeEl && activeEl.classList && activeEl.classList.contains('photo-select-checkbox'));
            const isTextInput = targetTag === 'textarea' || (targetTag === 'input' && !activeIsCheckbox);

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
                return;
            }

            if (document.getElementById('lightbox').classList.contains('active')) {
                if (e.key === 'ArrowRight') {
                    e.preventDefault();
                    changeImg(1);
                    return;
                }
                if (e.key === 'ArrowLeft') {
                    e.preventDefault();
                    changeImg(-1);
                    return;
                }
            } else if (!isTextInput && document.querySelectorAll('.photo-card[data-sha]').length > 0) {
                if (e.key === 'ArrowRight') {
                    e.preventDefault();
                    moveKeyboardFocus('right');
                    return;
                }
                if (e.key === 'ArrowLeft') {
                    e.preventDefault();
                    moveKeyboardFocus('left');
                    return;
                }
                if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    moveKeyboardFocus('up');
                    return;
                }
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    moveKeyboardFocus('down');
                    return;
                }
                if (e.key === ' ' || e.code === 'Space') {
                    e.preventDefault();
                    toggleFocusedPhotoSelection();
                    return;
                }
            }

            if (e.key.toLowerCase() === 'e') toggleSidebar();
        });

        document.addEventListener('DOMContentLoaded', () => {
            document.querySelectorAll('.photo-select-checkbox').forEach(cb => {
                cb.addEventListener('click', (e) => {
                    e.stopPropagation();
                    setFocusedPhoto(cb.dataset.sha, false);
                    toggleSelection(cb.dataset.sha);
                });
            });

            document.querySelectorAll('.photo-card[data-sha]').forEach(card => {
                card.addEventListener('click', () => setFocusedPhoto(card.dataset.sha, false));
                card.addEventListener('mousedown', () => setFocusedPhoto(card.dataset.sha, false));
            });

            document.querySelectorAll('.nav-bar a, .tag-pill, .breadcrumb a, .grid a').forEach(link => {
                link.addEventListener('click', () => clearSelection());
            });

            const firstCard = getVisiblePhotoCards()[0];
            if (firstCard) {
                focusedPhotoSha1 = firstCard.dataset.sha;
            }
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
    map_tiles_dir: Path
    thumb_dir: Path
    composite_dir: Path
    geo_db_path: Path
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
            "place": {},
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

            try:
                with sqlite3.connect(self.config.geo_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    geo_row = conn.execute(
                        """
                        SELECT pg.coord_key, pg.source_lat, pg.source_lon,
                               gc.country, gc.state, gc.county, gc.city, gc.town, gc.village,
                               gc.hamlet, gc.suburb, gc.place_name, gc.formatted, gc.road,
                               gc.lat_rounded, gc.lon_rounded
                        FROM photo_geo pg
                        JOIN geo_cache gc ON gc.coord_key = pg.coord_key
                        WHERE pg.sha1 = ?
                        LIMIT 1
                        """,
                        (sha1,),
                    ).fetchone()

                if geo_row:
                    gr = dict(geo_row)
                    locality_parts = [
                        gr.get("city") or "",
                        gr.get("town") or "",
                        gr.get("village") or "",
                        gr.get("hamlet") or "",
                        gr.get("suburb") or "",
                    ]
                    locality = next((p for p in locality_parts if p), "")
                    result["place"] = {
                        "formatted": gr.get("formatted") or "",
                        "place_name": gr.get("place_name") or "",
                        "road": gr.get("road") or "",
                        "suburb": gr.get("suburb") or "",
                        "locality": locality,
                        "city": gr.get("city") or "",
                        "town": gr.get("town") or "",
                        "village": gr.get("village") or "",
                        "hamlet": gr.get("hamlet") or "",
                        "county": gr.get("county") or "",
                        "state": gr.get("state") or "",
                        "country": gr.get("country") or "",
                        "coord_key": gr.get("coord_key") or "",
                        "source_lat": "" if gr.get("source_lat") is None else f"{float(gr.get('source_lat')):.6f}",
                        "source_lon": "" if gr.get("source_lon") is None else f"{float(gr.get('source_lon')):.6f}",
                        "lat_rounded": "" if gr.get("lat_rounded") is None else str(gr.get("lat_rounded")),
                        "lon_rounded": "" if gr.get("lon_rounded") is None else str(gr.get("lon_rounded")),
                    }
            except Exception:
                pass

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
        map_tiles_dir=root / "_web_layout" / "map_tiles",
        thumb_dir=root / "_thumbs",
        composite_dir=root / "_thumbs" / "_composites",
        geo_db_path=root / "geo_tags.sqlite",
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


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 <= edge0:
        return 1.0 if x >= edge1 else 0.0
    t = _clamp01((x - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


def _face_subjectness_from_meta(meta: dict[str, Any]) -> float:
    faces_meta = meta.get("faces") or {}
    face_summary = faces_meta.get("summary") or {}
    face_boxes = faces_meta.get("boxes") or []

    if not isinstance(face_boxes, list):
        face_boxes = []

    area_ratios: list[float] = []
    for box in face_boxes:
        if not isinstance(box, dict):
            continue
        ratio = _safe_float(box.get("area_ratio"), 0.0)
        if ratio > 0:
            area_ratios.append(ratio)

    area_ratios.sort(reverse=True)

    face_count = int(face_summary.get("face_count") or 0)
    if face_count <= 0 and area_ratios:
        face_count = len(area_ratios)
    if face_count <= 0:
        return 0.0

    largest_ratio = area_ratios[0] if area_ratios else _safe_float(face_summary.get("largest_face_area_ratio"), 0.0)
    second_ratio = area_ratios[1] if len(area_ratios) > 1 else 0.0
    top2_total = largest_ratio + second_ratio

    # Three practical regimes:
    # 1) tiny background faces -> near zero contribution
    # 2) subject-level medium faces -> ramp up quickly
    # 3) dominant close-up faces -> saturate near the top
    largest_signal = _smoothstep(0.006, 0.038, largest_ratio)
    second_signal = _smoothstep(0.003, 0.022, second_ratio)
    pair_signal = _smoothstep(0.010, 0.055, top2_total)
    count_signal = _smoothstep(1.0, 3.0, float(face_count))

    subjectness = (
        largest_signal * 0.44 +
        second_signal * 0.18 +
        pair_signal * 0.30 +
        count_signal * 0.08
    )

    # Preserve the near-zero behavior for truly incidental background people.
    if largest_ratio < 0.005 and top2_total < 0.008:
        subjectness *= 0.20

    return _clamp01(subjectness)


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

    # Count features still matter, but they should not overpower the actual
    # emotional quality signal.
    count_bonus = min(1.0, (0.45 * good_count + 0.35 * smiling_count + 0.20 * eyes_open_count) / 3.0)

    # Strongly prefer group coherence over one slightly better single face.
    # In the cheeky-grin case, the image with the better overall moment should
    # win even if the rival has a trivially higher best-face score.
    expression_gap = max(0.0, best_face - avg_top2)
    coherence_expr = max(0.0, avg_top2 - (expression_gap * 0.60))
    consistency_bonus = _clamp01(1.0 - (expression_gap / 0.06))

    # Soft asymmetry bonus. Slight imperfection can read as more human / playful.
    asymmetry_bonus = 0.0
    face_expression = meta.get("face_expression") or {}
    face_rows = face_expression.get("faces") or face_expression.get("face_rows") or []
    if isinstance(face_rows, list) and face_rows:
        asym_values = []
        for row in face_rows:
            if not isinstance(row, dict):
                continue
            asym = row.get("asymmetry_score")
            if asym is None:
                continue
            asym_values.append(_safe_float(asym, 0.0))
        if asym_values:
            avg_asym = sum(asym_values[:2]) / min(2, len(asym_values))
            # Favor slight asymmetry, but do not reward extreme distortion.
            asymmetry_bonus = _clamp01(avg_asym / 0.16) * 0.05

    signal = (
        coherence_expr * 0.62 +
        best_face * 0.10 +
        prominent_expr * 0.04 +
        people_moment * 0.09 +
        count_bonus * 0.05 +
        consistency_bonus * 0.05 +
        asymmetry_bonus
    )
    return _clamp01(signal)


def _people_weight_from_meta(meta: dict[str, Any]) -> float:
    faces_meta = meta.get("faces") or {}
    faces = faces_meta.get("summary") or {}
    face_boxes = faces_meta.get("boxes") or []
    face_count = int(faces.get("face_count") or 0)
    prominent_count = int(faces.get("prominent_face_count") or 0)

    if face_count <= 0:
        return 0.0

    if not isinstance(face_boxes, list):
        face_boxes = []

    area_ratios = sorted(
        [_safe_float(box.get("area_ratio"), 0.0) for box in face_boxes if isinstance(box, dict)],
        reverse=True,
    )
    largest_ratio = area_ratios[0] if area_ratios else _safe_float(faces.get("largest_face_area_ratio"), 0.0)
    second_ratio = area_ratios[1] if len(area_ratios) > 1 else 0.0
    top2_total = largest_ratio + second_ratio

    subjectness = _face_subjectness_from_meta(meta)

    # Map face geometry into a smooth people-weight curve. This avoids a brittle
    # binary threshold while still pushing subject-level faces into people mode.
    weight = 0.02 + (subjectness ** 0.82) * 0.86

    # Truly tiny background faces should contribute almost nothing.
    if prominent_count <= 0 and largest_ratio < 0.006 and top2_total < 0.009:
        return 0.02

    # Two medium faces are a legitimate human-subject photo even when the raw
    # detector does not call either one "prominent".
    if face_count >= 2 and largest_ratio >= 0.018 and second_ratio >= 0.008:
        weight = max(weight, 0.64)
    if face_count >= 2 and largest_ratio >= 0.022 and second_ratio >= 0.010:
        weight = max(weight, 0.72)
    if face_count >= 2 and top2_total >= 0.034:
        weight = max(weight, 0.78)

    # Close-up portraits should still saturate aggressively.
    if prominent_count >= 1 and largest_ratio >= 0.018:
        weight = max(weight, 0.74)
    if prominent_count >= 1 and largest_ratio >= 0.028:
        weight = max(weight, 0.86)
    if prominent_count >= 1 and largest_ratio >= 0.045:
        weight = max(weight, 0.93)
    if prominent_count >= 2 and largest_ratio >= 0.028:
        weight = max(weight, 0.91)

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

    scene_score = (
        technical * 0.16 +
        aesthetic * 0.30 +
        subject_prominence * 0.28 +
        semantic * 0.18 +
        face_score * 0.08
    )

    people_score = (
        technical * 0.05 +
        aesthetic * 0.03 +
        subject_prominence * 0.01 +
        semantic * 0.01 +
        face_score * 0.01 +
        face_expression_score * 0.89
    )

    score = (scene_score * (1.0 - people_weight)) + (people_score * people_weight)

    note_parts = []
    if contains_people:
        score += 0.03
        note_parts.append("people")
    if contains_animals:
        score += 0.06
        note_parts.append("animals")
        score += subject_prominence * 0.10
        note_parts.append("animal_prominence")
    if dog_bonus > 0:
        score += dog_bonus
        note_parts.append("dog_summary_bonus")
    if is_landscape and people_weight < 0.18:
        score += 0.03
        note_parts.append("landscape")
    if is_food:
        score += 0.02
        note_parts.append("food")
    if people_weight >= 0.20:
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

    scene_score = (
        technical * 0.34 +
        aesthetic * 0.26 +
        subject_prominence * 0.24 +
        face_score * 0.08 +
        semantic * 0.08
    )

    people_score = (
        technical * 0.08 +
        aesthetic * 0.03 +
        subject_prominence * 0.01 +
        semantic * 0.01 +
        face_score * 0.01 +
        face_expression_score * 0.86
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
    if people_weight >= 0.20:
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
    places_bucket_store: dict[str, dict[str, Any]] = {}

    def register_places_bucket(sha1s: list[str], label: str, place: str, back: str) -> str:
        token = secrets.token_urlsafe(12)
        places_bucket_store[token] = {
            'ids': list(sha1s),
            'label': label,
            'place': place,
            'back': back,
            'created_at': time.time(),
        }
        if len(places_bucket_store) > 2000:
            cutoff = time.time() - 86400
            stale = [k for k, v in list(places_bucket_store.items()) if float(v.get('created_at', 0)) < cutoff]
            for k in stale[:1000]:
                places_bucket_store.pop(k, None)
        return token

    places = PlacesService(
        config.archive_root,
        config.geo_db_path,
        store.choose_best_interesting_item,
        bucket_registrar=register_places_bucket,
    )
    places_map_service = PlacesMapService()
    job_lock = threading.Lock()
    jobs: dict[str, dict[str, Any]] = {}

    def create_job(title: str, subtitle: str = '', total: int = 0) -> str:
        job_id = uuid.uuid4().hex
        with job_lock:
            jobs[job_id] = {
                'job_id': job_id,
                'title': title,
                'subtitle': subtitle,
                'detail': '',
                'status': 'queued',
                'completed': 0,
                'total': total,
                'created_at': time.time(),
                'finished_at': None,
                'result': {},
            }
        return job_id

    def update_job(job_id: str, **fields: Any) -> None:
        with job_lock:
            if job_id not in jobs:
                return
            jobs[job_id].update(fields)

    def get_job(job_id: str) -> dict[str, Any] | None:
        with job_lock:
            job = jobs.get(job_id)
            return dict(job) if job else None

    def job_progress(job_id: str, completed: int | None = None, total: int | None = None, detail: str | None = None, subtitle: str | None = None) -> None:
        payload: dict[str, Any] = {}
        if completed is not None:
            payload['completed'] = completed
        if total is not None:
            payload['total'] = total
        if detail is not None:
            payload['detail'] = detail
        if subtitle is not None:
            payload['subtitle'] = subtitle
        if payload:
            update_job(job_id, **payload)

    def finalize_job(job_id: str, status: str, result: dict[str, Any] | None = None, detail: str = '') -> None:
        update_job(job_id, status=status, result=result or {}, detail=detail, finished_at=time.time())

    def make_places_action(url: str) -> dict[str, Any]:
        return {"label": "📍 Places", "url": url, "active": False, "classes": "places-action"}

    def render_places_page(page_title: str, breadcrumb: str, context_title: str, scope_type: str, scope_url: str, items: list[dict[str, Any]], extra_actions: list[dict[str, Any]] | None = None) -> str:
        store.load_cache()
        for item in items:
            item["_hero_score"] = store.get_hero_score(item)
        node = request.args.get("node")
        view = places.places_get_view(PlacesContext(scope_type=scope_type, title=context_title, breadcrumb=breadcrumb, scope_url=scope_url), items, selected_node_id=node)

        manifests = {}
        all_place_card = view.get("all_place_card") if isinstance(view, dict) else None
        if all_place_card and all_place_card.get("cover_items"):
            item_by_sha1 = {str(item.get("sha1")): item for item in items if item.get("sha1")}
            cover_sha1s = [str(cp.get("sha1") or "") for cp in all_place_card.get("cover_items", []) if str(cp.get("sha1") or "")]
            cover_manifest_items = [item_by_sha1[sha1] for sha1 in cover_sha1s if sha1 in item_by_sha1]
            if cover_manifest_items:
                selected_node = (view.get("selected_node") or {}) if isinstance(view, dict) else {}
                node_id = str(selected_node.get("node_id") or "places")
                manifest_key = f"places_all_{node_id}"
                manifests[manifest_key] = store.build_manifest(cover_manifest_items)
                all_place_card["manifest_key"] = manifest_key
                for idx, cp in enumerate(all_place_card.get("cover_items", [])):
                    cp["lightbox_index"] = idx

        selected_node_for_map = view.get("selected_node")
        if selected_node_for_map and view.get("leaf_cards"):
            selected_node_for_map = dict(selected_node_for_map)
            selected_node_for_map["children_data"] = [
                {
                    "label": leaf.get("label"),
                    "photo_count": leaf.get("photo_count"),
                    "level": leaf.get("level") or "place",
                    "lat": leaf.get("lat"),
                    "lon": leaf.get("lon"),
                }
                for leaf in view.get("leaf_cards", [])
                if leaf.get("lat") is not None and leaf.get("lon") is not None
            ]

        places_map_view = places_map_service.build_map_view(selected_node_for_map, context_title)
        places_map_view["tile_url_template"] = "/map_tiles/{z}/{x}/{y}.png"
        places_map_view["tiles_available"] = bool(config.map_tiles_dir.exists())

        action_links = [make_places_action(scope_url)]
        if extra_actions:
            action_links.extend(extra_actions)
        return render_page(
            page_title=page_title,
            active_tab="places",
            banner_img="hero-places.png",
            breadcrumb=breadcrumb,
            action_links=action_links,
            places_view=view,
            places_map_view=places_map_view,
            manifests=manifests,
        )

    def render_page(**kwargs: Any) -> str:
        kwargs.setdefault('manifests', {})
        kwargs.setdefault('action_links', None)
        kwargs.setdefault('day_calendar', None)
        kwargs.setdefault('card_scopes', {})
        kwargs.setdefault('stash_categories', STASH_CATEGORIES)
        kwargs.setdefault('places_view', None)
        kwargs.setdefault('places_map_view', None)
        kwargs.setdefault('auto_open', None)
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

    def move_media_records(
        sha1_list: list[str],
        target_dir_name: str,
        progress_callback: Any | None = None,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        if not sha1_list:
            return False, "sha1_list required", {}

        target_dir = config.archive_root / target_dir_name
        target_dir.mkdir(parents=True, exist_ok=True)

        moved_count = 0
        skipped_count = 0
        total = len(sha1_list)

        with sqlite3.connect(config.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT sha1, rel_fqn FROM media WHERE sha1 IN ({','.join('?' for _ in sha1_list)})",
                tuple(sha1_list),
            ).fetchall()

            found = {str(r["sha1"]): r for r in rows}
            for idx, sha1 in enumerate(sha1_list, start=1):
                sha1 = str(sha1)
                if progress_callback:
                    progress_callback(idx - 1, total, f"Processing {idx} of {total}…")
                if sha1 not in found:
                    skipped_count += 1
                    continue
                row = found[sha1]
                old_rel = str(row["rel_fqn"]).replace("\\", "/")
                if old_rel.startswith("_trash/") or old_rel.startswith("_stash/") or old_rel.startswith("_cull/"):
                    skipped_count += 1
                    continue

                old_full = config.archive_root / old_rel
                if not old_full.exists():
                    skipped_count += 1
                    continue

                new_rel_path = safe_destination_relative(target_dir_name, old_rel, sha1)
                new_full = config.archive_root / new_rel_path
                new_full.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_full), str(new_full))
                conn.execute(
                    "UPDATE media SET rel_fqn=?, is_deleted=1 WHERE sha1=?",
                    (str(new_rel_path).replace('/', '\\'), sha1),
                )
                moved_count += 1
                if progress_callback:
                    progress_callback(idx, total, str(new_rel_path).replace('\\', '/'))

            conn.commit()

        invalidate_composites()
        return True, None, {
            'requested': total,
            'moved': moved_count,
            'skipped': skipped_count,
            'target_dir': target_dir_name,
        }

    def perform_cull(sha1_list: list[str], progress_callback: Any | None = None) -> tuple[bool, str | None, dict[str, Any]]:
        if not sha1_list:
            return False, "sha1_list required", {}

        store.load_cache()
        all_items = store.db_cache + store.undated_cache
        item_map = {str(item.get('sha1')): item for item in all_items}
        items = [item_map[str(sha1)] for sha1 in sha1_list if str(sha1) in item_map]
        if not items:
            return False, "No matching items found", {}

        if progress_callback:
            progress_callback(0, 0, "Analyzing clusters…")

        clusters = _find_clusters_in_items(items, time_threshold_seconds=15)
        losers: list[str] = []
        cluster_summaries: list[dict[str, Any]] = []
        for idx, cluster in enumerate(clusters, start=1):
            cluster_sha1s = [str(item.get('sha1')) for item in cluster]
            keep_count = 2 if len(cluster) > 4 else 1
            ranked = _rank_sha1s_for_mode(store, cluster_sha1s, 'cull')
            keepers = [item['sha1'] for item in ranked[:keep_count]]
            keeper_set = set(keepers)
            drop_list = [sha for sha in cluster_sha1s if sha not in keeper_set]
            losers.extend(drop_list)
            cluster_summaries.append({
                'cluster_id': idx,
                'size': len(cluster),
                'keep_count': keep_count,
                'keepers': keepers,
                'dropped': drop_list,
            })
            if progress_callback:
                progress_callback(idx, len(clusters), f"Cluster {idx} of {len(clusters)} analyzed")

        if not losers:
            return True, None, {
                'requested': len(sha1_list),
                'clusters_found': len(clusters),
                'moved': 0,
                'kept': len(sha1_list),
                'clusters': cluster_summaries,
            }

        def move_progress(done: int, total: int, detail: str) -> None:
            if progress_callback:
                progress_callback(done, total, detail)

        ok, message, move_summary = move_media_records(losers, '_cull', progress_callback=move_progress)
        if not ok:
            return False, message, {}

        return True, None, {
            'requested': len(sha1_list),
            'clusters_found': len(clusters),
            'moved': move_summary.get('moved', 0),
            'kept': len(sha1_list) - move_summary.get('moved', 0),
            'clusters': cluster_summaries,
        }

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

    @app.route("/api/start_operation", methods=["POST"])
    def start_operation():
        payload = request.json or {}
        operation = str(payload.get('operation') or '').strip().lower()
        sha1_list = [str(x) for x in (payload.get('sha1_list') or []) if str(x)]
        title = str(payload.get('title') or 'Working…')
        subtitle = str(payload.get('subtitle') or '')
        target_dir = str(payload.get('target_dir') or '')

        if operation not in {'move', 'cull'}:
            return jsonify({'status': 'error', 'message': 'invalid operation'}), 400
        if not sha1_list:
            return jsonify({'status': 'error', 'message': 'sha1_list required'}), 400
        if operation == 'move' and not target_dir:
            return jsonify({'status': 'error', 'message': 'target_dir required'}), 400

        job_id = create_job(title=title, subtitle=subtitle, total=len(sha1_list))

        def worker() -> None:
            try:
                update_job(job_id, status='running')
                if operation == 'move':
                    ok, message, result = move_media_records(
                        sha1_list,
                        target_dir,
                        progress_callback=lambda completed, total, detail: job_progress(job_id, completed=completed, total=total, detail=detail),
                    )
                else:
                    ok, message, result = perform_cull(
                        sha1_list,
                        progress_callback=lambda completed, total, detail: job_progress(job_id, completed=completed, total=total, detail=detail),
                    )
                if ok:
                    finalize_job(job_id, 'completed', result=result, detail='Operation completed successfully.')
                else:
                    finalize_job(job_id, 'error', result=result, detail=message or 'Operation failed.')
            except Exception as exc:
                finalize_job(job_id, 'error', detail=str(exc))

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({'status': 'ok', 'job_id': job_id})

    @app.route("/api/job_status/<job_id>")
    def job_status(job_id: str):
        job = get_job(job_id)
        if not job:
            return jsonify({'status': 'missing', 'message': 'job not found'}), 404
        return jsonify(job)

    @app.route("/api/move_to_trash", methods=["POST"])
    def move_to_trash():
        payload = request.json or {}
        sha1_list = payload.get("sha1_list") or []
        if not isinstance(sha1_list, list) or not sha1_list:
            return jsonify({"status": "error", "message": "sha1_list required"}), 400
        ok, message, _summary = move_media_records([str(x) for x in sha1_list], "_trash")
        if not ok:
            return jsonify({"status": "error", "message": message}), 400
        return jsonify({"status": "ok"})

    @app.route("/api/move_to_stash", methods=["POST"])
    def move_to_stash():
        payload = request.json or {}
        sha1_list = payload.get("sha1_list") or []
        target_dir = str(payload.get("target_dir") or "_stash")
        if not isinstance(sha1_list, list) or not sha1_list:
            return jsonify({"status": "error", "message": "sha1_list required"}), 400
        ok, message, _summary = move_media_records([str(x) for x in sha1_list], target_dir)
        if not ok:
            return jsonify({"status": "error", "message": message}), 400
        return jsonify({"status": "ok"})

    @app.route("/api/empty_trash", methods=["POST"])
    def empty_trash():
        ok, message = empty_trash_impl()
        if not ok:
            return jsonify({"status": "error", "message": message}), 400
        return jsonify({"status": "ok"})


    @app.route("/api/cull", methods=["POST"])
    def cull_now():
        payload = request.json or {}
        sha1_list = payload.get("sha1_list") or []
        if not isinstance(sha1_list, list) or not sha1_list:
            return jsonify({"status": "error", "message": "sha1_list required"}), 400
        ok, message, summary = perform_cull([str(x) for x in sha1_list])
        if not ok:
            return jsonify({"status": "error", "message": message}), 400
        return jsonify({"status": "ok", "summary": summary})

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


    @app.route("/api/cull_select", methods=["POST"])
    def cull_select():
        payload = request.json or {}
        sha1_list = payload.get("sha1_list") or []
        if not isinstance(sha1_list, list) or not sha1_list:
            return jsonify({"status": "error", "message": "sha1_list required"}), 400

        store.load_cache()
        all_items = store.db_cache + store.undated_cache
        item_map = {str(item.get("sha1")): item for item in all_items}
        items = [item_map[str(sha1)] for sha1 in sha1_list if str(sha1) in item_map]
        if not items:
            return jsonify({"status": "error", "message": "No matching items found"}), 400

        clusters = _find_clusters_in_items(items, time_threshold_seconds=15)
        selected_sha1s: list[str] = []
        cluster_debug: list[dict[str, Any]] = []

        for idx, cluster in enumerate(clusters, start=1):
            cluster_sha1s = [str(item.get("sha1")) for item in cluster]
            ranked = _rank_sha1s_for_mode(store, cluster_sha1s, "cull")
            keep_count = 2 if len(cluster) > 4 else 1
            keepers = [item["sha1"] for item in ranked[:keep_count]]
            keeper_set = set(keepers)
            losers = [sha for sha in cluster_sha1s if sha not in keeper_set]
            selected_sha1s.extend(losers)

            cluster_debug.append({
                "cluster_id": idx,
                "size": len(cluster),
                "keep_count": keep_count,
                "primary_sha1": keepers[0] if len(keepers) >= 1 else "",
                "secondary_sha1": keepers[1] if len(keepers) >= 2 else "",
                "selected_sha1s": losers,
                "sha1s": cluster_sha1s,
            })

        return jsonify({
            "status": "ok",
            "selected_sha1s": selected_sha1s,
            "clusters": cluster_debug,
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
                    "places_url": f"/places/timeline/decade/{decade}",
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
            card_scopes={card['id']: [str(item['sha1']) for item in card['heroes']] for card in cards},
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
                    "places_url": f"/places/timeline/year/{year}",
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
            action_links=[make_places_action(f'/places/timeline/decade/{decade}')],
            cards=cards,
            manifests=manifests,
            card_scopes={card['id']: [str(item['sha1']) for item in card['heroes']] for card in cards},
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
                    "places_url": f"/places/timeline/month/{year}/{month_code}",
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
            card_scopes={card['id']: [str(item['sha1']) for item in card['heroes']] for card in cards},
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
            action_links=[make_places_action('/places/undated')],
            cards=cards,
            manifests=manifests,
            card_scopes={card['id']: [str(item['sha1']) for item in card['heroes']] for card in cards},
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
            action_links=[make_places_action(f'/places/undated/{folder}')],
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
                    "places_url": f"/places/folder/{prefix + folder_name}",
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
            action_links=[make_places_action(f'/places/folder/{subpath}' if subpath else '/places/folder')],
            cards=cards,
            photos=direct_files,
            manifests=manifests,
            card_scopes={card['id']: [str(item['sha1']) for item in card['heroes']] for card in cards},
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
                        "places_url": f"/places/tags/{tag_name}",
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
            action_links=[make_places_action(f'/places/tags/{tag}')],
            photos=imgs,
            manifests={"main_gallery": store.build_manifest(imgs)},
        )

    @app.route("/places")
    def places_root():
        store.load_cache()
        return render_places_page(
            page_title="Places",
            breadcrumb="Places",
            context_title="Entire archive",
            scope_type="global",
            scope_url="/places",
            items=store.db_cache,
        )


    @app.route("/places/timeline/decade/<decade>")
    def places_timeline_decade(decade: str):
        store.load_cache()
        items = [item for item in store.db_cache if item.get('_decade') == decade]
        return render_places_page(
            page_title=f"Places — {decade}",
            breadcrumb=f"<a href='/places'>Places</a> / {decade}",
            context_title=decade,
            scope_type='timeline-decade',
            scope_url=f'/places/timeline/decade/{decade}',
            items=items,
            extra_actions=[{'label': 'Timeline', 'url': f'/timeline/decade/{decade}', 'active': False}],
        )

    @app.route("/places/timeline/year/<year>")
    def places_timeline_year(year: str):
        store.load_cache()
        items = [item for item in store.db_cache if item.get("_year") == year]
        return render_places_page(
            page_title=f"Places · {year}",
            breadcrumb=f"<a href='/places'>Places</a> / <a href='/timeline/year/{year}'>{year}</a>",
            context_title=f"Places for {year}",
            scope_type="timeline-year",
            scope_url=f"/places/timeline/year/{year}",
            items=items,
            extra_actions=[{"label": "Back to Timeline", "url": f"/timeline/year/{year}", "active": False}],
        )

    @app.route("/places/timeline/month/<year>/<month>")
    def places_timeline_month(year: str, month: str):
        store.load_cache()
        items = [item for item in store.db_cache if item.get("_year") == year and item.get("_month") == month]
        month_name = items[0]["_month_name"] if items else datetime(int(year), int(month), 1).strftime("%B")
        return render_places_page(
            page_title=f"Places · {month_name} {year}",
            breadcrumb=f"<a href='/places'>Places</a> / <a href='/timeline/year/{year}'>{year}</a> / <a href='/timeline/month/{year}/{month}'>{month_name}</a>",
            context_title=f"Places for {month_name} {year}",
            scope_type="timeline-month",
            scope_url=f"/places/timeline/month/{year}/{month}",
            items=items,
            extra_actions=[{"label": "Back to Month", "url": f"/timeline/month/{year}/{month}", "active": False}],
        )

    @app.route("/places/timeline/month/<year>/<month>/day/<day>")
    def places_timeline_day(year: str, month: str, day: str):
        store.load_cache()
        day_int = int(day)
        items = [item for item in store.db_cache if item.get("_year") == year and item.get("_month") == month and int(item.get("_day_int", 0)) == day_int]
        month_name = items[0]["_month_name"] if items else datetime(int(year), int(month), 1).strftime("%B")
        return render_places_page(
            page_title=f"Places · {month_name} {day_int}, {year}",
            breadcrumb=f"<a href='/places'>Places</a> / <a href='/timeline/month/{year}/{month}/day/{day}'>{month_name} {day_int}, {year}</a>",
            context_title=f"Places for {month_name} {day_int}, {year}",
            scope_type="timeline-day",
            scope_url=f"/places/timeline/month/{year}/{month}/day/{day}",
            items=items,
            extra_actions=[{"label": "Back to Day", "url": f"/timeline/month/{year}/{month}/day/{day}", "active": False}],
        )

    @app.route("/places/folder")
    @app.route("/places/folder/<path:subpath>")
    def places_folder(subpath: str = ""):
        store.load_cache()
        all_media = store.db_cache + store.undated_cache
        prefix = f"{subpath}/" if subpath else ""
        items = [item for item in all_media if str(item.get("_web_path", "")).startswith(prefix)]
        title = "Root" if not subpath else subpath.replace("/", " / ")
        return render_places_page(
            page_title=f"Places · {title}",
            breadcrumb=f"<a href='/places'>Places</a> / <a href='/folder/{subpath}'>{title}</a>" if subpath else "<a href='/places'>Places</a> / <a href='/folder'>Root</a>",
            context_title=f"Places for {title}",
            scope_type="folder",
            scope_url=f"/places/folder/{subpath}" if subpath else "/places/folder",
            items=items,
            extra_actions=[{"label": "Back to Explorer", "url": f"/folder/{subpath}" if subpath else "/folder", "active": False}],
        )


    @app.route("/places/tags")
    def places_tags_root():
        store.load_cache()
        return render_places_page(
            page_title="Places by Tag Context",
            breadcrumb="<a href='/places'>Places</a> / Tags",
            context_title="Tags",
            scope_type="tags-root",
            scope_url="/places/tags",
            items=store.db_cache,
            extra_actions=[{'label': 'Tags', 'url': '/tags', 'active': False}],
        )

    @app.route("/places/tags/<tag>")
    def places_tag(tag: str):
        store.load_cache()
        items = store.global_tags.get(tag, [])
        return render_places_page(
            page_title=f"Places · #{tag}",
            breadcrumb=f"<a href='/places'>Places</a> / <a href='/tags/{tag}'>#{tag}</a>",
            context_title=f"Places for #{tag}",
            scope_type="tag",
            scope_url=f"/places/tags/{tag}",
            items=items,
            extra_actions=[{"label": "Back to Tag", "url": f"/tags/{tag}", "active": False}],
        )

    @app.route("/places/undated")
    @app.route("/places/undated/<folder>")
    def places_undated(folder: str | None = None):
        store.load_cache()
        if folder is None:
            items = store.undated_cache
            title = "Undated"
            scope_url = "/places/undated"
            back_url = "/undated"
        else:
            items = [item for item in store.undated_cache if item.get("_folder_group") == folder]
            title = folder
            scope_url = f"/places/undated/{folder}"
            back_url = f"/undated/{folder}"
        return render_places_page(
            page_title=f"Places · {title}",
            breadcrumb=f"<a href='/places'>Places</a> / <a href='{back_url}'>{title}</a>",
            context_title=f"Places for {title}",
            scope_type="undated",
            scope_url=scope_url,
            items=items,
            extra_actions=[{"label": "Back", "url": back_url, "active": False}],
        )

    @app.route("/places_bucket")
    def places_bucket():
        token = str(request.args.get("token") or "").strip()
        ids_raw = str(request.args.get("ids") or "").strip()
        label = str(request.args.get("label") or "Place Photos").strip() or "Place Photos"
        place_name = str(request.args.get("place") or "Places").strip() or "Places"
        back = str(request.args.get("back") or "/places").strip() or "/places"

        sha1_order: list[str] = []
        if token:
            payload = places_bucket_store.get(token) or {}
            sha1_order = [str(part).strip() for part in payload.get("ids", []) if str(part).strip()]
            label = str(payload.get("label") or label).strip() or "Place Photos"
            place_name = str(payload.get("place") or place_name).strip() or "Places"
            back = str(payload.get("back") or back).strip() or "/places"
        else:
            sha1_order = [part.strip() for part in ids_raw.split(",") if part.strip()]
        if not sha1_order:
            return "No images", 404

        store.load_cache()
        item_by_sha1 = {str(item.get("sha1")): item for item in store.db_cache}
        imgs = [item_by_sha1[s] for s in sha1_order if s in item_by_sha1]
        if not imgs:
            return "No images", 404

        for item in imgs:
            item["_hero_score"] = store.get_hero_score(item)

        return render_page(
            page_title=label,
            active_tab="places",
            banner_img="hero-places.png",
            breadcrumb=f"<a href='{back}'>Places</a> / {place_name} / {label}",
            photos=imgs,
            manifests={"main_gallery": store.build_manifest(imgs)},
        )

    @app.route("/places_lightbox")
    def places_lightbox():
        token = str(request.args.get("token") or "").strip()
        ids_raw = str(request.args.get("ids") or "").strip()
        selected_sha1 = str(request.args.get("sha1") or "").strip()
        label = str(request.args.get("label") or "Place Photos").strip() or "Place Photos"
        place_name = str(request.args.get("place") or "Places").strip() or "Places"
        back = str(request.args.get("back") or "/places").strip() or "/places"

        sha1_order: list[str] = []
        if token:
            payload = places_bucket_store.get(token) or {}
            sha1_order = [str(part).strip() for part in payload.get("ids", []) if str(part).strip()]
            label = str(payload.get("label") or label).strip() or "Place Photos"
            place_name = str(payload.get("place") or place_name).strip() or "Places"
            back = str(payload.get("back") or back).strip() or "/places"
        else:
            sha1_order = [part.strip() for part in ids_raw.split(",") if part.strip()]
        if not sha1_order:
            return "No images", 404

        store.load_cache()
        item_by_sha1 = {str(item.get("sha1")): item for item in store.db_cache}
        imgs = [item_by_sha1[s] for s in sha1_order if s in item_by_sha1]
        if not imgs:
            return "No images", 404

        for item in imgs:
            item["_hero_score"] = store.get_hero_score(item)

        if selected_sha1 and selected_sha1 not in {str(item.get("sha1")) for item in imgs}:
            selected_sha1 = str(imgs[0].get("sha1") or "")
        elif not selected_sha1:
            selected_sha1 = str(imgs[0].get("sha1") or "")

        initial_index = 0
        for idx, item in enumerate(imgs):
            if str(item.get("sha1")) == selected_sha1:
                initial_index = idx
                break

        return render_page(
            page_title=label,
            active_tab="places",
            banner_img="hero-places.png",
            breadcrumb=f"<a href='{back}'>Places</a> / {place_name} / {label}",
            photos=imgs,
            manifests={"main_gallery": store.build_manifest(imgs)},
            auto_open={"manifest": "main_gallery", "index": initial_index},
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

    @app.route("/map_tiles/<int:z>/<int:x>/<int:y>.png")
    def serve_map_tile(z: int, x: int, y: int):
        tile_path = config.map_tiles_dir / str(z) / str(x) / f"{y}.png"
        if tile_path.exists():
            return send_file(tile_path)
        return ("", 404)

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Life Archive baseline backend")
    parser.add_argument(
        "--archive-root",
        default=os.environ.get("LIFE_ARCHIVE_ROOT", r"c:\LifeArchive"),
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
