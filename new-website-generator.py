import os
import sqlite3
import json
import urllib.parse
import re
from collections import defaultdict
from datetime import datetime

# --- CONFIGURATION ---
ARCHIVE_ROOT = r"C:\Photo-Website" 
DB_PATH = os.path.join(ARCHIVE_ROOT, "archive_index.db")
OUTPUT_DIR = os.path.join(ARCHIVE_ROOT, "_web_layout")
ASSETS_DIR = os.path.join(OUTPUT_DIR, "assets")

THEME_COLOR = "#bb86fc"
MONTH_MAP = {"01": "January", "02": "February", "03": "March", "04": "April", "05": "May", "06": "June", "07": "July", "08": "August", "09": "September", "10": "October", "11": "November", "12": "December"}
TAG_THRESHOLD = 200

def get_db_connection():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Database not found at {DB_PATH}. Run ingestor first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def safe_url(path):
    return urllib.parse.quote(path.replace('\\', '/'))

def get_page_filename(prefix, path_parts):
    if not path_parts: return f"{prefix}.html"
    safe_parts = [re.sub(r'[^a-zA-Z0-9_-]', '_', str(p)) for p in path_parts]
    return f"{prefix}_" + "_".join(safe_parts) + ".html"

def get_heroes(photo_list, count=16):
    if not photo_list: return []
    if len(photo_list) <= count: return photo_list
    step = len(photo_list) / count
    return [photo_list[int(i * step)] for i in range(count)]

def get_all_photos_in_node(node):
    photos = list(node.get('_files', []))
    for key, child in node.items():
        if key != '_files': photos.extend(get_all_photos_in_node(child))
    return photos

def get_unique_tags(photo_list):
    tags = set()
    for p in photo_list:
        if p['path_tags']:
            for t in p['path_tags'].split(','):
                clean_tag = t.strip()
                if clean_tag: tags.add(clean_tag)
    return sorted(list(tags))

# --- HTML TEMPLATES & CSS ---

