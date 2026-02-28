import os
import sqlite3
import json
import logging
import mimetypes
import hashlib
import time
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

# --- SILENCE FLASK LOGGING ---
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# --- GLOBAL STATE ---
DB_CACHE = []
GLOBAL_TAGS = defaultdict(list)

def load_cache():
    global DB_CACHE, GLOBAL_TAGS
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM media WHERE is_deleted = 0 ORDER BY final_dt DESC")
        rows = cursor.fetchall()
        DB_CACHE = []
        GLOBAL_TAGS.clear()
        for row in rows:
            dt_str = row['final_dt'][:10]
            yyyy = dt_str[:4]
            decade = yyyy[:3] + "0s"
            item = dict(row)
            item.update({'_year': yyyy, '_decade': decade})
            
            # Extract tags for pills
            tags = []
            if row['path_tags']: tags.extend([t.strip() for t in row['path_tags'].split(',')])
            if row['custom_tags']: tags.extend([t.strip() for t in row['custom_tags'].split(',')])
            item['_tags_list'] = sorted(list(set([t.title() for t in tags if t])))
            
            DB_CACHE.append(item)
            for t in item['_tags_list']: GLOBAL_TAGS[t].append(item)
    except Exception as e: print(f"Cache Error: {e}")
    finally: conn.close()

def generate_thumbnail(source_path, sha1):
    try:
        dest_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
        with Image.open(source_path) as img:
            img.thumbnail((400, 400), Image.Resampling.LANCZOS)
            img.convert("RGB").save(dest_path, "JPEG", quality=85)
        return True
    except: return False

