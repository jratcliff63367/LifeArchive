
import os
import re
import collections
import random
import math
import urllib.parse
from PIL import Image, ImageFile, ImageOps

# Tell Pillow to be forgiving with older, slightly corrupted JPEGs
ImageFile.LOAD_TRUNCATED_IMAGES = True

# --- CONFIGURATION ---
ARCHIVE_DIR = r"C:\GatherPictures"
#ARCHIVE_DIR = r"C:\John-Photos"
WEB_DIR_NAME = "_WebBrowser"
THUMB_SIZE = (400, 400)   
PROGRESS_INTERVAL = 100   

MONTH_MAP = {
    "M01": "January", "M02": "February", "M03": "March", "M04": "April",
    "M05": "May", "M06": "June", "M07": "July", "M08": "August",
    "M09": "September", "M10": "October", "M11": "November", "M12": "December"
}

def clean_year_label(folder_name):
    """Converts Y1992 to 'Year 1992'"""
    return folder_name.replace('Y', 'Year ')

def clean_month_label(month_folder, year_folder):
    """Converts M04 and Y2009 to 'April 2009'"""
    year = year_folder.replace('Y', '')
    month_code = month_folder[-3:] 
    month_name = MONTH_MAP.get(month_code, month_folder)
    return f"{month_name} {year}"

