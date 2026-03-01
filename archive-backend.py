import os
import sqlite3
import hashlib
from collections import defaultdict
from flask import Flask, request, render_template_string, send_from_directory, send_file, jsonify
from urllib.parse import unquote
from PIL import Image

# --- CONFIGURATION ---
ARCHIVE_ROOT = r"C:\website-test" 
DB_PATH = os.path.join(ARCHIVE_ROOT, "archive_index.db")
ASSETS_DIR = os.path.join(ARCHIVE_ROOT, "_web_layout", "assets")
THUMB_DIR = os.path.join(ARCHIVE_ROOT, "_thumbs")
COMPOSITE_DIR = os.path.join(THUMB_DIR, "_composites")
os.makedirs(COMPOSITE_DIR, exist_ok=True)

THEME_COLOR = "#bb86fc"

app = Flask(__name__)

### ---------------------------------------------------------------------------
### LAYER: DATA_PERSISTENCE
### ---------------------------------------------------------------------------

DB_CACHE, UNDATED_CACHE, GLOBAL_TAGS = [], [], defaultdict(list)

def load_cache():
    global DB_CACHE, UNDATED_CACHE, GLOBAL_TAGS
    if not os.path.exists(DB_PATH): return
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM media WHERE is_deleted = 0 ORDER BY final_dt DESC").fetchall()
    DB_CACHE, UNDATED_CACHE, GLOBAL_TAGS = [], [], defaultdict(list)
    for row in rows:
        item = dict(row)
        dt = str(item['final_dt'])
        item['_web_path'] = item['rel_fqn'].replace('\\', '/')
        tags = [t.strip().title() for t in f"{item['path_tags'] or ''},{item['custom_tags'] or ''}".split(',') if t.strip()]
        item['_tags_list'] = sorted(list(set(tags)))
        if dt.startswith("0000"):
            parts = item['_web_path'].split('/')
            item['_folder_group'] = parts[-2] if len(parts) > 1 else "Root"
            UNDATED_CACHE.append(item)
        else:
            item.update({'_year': dt[:4], '_decade': dt[:3]+"0s"})
            DB_CACHE.append(item)
        for t in item['_tags_list']: GLOBAL_TAGS[t].append(item)
    conn.close()

