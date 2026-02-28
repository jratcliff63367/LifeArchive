import os
import sqlite3
import json
import logging
from collections import defaultdict
from flask import Flask, request, render_template_string, send_from_directory, redirect, url_for

# --- CONFIGURATION ---
ARCHIVE_ROOT = r"C:\website-test" 
DB_PATH = os.path.join(ARCHIVE_ROOT, "archive_index.db")
ASSETS_DIR = os.path.join(ARCHIVE_ROOT, "_web_layout", "assets")

THEME_COLOR = "#bb86fc"
MONTH_MAP = {"01": "January", "02": "February", "03": "March", "04": "April", "05": "May", "06": "June", "07": "July", "08": "August", "09": "September", "10": "October", "11": "November", "12": "December"}
TAG_THRESHOLD = 200

# --- SILENCE FLASK LOGGING ---
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# --- IN-MEMORY CACHE & PRE-COMPUTED DATA ---
DB_CACHE = []
GLOBAL_TAGS = defaultdict(list)

def load_cache():
    global DB_CACHE, GLOBAL_TAGS
    print("Pre-computing and caching 100,000+ photos into RAM for instant browsing...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    photos = conn.execute("SELECT * FROM photos WHERE final_dt IS NOT NULL ORDER BY final_dt ASC").fetchall()
    
    for p in photos:
        p_dict = dict(p)
        
        # 1. Pre-slice dates (Massive Speedup)
        dt = str(p_dict['final_dt'])
        if len(dt) >= 10:
            p_dict['_year'] = dt[:4]
            p_dict['_month'] = dt[5:7]
            p_dict['_decade'] = f"{dt[:3]}0s"
        else:
            p_dict['_year'] = "Unknown"
            p_dict['_month'] = "01"
            p_dict['_decade'] = "Unknown"
            
        # 2. Pre-split tags
        p_dict['_tag_list'] = [t.strip() for t in p_dict['path_tags'].split(',')] if p_dict['path_tags'] else []
        p_dict['_tag_list'] = [t for t in p_dict['_tag_list'] if t]
        
        # 3. Pre-build Master Tag Index
        for t in p_dict['_tag_list']:
            GLOBAL_TAGS[t].append(p_dict)
            
        DB_CACHE.append(p_dict)
        
    print(f"[SUCCESS] {len(DB_CACHE)} photos cached! Data structures optimized. Server is ready.")

def get_filtered_photos(tag_filter=None, source_list=None):
    dataset = source_list if source_list is not None else DB_CACHE
    if not tag_filter:
        return dataset
    return [p for p in dataset if tag_filter in p['_tag_list']]

def get_unique_tags(photo_list):
    tags = set()
    for p in photo_list:
        tags.update(p['_tag_list'])
    return sorted(list(tags))

def get_heroes(photo_list, count=16):
    if not photo_list: return []
    if len(photo_list) <= count: return photo_list
    step = len(photo_list) / count
    return [photo_list[int(i * step)] for i in range(count)]

# --- HTML TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{{ page_title }} | Legacy Archive</title>
    <link rel="preload" as="image" href="/assets/{{ banner_img }}">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;700;900&display=swap" rel="stylesheet">
    <style>
        :root { --accent: {{ theme_color }}; --bg: #0d0d0d; --card-bg: #1a1a1a; }
        body { font-family: 'Inter', sans-serif; background: var(--bg); color: #fff; margin: 0; padding: 0; overflow-x: hidden; }
        
        .hero-banner { 
            width: 100%; height: 300px; background: #111; 
            background-image: linear-gradient(to bottom, rgba(13,13,13,0) 60%, rgba(13,13,13,1) 100%), url('/assets/{{ banner_img }}'); 
            background-size: cover; background-position: center; border-bottom: 1px solid #333; 
        }
        
        .nav-bar { background: rgba(10,10,10,0.9); backdrop-filter: blur(15px); padding: 15px 40px; border-bottom: 1px solid #333; display: flex; gap: 40px; position: sticky; top: 0; z-index: 1000; }
        .nav-bar a { color: #888; text-decoration: none; font-weight: 700; font-size: 0.85em; letter-spacing: 1px; text-transform: uppercase; transition: 0.2s; }
        .nav-bar a.active, .nav-bar a:hover { color: var(--accent); }
        .nav-bar a.active { border-bottom: 2px solid var(--accent); padding-bottom: 5px; }
        #filter-banner { background: var(--accent); color: #000; padding: 12px 40px; font-weight: 800; text-align: center; letter-spacing: 1px; border-bottom: 1px solid #000; }
        #filter-banner a { color: #000; text-decoration: none; }
        #filter-banner a:hover { text-decoration: underline; }
        .content { padding: 40px; max-width: 1600px; margin: 0 auto; }
        h1 { font-size: 3em; font-weight: 900; letter-spacing: -1px; text-transform: uppercase; margin-top: 0; }
        .breadcrumb { color: #888; font-size: 0.9em; margin-bottom: 30px; font-weight: 600; }
        .breadcrumb a { color: var(--accent); text-decoration: none; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 30px; }
        .photo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 15px; }
        .card { background: var(--card-bg); border-radius: 12px; border: 1px solid #333; overflow: hidden; display: flex; flex-direction: column; transition: 0.3s; }
        .card:hover { border-color: var(--accent); transform: translateY(-5px); box-shadow: 0 15px 30px rgba(0,0,0,0.6); }
        .hero-preview { display: grid; grid-template-columns: repeat(4, 1fr); gap: 2px; background: #000; padding: 2px; }
        .hero-preview img { width: 100%; aspect-ratio: 1/1; object-fit: cover; cursor: pointer; transition: 0.2s; }
        .hero-preview img:hover { opacity: 0.7; }
        .dir-info { padding: 20px 20px 10px 20px; flex-grow: 1; display: flex; flex-direction: column; gap: 8px; text-decoration: none; color: inherit; cursor: pointer; }
        .dir-info:hover .card-title { color: var(--accent); }
        .card-title { font-size: 1.4em; font-weight: 800; color: #fff; margin: 0; transition: 0.2s; }
        .card-sub { color: #888; font-size: 0.85em; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; }
        .tag-row { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 20px 20px 20px; }
        .tag-pill { background: #333; color: #ccc; padding: 4px 10px; border-radius: 12px; font-size: 0.75em; font-weight: bold; text-decoration: none; transition: 0.2s; display: inline-block; border: 1px solid transparent; }
        .tag-pill:hover { background: var(--accent); color: #000; transform: scale(1.05); }
        .explore-btn { display: block; width: 100%; background: #111; color: var(--accent); text-align: center; padding: 15px 0; text-decoration: none; font-weight: 800; text-transform: uppercase; border-top: 1px solid #333; transition: 0.2s; }
        .explore-btn:hover { background: var(--accent); color: #000; }
        
        /* LIGHTBOX */
        #lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.95); z-index: 9999; align-items: center; justify-content: center; overflow: hidden; }
        #lightbox.active { display: flex; }
        #lb-img-container { display: flex; align-items: center; justify-content: center; width: 100%; height: 100%; }
        #lb-img { max-width: 90%; max-height: 85vh; object-fit: contain; box-shadow: 0 0 50px rgba(0,0,0,0.8); transition: transform 0.1s ease-out; cursor: grab; }
        #lb-img:active { cursor: grabbing; }
        
        .lb-nav { position: absolute; top: 50%; transform: translateY(-50%); background: none; border: none; color: #fff; font-size: 5em; cursor: pointer; opacity: 0.2; transition: 0.2s; padding: 20px; z-index: 10000; }
        .lb-nav:hover { opacity: 1; color: var(--accent); }
        #lb-prev { left: 20px; } #lb-next { right: 20px; }
        .close-btn { position: absolute; top: 20px; right: 30px; font-size: 3em; cursor: pointer; color: #888; z-index: 10000; }
        .info-trigger { position: absolute; bottom: 30px; right: 30px; background: #222; border: 1px solid #444; color: #fff; padding: 12px 24px; border-radius: 40px; cursor: pointer; font-weight: 800; z-index: 10000; }
        .info-trigger:hover { background: var(--accent); color: #000; }
        #lb-info-panel { position: absolute; bottom: 0; left: 0; right: 0; background: rgba(15,15,15,0.95); backdrop-filter: blur(20px); padding: 30px 50px; border-top: 1px solid #333; display: none; z-index: 10001; }
        #lb-info-panel.visible { display: block; }
        .meta-label { color: var(--accent); font-weight: 900; text-transform: uppercase; font-size: 0.7em; letter-spacing: 2px; }
        .meta-val { font-size: 0.95em; color: #ccc; overflow-wrap: break-word; line-height: 1.4; }
    </style>
</head>
<body>
    <div class="hero-banner"></div>
    <div class="nav-bar">
        <a href="/timeline" class="{{ 'active' if active_tab=='timeline' else '' }}">Timeline</a>
        <a href="/explorer" class="{{ 'active' if active_tab=='file' else '' }}">File View</a>
        <a href="/tags" class="{{ 'active' if active_tab=='tags' else '' }}">Master Tags</a>
    </div>
    
    {% if active_tag %}
    <div id="filter-banner">
        <a href="{{ clear_url }}">ACTIVE FILTER: #{{ active_tag }} &nbsp;&nbsp;[ &#10006; CLEAR FILTER ]</a>
    </div>
    {% endif %}

    <div class="content">
        <h1>{{ page_title }}</h1>
        <div class="breadcrumb">{{ breadcrumb | safe }}</div>
        
        {% if view_type == 'grid' %}
            <div class="grid">
                {% for card in cards %}
                <div class="card">
                    <div class="hero-preview">
                        {% for thumb in card.heroes %}
                            <img src="/thumbs/{{ thumb.sha1 }}.jpg" loading="lazy" onclick="event.preventDefault(); openLB({{ loop.index0 }}, '{{ card.id | replace(\"\'\", \"\\\\'\") }}')">
                        {% endfor %}
                        {% for _ in range(16 - card.heroes|length) %}<div style="background:#111;"></div>{% endfor %}
                    </div>
                    <a href="{{ card.url }}" class="dir-info">
                        <div class="card-title">{{ card.title }}</div>
                        <div class="card-sub">{{ card.subtitle }}</div>
                    </a>
                    {% if card.tags %}
                        <div class="tag-row">
                            {% for tag in card.tags[:5] %}
                            <a href="?tag={{ tag }}" class="tag-pill">#{{ tag }}</a>
                            {% endfor %}
                        </div>
                    {% endif %}
                    <a href="{{ card.url }}" class="explore-btn">{{ card.btn_text }}</a>
                </div>
                {% endfor %}
            </div>
            
        {% elif view_type == 'photos' %}
            <div class="photo-grid">
                {% for p in photos %}
                <div class="card" onclick="openLB({{ loop.index0 }}, 'main_gallery')" style="cursor:pointer;">
                    <img src="/thumbs/{{ p.sha1 }}.jpg" style="width:100%; aspect-ratio:1/1; object-fit:cover;" loading="lazy">
                </div>
                {% endfor %}
            </div>
        {% endif %}
    </div>

    <div id="lightbox">
        <span class="close-btn" onclick="closeLB()">&times;</span>
        <button id="lb-prev" class="lb-nav" onclick="changeImg(-1)">&#10094;</button>
        <div id="lb-img-container">
            <img id="lb-img" src="" alt="Gallery Image">
        </div>
        <button id="lb-next" class="lb-nav" onclick="changeImg(1)">&#10095;</button>
        <div id="info-btn" class="info-trigger" onclick="toggleInfo()">SHOW INFO (I)</div>
        <div id="lb-info-panel">
            <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 30px;">
                <div><span class="meta-label">Filename</span><div id="meta-file" class="meta-val"></div></div>
                <div><span class="meta-label">Capture Date</span><div id="meta-date" class="meta-val"></div></div>
                <div><span class="meta-label">Source Path</span><div id="meta-fqn" class="meta-val"></div></div>
                <div style="grid-column: span 3;"><span class="meta-label">Notes & Tags</span><div id="meta-notes" class="meta-val"></div></div>
            </div>
        </div>
    </div>

    <script>
        const manifests = {{ manifests | tojson | safe }};
        let curManifest = null; let curIdx = 0; let infoOpen = false;
        
        // --- ZOOM & PAN LOGIC ---
        const imgContainer = document.getElementById('lb-img-container');
        const lbImg = document.getElementById('lb-img');
        let scale = 1, panning = false, pointX = 0, pointY = 0, startX = 0, startY = 0;

        function setTransform() {
            lbImg.style.transform = `translate(${pointX}px, ${pointY}px) scale(${scale})`;
        }

        function openLB(idx, manifestKey) { 
            curManifest = manifestKey; curIdx = idx; updateLB(); 
            document.getElementById('lightbox').classList.add('active'); 
            document.body.style.overflow = 'hidden'; 
        }
        
        function closeLB() { 
            document.getElementById('lightbox').classList.remove('active'); 
            document.body.style.overflow = 'auto'; 
        }
        
        function toggleInfo() { 
            infoOpen = !infoOpen; 
            document.getElementById('lb-info-panel').classList.toggle('visible', infoOpen); 
            document.getElementById('info-btn').innerText = infoOpen ? "HIDE INFO (I)" : "SHOW INFO (I)"; 
        }
        
        function updateLB() {
            scale = 1; pointX = 0; pointY = 0; setTransform();
            
            const item = manifests[curManifest][curIdx];
            lbImg.src = item.src;
            document.getElementById('meta-file').innerText = item.filename;
            document.getElementById('meta-date').innerText = item.date + " (" + item.src_type + ")";
            document.getElementById('meta-fqn').innerText = item.fqn;
            document.getElementById('meta-notes').innerText = (item.tags ? "Tags: " + item.tags + "\\n\\n" : "") + (item.notes || "No metadata.");
        }
        
        function changeImg(n) { 
            curIdx = (curIdx + n + manifests[curManifest].length) % manifests[curManifest].length; 
            updateLB(); 
        }

        // --- MOUSE EVENTS FOR ZOOM & PAN ---
        imgContainer.onmousedown = function(e) {
            e.preventDefault();
            startX = e.clientX - pointX;
            startY = e.clientY - pointY;
            panning = true;
        }

        imgContainer.onmouseup = function(e) { panning = false; }
        imgContainer.onmouseleave = function(e) { panning = false; }

        imgContainer.onmousemove = function(e) {
            if (!panning) return;
            pointX = (e.clientX - startX);
            pointY = (e.clientY - startY);
            setTransform();
        }

        imgContainer.onwheel = function(e) {
            e.preventDefault();
            
            const delta = (e.wheelDelta ? e.wheelDelta : -e.deltaY);
            const prevScale = scale;
            
            (delta > 0) ? (scale *= 1.2) : (scale /= 1.2);
            
            if (scale < 1) scale = 1;
            if (scale > 15) scale = 15;
            
            if (scale === 1) {
                pointX = 0; pointY = 0;
            } else {
                // Determine the exact center of the flexbox container to calculate cursor offset
                const ratio = scale / prevScale;
                const rect = imgContainer.getBoundingClientRect();
                const centerX = rect.left + rect.width / 2;
                const centerY = rect.top + rect.height / 2;

                const mouseX = e.clientX - centerX;
                const mouseY = e.clientY - centerY;

                pointX = mouseX - (mouseX - pointX) * ratio;
                pointY = mouseY - (mouseY - pointY) * ratio;
            }
            setTransform();
        }

        // --- KEYBOARD CONTROLS ---
        document.addEventListener('keydown', e => {
            if(!document.getElementById('lightbox').classList.contains('active')) return;
            if(e.key === "ArrowRight") changeImg(1);
            if(e.key === "ArrowLeft") changeImg(-1);
            if(e.key === "Escape") closeLB();
            if(e.key.toLowerCase() === "i") toggleInfo();
        });
    </script>
</body>
</html>
"""

# --- ROUTES ---
@app.route('/')
def index(): return redirect(url_for('timeline'))

@app.route('/assets/<path:filename>')
def serve_assets(filename): return send_from_directory(ASSETS_DIR, filename)

@app.route('/thumbs/<path:filename>')
def serve_thumbs(filename): return send_from_directory(os.path.join(ARCHIVE_ROOT, "_thumbs"), filename)

@app.route('/media/<path:filename>')
def serve_media(filename): return send_from_directory(ARCHIVE_ROOT, filename)

def build_manifest(photos):
    return [{"src": f"/media/{p['rel_fqn'].replace('\\', '/')}", "filename": p['original_filename'], "date": p['final_dt'], "src_type": p['dt_source'], "fqn": p['rel_fqn'], "tags": p['path_tags'], "notes": p['notes']} for p in photos]

# 1. TIMELINE ROUTE
@app.route('/timeline')
@app.route('/timeline/<decade>')
@app.route('/timeline/<decade>/<year>')
@app.route('/timeline/<decade>/<year>/<month>')
def timeline(decade=None, year=None, month=None):
    tag_filter = request.args.get('tag')
    photos = get_filtered_photos(tag_filter)
    cards = []
    manifests = {}
    
    if not decade:
        groups = defaultdict(list)
        for p in photos: groups[p['_decade']].append(p)
        for d in sorted(groups.keys(), reverse=True):
            heroes = get_heroes(groups[d], 16)
            manifests[d] = build_manifest(heroes)
            cards.append({"id": d, "title": d, "subtitle": f"{len(groups[d])} Photos", "heroes": heroes, "tags": get_unique_tags(groups[d]), "url": f"/timeline/{d}" + (f"?tag={tag_filter}" if tag_filter else ""), "btn_text": "View Decade"})
        return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="The Timeline", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb="Archive Root", view_type="grid", cards=cards, manifests=manifests, active_tag=tag_filter, clear_url=request.path)

    elif not year:
        groups = defaultdict(list)
        for p in photos:
            if p['_decade'] == decade: groups[p['_year']].append(p)
        for y in sorted(groups.keys(), reverse=True):
            heroes = get_heroes(groups[y], 16)
            manifests[y] = build_manifest(heroes)
            cards.append({"id": y, "title": y, "subtitle": f"{len(groups[y])} Photos", "heroes": heroes, "tags": get_unique_tags(groups[y]), "url": f"/timeline/{decade}/{y}" + (f"?tag={tag_filter}" if tag_filter else ""), "btn_text": "View Year"})
        return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"The {decade}", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb=f"<a href='/timeline'>Timeline</a> / {decade}", view_type="grid", cards=cards, manifests=manifests, active_tag=tag_filter, clear_url=request.path)

    elif not month:
        groups = defaultdict(list)
        for p in photos:
            if p['_year'] == year: groups[p['_month']].append(p)
        for m in sorted(groups.keys()):
            heroes = get_heroes(groups[m], 16)
            manifests[m] = build_manifest(heroes)
            m_name = MONTH_MAP.get(m, m)
            cards.append({"id": m, "title": m_name, "subtitle": f"{len(groups[m])} Photos", "heroes": heroes, "tags": get_unique_tags(groups[m]), "url": f"/timeline/{decade}/{year}/{m}" + (f"?tag={tag_filter}" if tag_filter else ""), "btn_text": "View Month"})
        return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"{year}", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb=f"<a href='/timeline'>Timeline</a> / <a href='/timeline/{decade}'>{decade}</a> / {year}", view_type="grid", cards=cards, manifests=manifests, active_tag=tag_filter, clear_url=request.path)

    else:
        m_photos = [p for p in photos if p['_year'] == year and p['_month'] == month]
        manifests['main_gallery'] = build_manifest(m_photos)
        m_name = MONTH_MAP.get(month, month)
        bc = f"<a href='/timeline'>Timeline</a> / <a href='/timeline/{decade}'>{decade}</a> / <a href='/timeline/{decade}/{year}'>{year}</a> / {m_name}"
        return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"{m_name} {year}", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb=bc, view_type="photos", photos=m_photos, manifests=manifests, active_tag=tag_filter, clear_url=request.path)

# 2. EXPLORER ROUTE
@app.route('/explorer/', defaults={'req_path': ''})
@app.route('/explorer/<path:req_path>')
def explorer(req_path):
    tag_filter = request.args.get('tag')
    photos = get_filtered_photos(tag_filter)
    
    target_prefix = req_path.replace('/', '\\') + '\\' if req_path else ""
    current_files = []
    subdirs = defaultdict(list)
    
    for p in photos:
        fqn = p['rel_fqn']
        if target_prefix and not fqn.startswith(target_prefix): continue
        rem = fqn[len(target_prefix):]
        parts = rem.split('\\')
        if len(parts) == 1: current_files.append(p)
        else: subdirs[parts[0]].append(p)
            
    cards = []
    manifests = {}
    
    for sub in sorted(subdirs.keys()):
        s_photos = subdirs[sub]
        heroes = get_heroes(s_photos, 16)
        manifests[sub] = build_manifest(heroes)
        url_path = f"{req_path}/{sub}" if req_path else sub
        cards.append({"id": sub, "title": f"📁 {sub}", "subtitle": f"{len(s_photos)} Files", "heroes": heroes, "tags": get_unique_tags(s_photos), "url": f"/explorer/{url_path}" + (f"?tag={tag_filter}" if tag_filter else ""), "btn_text": "View Folder"})

    if current_files:
        manifests['main_gallery'] = build_manifest(current_files)
        
    folder_name = req_path.split('/')[-1] if req_path else "Archive Root"
    bc_parts = req_path.split('/') if req_path else []
    bc = "<a href='/explorer'>ROOT</a>"
    for i, part in enumerate(bc_parts): bc += f" / <a href='/explorer/{'/'.join(bc_parts[:i+1])}'>{part.upper()}</a>"

    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=folder_name, active_tab="file", banner_img="hero-files.png", breadcrumb=bc, view_type="grid" if subdirs else "photos", cards=cards, photos=current_files, manifests=manifests, active_tag=tag_filter, clear_url=request.path)

# 3. TAGS ROUTE
@app.route('/tags')
@app.route('/tags/<tag_name>')
@app.route('/tags/<tag_name>/<decade>')
def tags_view(tag_name=None, decade=None):
    tag_filter = request.args.get('tag')
    
    if not tag_name:
        filtered_tags = {k: v for k, v in GLOBAL_TAGS.items() if len(v) > 1}
        cards = []
        manifests = {}
        for t in sorted(filtered_tags.keys()):
            t_photos = filtered_tags[t]
            if tag_filter:
                t_photos = [p for p in t_photos if tag_filter in p['_tag_list']]
                if not t_photos: continue
            heroes = get_heroes(t_photos, 16)
            manifests[t] = build_manifest(heroes)
            cards.append({"id": t, "title": f"#{t}", "subtitle": f"{len(t_photos)} Photos", "heroes": heroes, "tags": [t], "url": f"/tags/{t}", "btn_text": "View Tag"})
            
        return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Master Tags", active_tab="tags", banner_img="hero-tags.png", breadcrumb="All Extracted Metadata Tags", view_type="grid", cards=cards, manifests=manifests, active_tag=tag_filter, clear_url=request.path)

    else:
        t_photos = GLOBAL_TAGS.get(tag_name, [])
        if tag_filter: 
            t_photos = [p for p in t_photos if tag_filter in p['_tag_list']]
        
        if len(t_photos) > TAG_THRESHOLD and not decade:
            tag_decades = defaultdict(list)
            for p in t_photos: tag_decades[p['_decade']].append(p)
            cards = []
            manifests = {}
            for dec in sorted(tag_decades.keys(), reverse=True):
                dec_photos = tag_decades[dec]
                heroes = get_heroes(dec_photos, 16)
                manifests[dec] = build_manifest(heroes)
                cards.append({"id": dec, "title": dec, "subtitle": f"{len(dec_photos)} Photos", "heroes": heroes, "tags": [tag_name], "url": f"/tags/{tag_name}/{dec}", "btn_text": f"View {dec}"})
            return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"#{tag_name}", active_tab="tags", banner_img="hero-tags.png", breadcrumb=f"<a href='/tags'>Tags</a> / {tag_name}", view_type="grid", cards=cards, manifests=manifests, active_tag=tag_filter, clear_url=request.path)
            
        elif decade:
            dec_photos = [p for p in t_photos if p['_decade'] == decade]
            manifests = {'main_gallery': build_manifest(dec_photos)}
            return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"#{tag_name} - {decade}", active_tab="tags", banner_img="hero-tags.png", breadcrumb=f"<a href='/tags'>Tags</a> / <a href='/tags/{tag_name}'>{tag_name}</a> / {decade}", view_type="photos", photos=dec_photos, manifests=manifests, active_tag=tag_filter, clear_url=request.path)
            
        else:
            manifests = {'main_gallery': build_manifest(t_photos)}
            return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"#{tag_name}", active_tab="tags", banner_img="hero-tags.png", breadcrumb=f"<a href='/tags'>Tags</a> / {tag_name}", view_type="photos", photos=t_photos, manifests=manifests, active_tag=tag_filter, clear_url=request.path)

if __name__ == '__main__':
    load_cache()
    print("Access the archive at: http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)