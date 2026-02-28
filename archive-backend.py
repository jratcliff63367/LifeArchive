import os
import sqlite3
import json
import logging
import mimetypes
import hashlib
from collections import defaultdict
from flask import Flask, request, render_template_string, send_from_directory, redirect, url_for, jsonify
from PIL import Image

# --- CONFIGURATION ---
ARCHIVE_ROOT = r"C:\website-test" 
DB_PATH = os.path.join(ARCHIVE_ROOT, "archive_index.db")
ASSETS_DIR = os.path.join(ARCHIVE_ROOT, "_web_layout", "assets")
THUMB_DIR = os.path.join(ARCHIVE_ROOT, "_thumbs")
COMPOSITE_DIR = os.path.join(THUMB_DIR, "_composites")

os.makedirs(COMPOSITE_DIR, exist_ok=True)

THEME_COLOR = "#bb86fc"
MONTH_MAP = {"01": "January", "02": "February", "03": "March", "04": "April", "05": "May", "06": "June", "07": "July", "08": "August", "09": "September", "10": "October", "11": "November", "12": "December"}

# --- SILENCE FLASK LOGGING ---
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# --- IN-MEMORY CACHE ---
DB_CACHE = []
GLOBAL_TAGS = defaultdict(list)

def load_cache():
    global DB_CACHE, GLOBAL_TAGS
    print("Loading database into RAM for instant browsing...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM media WHERE is_deleted = 0 ORDER BY final_dt DESC")
    rows = cursor.fetchall()
    
    DB_CACHE = []
    GLOBAL_TAGS.clear()
    for row in rows:
        dt_str = row['final_dt'][:10]
        yyyy, mm, dd = dt_str.split('-')
        item = dict(row)
        item.update({'_year': yyyy, '_month': mm, '_decade': yyyy[:3] + "0s"})
        
        tags = []
        if row['path_tags']: tags.extend([t.strip() for t in row['path_tags'].split(',')])
        if row['custom_tags']: tags.extend([t.strip() for t in row['custom_tags'].split(',')])
        item['_tags_list'] = [t for t in tags if t]
        
        DB_CACHE.append(item)
        for t in item['_tags_list']: GLOBAL_TAGS[t.title()].append(item)
    conn.close()