def get_composite_hash(heroes):
    if not heroes: return None
    sha1s = [h['sha1'] for h in heroes[:16]]
    h_hash = hashlib.md5("".join(sha1s).encode()).hexdigest()
    cp = os.path.join(COMPOSITE_DIR, f"{h_hash}.jpg")
    if os.path.exists(cp): return h_hash
    canvas = Image.new('RGB', (400, 400), (13, 13, 13))
    for i, h in enumerate(heroes[:16]):
        tp = os.path.join(THUMB_DIR, f"{h['sha1']}.jpg")
        if os.path.exists(tp):
            try:
                with Image.open(tp) as img:
                    img.thumbnail((100, 100)); canvas.paste(img, ((i % 4) * 100, (i // 4) * 100))
            except: continue
    canvas.save(cp, "JPEG", quality=80); return h_hash

def build_manifest(media_list):
    return [{'sha1': p['sha1'], 'path': p['_web_path'], 'filename': p['original_filename']} for p in media_list]

### ---------------------------------------------------------------------------
### LAYER: VIEW_CONTROLLER
### ---------------------------------------------------------------------------

HTML_TEMPLATE = """
<!DOCTYPE html><html><head><meta charset="UTF-8"><title>{{page_title}} | Archive</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap" rel="stylesheet">
<style>
    :root { --accent: {{ theme_color }}; --bg: #0d0d0d; --card-bg: #1a1a1a; }
    body { font-family: 'Inter', sans-serif; background: var(--bg); color: #fff; margin: 0; overflow-x: hidden; }
    .nav-bar { background: rgba(10,10,10,0.9); backdrop-filter: blur(15px); padding: 15px 40px; border-bottom: 1px solid #333; display: flex; gap: 30px; position: sticky; top: 0; z-index: 1000; }
    .nav-bar a { color: #888; text-decoration: none; font-weight: 700; font-size: 0.8em; text-transform: uppercase; letter-spacing: 1px; }
    .nav-bar a.active { color: var(--accent); border-bottom: 2px solid var(--accent); padding-bottom: 5px; }
    .hero-banner { height: 200px; width: 100%; overflow: hidden; position: relative; border-bottom: 1px solid #333; }
    .hero-banner img { width: 100%; height: 100%; object-fit: cover; opacity: 0.4; }
    .content { padding: 40px; max-width: 1600px; margin: 0 auto; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 30px; }
    .photo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
    .card { background: var(--card-bg); border-radius: 12px; border: 1px solid #333; overflow: hidden; cursor: pointer; transition: 0.2s; position: relative; }
    .card:hover { border-color: var(--accent); transform: translateY(-5px); }
    .hero-preview { width: 100%; aspect-ratio: 1/1; background: #000; overflow: hidden; }
    .hero-preview img { width: 100%; height: 100%; object-fit: cover; }
    .breadcrumb { font-weight: 800; color: #666; margin-bottom: 30px; text-transform: uppercase; font-size: 0.8em; }
    .breadcrumb a { color: var(--accent); text-decoration: none; }
    #lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.98); z-index: 9999; align-items: center; justify-content: center; user-select: none; }
    #lightbox.active { display: flex; }
    #lb-img { max-width: 85%; max-height: 85vh; object-fit: contain; box-shadow: 0 0 80px rgba(0,0,0,0.8); }
    .lb-nav { position: absolute; top: 0; bottom: 0; width: 12%; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: 0.3s; font-size: 3em; color: rgba(255,255,255,0.1); z-index: 10002; }
    .lb-nav:hover { background: rgba(255,255,255,0.05); color: var(--accent); }
    #lb-prev { left: 0; }
    #lb-next { right: 0; }
    #lb-sidebar { position: fixed; top: 0; right: -450px; width: 400px; height: 100vh; background: #111; border-left: 1px solid #333; padding: 60px 30px; transition: 0.4s cubic-bezier(0.16, 1, 0.3, 1); z-index: 10005; box-shadow: -20px 0 50px rgba(0,0,0,0.5); overflow-y: auto; }
    #lb-sidebar.visible { right: 0; }
    #context-menu { display: none; position: fixed; background: #222; border: 1px solid #444; z-index: 100000; width: 220px; border-radius: 8px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
    .menu-item { padding: 12px 20px; cursor: pointer; font-size: 0.9em; font-weight: 700; transition: 0.1s; }
    .menu-item:hover { background: var(--accent); color: #000; }
</style>
</head><body>
<div id="context-menu">
    <div class="menu-item" onclick="rotateImage(90)">Rotate 90° Clockwise ↻</div>
    <div class="menu-item" onclick="rotateImage(270)">Rotate 90° Counter ↺</div>
    <div class="menu-item" style="color:#ff5555; border-top:1px solid #444;">Mark for Deletion</div>
</div>
<div class="nav-bar">
    <a href="/timeline" class="{{'active' if active_tab=='timeline'}}">Timeline</a>
    <a href="/undated" class="{{'active' if active_tab=='undated'}}">Undated</a>
    <a href="/folder" class="{{'active' if active_tab=='file'}}">Explorer</a>
    <a href="/tags" class="{{'active' if active_tab=='tags'}}">Tags</a>
</div>
<div class="hero-banner"><img src="/assets/{{ banner_img }}" onerror="this.src='/assets/hero-timeline.png'"></div>
<div class="content">
    <h1>{{ page_title }}</h1><div class="breadcrumb">{{ breadcrumb | safe }}</div>
    {% if cards %}<div class="grid" style="margin-bottom: 50px;">
        {% for c in cards %}<div class="card" onclick="handleGridClick(event, '{{c.id}}')">
            <div class="hero-preview">{% if c.comp_hash %}<img src="/composite/{{c.comp_hash}}.jpg" loading="lazy">{% endif %}</div>
            <div style="padding:20px;"><a href="{{ c.url }}" style="text-decoration:none; color:#fff;"><h3 style="margin:0;">{{ c.title }}</h3></a>
            <div style="color:#666; font-size:0.85em; font-weight:700;">{{ c.subtitle }}</div></div>
        </div>{% endfor %}
    </div>{% endif %}
    {% if photos %}<div class="photo-grid">
        {% for p in photos %}<div class="card" oncontextmenu="handleCtx(event, '{{p.sha1}}')">
            <div class="hero-preview" onclick="openLB({{loop.index0}}, 'main_gallery')">
                <img id="thumb-{{p.sha1}}" src="/thumbs/{{p.sha1}}.jpg" loading="lazy">
            </div>
        </div>{% endfor %}
    </div>{% endif %}
</div>
<div id="lightbox" onclick="if(event.target===this) closeLB()">
    <div id="lb-prev" class="lb-nav" onclick="changeImg(-1)">&#10094;</div>
    <img id="lb-img" src="" oncontextmenu="handleCtxFromLightbox(event)">
    <div id="lb-next" class="lb-nav" onclick="changeImg(1)">&#10095;</div>
    <div style="position:absolute; bottom:30px; right:30px; background:#222; padding:12px 24px; border-radius:40px; cursor:pointer; font-weight:800; z-index:10006; border:1px solid #444; color:var(--accent);" onclick="toggleSidebar()">CURATE (E)</div>
    <div id="lb-sidebar" onclick="event.stopPropagation()">
        <h2 style="margin:0; color:var(--accent);">Curation</h2>
        <div id="meta-file" style="color:#888; font-size:0.8em; margin-top:20px; word-break:break-all; font-weight:700;"></div>
        <hr style="border:0; border-top:1px solid #333; margin:30px 0;">
        <label style="font-size:0.7em; color:#666; font-weight:900; letter-spacing:1px;">NOTES</label>
        <textarea id="input-notes" style="width:100%; background:#222; border:1px solid #444; color:#fff; padding:15px; margin-top:10px; border-radius:8px; font-family:inherit; resize: none;" rows="8" placeholder="Add custom notes..."></textarea>
    </div>
</div>
<script>
    const manifests = {{ manifests | tojson | safe }};
    let curM = null, curI = 0, menuSha1 = '';
    function handleGridClick(e, k) { if (e.target.closest('a')) return; const rect = e.currentTarget.getBoundingClientRect(); const x = e.clientX - rect.left, y = e.clientY - rect.top; const col = Math.floor(x / (rect.width / 4)), row = Math.floor(y / (rect.height / 4)); openLB((row * 4) + col, k); }
    function openLB(i, k) { if (!manifests[k]) return; curM = k; curI = Math.min(i, manifests[k].length-1); updateLB(); document.getElementById('lightbox').classList.add('active'); }
    function updateLB() { const itm = manifests[curM][curI]; const path = itm.path.split('/').map(encodeURIComponent).join('/'); document.getElementById('lb-img').src = "/media/" + path + "?t=" + new Date().getTime(); document.getElementById('meta-file').innerText = itm.filename; }
    function changeImg(step) { if(!curM) return; curI = (curI + step + manifests[curM].length) % manifests[curM].length; updateLB(); }
    function toggleSidebar() { document.getElementById('lb-sidebar').classList.toggle('visible'); }
    function closeLB() { document.getElementById('lightbox').classList.remove('active'); document.getElementById('lb-sidebar').classList.remove('visible'); }
    function handleCtx(e, s) { e.preventDefault(); menuSha1 = s; const m = document.getElementById('context-menu'); m.style.display='block'; m.style.left=e.clientX+'px'; m.style.top=e.clientY+'px'; }
    function handleCtxFromLightbox(e) { if(!curM) return; handleCtx(e, manifests[curM][curI].sha1); }
    function rotateImage(d) { fetch('/api/rotate/'+menuSha1, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({degrees:d})}).then(r => r.json()).then(data => { if(data.status==='ok') { const ts = new Date().getTime(); if(document.getElementById('lightbox').classList.contains('active')) updateLB(); const thumb = document.getElementById('thumb-'+menuSha1); if(thumb) thumb.src = "/thumbs/" + menuSha1 + ".jpg?t=" + ts; } }); }
    window.onclick = () => document.getElementById('context-menu').style.display='none';
    document.onkeydown = e => { if(e.key==="Escape") closeLB(); if(document.getElementById('lightbox').classList.contains('active')) { if(e.key==="ArrowRight") changeImg(1); if(e.key==="ArrowLeft") changeImg(-1); } if(e.key.toLowerCase()==="e") toggleSidebar(); };
</script></body></html>
"""

# --- ROUTES ---

@app.route('/media/<path:p>')
def serve_m(p):
    disk_path = os.path.normpath(os.path.join(ARCHIVE_ROOT, unquote(p).replace('/', os.sep)))
    if os.path.exists(disk_path): return send_file(disk_path)
    return "Not Found", 404

@app.route('/api/rotate/<sha1>', methods=['POST'])
def rotate(sha1):
    degrees = request.json.get('degrees', 90)
    conn = sqlite3.connect(DB_PATH); row = conn.execute("SELECT rel_fqn FROM media WHERE sha1=?", (sha1,)).fetchone(); conn.close()
    if not row: return jsonify({"status":"error"}), 404
    fp = os.path.join(ARCHIVE_ROOT, row[0])
    try:
        with Image.open(fp) as img:
            exif = img.info.get('exif')
            method = Image.ROTATE_270 if degrees == 90 else Image.ROTATE_90
            rotated = img.transpose(method); rotated.save(fp, "JPEG", exif=exif, quality=95)
        with Image.open(fp) as img:
            img.thumbnail((400,400)); img.convert("RGB").save(os.path.join(THUMB_DIR, f"{sha1}.jpg"), "JPEG")
        return jsonify({"status":"ok"})
    except Exception as e: return jsonify({"status":"error", "message": str(e)}), 500

@app.route('/')
@app.route('/timeline')
def timeline():
    load_cache(); decades = sorted(list(set(p['_decade'] for p in DB_CACHE)), reverse=True)
    cards = [{'id':f"d_{d}", 'title':d, 'subtitle':f"{len([p for p in DB_CACHE if p['_decade']==d])} items", 'url':f"/timeline/decade/{d}", 'heroes':[p for p in DB_CACHE if p['_decade']==d]} for d in decades]
    for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Timeline", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb="Decades", cards=cards, manifests={c['id']:build_manifest(c['heroes']) for c in cards})

@app.route('/timeline/decade/<decade>')
def timeline_decade(decade):
    load_cache(); years = sorted(list(set(p['_year'] for p in DB_CACHE if p['_decade'] == decade)), reverse=True)
    cards = [{'id':f"y_{y}", 'title':y, 'subtitle':f"{len([p for p in DB_CACHE if p['_year']==y])} items", 'url':f"/timeline/year/{y}", 'heroes':[p for p in DB_CACHE if p['_year']==y]} for y in years]
    for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"The {decade}", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb=f"<a href='/timeline'>Timeline</a> / {decade}", cards=cards, manifests={c['id']:build_manifest(c['heroes']) for c in cards})

@app.route('/timeline/year/<year>')
def timeline_year(year):
    load_cache(); imgs = [p for p in DB_CACHE if p['_year'] == year]
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=year, active_tab="timeline", banner_img="hero-timeline.png", breadcrumb=f"<a href='/timeline'>Timeline</a> / <a href='/timeline/decade/{year[:3]}0s'>{year[:3]}0s</a> / {year}", photos=imgs, manifests={'main_gallery':build_manifest(imgs)})

@app.route('/undated')
def undated():
    load_cache(); f_map = defaultdict(list)
    for p in UNDATED_CACHE: f_map[p['_folder_group']].append(p)
    cards = [{'id':f"u_{f}", 'title':f, 'subtitle':f"{len(imgs)} items", 'url':f"/undated/{f}", 'heroes':imgs} for f, imgs in sorted(f_map.items())]
    for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Undated Archive", active_tab="undated", banner_img="hero-undated.png", breadcrumb="Undated", cards=cards, manifests={c['id']:build_manifest(c['heroes']) for c in cards})

@app.route('/undated/<folder>')
def undated_view(folder):
    load_cache(); imgs = [p for p in UNDATED_CACHE if p['_folder_group'] == folder]
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=folder, active_tab="undated", banner_img="hero-undated.png", breadcrumb="<a href='/undated'>Undated</a> / "+folder, photos=imgs, manifests={'main_gallery':build_manifest(imgs)})