def get_composite_hash(heroes):
    if not heroes: return None
    sha1_list = [h['sha1'] for h in heroes[:16]]
    comp_hash = hashlib.md5("".join(sha1_list).encode('utf-8')).hexdigest()
    comp_path = os.path.join(COMPOSITE_DIR, f"{comp_hash}.jpg")
    if os.path.exists(comp_path): return comp_hash
    
    canvas = Image.new('RGB', (400, 400), color=(17, 17, 17))
    for i, sha1 in enumerate(sha1_list):
        t_path = os.path.join(THUMB_DIR, f"{sha1}.jpg")
        if os.path.exists(t_path):
            try:
                with Image.open(t_path) as img:
                    img.thumbnail((100, 100), Image.Resampling.LANCZOS)
                    w, h = img.size
                    canvas.paste(img, ((i%4)*100+(100-w)//2, (i//4)*100+(100-h)//2))
            except: pass
    canvas.save(comp_path, "JPEG", quality=85)
    return comp_hash

def build_manifest(media_list):
    return [{
        'sha1': p['sha1'], 'path': p['rel_fqn'], 'filename': p['original_filename'],
        'date': p['final_dt'], 'type': p['media_type'],
        'custom_notes': p['custom_notes'], 'custom_tags': p['custom_tags']
    } for p in media_list]

# --- HTML TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{{ page_title }} | Legacy Archive</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;700;900&display=swap" rel="stylesheet">
    <style>
        :root { --accent: {{ theme_color }}; --bg: #0d0d0d; --card-bg: #1a1a1a; --select: #4285f4; }
        body { font-family: 'Inter', sans-serif; background: var(--bg); color: #fff; margin: 0; padding: 0; overflow-x: hidden; }
        
        /* SELECTION BAR */
        #selection-bar { 
            position: fixed; top: -100px; left: 0; right: 0; height: 70px; 
            background: #1a1a1a; border-bottom: 2px solid var(--select); 
            z-index: 10005; display: flex; align-items: center; padding: 0 40px; 
            transition: 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        #selection-bar.active { top: 0; }
        .bar-btn { background: #333; color: #fff; border: none; padding: 8px 16px; border-radius: 4px; margin-left: 15px; cursor: pointer; font-weight: 700; }
        .bar-btn:hover { background: var(--select); }

        .hero-banner { width: 100%; height: 250px; background: #111; background-image: linear-gradient(to bottom, rgba(13,13,13,0) 60%, rgba(13,13,13,1) 100%), url('/assets/{{ banner_img }}'); background-size: cover; background-position: center; border-bottom: 1px solid #333; }
        .nav-bar { background: rgba(10,10,10,0.9); backdrop-filter: blur(15px); padding: 15px 40px; border-bottom: 1px solid #333; display: flex; gap: 40px; position: sticky; top: 0; z-index: 1000; }
        .nav-bar a { color: #888; text-decoration: none; font-weight: 700; font-size: 0.85em; letter-spacing: 1px; text-transform: uppercase; }
        .nav-bar a.active { color: var(--accent); border-bottom: 2px solid var(--accent); padding-bottom: 5px; }
        
        .content { padding: 40px; max-width: 1600px; margin: 0 auto; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 30px; }
        .photo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 15px; }
        
        /* CARDS */
        .card { background: var(--card-bg); border-radius: 12px; border: 1px solid #333; overflow: hidden; position: relative; transition: 0.2s; display: flex; flex-direction: column; }
        .card:hover { border-color: var(--accent); }
        .hero-preview { width: 100%; aspect-ratio: 1/1; background: #000; cursor: pointer; position: relative; }
        .hero-preview img { width: 100%; height: 100%; object-fit: cover; }
        
        /* SELECTION DOTS */
        .select-dot { 
            position: absolute; top: 12px; left: 12px; width: 24px; height: 24px; 
            background: rgba(0,0,0,0.4); border: 2px solid #fff; border-radius: 50%; 
            z-index: 100; cursor: pointer; display: none; align-items: center; justify-content: center; 
        }
        .card:hover .select-dot, .card.selected .select-dot { display: flex; }
        .card.selected .select-dot { background: var(--select); border-color: var(--select); color: #fff; }
        .card.selected { border: 3px solid var(--select); }

        /* TAG PILLS */
        .tag-pill { display: inline-block; background: #333; color: #aaa; padding: 4px 10px; border-radius: 4px; font-size: 0.75em; font-weight: 800; margin-right: 5px; margin-top: 5px; text-decoration: none; }
        .tag-pill:hover { background: var(--accent); color: #000; }

        #context-menu { display: none; position: fixed; background: #222; border: 1px solid #444; z-index: 99999; width: 240px; border-radius: 8px; overflow: hidden; }
        .menu-item { padding: 12px 20px; cursor: pointer; font-size: 14px; color: #eee; }
        .menu-item:hover { background: var(--accent); color: #000; }

        #lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.98); z-index: 9999; align-items: center; justify-content: center; }
        #lightbox.active { display: flex; }
        #lb-img { max-width: 90%; max-height: 85vh; object-fit: contain; }
        .close-btn { position: absolute; top: 20px; right: 30px; font-size: 40px; cursor: pointer; color: #888; }
        .info-trigger { position: absolute; bottom: 30px; right: 30px; background: #222; padding: 12px 24px; border-radius: 40px; cursor: pointer; font-weight: 800; border: 1px solid #444; z-index: 10005; }
        #lb-sidebar { position: absolute; top: 0; right: -450px; width: 450px; bottom: 0; background: rgba(15,15,15,0.98); border-left: 1px solid #333; padding: 40px 30px; z-index: 10001; transition: 0.3s; display: flex; flex-direction: column; box-sizing: border-box; }
        #lb-sidebar.visible { right: 0; }
        .meta-label { color: var(--accent); font-weight: 900; text-transform: uppercase; font-size: 0.75em; letter-spacing: 2px; display: block; margin-top: 15px; }
        .action-btn { background: var(--accent); color: #000; border: none; padding: 12px; border-radius: 6px; font-weight: bold; width: 100%; margin-top: 10px; cursor: pointer; text-transform: uppercase; }
    </style>
</head>
<body>

<div id="selection-bar">
    <div id="selection-count" style="font-weight:900; font-size:1.2em;">0 selected</div>
    <div style="flex-grow:1;"></div>
    <button class="bar-btn" onclick="bulkRotate(90)">Rotate 90° ↻</button>
    <button class="bar-btn" style="background:#cf6679;" onclick="bulkDelete()">Hide Selected</button>
    <button class="bar-btn" onclick="clearSelection()">Cancel</button>
</div>

<div id="context-menu">
    <div class="menu-item" onclick="rotateImage(90)">Rotate Clockwise 90°</div>
    <div class="menu-item" onclick="rotateImage(-90)">Rotate Counter-Clockwise 90°</div>
</div>

<div class="hero-banner"></div>
<div class="nav-bar">
    <a href="/timeline" class="{{ 'active' if active_tab=='timeline' else '' }}">Timeline</a>
    <a href="/folder" class="{{ 'active' if active_tab=='file' else '' }}">File View</a>
    <a href="/tags" class="{{ 'active' if active_tab=='tags' else '' }}">Master Tags</a>
</div>

<div class="content">
    <h1>{{ page_title }}</h1>
    <div class="breadcrumb">{{ breadcrumb | safe }}</div>
    
    {% if view_type == 'grid' %}
        <div class="grid">
            {% for card in cards %}
            <div class="card" id="card-{{ card.id }}">
                <div class="hero-preview" onclick="handleGridClick(event, '{{ card.id | replace(\"'\", \"\\\\'\") }}')">
                    {% if card.comp_hash %}
                        <img src="/composite/{{ card.comp_hash }}.jpg" loading="lazy">
                    {% else %}<div style="width:100%; height:100%; background:#111;"></div>{% endif %}
                </div>
                <div style="padding:15px;">
                    <a href="{{ card.url }}" style="text-decoration:none;"><div style="font-weight:900; color:#fff; font-size:1.4em;">{{ card.title }}</div></a>
                    <div style="color:#666; font-size:0.85em; font-weight:700;">{{ card.subtitle }}</div>
                    <div style="margin-top:5px;">
                        {% for tag in card.tags %}
                            <a href="/tags/{{ tag }}" class="tag-pill">#{{ tag }}</a>
                        {% endfor %}
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
    {% elif view_type == 'photos' %}
        <div class="photo-grid">
            {% for p in photos %}
            <div class="card photo-card" id="card-{{ p.sha1 }}" data-sha1="{{ p.sha1 }}" data-idx="{{ loop.index0 }}">
                <div class="select-dot" onclick="toggleSelection(event, '{{ p.sha1 }}')">✓</div>
                <div class="hero-preview" onclick="handlePhotoClick(event, {{ loop.index0 }})" oncontextmenu="handlePhotoContext(event, '{{ p.sha1 }}')">
                    <img id="img-{{ p.sha1 }}" src="/thumbs/{{ p.sha1 }}.jpg" loading="lazy">
                </div>
            </div>
            {% endfor %}
        </div>
    {% endif %}
</div>

<div id="lightbox">
    <span class="close-btn" onclick="closeLB()">&times;</span>
    <div id="lb-img-container" oncontextmenu="handlePhotoContext(event, manifests[curManifest][curIdx].sha1)"><img id="lb-img" src=""></div>
    <div class="info-trigger" onclick="toggleInfo()">CURATE (E)</div>
    <div id="lb-sidebar">
        <h2 style="margin:0;">Curation</h2>
        <span class="meta-label">File</span><div id="meta-file" style="font-size:0.8em; color:#888;"></div>
        <button class="action-btn" onclick="rotateImage(90, true)">Rotate 90° ↻</button>
        <span class="meta-label">Notes</span><textarea id="input-notes" style="height:120px;"></textarea>
        <span class="meta-label">Tags</span><input type="text" id="input-tags">
        <button class="action-btn" onclick="saveMetadata()">Save Changes</button>
    </div>
</div>

<script>
    const manifests = {{ manifests | tojson | safe }};
    let curManifest = null, curIdx = 0, menuSha1 = '', selectedSet = new Set(), lastSelectedIndex = -1;

    // --- GRID COORDINATE PORTAL ---
    function handleGridClick(event, mKey) {
        const rect = event.target.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        const col = Math.floor(x / (rect.width / 4));
        const row = Math.floor(y / (rect.height / 4));
        const clickedIdx = (row * 4) + col;
        openLB(Math.min(clickedIdx, manifests[mKey].length - 1), mKey);
    }

    // --- SELECTION ENGINE ---
    function toggleSelection(e, sha1) {
        e.stopPropagation();
        if (selectedSet.has(sha1)) selectedSet.delete(sha1); else selectedSet.add(sha1);
        lastSelectedIndex = parseInt(document.getElementById('card-' + sha1).dataset.idx);
        renderSelection();
    }

    function handlePhotoClick(e, idx) {
        if (e.shiftKey && lastSelectedIndex !== -1) {
            const start = Math.min(lastSelectedIndex, idx), end = Math.max(lastSelectedIndex, idx);
            const cards = document.querySelectorAll('.photo-card');
            for (let i = start; i <= end; i++) selectedSet.add(cards[i].dataset.sha1);
            renderSelection();
        } else { openLB(idx, 'main_gallery'); }
    }

    function renderSelection() {
        document.querySelectorAll('.photo-card').forEach(c => c.classList.toggle('selected', selectedSet.has(c.dataset.sha1)));
        document.getElementById('selection-bar').classList.toggle('active', selectedSet.size > 0);
        document.getElementById('selection-count').innerText = selectedSet.size + " selected";
    }
    function clearSelection() { selectedSet.clear(); lastSelectedIndex = -1; renderSelection(); }

    // --- AJAX ROTATION (CACHE BUST) ---
    function rotateImage(deg, fromLB = false) {
        const sha1 = fromLB ? manifests[curManifest][curIdx].sha1 : menuSha1;
        fetch('/api/rotate/' + sha1, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({degrees:deg})})
        .then(() => {
            const ts = new Date().getTime();
            if (fromLB) document.getElementById('lb-img').src = document.getElementById('lb-img').src.split('?')[0] + '?t=' + ts;
            const thumb = document.getElementById('img-' + sha1); 
            if (thumb) thumb.src = thumb.src.split('?')[0] + '?t=' + ts;
        });
    }

    function bulkRotate(deg) {
        const list = Array.from(selectedSet);
        Promise.all(list.map(s => fetch('/api/rotate/' + s, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({degrees:deg})}))).then(() => location.reload());
    }

    // --- LIGHTBOX ---
    function openLB(idx, mKey) { curManifest = mKey; curIdx = idx; updateLB(); document.getElementById('lightbox').classList.add('active'); }
    function updateLB() {
        const item = manifests[curManifest][curIdx];
        document.getElementById('lb-img').src = "/media/" + encodeURIComponent(item.path);
        document.getElementById('meta-file').innerText = item.filename;
        document.getElementById('input-notes').value = item.custom_notes || '';
        document.getElementById('input-tags').value = item.custom_tags || '';
    }
    function handlePhotoContext(e, sha1) { e.preventDefault(); menuSha1 = sha1; const m = document.getElementById('context-menu'); m.style.display = 'block'; m.style.left = e.clientX + 'px'; m.style.top = e.clientY + 'px'; }
    window.onclick = () => { document.getElementById('context-menu').style.display = 'none'; }
    function closeLB() { document.getElementById('lightbox').classList.remove('active'); }
    function toggleInfo() { document.getElementById('lb-sidebar').classList.toggle('visible'); }
    
    function saveMetadata() {
        const item = manifests[curManifest][curIdx];
        fetch('/api/update_metadata/' + item.sha1, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({custom_notes: document.getElementById('input-notes').value, custom_tags: document.getElementById('input-tags').value})}).then(() => alert("Saved"));
    }
</script>
</body>
</html>
"""

# --- API ---
@app.route('/api/rotate/<sha1>', methods=['POST'])
def rotate_image(sha1):
    deg = request.json.get('degrees', 90)
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM media WHERE sha1 = ?", (sha1,)).fetchone()
    full_path = os.path.join(ARCHIVE_ROOT, row['rel_fqn'])
    try:
        with Image.open(full_path) as img:
            exif = img.info.get('exif')
            rotated = img.transpose(Image.ROTATE_270 if deg == 90 else Image.ROTATE_90)
            rotated.load() 
        rotated.save(full_path, "JPEG", exif=exif, quality=95)
        generate_thumbnail(full_path, sha1)
        for f in os.listdir(COMPOSITE_DIR): os.remove(os.path.join(COMPOSITE_DIR, f))
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/update_metadata/<sha1>', methods=['POST'])
def update_metadata(sha1):
    data = request.json
    conn = sqlite3.connect(DB_PATH); conn.execute("UPDATE media SET custom_notes = ?, custom_tags = ? WHERE sha1 = ?", (data['custom_notes'], data['custom_tags'], sha1)); conn.commit(); conn.close()
    return jsonify({"status": "success"})

# --- FILE SERVING ---
@app.route('/composite/<comp_hash>.jpg')
def serve_comp(comp_hash): return send_from_directory(COMPOSITE_DIR, f"{comp_hash}.jpg")
@app.route('/thumbs/<f>')
def serve_thumb(f): return send_from_directory(THUMB_DIR, f)
@app.route('/assets/<path:f>')
def serve_assets(f): return send_from_directory(ASSETS_DIR, f)
@app.route('/media/<path:p>')
def serve_media(p):
    fp = os.path.join(ARCHIVE_ROOT, p)
    return send_from_directory(os.path.dirname(fp), os.path.basename(fp))

# --- NAVIGATION ---

@app.route('/')
@app.route('/timeline')
def timeline():
    load_cache()
    decades = sorted(list(set(p['_decade'] for p in DB_CACHE)), reverse=True)
    cards = []
    for d in decades:
        d_p = [p for p in DB_CACHE if p['_decade'] == d]
        cards.append({'id': f"d_{d}", 'title': d, 'subtitle': f"{len(d_p)} items", 'url': f"/timeline/decade/{d}", 'heroes': d_p[:16], 'tags': []})
    for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Timeline", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb="Timeline", view_type="grid", cards=cards, manifests={c['id']: build_manifest(c['heroes']) for c in cards})

@app.route('/timeline/decade/<decade>')
def timeline_decade(decade):
    load_cache()
    d_p = [p for p in DB_CACHE if p['_decade'] == decade]
    years = sorted(list(set(p['_year'] for p in d_p)), reverse=True)
    cards = []
    for y in years:
        y_p = [p for p in d_p if p['_year'] == y]
        cards.append({'id': f"y_{y}", 'title': y, 'subtitle': f"{len(y_p)} items", 'url': f"/timeline/year/{y}", 'heroes': y_p[:16], 'tags': []})
    for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=decade, active_tab="timeline", banner_img="hero-timeline.png", breadcrumb="<a href='/timeline'>Timeline</a> / " + decade, view_type="grid", cards=cards, manifests={c['id']: build_manifest(c['heroes']) for c in cards})

@app.route('/timeline/year/<year>')
def timeline_year(year):
    load_cache()
    photos = [p for p in DB_CACHE if p['_year'] == year]
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=year, active_tab="timeline", banner_img="hero-timeline.png", breadcrumb=f"<a href='/timeline'>Timeline</a> / {year[:3]}0s / {year}", view_type="photos", photos=photos, manifests={'main_gallery': build_manifest(photos)})

@app.route('/folder')
@app.route('/folder/<path:sub>')
def explorer(sub=""):
    load_cache()
    if not sub:
        roots = sorted(list(set(p['rel_fqn'].split(os.sep)[0] for p in DB_CACHE)))
        cards = []
        for r in roots:
            p_list = [p for p in DB_CACHE if p['rel_fqn'].startswith(r + os.sep)]
            cards.append({'id': f"r_{r}", 'title': r, 'subtitle': "Root", 'url': f"/folder/{r}", 'heroes': p_list[:16], 'tags': []})
        for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
        return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Explorer", active_tab="file", banner_img="hero-folder.png", breadcrumb="Root", view_type="grid", cards=cards, manifests={c['id']: build_manifest(c['heroes']) for c in cards})
    
    folder_photos = [p for p in DB_CACHE if p['rel_fqn'].replace(os.sep, '/').startswith(sub + '/')]
    subdirs = sorted(list(set(p['rel_fqn'].replace(os.sep, '/')[len(sub)+1:].split('/')[0] for p in folder_photos if '/' in p['rel_fqn'].replace(os.sep, '/')[len(sub)+1:])))
    exact_files = [p for p in folder_photos if '/' not in p['rel_fqn'].replace(os.sep, '/')[len(sub)+1:]]
    cards = []
    for d in subdirs:
        d_p = [p for p in folder_photos if p['rel_fqn'].replace(os.sep, '/').startswith(f"{sub}/{d}/")]
        # Combine folder-specific tags
        all_tags = []
        for p in d_p: all_tags.extend(p['_tags_list'])
        cards.append({'id': f"d_{d}", 'title': d, 'subtitle': f"{len(d_p)} items", 'url': f"/folder/{sub}/{d}", 'heroes': d_p[:16], 'tags': sorted(list(set(all_tags)))[:5]})
    for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
    bc = f"<a href='/folder'>Root</a> / {sub.replace('/', ' / ')}"
    manifests = {c['id']: build_manifest(c['heroes']) for c in cards}
    if exact_files: manifests['main_gallery'] = build_manifest(exact_files)
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=sub.split('/')[-1], active_tab="file", banner_img="hero-folder.png", breadcrumb=bc, view_type="grid" if cards else "photos", cards=cards, photos=exact_files, manifests=manifests)

@app.route('/tags')
@app.route('/tags/<tag_name>')
def tags(tag_name=None):
    load_cache()
    if not tag_name:
        cards = [{'id': f"t_{t}", 'title': f"#{t}", 'subtitle': f"{len(p)} items", 'url': f"/tags/{t}", 'heroes': p[:16], 'tags': []} for t, p in sorted(GLOBAL_TAGS.items())]
        for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
        return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Tags", active_tab="tags", banner_img="hero-tags.png", breadcrumb="Tags", view_type="grid", cards=cards, manifests={c['id']: build_manifest(c['heroes']) for c in cards})
    p = GLOBAL_TAGS.get(tag_name, [])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"#{tag_name}", active_tab="tags", banner_img="hero-tags.png", breadcrumb="<a href='/tags'>Tags</a> / " + tag_name, view_type="photos", photos=p, manifests={'main_gallery': build_manifest(p)})

if __name__ == '__main__':
    load_cache()
    app.run(host='0.0.0.0', port=5000, debug=True)