def get_16_heroes(photos_list):
    if not photos_list: 
        return []
    if len(photos_list) <= 16:
        return photos_list
    gps_data = []
    for idx, (thumb, orig) in enumerate(photos_list):
        match = re.search(r'P\d{4}-\d{2}-\d{2}-([+-]\d{2}\.\d{4})([+-]\d{3}\.\d{4})-', os.path.basename(orig))
        if match:
            lat, lon = float(match.group(1)), float(match.group(2))
            if lat != 0.0 or lon != 0.0:
                gps_data.append((lat, lon, idx))
    selected_indices = set()
    if gps_data:
        unique_locs = list(set((lat, lon) for lat, lon, _ in gps_data))
        k = min(16, len(unique_locs))
        random.seed(42) 
        centroids = random.sample(unique_locs, k)
        for _ in range(20): 
            clusters = {c: [] for c in centroids}
            for lat, lon, idx in gps_data:
                closest = min(centroids, key=lambda c: math.hypot(lat-c[0], lon-c[1]))
                clusters[closest].append((lat, lon, idx))
            new_centroids = []
            for c, items in clusters.items():
                if items:
                    avg_lat = sum(i[0] for i in items) / len(items)
                    avg_lon = sum(i[1] for i in items) / len(items)
                    new_centroids.append((avg_lat, avg_lon))
                else:
                    new_centroids.append(c)
            if set(centroids) == set(new_centroids):
                break
            centroids = new_centroids
        for items in clusters.values():
            if items:
                items.sort(key=lambda x: x[2]) 
                selected_indices.add(items[len(items)//2][2])
    remaining_count = 16 - len(selected_indices)
    if remaining_count > 0:
        unselected = [i for i in range(len(photos_list)) if i not in selected_indices]
        if unselected:
            step = max(1.0, len(unselected) / remaining_count)
            for i in range(remaining_count):
                idx = int(i * step)
                if idx < len(unselected):
                    selected_indices.add(unselected[idx])
    final_indices = sorted(list(selected_indices))[:16]
    return [photos_list[i] for i in final_indices]

def extract_and_register_tags(photos_list, global_registry):
    ignore_words = {'img', 'dsc', 'dcp', 'sam', 'photo', 'image', 'jpg', 'jpeg', 'png', 'copy', 'orig'}
    word_counts = collections.Counter()
    for thumb_path, orig_path in photos_list:
        filename = os.path.basename(orig_path)
        clean_name = re.sub(r'^P\d{4}-\d{2}-\d{2}-[+-]\d{2}\.\d{4}[+-]\d{3}\.\d{4}-', '', filename)
        clean_name = os.path.splitext(clean_name)[0]
        words = re.split(r'[^a-zA-Z]+', clean_name)
        valid_words = set()
        for w in words:
            w_lower = w.lower()
            if len(w_lower) > 2 and w_lower not in ignore_words:
                valid_words.add(w_lower)
        for w in valid_words:
            word_counts[w] += 1
            global_registry[w].add((thumb_path, orig_path))
    top_tags = [word.title() for word, count in word_counts.most_common(10) if count >= 5]
    return top_tags[:5]

def build_gallery():
    print(f"\n{'='*40}")
    print("=== PREMIUM UI BUILDER: REFINED LABELS ===")
    print(f"ARCHIVE: {ARCHIVE_DIR}")
    print(f"{'='*40}\n")
    if not os.path.exists(ARCHIVE_DIR):
        print(f"[FATAL] Archive directory {ARCHIVE_DIR} does not exist.")
        return
    web_dir_path = os.path.join(ARCHIVE_DIR, WEB_DIR_NAME)
    thumbs_dir_path = os.path.join(web_dir_path, "thumbs")
    assets_dir_path = os.path.join(web_dir_path, "assets")
    os.makedirs(thumbs_dir_path, exist_ok=True)
    os.makedirs(assets_dir_path, exist_ok=True)

    gallery_data = {}
    global_tag_registry = collections.defaultdict(set)
    for root, dirs, files in os.walk(ARCHIVE_DIR):
        if WEB_DIR_NAME in dirs:
            dirs.remove(WEB_DIR_NAME)
        rel_path = os.path.relpath(root, ARCHIVE_DIR)
        path_parts = rel_path.split(os.sep)
        if len(path_parts) >= 2 and path_parts[0].startswith('Y') and 'M' in path_parts[1]:
            year_folder = path_parts[0]
            month_folder = path_parts[1]
            if year_folder not in gallery_data:
                gallery_data[year_folder] = {}
            if month_folder not in gallery_data[year_folder]:
                gallery_data[year_folder][month_folder] = []
            thumb_dest_dir = os.path.join(thumbs_dir_path, year_folder, month_folder)
            os.makedirs(thumb_dest_dir, exist_ok=True)
            jpg_files = [f for f in files if f.lower().endswith(('.jpg', '.jpeg'))]
            for file in jpg_files:
                original_path = os.path.join(root, file)
                base_name = os.path.splitext(file)[0]
                thumb_filename = f"{base_name}.png"
                thumb_path = os.path.join(thumb_dest_dir, thumb_filename)
                rel_original_path = f"../{year_folder}/{month_folder}/{file}"
                rel_thumb_path = f"thumbs/{year_folder}/{month_folder}/{thumb_filename}"
                if not os.path.exists(thumb_path):
                    try:
                        with Image.open(original_path) as img:
                            img = ImageOps.exif_transpose(img)
                            img.thumbnail(THUMB_SIZE)
                            img.save(thumb_path, "PNG")
                    except: continue
                gallery_data[year_folder][month_folder].append((rel_thumb_path, rel_original_path))
    generate_html(web_dir_path, gallery_data, global_tag_registry)
    print(f"\n[SUCCESS] UI Ready! Check index.html.")

def generate_html(web_dir_path, gallery_data, global_tag_registry):
    css_style = """
    <style>
        :root { --accent: #bb86fc; --bg: #0d0d0d; --card-bg: rgba(30, 30, 30, 0.7); }
        body { font-family: 'Inter', 'Segoe UI', sans-serif; background-color: var(--bg); color: #fff; margin: 0; padding: 0; }
        .hero-banner { 
            width: 100%; height: 400px; 
            background: url('assets/hero-banner.png') no-repeat center center;
            background-size: cover;
            display: flex; align-items: flex-end; justify-content: center;
            border-bottom: 2px solid #222; position: relative;
        }
        .hero-banner::after {
            content: ""; position: absolute; top: 0; left: 0; right: 0; bottom: 0;
            background: linear-gradient(to bottom, rgba(13,13,13,0) 60%, rgba(13,13,13,1) 100%);
        }
        .content-wrap { max-width: 1400px; margin: 0 auto; padding: 40px; position: relative; z-index: 2; }
        h1 { font-size: 3.5em; color: var(--accent); margin-top: -60px; text-transform: uppercase; letter-spacing: 4px; text-shadow: 0 4px 10px rgba(0,0,0,0.5); }
        .subtitle { color: #888; font-size: 1.1em; margin-bottom: 40px; }
        .directory-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 40px; }
        .dir-card { 
            background: var(--card-bg); backdrop-filter: blur(10px); 
            border-radius: 20px; border: 1px solid #333; overflow: hidden; 
            display: flex; flex-direction: column;
            transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
        }
        .dir-card:hover { border-color: var(--accent); box-shadow: 0 20px 40px rgba(0,0,0,0.6); }
        .hero-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; padding: 10px; background: #000; }
        .hero-grid a { aspect-ratio: 1/1; overflow: hidden; display: block; }
        .hero-grid img { width: 100%; height: 100%; object-fit: cover; transition: transform 0.3s; }
        .hero-grid img:hover { transform: scale(1.15); opacity: 0.8; }
        .dir-info { padding: 25px; flex-grow: 1; text-align: left; }
        .dir-title { font-size: 2.2em; font-weight: 800; color: #fff; margin: 0; }
        .dir-count { color: #aaa; font-weight: 600; margin-bottom: 15px; display: block; letter-spacing: 1px; font-size: 0.9em; }
        .tag-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 15px; }
        .tag { background: #222; color: #ddd; padding: 6px 14px; border-radius: 30px; font-size: 0.85em; text-decoration: none; border: 1px solid #444; }
        .tag:hover { background: var(--accent); color: #000; border-color: var(--accent); }
        .explore-btn {
            background: #222; color: #fff; text-align: center; padding: 18px; 
            text-decoration: none; font-weight: 800; text-transform: uppercase; 
            letter-spacing: 2px; border-top: 1px solid #333; transition: all 0.3s;
        }
        .explore-btn:hover { background: var(--accent); color: #000; }
        .nav-bar { padding: 20px 40px; background: #111; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; }
        .nav-bar a { color: #fff; text-decoration: none; font-weight: bold; font-size: 0.9em; letter-spacing: 1px; }
        .gallery-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 20px; }
        .thumb-box { background: #1a1a1a; padding: 8px; border-radius: 12px; transition: transform 0.2s; }
    </style>
    """

    def safe_url(path): return urllib.parse.quote(path, safe='/')

    def get_decade_color(decade):
        colors = {"1950s": "#009688", "1960s": "#FF9800", "1970s": "#795548", "1980s": "#E91E63", "1990s": "#2196F3", "2000s": "#4CAF50", "2010s": "#FFC107", "2020s": "#00BCD4"}
        return colors.get(decade, "#bb86fc")

    decades_data = collections.defaultdict(dict)
    for year, months in gallery_data.items():
        try:
            year_int = int(year[1:])
            decade_str = f"{year_int // 10 * 10}s"
        except: decade_str = "Unknown"
        decades_data[decade_str][year] = months

    grand_total = sum(len(p) for y in gallery_data.values() for p in y.values())

    # 1. Main Index (Decades)
    with open(os.path.join(web_dir_path, "index.html"), 'w', encoding='utf-8') as f:
        f.write(f"<html><head><title>Archive</title>{css_style}</head><body>")
        f.write("<div class='nav-bar'><span>FAMILY LEGACY ARCHIVE</span><a href='all_tags.html'>BROWSE ALL TAGS</a></div>")
        f.write("<div class='hero-banner'></div>")
        f.write(f"<div class='content-wrap'><h1>THE TIMELINE</h1><p class='subtitle'>{grand_total} MOMENTS</p><div class='directory-grid'>")
        for decade in sorted(decades_data.keys()):
            photos = [p for y in decades_data[decade].values() for m in y.values() for p in m]
            heroes, tags = get_16_heroes(photos), extract_and_register_tags(photos, global_tag_registry)
            accent = get_decade_color(decade)
            f.write(f"<div class='dir-card' style='--accent: {accent}'><div class='hero-grid'>")
            for i in range(16):
                if i < len(heroes): f.write(f"<a href='{safe_url(heroes[i][1])}'><img src='{safe_url(heroes[i][0])}' loading='lazy'></a>")
                else: f.write("<div class='empty-slot'></div>")
            f.write(f"</div><div class='dir-info'><div class='dir-title'>{decade}</div><span class='dir-count'>{len(photos)} PHOTOS</span>")
            f.write("<div class='tag-row'>" + "".join([f"<a href='tag_{safe_url(t.lower())}.html' class='tag'>#{t}</a>" for t in tags]) + "</div></div>")
            f.write(f"<a href='{decade}.html' class='explore-btn'>Explore {decade} &rarr;</a></div>")
        f.write("</div></div></body></html>")

    # 2. Decade Pages
    for decade, years in decades_data.items():
        accent = get_decade_color(decade)
        with open(os.path.join(web_dir_path, f"{decade}.html"), 'w', encoding='utf-8') as f:
            f.write(f"<html><head>{css_style}</head><body><div class='nav-bar'><a href='index.html'>← BACK TO MAIN</a></div><div class='hero-banner' style='filter: hue-rotate({random.randint(0,360)}deg) brightness(0.7);'></div>")
            f.write(f"<div class='content-wrap'><h1>THE {decade}</h1><div class='directory-grid'>")
            for year in sorted(years.keys()):
                photos = [p for m in years[year].values() for p in m]
                heroes, tags = get_16_heroes(photos), extract_and_register_tags(photos, global_tag_registry)
                f.write(f"<div class='dir-card' style='--accent: {accent}'><div class='hero-grid'>")
                for i in range(16):
                    if i < len(heroes): f.write(f"<a href='{safe_url(heroes[i][1])}'><img src='{safe_url(heroes[i][0])}' loading='lazy'></a>")
                    else: f.write("<div class='empty-slot'></div>")
                f.write(f"</div><div class='dir-info'><div class='dir-title'>{clean_year_label(year)}</div><span class='dir-count'>{len(photos)} PHOTOS</span>")
                f.write("<div class='tag-row'>" + "".join([f"<a href='tag_{safe_url(t.lower())}.html' class='tag'>#{t}</a>" for t in tags]) + "</div></div>")
                f.write(f"<a href='{year}.html' class='explore-btn'>View {year[1:]} &rarr;</a></div>")
            f.write("</div></div></body></html>")

    # 3. Year Pages
    for year, months in gallery_data.items():
        try:
            year_int = int(year[1:])
            decade_str = f"{year_int // 10 * 10}s"
        except: decade_str = "index"
        with open(os.path.join(web_dir_path, f"{year}.html"), 'w', encoding='utf-8') as f:
            f.write(f"<html><head>{css_style}</head><body><div class='nav-bar'><a href='{decade_str}.html'>← BACK TO {decade_str}</a></div><div class='content-wrap'><h1>{clean_year_label(year)}</h1><div class='directory-grid'>")
            for month, photos in sorted(months.items()):
                heroes, tags = get_16_heroes(photos), extract_and_register_tags(photos, global_tag_registry)
                full_label = clean_month_label(month, year)
                f.write(f"<div class='dir-card'><div class='hero-grid'>")
                for i in range(16):
                    if i < len(heroes): f.write(f"<a href='{safe_url(heroes[i][1])}'><img src='{safe_url(heroes[i][0])}' loading='lazy'></a>")
                    else: f.write("<div class='empty-slot'></div>")
                f.write(f"</div><div class='dir-info'><div class='dir-title'>{full_label.split()[0]}</div><span class='dir-count'>{len(photos)} PHOTOS</span>")
                f.write("<div class='tag-row'>" + "".join([f"<a href='tag_{safe_url(t.lower())}.html' class='tag'>#{t}</a>" for t in tags]) + "</div></div>")
                f.write(f"<a href='{year}_{month}.html' class='explore-btn'>Open {full_label} &rarr;</a></div>")
            f.write("</div></div></body></html>")
        
        for month, photos in months.items():
            full_label = clean_month_label(month, year)
            with open(os.path.join(web_dir_path, f"{year}_{month}.html"), 'w', encoding='utf-8') as f:
                f.write(f"<html><head>{css_style}</head><body><div class='nav-bar'><a href='{year}.html'>← BACK TO {clean_year_label(year)}</a></div><div class='content-wrap'><h2>{full_label}</h2><div class='gallery-grid'>")
                for thumb, orig in photos:
                    f.write(f"<div class='thumb-box'><a href='{safe_url(orig)}'><img src='{safe_url(thumb)}' style='width:100%; border-radius:8px;'></a></div>")
                f.write("</div></div></body></html>")

    # [Tag logic for All Tags remains the same]
    all_tags_path = os.path.join(web_dir_path, "all_tags.html")
    with open(all_tags_path, 'w', encoding='utf-8') as f:
        f.write(f"<html><head><title>Tags</title>{css_style}</head><body><div class='nav-bar'><a href='index.html'>← BACK TO MAIN</a></div>")
        filtered_tags = {t: p for t, p in global_tag_registry.items() if len(p) >= 5}
        f.write(f"<div class='content-wrap'><h1>Master Tag Index</h1><p class='subtitle'>{len(filtered_tags)} tags</p><div class='tag-row'>")
        for tag_lower in sorted(filtered_tags.keys()):
            count = len(filtered_tags[tag_lower])
            f.write(f"<a href='tag_{safe_url(tag_lower)}.html' class='tag'>#{tag_lower.title()} ({count})</a>")
        f.write("</div></div></body></html>")
        for tag_lower, photos_set in filtered_tags.items():
            tag_page_path = os.path.join(web_dir_path, f"tag_{tag_lower}.html")
            with open(tag_page_path, 'w', encoding='utf-8') as f:
                f.write(f"<html><head><title>Tag: #{tag_lower.title()}</title>{css_style}</head><body><div class='nav-bar'><a href='all_tags.html'>← BACK TO ALL TAGS</a></div>")
                f.write(f"<div class='content-wrap'><h1>#{tag_lower.title()}</h1><p class='subtitle'>{len(photos_set)} photos</p><div class='gallery-grid'>")
                for thumb, orig in sorted(list(photos_set)):
                    f.write(f"<div class='thumb-box'><a href='{safe_url(orig)}'><img src='{safe_url(thumb)}' style='width:100%; border-radius:8px;'></a></div>")
                f.write("</div></div></body></html>")

if __name__ == "__main__":
    build_gallery()