@app.route('/folder')
@app.route('/folder/<path:sub>')
def explorer(sub=""):
    load_cache(); all_m = DB_CACHE + UNDATED_CACHE; prefix = (sub + "/") if sub else ""; unique_subs, direct_files = set(), []
    for p in all_m:
        w_path = p['_web_path']
        if w_path.startswith(prefix):
            rem = w_path[len(prefix):]; (unique_subs.add(rem.split('/')[0]) if '/' in rem else direct_files.append(p))
    cards = [{'id':f"f_{f}", 'title':f, 'subtitle':f"{len([p for p in all_m if p['_web_path'].startswith(prefix+f+'/')])} items", 'url':f"/folder/{prefix+f}", 'heroes':[p for p in all_m if p['_web_path'].startswith(prefix+f+'/')] } for f in sorted(list(unique_subs))]
    for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Explorer", active_tab="file", banner_img="hero-files.png", breadcrumb="Root" if not sub else f"<a href='/folder'>Root</a> / {sub.replace('/', ' / ')}", cards=cards, photos=direct_files, manifests={**{c['id']:build_manifest(c['heroes']) for c in cards}, 'main_gallery':build_manifest(direct_files)})

@app.route('/tags')
@app.route('/tags/<tag>')
def tags(tag=None):
    load_cache()
    if not tag:
        cards = [{'id':f"t_{t}", 'title':f"#{t}", 'subtitle':f"{len(imgs)} items", 'url':f"/tags/{t}", 'heroes':imgs} for t, imgs in sorted(GLOBAL_TAGS.items())]
        for c in cards: c['comp_hash'] = get_composite_hash(c['heroes'])
        return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Tags", active_tab="tags", banner_img="hero-tags.png", breadcrumb="All Tags", cards=cards, manifests={c['id']:build_manifest(c['heroes']) for c in cards})
    imgs = GLOBAL_TAGS.get(tag, [])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"#{tag}", active_tab="tags", banner_img="hero-tags.png", breadcrumb=f"<a href='/tags'>Tags</a> / {tag}", photos=imgs, manifests={'main_gallery':build_manifest(imgs)})

@app.route('/composite/<h>.jpg')
def serve_c(h): return send_from_directory(COMPOSITE_DIR, f"{h}.jpg")
@app.route('/thumbs/<f>')
def serve_t(f): return send_from_directory(THUMB_DIR, f)
@app.route('/assets/<path:f>')
def serve_assets(f): return send_from_directory(ASSETS_DIR, f)

if __name__ == '__main__':
    load_cache(); app.run(host='0.0.0.0', port=5000, debug=True)