def generate_header(title, active_tab="timeline", banner_img="hero-timeline.png"):
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>{title} | Legacy Archive</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;700;900&display=swap" rel="stylesheet">
        <style>
            :root {{ --accent: {THEME_COLOR}; --bg: #0d0d0d; --card-bg: #1a1a1a; }}
            body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: #fff; margin: 0; padding: 0; overflow-x: hidden; }}
            
            /* --- BANNERS & NAV --- */
            .hero-banner {{ 
                width: 100%; height: 300px; background: #111; 
                background-image: linear-gradient(to bottom, rgba(13,13,13,0) 60%, rgba(13,13,13,1) 100%), url('assets/{banner_img}');
                background-size: cover; background-position: center; border-bottom: 1px solid #333;
            }}
            .nav-bar {{ 
                background: rgba(10,10,10,0.9); backdrop-filter: blur(15px); padding: 15px 40px; 
                border-bottom: 1px solid #333; display: flex; gap: 40px; position: sticky; top: 0; z-index: 1000; 
            }}
            .nav-bar a {{ color: #888; text-decoration: none; font-weight: 700; font-size: 0.85em; letter-spacing: 1px; text-transform: uppercase; transition: 0.2s; }}
            .nav-bar a.active, .nav-bar a:hover {{ color: var(--accent); }}
            .nav-bar a.active {{ border-bottom: 2px solid var(--accent); padding-bottom: 5px; }}
            
            /* --- LAYOUT --- */
            .content {{ padding: 40px; max-width: 1600px; margin: 0 auto; }}
            h1 {{ font-size: 3em; font-weight: 900; letter-spacing: -1px; text-transform: uppercase; margin-top: 0; }}
            .breadcrumb {{ color: #888; font-size: 0.9em; margin-bottom: 30px; font-weight: 600; }}
            .breadcrumb a {{ color: var(--accent); text-decoration: none; }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 30px; }}
            .photo-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 15px; }}
            
            /* --- CARD ANATOMY --- */
            .card {{ background: var(--card-bg); border-radius: 12px; border: 1px solid #333; overflow: hidden; display: flex; flex-direction: column; transition: 0.3s; }}
            .card:hover {{ border-color: var(--accent); transform: translateY(-5px); box-shadow: 0 15px 30px rgba(0,0,0,0.6); }}
            
            .hero-preview {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 2px; background: #000; padding: 2px; }}
            .hero-preview img {{ width: 100%; aspect-ratio: 1/1; object-fit: cover; cursor: pointer; transition: 0.2s; }}
            .hero-preview img:hover {{ opacity: 0.7; }}
            
            .dir-info {{ padding: 20px 20px 10px 20px; flex-grow: 1; display: flex; flex-direction: column; gap: 8px; transition: 0.2s; cursor: pointer; }}
            .dir-info:hover .card-title {{ color: var(--accent); }}
            .card-title {{ font-size: 1.4em; font-weight: 800; color: #fff; margin: 0; transition: 0.2s; }}
            .card-sub {{ color: #888; font-size: 0.85em; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; }}
            
            .tag-row {{ display: flex; flex-wrap: wrap; gap: 6px; padding: 0 20px 20px 20px; }}
            .tag-pill {{ background: #333; color: #ccc; padding: 4px 10px; border-radius: 12px; font-size: 0.75em; font-weight: bold; text-decoration: none; transition: 0.2s; display: inline-block; border: 1px solid transparent; }}
            .tag-pill:hover {{ background: var(--accent); color: #000; transform: scale(1.05); }}
            
            .explore-btn {{ display: block; width: 100%; background: #111; color: var(--accent); text-align: center; padding: 15px 0; text-decoration: none; font-weight: 800; letter-spacing: 1px; text-transform: uppercase; border-top: 1px solid #333; transition: 0.2s; }}
            .explore-btn:hover {{ background: var(--accent); color: #000; }}

            /* --- LIGHTBOX --- */
            #lightbox {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.95); z-index: 9999; align-items: center; justify-content: center; }}
            #lightbox.active {{ display: flex; }}
            #lb-img {{ max-width: 90%; max-height: 85vh; object-fit: contain; box-shadow: 0 0 50px rgba(0,0,0,0.8); }}
            .lb-nav {{ position: absolute; top: 50%; transform: translateY(-50%); background: none; border: none; color: #fff; font-size: 5em; cursor: pointer; opacity: 0.2; transition: 0.2s; padding: 20px; user-select: none; }}
            .lb-nav:hover {{ opacity: 1; color: var(--accent); }}
            #lb-prev {{ left: 20px; }} #lb-next {{ right: 20px; }}
            .close-btn {{ position: absolute; top: 20px; right: 30px; font-size: 3em; cursor: pointer; color: #888; user-select: none; }}
            
            .info-trigger {{ position: absolute; bottom: 30px; right: 30px; background: #222; border: 1px solid #444; color: #fff; padding: 12px 24px; border-radius: 40px; cursor: pointer; font-weight: 800; font-size: 0.8em; letter-spacing: 1px; user-select: none; transition: 0.2s; }}
            .info-trigger:hover {{ background: var(--accent); color: #000; }}
            #lb-info-panel {{ position: fixed; bottom: 0; left: 0; right: 0; background: rgba(15,15,15,0.95); backdrop-filter: blur(20px); padding: 30px 50px; border-top: 1px solid #333; display: none; z-index: 10001; }}
            #lb-info-panel.visible {{ display: block; }}
            .meta-label {{ color: var(--accent); font-weight: 900; text-transform: uppercase; font-size: 0.7em; letter-spacing: 2px; margin-bottom: 5px; display: block; }}
            .meta-val {{ font-size: 0.95em; color: #ccc; overflow-wrap: break-word; line-height: 1.4; }}
        </style>
    </head>
    <body>
        <div class="hero-banner"></div>
        <div class="nav-bar">
            <a href="index.html" class="{'active' if active_tab=='timeline' else ''}">Timeline</a>
            <a href="explorer.html" class="{'active' if active_tab=='file' else ''}">File View</a>
            <a href="tags.html" class="{'active' if active_tab=='tags' else ''}">Master Tags</a>
        </div>
    """

LIGHTBOX_HTML = """
<div id="lightbox">
    <span class="close-btn" onclick="closeLB()">&times;</span>
    <button id="lb-prev" class="lb-nav" onclick="changeImg(-1)">&#10094;</button>
    <img id="lb-img" src="" alt="Gallery Image">
    <button id="lb-next" class="lb-nav" onclick="changeImg(1)">&#10095;</button>
    <div id="info-btn" class="info-trigger" onclick="toggleInfo()">SHOW INFO (I)</div>
    <div id="lb-info-panel">
        <div id="lb-meta-grid" style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 30px;">
            <div><span class="meta-label">Filename</span><div id="meta-file" class="meta-val"></div></div>
            <div><span class="meta-label">Capture Date</span><div id="meta-date" class="meta-val"></div></div>
            <div><span class="meta-label">Source Path</span><div id="meta-fqn" class="meta-val"></div></div>
            <div style="grid-column: span 3;"><span class="meta-label">Notes & Tags</span><div id="meta-notes" class="meta-val"></div></div>
        </div>
    </div>
</div>
<script>
    let currentManifestKey = null; let curIdx = 0; let infoOpen = false;
    
    function openLB(idx, manifestKey) { 
        currentManifestKey = manifestKey; curIdx = idx; updateLB(); 
        document.getElementById('lightbox').classList.add('active'); 
        document.body.style.overflow = 'hidden'; 
    }
    
    function closeLB() { 
        document.getElementById('lightbox').classList.remove('active'); 
        document.body.style.overflow = 'auto'; infoOpen = false; 
        document.getElementById('lb-info-panel').classList.remove('visible'); 
        document.getElementById('info-btn').innerText = "SHOW INFO (I)"; 
    }
    
    function toggleInfo() { 
        infoOpen = !infoOpen; 
        document.getElementById('lb-info-panel').classList.toggle('visible', infoOpen); 
        document.getElementById('info-btn').innerText = infoOpen ? "HIDE INFO (I)" : "SHOW INFO (I)"; 
    }
    
    function updateLB() {
        const item = pageManifests[currentManifestKey][curIdx];
        document.getElementById('lb-img').src = item.src;
        document.getElementById('meta-file').innerText = item.filename;
        document.getElementById('meta-date').innerText = item.date + " (" + item.src_type + ")";
        document.getElementById('meta-fqn').innerText = item.fqn;
        let notesText = "";
        if (item.tags) notesText += "Tags: " + item.tags + "\\n\\n";
        notesText += item.notes || "No additional metadata found.";
        document.getElementById('meta-notes').innerText = notesText;
    }
    
    function changeImg(n) { 
        const dataset = pageManifests[currentManifestKey]; 
        curIdx = (curIdx + n + dataset.length) % dataset.length; 
        updateLB(); 
    }
    
    document.addEventListener('keydown', e => {
        if(!document.getElementById('lightbox').classList.contains('active')) return;
        if(e.key === "ArrowRight") changeImg(1);
        if(e.key === "ArrowLeft") changeImg(-1);
        if(e.key === "Escape") closeLB();
        if(e.key.toLowerCase() === "i") toggleInfo();
    });
</script>
"""

def build_card_html(title, subtitle, heroes, link_url, link_text, manifest_key, tags=None):
    html = f"<div class='card'><div class='hero-preview'>"
    for i, h in enumerate(heroes): html += f"<img src='../_thumbs/{h['sha1']}.jpg' onclick='event.preventDefault(); openLB({i}, \"{manifest_key}\")'>"
    for _ in range(16 - len(heroes)): html += "<div style='background:#111;'></div>"
    html += f"</div>"
    
    # 1. Make the Folder Info block fully clickable
    html += f"<a href='{link_url}' class='dir-info' style='text-decoration:none; color:inherit;'>"
    html += f"<div class='card-title'>{title}</div><div class='card-sub'>{subtitle}</div>"
    html += "</a>"
    
    # 2. Make Tag Pills direct links to their Master Tag Pages
    if tags:
        html += "<div class='tag-row'>"
        for t in tags[:5]: 
            tag_url = get_page_filename("tag", [t])
            html += f"<a href='{tag_url}' class='tag-pill'>#{t}</a>"
        html += "</div>"
        
    html += f"<a href='{link_url}' class='explore-btn'>{link_text}</a></div>"
    return html

def build_website():
    conn = get_db_connection()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(ASSETS_DIR, exist_ok=True)
    
    photos = conn.execute("SELECT * FROM photos WHERE final_dt IS NOT NULL ORDER BY final_dt ASC").fetchall()
    
    # 1. Structures
    timeline = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for p in photos:
        try:
            dt = datetime.strptime(p['final_dt'][:19], '%Y-%m-%d %H:%M:%S')
            timeline[f"{dt.year // 10 * 10}s"][dt.year][f"{dt.month:02d}"].append(p)
        except: continue

    explorer_tree = lambda: defaultdict(explorer_tree)
    root_node = explorer_tree()
    for p in photos:
        curr = root_node
        for part in p['rel_fqn'].replace('\\', '/').split('/')[:-1]: curr = curr[part]
        if '_files' not in curr: curr['_files'] = []
        curr['_files'].append(p)

    tag_registry = defaultdict(list)
    for p in photos:
        if p['path_tags']:
            for t in [x.strip() for x in p['path_tags'].split(',') if x.strip()]: tag_registry[t].append(p)

    def write_page(file_name, html_content, manifest_dict):
        script_block = f"<script>\nconst pageManifests = {json.dumps(manifest_dict)};\n</script>"
        with open(os.path.join(OUTPUT_DIR, file_name), 'w', encoding='utf-8') as f:
            f.write(html_content.replace("</body></html>", script_block + "\n</body></html>"))

    def create_manifest(photo_list):
        return [{"src": f"../{safe_url(p['rel_fqn'])}", "filename": p['original_filename'], "date": p['final_dt'], "src_type": p['dt_source'], "fqn": p['rel_fqn'], "tags": p['path_tags'], "notes": p['notes']} for p in photo_list]

    # --- BUILD TIMELINE ---
    page_manifests = {}
    html = generate_header("The Timeline", "timeline", "hero-timeline.png")
    html += f"<div class='content'><h1>THE TIMELINE</h1><div class='breadcrumb'>Archive Root</div><div class='grid'>"
    for decade in sorted(timeline.keys(), reverse=True):
        d_photos = [p for y in timeline[decade].values() for m in y.values() for p in m]
        heroes = get_heroes(d_photos, 16)
        d_tags = get_unique_tags(d_photos)
        m_key = f"dec_{decade}"
        page_manifests[m_key] = create_manifest(heroes)
        html += build_card_html(decade, f"{len(d_photos)} Photos", heroes, f"time_{decade}.html", "Explore Decade", m_key, d_tags)
    html += "</div></div>" + LIGHTBOX_HTML + "</body></html>"
    write_page("index.html", html, page_manifests)

    for decade, years in timeline.items():
        page_manifests = {}
        html = generate_header(f"The {decade}", "timeline", "hero-timeline.png")
        html += f"<div class='content'><h1>The {decade}</h1><div class='breadcrumb'><a href='index.html'>Timeline</a> / {decade}</div><div class='grid'>"
        for year in sorted(years.keys(), reverse=True):
            y_photos = [p for m in years[year].values() for p in m]
            heroes = get_heroes(y_photos, 16)
            y_tags = get_unique_tags(y_photos)
            m_key = f"yr_{year}"
            page_manifests[m_key] = create_manifest(heroes)
            html += build_card_html(year, f"{len(y_photos)} Photos", heroes, f"time_{year}.html", "Explore Year", m_key, y_tags)
        html += "</div></div>" + LIGHTBOX_HTML + "</body></html>"
        write_page(f"time_{decade}.html", html, page_manifests)

    for decade, years in timeline.items():
        for year, months in years.items():
            page_manifests = {}
            html = generate_header(f"{year}", "timeline", "hero-timeline.png")
            html += f"<div class='content'><h1>{year}</h1><div class='breadcrumb'><a href='index.html'>Timeline</a> / <a href='time_{decade}.html'>{decade}</a> / {year}</div><div class='grid'>"
            for month in sorted(months.keys()):
                m_photos = months[month]
                heroes = get_heroes(m_photos, 16)
                m_tags = get_unique_tags(m_photos)
                m_key = f"mo_{year}_{month}"
                page_manifests[m_key] = create_manifest(heroes)
                month_name = MONTH_MAP.get(month, f"Month {month}")
                html += build_card_html(month_name, f"{len(m_photos)} Photos", heroes, f"time_{year}_{month}.html", "View Month", m_key, m_tags)
            html += "</div></div>" + LIGHTBOX_HTML + "</body></html>"
            write_page(f"time_{year}.html", html, page_manifests)
            
            for month, m_photos in months.items():
                m_page_manifests = {}
                month_name = MONTH_MAP.get(month, f"Month {month}")
                html = generate_header(f"{month_name} {year}", "timeline", "hero-timeline.png")
                html += f"<div class='content'><h1>{month_name} {year}</h1><div class='breadcrumb'><a href='index.html'>Timeline</a> / <a href='time_{decade}.html'>{decade}</a> / <a href='time_{year}.html'>{year}</a> / {month_name}</div><div class='photo-grid'>"
                m_key = "month_view"
                m_page_manifests[m_key] = create_manifest(m_photos)
                for i, p in enumerate(m_photos):
                    html += f"<div class='card' onclick='openLB({i}, \"{m_key}\")' style='cursor:pointer;'><img src='../_thumbs/{p['sha1']}.jpg' style='width:100%; aspect-ratio:1/1; object-fit:cover;' loading='lazy'></div>"
                html += "</div></div>" + LIGHTBOX_HTML + "</body></html>"
                write_page(f"time_{year}_{month}.html", html, m_page_manifests)

    # --- BUILD EXPLORER TREE ---
    def generate_explorer(node, path_parts):
        folder_name = path_parts[-1] if path_parts else "Archive Root"
        file_name = get_page_filename("explorer", path_parts)
        subfolders = [k for k in node.keys() if k != '_files']
        folder_files = node.get('_files', [])
        page_manifests = {}
        
        html = generate_header(folder_name, "file", "hero-files.png")
        html += f"<div class='content'><h1>{folder_name}</h1><div class='breadcrumb'><a href='explorer.html'>ROOT</a>"
        for i, p in enumerate(path_parts): html += f" / <a href='{get_page_filename('explorer', path_parts[:i+1])}'>{p.upper()}</a>"
        html += "</div>"

        if subfolders:
            html += "<div class='grid' style='margin-bottom: 60px;'>"
            for sub in sorted(subfolders):
                sub_node = node[sub]
                all_sub_photos = get_all_photos_in_node(sub_node)
                f_heroes = get_heroes(all_sub_photos, 16)
                sub_tags = get_unique_tags(all_sub_photos)
                m_key = f"sub_{sub}"
                page_manifests[m_key] = create_manifest(f_heroes)
                html += build_card_html(f"📁 {sub}", f"{len(all_sub_photos)} Files", f_heroes, get_page_filename('explorer', path_parts + [sub]), "Open Folder", m_key, sub_tags)
            html += "</div>"

        if folder_files:
            html += "<h2>Photos in this Folder</h2><div class='photo-grid'>"
            m_key = "folder_view"
            page_manifests[m_key] = create_manifest(folder_files)
            for i, p in enumerate(folder_files): 
                html += f"<div class='card' onclick='openLB({i}, \"{m_key}\")' style='cursor:pointer;'><img src='../_thumbs/{p['sha1']}.jpg' style='width:100%; aspect-ratio:1/1; object-fit:cover;' loading='lazy'></div>"
            html += "</div>"
        html += "</div>" + LIGHTBOX_HTML + "</body></html>"
        write_page(file_name, html, page_manifests)
        for sub in subfolders: generate_explorer(node[sub], path_parts + [sub])

    generate_explorer(root_node, [])

    # --- BUILD TAGS INDEX ---
    page_manifests = {}
    html = generate_header("Master Tag Index", "tags", "hero-tags.png")
    html += f"<div class='content'><h1>Master Tags</h1><div class='breadcrumb'>All Extracted Metadata Tags</div><div class='grid'>"
    
    filtered_tags = {k: v for k, v in tag_registry.items() if len(v) > 1}
    for tag in sorted(filtered_tags.keys()):
        t_photos = filtered_tags[tag]
        heroes = get_heroes(t_photos, 16)
        m_key = f"tag_{tag}"
        page_manifests[m_key] = create_manifest(heroes)
        html += build_card_html(f"#{tag}", f"{len(t_photos)} Photos", heroes, get_page_filename("tag", [tag]), "View Tag", m_key, [tag])
    html += "</div></div>" + LIGHTBOX_HTML + "</body></html>"
    write_page("tags.html", html, page_manifests)

    # --- INDIVIDUAL TAG PAGES (With Threshold Drill-Down) ---
    for tag, t_photos in filtered_tags.items():
        if len(t_photos) > TAG_THRESHOLD:
            t_page_manifests = {}
            html = generate_header(f"#{tag}", "tags", "hero-tags.png")
            html += f"<div class='content'><h1>#{tag}</h1><div class='breadcrumb'><a href='tags.html'>Tags</a> / {tag}</div><div class='grid'>"
            
            tag_decades = defaultdict(list)
            for p in t_photos:
                try: tag_decades[f"{datetime.strptime(p['final_dt'][:19], '%Y-%m-%d %H:%M:%S').year // 10 * 10}s"].append(p)
                except: pass
            
            for dec in sorted(tag_decades.keys(), reverse=True):
                dec_photos = tag_decades[dec]
                heroes = get_heroes(dec_photos, 16)
                m_key = f"tag_{tag}_{dec}"
                t_page_manifests[m_key] = create_manifest(heroes)
                html += build_card_html(f"{dec}", f"{len(dec_photos)} Photos", heroes, get_page_filename("tag", [tag, dec]), f"View {dec}", m_key, [tag])
            html += "</div></div>" + LIGHTBOX_HTML + "</body></html>"
            write_page(get_page_filename("tag", [tag]), html, t_page_manifests)
            
            for dec, dec_photos in tag_decades.items():
                d_page_manifests = {}
                html = generate_header(f"#{tag} - {dec}", "tags", "hero-tags.png")
                html += f"<div class='content'><h1>#{tag} - {dec}</h1><div class='breadcrumb'><a href='tags.html'>Tags</a> / <a href='{get_page_filename('tag', [tag])}'>{tag}</a> / {dec}</div><div class='photo-grid'>"
                m_key = "tag_dec_view"
                d_page_manifests[m_key] = create_manifest(dec_photos)
                for i, p in enumerate(dec_photos):
                    html += f"<div class='card' onclick='openLB({i}, \"{m_key}\")' style='cursor:pointer;'><img src='../_thumbs/{p['sha1']}.jpg' style='width:100%; aspect-ratio:1/1; object-fit:cover;' loading='lazy'></div>"
                html += "</div></div>" + LIGHTBOX_HTML + "</body></html>"
                write_page(get_page_filename("tag", [tag, dec]), html, d_page_manifests)

        else:
            t_page_manifests = {}
            html = generate_header(f"#{tag}", "tags", "hero-tags.png")
            html += f"<div class='content'><h1>#{tag}</h1><div class='breadcrumb'><a href='tags.html'>Tags</a> / {tag}</div><div class='photo-grid'>"
            m_key = "tag_view"
            t_page_manifests[m_key] = create_manifest(t_photos)
            for i, p in enumerate(t_photos):
                html += f"<div class='card' onclick='openLB({i}, \"{m_key}\")' style='cursor:pointer;'><img src='../_thumbs/{p['sha1']}.jpg' style='width:100%; aspect-ratio:1/1; object-fit:cover;' loading='lazy'></div>"
            html += "</div></div>" + LIGHTBOX_HTML + "</body></html>"
            write_page(get_page_filename("tag", [tag]), html, t_page_manifests)

    print(f"\n[SUCCESS] Web Build Complete. Output directory: {OUTPUT_DIR}")
pi
if __name__ == "__main__":
    build_website()