def get_composite_hash(heroes):
    """Generates a 4x4 composite image. Centers non-square images within their slots."""
    if not heroes: return None
    sha1_list = [h['sha1'] for h in heroes[:16]]
    comp_hash = hashlib.md5("".join(sha1_list).encode('utf-8')).hexdigest()
    comp_path = os.path.join(COMPOSITE_DIR, f"{comp_hash}.jpg")
    
    if os.path.exists(comp_path): return comp_hash
    
    # 400x400 grid (100x100 slots)
    canvas = Image.new('RGB', (400, 400), color=(17, 17, 17))
    for i, sha1 in enumerate(sha1_list):
        t_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
        if os.path.exists(t_path):
            try:
                with Image.open(t_path) as img:
                    # 'thumbnail' preserves aspect ratio
                    img.thumbnail((100, 100), Image.Resampling.LANCZOS)
                    w, h = img.size
                    # Calculate center within the 100x100 slot
                    x_off = (i % 4) * 100 + (100 - w) // 2
                    y_off = (i // 4) * 100 + (100 - h) // 2
                    canvas.paste(img, (x_off, y_off))
            except: pass
    canvas.save(comp_path, "JPEG", quality=85)
    return comp_hash

def build_manifest(media_list):
    return [{
        'sha1': p['sha1'], 'path': p['rel_fqn'], 'filename': p['original_filename'],
        'date': p['final_dt'], 'src_type': p['dt_source'], 'fqn': p['rel_fqn'],
        'type': p['media_type'], 'takeout_notes': p['takeout_notes'],
        'custom_notes': p['custom_notes'], 'custom_tags': p['custom_tags'],
        'is_favorite': p['is_favorite']
    } for p in media_list]

# --- HTML TEMPLATE (Unified V2) ---
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
        .hero-banner { width: 100%; height: 300px; background: #111; background-image: linear-gradient(to bottom, rgba(13,13,13,0) 60%, rgba(13,13,13,1) 100%), url('/assets/{{ banner_img }}'); background-size: cover; background-position: center; border-bottom: 1px solid #333; }
        .nav-bar { background: rgba(10,10,10,0.9); backdrop-filter: blur(15px); padding: 15px 40px; border-bottom: 1px solid #333; display: flex; gap: 40px; position: sticky; top: 0; z-index: 1000; }
        .nav-bar a { color: #888; text-decoration: none; font-weight: 700; font-size: 0.85em; letter-spacing: 1px; text-transform: uppercase; }
        .nav-bar a.active, .nav-bar a:hover { color: var(--accent); }
        .nav-bar a.active { border-bottom: 2px solid var(--accent); padding-bottom: 5px; }
        .content { padding: 40px; max-width: 1600px; margin: 0 auto; }
        h1 { font-size: 3em; font-weight: 900; letter-spacing: -1px; text-transform: uppercase; margin-top: 0; }
        .breadcrumb { color: #888; font-size: 0.9em; margin-bottom: 30px; font-weight: 600; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 30px; }
        .photo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 15px; }
        .card { background: var(--card-bg); border-radius: 12px; border: 1px solid #333; overflow: hidden; display: flex; flex-direction: column; transition: 0.3s; position: relative;}
        .card:hover { border-color: var(--accent); transform: translateY(-5px); box-shadow: 0 15px 30px rgba(0,0,0,0.6); }
        .hero-preview { width: 100%; aspect-ratio: 1/1; background: #000; overflow: hidden; }
        .hero-preview img { width: 100%; height: 100%; object-fit: cover; cursor: pointer; display: block; }
        .dir-info { padding: 20px 20px 10px 20px; flex-grow: 1; text-decoration: none; color: inherit; cursor: pointer; }
        .card-title { font-size: 1.4em; font-weight: 800; color: #fff; margin: 0; }
        .card-sub { color: #888; font-size: 0.85em; font-weight: 700; text-transform: uppercase; }
        .explore-btn { display: block; width: 100%; background: #111; color: var(--accent); text-align: center; padding: 15px 0; text-decoration: none; font-weight: 800; text-transform: uppercase; border-top: 1px solid #333; }

        /* LIGHTBOX & SIDEBAR */
        #lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.95); z-index: 9999; align-items: center; justify-content: center; }
        #lightbox.active { display: flex; }
        #lb-img-container { display: flex; align-items: center; justify-content: center; width: 100%; height: 100%; }
        #lb-img { max-width: 90%; max-height: 85vh; object-fit: contain; cursor: grab; transition: transform 0.1s ease-out; }
        #lb-video { max-width: 90%; max-height: 85vh; display: none; outline: none; }
        .lb-nav { position: absolute; top: 50%; transform: translateY(-50%); background: none; border: none; color: #fff; font-size: 5em; cursor: pointer; opacity: 0.2; z-index: 10000; }
        #lb-prev { left: 20px; } #lb-next { right: 20px; }
        .close-btn { position: absolute; top: 20px; right: 30px; font-size: 3em; cursor: pointer; color: #888; z-index: 10000; }
        .info-trigger { position: absolute; bottom: 30px; right: 30px; background: #222; border: 1px solid #444; color: #fff; padding: 12px 24px; border-radius: 40px; cursor: pointer; font-weight: 800; z-index: 10000; }
        
        #lb-sidebar { position: absolute; top: 0; right: -450px; width: 450px; bottom: 0; background: rgba(15,15,15,0.98); backdrop-filter: blur(20px); border-left: 1px solid #333; padding: 40px 30px; z-index: 10001; transition: 0.3s ease-in-out; box-sizing: border-box; overflow-y: auto; }
        #lb-sidebar.visible { right: 0; }
        .meta-label { color: var(--accent); font-weight: 900; text-transform: uppercase; font-size: 0.75em; letter-spacing: 2px; display: block; margin-top: 15px; }
        .meta-val { font-size: 0.95em; color: #ccc; }
        textarea, input { width: 100%; background: #222; border: 1px solid #444; color: #fff; padding: 10px; margin-top: 5px; border-radius: 4px; box-sizing: border-box; }
        .action-btn { background: var(--accent); color: #000; border: none; padding: 10px; border-radius: 4px; font-weight: bold; width: 100%; margin-top: 10px; cursor: pointer; }
        .btn-fav { background: transparent; border: 2px solid gold; color: gold; width: 100%; padding: 10px; border-radius: 4px; margin-top: 10px; cursor: pointer; font-weight: bold; }
        .btn-fav.active { background: gold; color: #000; }
    </style>
</head>
<body>
    <div class="hero-banner"></div>
    <div class="nav-bar">
        <a href="/" class="{{ 'active' if active_tab=='timeline' else '' }}">Timeline</a>
        <a href="/folder" class="{{ 'active' if active_tab=='explorer' else '' }}">File View</a>
        <a href="/tags" class="{{ 'active' if active_tab=='tags' else '' }}">Master Tags</a>
    </div>

    <div class="content">
        <h1>{{ page_title }}</h1>
        <div class="breadcrumb">{{ breadcrumb | safe }}</div>
        
        {% if view_type == 'grid' %}
            <div class="grid">
                {% for card in cards %}
                <div class="card">
                    <div class="hero-preview">
                        {% if card.comp_hash %}
                            <img src="/composite/{{ card.comp_hash }}.jpg" loading="lazy" onclick="handleGridClick(event, '{{ card.id | replace(\"'\", \"\\\\'\") }}')">
                        {% endif %}
                    </div>
                    <a href="{{ card.url }}" class="dir-info">
                        <div class="card-title">{{ card.title }}</div>
                        <div class="card-sub">{{ card.subtitle }}</div>
                    </a>
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
            <img id="lb-img" src="">
            <video id="lb-video" controls autoplay></video>
        </div>
        <button id="lb-next" class="lb-nav" onclick="changeImg(1)">&#10095;</button>
        <div class="info-trigger" onclick="toggleInfo()">CURATE (E)</div>
        
        <div id="lb-sidebar">
            <h2 style="margin:0;">Curation</h2>
            <span class="meta-label">File</span><div id="meta-file" class="meta-val"></div>
            <span class="meta-label">Date</span><div id="meta-date" class="meta-val"></div>
            <div id="takeout-notes-container" style="margin-top:15px; background:#222; padding:10px;">
                <span class="meta-label">Original Notes</span><div id="lb-takeout-notes" class="meta-val"></div>
            </div>
            <button id="btn-fav" class="btn-fav" onclick="toggleFavorite()">★ Favorite</button>
            <span class="meta-label">Custom Notes</span><textarea id="input-notes"></textarea>
            <span class="meta-label">Custom Tags</span><input type="text" id="input-tags">
            <button class="action-btn" onclick="saveMetadata()">Save Edits</button>
            <button class="action-btn" style="background:#cf6679; margin-top:30px;" onclick="softDelete()">Hide File</button>
            <div id="save-status" style="margin-top:10px; color:var(--accent); text-align:center;"></div>
        </div>
    </div>

    <script>
        const manifests = {{ manifests | tojson | safe }};
        let curManifest = null; let curIdx = 0; let infoOpen = false; let curSha1 = '';
        const imgCont = document.getElementById('lb-img-container');
        const lbImg = document.getElementById('lb-img');
        const lbVideo = document.getElementById('lb-video');
        let scale = 1, panning = false, pointX = 0, pointY = 0, startX = 0, startY = 0;

        function handleGridClick(event, manifestKey) {
            const rect = event.target.getBoundingClientRect();
            const x = event.clientX - rect.left;
            const y = event.clientY - rect.top;
            const tileW = rect.width / 4;
            const tileH = rect.height / 4;
            let clickedIdx = (Math.floor(y / tileH) * 4) + Math.floor(x / tileW);
            const maxIdx = manifests[manifestKey].length - 1;
            if (clickedIdx > maxIdx) clickedIdx = maxIdx;
            openLB(clickedIdx, manifestKey);
        }

        function setTransform() { lbImg.style.transform = `translate(${pointX}px, ${pointY}px) scale(${scale})`; }
        function openLB(idx, mKey) { curManifest = mKey; curIdx = idx; updateLB(); document.getElementById('lightbox').classList.add('active'); document.body.style.overflow = 'hidden'; }
        function closeLB() { document.getElementById('lightbox').classList.remove('active'); document.body.style.overflow = 'auto'; lbVideo.pause(); lbVideo.src = ""; document.getElementById('lb-sidebar').classList.remove('visible'); infoOpen = false; }
        function toggleInfo() { infoOpen = !infoOpen; document.getElementById('lb-sidebar').classList.toggle('visible', infoOpen); }

        function updateLB() {
            scale = 1; pointX = 0; pointY = 0; setTransform();
            const item = manifests[curManifest][curIdx];
            curSha1 = item.sha1;
            if (item.type === 'video') { lbImg.style.display = 'none'; lbVideo.style.display = 'block'; lbVideo.src = "/media/" + encodeURIComponent(item.path); }
            else { lbVideo.style.display = 'none'; lbVideo.pause(); lbImg.style.display = 'block'; lbImg.src = "/media/" + encodeURIComponent(item.path); }
            document.getElementById('meta-file').innerText = item.filename;
            document.getElementById('meta-date').innerText = item.date;
            document.getElementById('lb-takeout-notes').innerText = item.takeout_notes || '';
            document.getElementById('input-notes').value = item.custom_notes || '';
            document.getElementById('input-tags').value = item.custom_tags || '';
            const fBtn = document.getElementById('btn-fav');
            fBtn.classList.toggle('active', item.is_favorite);
            fBtn.innerText = item.is_favorite ? '★ Favorited' : '☆ Favorite';
        }

        function changeImg(n) { curIdx = (curIdx + n + manifests[curManifest].length) % manifests[curManifest].length; updateLB(); }

        function toggleFavorite() {
            const item = manifests[curManifest][curIdx];
            fetch(`/api/favorite/${curSha1}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({is_favorite: !item.is_favorite})})
            .then(() => { item.is_favorite = !item.is_favorite; updateLB(); });
        }

        function saveMetadata() {
            const n = document.getElementById('input-notes').value;
            const t = document.getElementById('input-tags').value;
            const s = document.getElementById('save-status'); s.innerText = 'Saving...';
            fetch(`/api/update_metadata/${curSha1}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({custom_notes: n, custom_tags: t})})
            .then(() => { manifests[curManifest][curIdx].custom_notes = n; manifests[curManifest][curIdx].custom_tags = t; s.innerText = 'Saved!'; setTimeout(()=>s.innerText='', 2000); });
        }

        function softDelete() { if(confirm("Hide this file?")) fetch(`/api/delete/${curSha1}`, {method:'POST'}).then(() => location.reload()); }

        imgCont.onmousedown = e => { if(manifests[curManifest][curIdx].type==='video') return; e.preventDefault(); startX=e.clientX-pointX; startY=e.clientY-pointY; panning=true; }
        imgCont.onmouseup = () => panning = false;
        imgCont.onmousemove = e => { if(!panning || manifests[curManifest][curIdx].type==='video') return; pointX=e.clientX-startX; pointY=e.clientY-startY; setTransform(); }
        imgCont.onwheel = e => {
            if(manifests[curManifest][curIdx].type==='video') return; e.preventDefault();
            const prev = scale; (e.deltaY < 0) ? (scale *= 1.2) : (scale /= 1.2);
            scale = Math.min(Math.max(1, scale), 15);
            if(scale===1) { pointX=0; pointY=0; } else {
                const ratio = scale / prev; const rect = imgCont.getBoundingClientRect();
                const mX = e.clientX - (rect.left + rect.width/2); const mY = e.clientY - (rect.top + rect.height/2);
                pointX = mX - (mX - pointX) * ratio; pointY = mY - (mY - pointY) * ratio;
            }
            setTransform();
        }

        document.onkeydown = e => {
            if(!document.getElementById('lightbox').classList.contains('active')) return;
            if(e.target.tagName==='INPUT' || e.target.tagName==='TEXTAREA') return;
            if(e.key==="ArrowRight") changeImg(1); if(e.key==="ArrowLeft") changeImg(-1);
            if(e.key==="Escape") closeLB(); if(e.key.toLowerCase()==="e") toggleInfo();
        };
    </script>
</body>
</html>
"""

# --- V2 API & VIEWS ---
@app.route('/api/delete/<sha1>', methods=['POST'])
def soft_delete(sha1):
    conn = sqlite3.connect(DB_PATH); conn.execute("UPDATE media SET is_deleted=1 WHERE sha1=?", (sha1,)); conn.commit(); conn.close()
    global DB_CACHE; DB_CACHE = [p for p in DB_CACHE if p['sha1'] != sha1]
    return jsonify({"status": "success"})

@app.route('/api/favorite/<sha1>', methods=['POST'])
def toggle_favorite(sha1):
    is_fav = 1 if request.json.get('is_favorite') else 0
    conn = sqlite3.connect(DB_PATH); conn.execute("UPDATE media SET is_favorite=? WHERE sha1=?", (is_fav, sha1)); conn.commit(); conn.close()
    for p in DB_CACHE: 
        if p['sha1'] == sha1: p['is_favorite'] = is_fav; break
    return jsonify({"status": "success"})

@app.route('/api/update_metadata/<sha1>', methods=['POST'])
def update_metadata(sha1):
    n, t = request.json.get('custom_notes', ''), request.json.get('custom_tags', '')
    conn = sqlite3.connect(DB_PATH); conn.execute("UPDATE media SET custom_notes=?, custom_tags=? WHERE sha1=?", (n, t, sha1)); conn.commit(); conn.close()
    for p in DB_CACHE:
        if p['sha1'] == sha1: p['custom_notes'], p['custom_tags'] = n, t; break
    return jsonify({"status": "success"})

@app.route('/composite/<comp_hash>.jpg')
def serve_comp(comp_hash): return send_from_directory(COMPOSITE_DIR, f"{comp_hash}.jpg")

@app.route('/thumbs/<f>')
def serve_thumb(f): return send_from_directory(THUMB_DIR, f)

@app.route('/assets/<path:f>')
def serve_assets(f): return send_from_directory(ASSETS_DIR, f)

@app.route('/media/<path:p>')
def serve_media(p):
    fp = os.path.join(ARCHIVE_ROOT, p); mt, _ = mimetypes.guess_type(fp)
    return send_from_directory(os.path.dirname(fp), os.path.basename(fp), mimetype=mt)

@app.route('/')
@app.route('/timeline')
def timeline():
    years = defaultdict(list)
    for p in DB_CACHE: years[p['_year']].append(p)
    cards = []
    for y in sorted(years.keys(), reverse=True):
        cards.append({'id': f"y_{y}", 'title': y, 'subtitle': f"{len(years[y])} items", 'url': f"/timeline/{y}", 'heroes': years[y][:16], 'btn_text': f"View {y}"})
    for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
    manifests = {c['id']: build_manifest(years[c['title']][:16]) for c in cards}
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Timeline", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb="Timeline", view_type="grid", cards=cards, manifests=manifests)

@app.route('/timeline/<year>')
def timeline_year(year):
    photos = [p for p in DB_CACHE if p['_year'] == year]
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=year, active_tab="timeline", banner_img="hero-timeline.png", breadcrumb=f"<a href='/timeline'>Timeline</a> / {year}", view_type="photos", photos=photos, manifests={'main_gallery': build_manifest(photos)})

@app.route('/folder')
@app.route('/folder/<path:sub>')
def explorer(sub=""):
    if not sub:
        roots = sorted(list(set(p['rel_fqn'].split(os.sep)[0] for p in DB_CACHE)))
        cards = []
        for r in roots:
            p_list = [p for p in DB_CACHE if p['rel_fqn'].startswith(r + os.sep)]
            cards.append({'id': f"r_{r}", 'title': r, 'subtitle': f"{len(p_list)} files", 'url': f"/folder/{r}", 'heroes': p_list[:16], 'btn_text': "Explore"})
        for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
        manifests = {c['id']: build_manifest([p for p in DB_CACHE if p['rel_fqn'].startswith(c['title'] + os.sep)][:16]) for c in cards}
        return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Explorer", active_tab="file", banner_img="hero-folder.png", breadcrumb="Root", view_type="grid", cards=cards, manifests=manifests)
    
    parts = sub.split('/')
    folder_photos = [p for p in DB_CACHE if p['rel_fqn'].replace(os.sep, '/').startswith(sub + '/')]
    subdirs = sorted(list(set(p['rel_fqn'].replace(os.sep, '/')[len(sub)+1:].split('/')[0] for p in folder_photos if '/' in p['rel_fqn'].replace(os.sep, '/')[len(sub)+1:])))
    exact_files = [p for p in folder_photos if '/' not in p['rel_fqn'].replace(os.sep, '/')[len(sub)+1:]]
    
    cards = []
    for d in subdirs:
        d_photos = [p for p in folder_photos if p['rel_fqn'].replace(os.sep, '/').startswith(f"{sub}/{d}/")]
        cards.append({'id': f"d_{d}", 'title': d, 'subtitle': f"{len(d_photos)} files", 'url': f"/folder/{sub}/{d}", 'heroes': d_photos[:16], 'btn_text': "Open"})
    for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
    
    manifests = {c['id']: build_manifest([p for p in folder_photos if p['rel_fqn'].replace(os.sep, '/').startswith(f"{sub}/{c['title']}/")][:16]) for c in cards}
    if exact_files: manifests['main_gallery'] = build_manifest(exact_files)
    
    bc = f"<a href='/folder'>Root</a> / {' / '.join(parts)}"
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=parts[-1], active_tab="file", banner_img="hero-folder.png", breadcrumb=bc, view_type="grid" if cards else "photos", cards=cards, photos=exact_files, manifests=manifests)

@app.route('/tags')
@app.route('/tags/<tag_name>')
def tags(tag_name=None):
    if not tag_name:
        cards = []
        for t, photos in sorted(GLOBAL_TAGS.items()):
            cards.append({'id': f"t_{t}", 'title': f"#{t}", 'subtitle': f"{len(photos)} items", 'url': f"/tags/{t}", 'heroes': photos[:16], 'btn_text': "View Tag"})
        for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
        manifests = {c['id']: build_manifest(GLOBAL_TAGS[c['title'][1:]][:16]) for c in cards}
        return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Tags", active_tab="tags", banner_img="hero-tags.png", breadcrumb="Tags", view_type="grid", cards=cards, manifests=manifests)
    
    t_photos = GLOBAL_TAGS.get(tag_name, [])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"#{tag_name}", active_tab="tags", banner_img="hero-tags.png", breadcrumb=f"<a href='/tags'>Tags</a> / {tag_name}", view_type="photos", photos=t_photos, manifests={'main_gallery': build_manifest(t_photos)})

if __name__ == '__main__':
    load_cache()
    app.run(host='0.0.0.0', port=5000, debug=True)