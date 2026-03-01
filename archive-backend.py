import os
import sqlite3
import hashlib
import logging
from datetime import datetime
from collections import defaultdict, Counter
from flask import Flask, request, render_template_string, send_from_directory, send_file, jsonify
from urllib.parse import unquote
from PIL import Image

# --- LOGGING SUPPRESSION ---
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# --- CONFIGURATION ---
ARCHIVE_ROOT = r"C:\website-test" 
DB_PATH = os.path.join(ARCHIVE_ROOT, "archive_index.db")
ASSETS_DIR = os.path.join(ARCHIVE_ROOT, "_web_layout", "assets")
THUMB_DIR = os.path.join(ARCHIVE_ROOT, "_thumbs")
COMPOSITE_DIR = os.path.join(THUMB_DIR, "_composites")
THEME_COLOR = "#bb86fc"

os.makedirs(COMPOSITE_DIR, exist_ok=True)
app = Flask(__name__)

### ---------------------------------------------------------------------------
### LAYER: DATA_PERSISTENCE
### ---------------------------------------------------------------------------

DB_CACHE, UNDATED_CACHE, GLOBAL_TAGS = [], [], defaultdict(list)

def init_db_extensions():
    if not os.path.exists(DB_PATH): return
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS composite_cache (path_key TEXT PRIMARY KEY, sha1_list TEXT, composite_hash TEXT)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_media_dt ON media(final_dt)')
    conn.close()

def load_cache():
    global DB_CACHE, UNDATED_CACHE, GLOBAL_TAGS
    if not os.path.exists(DB_PATH): return
    init_db_extensions()
    
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM media WHERE is_deleted = 0 ORDER BY final_dt DESC").fetchall()
    
    DB_CACHE, UNDATED_CACHE, GLOBAL_TAGS = [], [], defaultdict(list)
    for row in rows:
        item = dict(row)
        dt_str = str(item['final_dt'])
        item['_web_path'] = item['rel_fqn'].replace('\\', '/')
        
        # Tag Parsing
        raw_tags = f"{item['path_tags'] or ''},{item['custom_tags'] or ''}"
        item['_tags_list'] = sorted(list(set([t.strip().title() for t in raw_tags.split(',') if t.strip()])))
        
        if dt_str.startswith("0000"):
            parts = item['_web_path'].split('/')
            item['_folder_group'] = parts[-2] if len(parts) > 1 else "Root"
            UNDATED_CACHE.append(item)
        else:
            dt_obj = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
            item.update({
                '_year': dt_str[:4], 
                '_month': dt_obj.strftime('%m'),
                '_month_name': dt_obj.strftime('%B'),
                '_decade': dt_str[:3]+"0s"
            })
            DB_CACHE.append(item)
            
        for t in item['_tags_list']:
            GLOBAL_TAGS[t].append(item)
    conn.close()

def get_top_tags(items, limit=3):
    """Aggregates the most frequent tags for a collection of items."""
    counts = Counter()
    for itm in items:
        counts.update(itm.get('_tags_list', []))
    return [t for t, _ in counts.most_common(limit)]

def get_composite_hash(path_key, media_list):
    if not media_list: return None
    conn = sqlite3.connect(DB_PATH)
    cached = conn.execute("SELECT composite_hash FROM composite_cache WHERE path_key=?", (path_key,)).fetchone()
    if cached and os.path.exists(os.path.join(COMPOSITE_DIR, f"{cached[0]}.jpg")):
        conn.close(); return cached[0]

    heroes = media_list[:16]
    sha1s = [h['sha1'] for h in heroes]
    h_hash = hashlib.md5("".join(sha1s).encode()).hexdigest()
    cp = os.path.join(COMPOSITE_DIR, f"{h_hash}.jpg")
    
    if not os.path.exists(cp):
        canvas = Image.new('RGB', (400, 400), (13, 13, 13))
        for i, h in enumerate(heroes):
            tp = os.path.join(THUMB_DIR, f"{h['sha1']}.jpg")
            if os.path.exists(tp):
                try:
                    with Image.open(tp) as img:
                        img.thumbnail((100, 100)); canvas.paste(img, ((i % 4) * 100, (i // 4) * 100))
                except: continue
        canvas.save(cp, "JPEG", quality=85)

    conn.execute("INSERT OR REPLACE INTO composite_cache (path_key, sha1_list, composite_hash) VALUES (?, ?, ?)",
                 (path_key, ",".join(sha1s), h_hash))
    conn.commit(); conn.close()
    return h_hash

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
    
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 30px; }
    .photo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
    
    .card { background: var(--card-bg); border-radius: 12px; border: 1px solid #333; overflow: hidden; cursor: pointer; transition: 0.2s; position: relative; display: flex; flex-direction: column; }
    .card:hover { border-color: var(--accent); transform: translateY(-5px); }
    .hero-preview { width: 100%; aspect-ratio: 1/1; background: #000; overflow: hidden; }
    .hero-preview img { width: 100%; height: 100%; object-fit: cover; }
    
    .tag-container { padding: 10px 15px 15px; display: flex; flex-wrap: wrap; gap: 6px; }
    .tag-pill { font-size: 0.65em; background: #333; color: #aaa; padding: 4px 10px; border-radius: 4px; font-weight: 800; text-transform: uppercase; text-decoration: none; border: 1px solid transparent; }
    .tag-pill:hover { background: var(--accent); color: #000; border-color: #fff; }

    .breadcrumb { font-weight: 800; color: #666; margin-bottom: 30px; text-transform: uppercase; font-size: 0.8em; }
    .breadcrumb a { color: var(--accent); text-decoration: none; }
    
    #lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.98); z-index: 9999; align-items: center; justify-content: center; user-select: none; }
    #lightbox.active { display: flex; }
    #lb-img { max-width: 85%; max-height: 85vh; object-fit: contain; box-shadow: 0 0 80px rgba(0,0,0,0.8); }
    .lb-close { position: absolute; top: 20px; right: 30px; font-size: 40px; color: #fff; cursor: pointer; z-index: 10007; opacity: 0.5; transition: 0.2s; }
    .lb-close:hover { opacity: 1; color: var(--accent); transform: scale(1.1); }
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
            <div style="padding:20px 20px 5px;"><a href="{{ c.url }}" style="text-decoration:none; color:#fff;"><h3 style="margin:0;">{{ c.title }}</h3></a>
            <div style="color:#666; font-size:0.85em; font-weight:700;">{{ c.subtitle }}</div></div>
            <div class="tag-container">
                {% for t in c.tags %}<a href="/tags/{{t}}" class="tag-pill">{{t}}</a>{% endfor %}
            </div>
        </div>{% endfor %}
    </div>{% endif %}

    {% if photos %}<div class="photo-grid">
        {% for p in photos %}<div class="card" oncontextmenu="handleCtx(event, '{{p.sha1}}')">
            <div class="hero-preview" onclick="openLB({{loop.index0}}, 'main_gallery')">
                <img id="thumb-{{p.sha1}}" src="/thumbs/{{p.sha1}}.jpg" loading="lazy">
            </div>
            <div class="tag-container">
                {% for t in p._tags_list[:3] %}<a href="/tags/{{t}}" class="tag-pill">{{t}}</a>{% endfor %}
                {% if p._tags_list|length > 3 %}<span class="tag-pill">+{{p._tags_list|length - 3}}</span>{% endif %}
            </div>
        </div>{% endfor %}
    </div>{% endif %}
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
        <textarea id="input-notes" style="width:100%; background:#222; border:1px solid #444; color:#fff; padding:15px; margin-top:10px; border-radius:8px; resize: none;" rows="8" placeholder="Notes..."></textarea>
    </div>
</div>
<script>
    const manifests = {{ manifests | tojson | safe }};
    let curM = null, curI = 0, menuSha1 = '';
    function handleGridClick(e, k) { if (e.target.closest('a') || e.target.closest('.tag-pill')) return; const rect = e.currentTarget.getBoundingClientRect(); const x = e.clientX - rect.left, y = e.clientY - rect.top; openLB(Math.floor(y / (rect.width / 4)) * 4 + Math.floor(x / (rect.width / 4)), k); }
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
    with Image.open(fp) as img:
        exif = img.info.get('exif')
        method = Image.ROTATE_270 if degrees == 90 else Image.ROTATE_90
        rotated = img.transpose(method); rotated.save(fp, "JPEG", exif=exif, quality=95)
    with Image.open(fp) as img:
        img.thumbnail((400,400)); img.convert("RGB").save(os.path.join(THUMB_DIR, f"{sha1}.jpg"), "JPEG")
    return jsonify({"status":"ok"})

@app.route('/')
@app.route('/timeline')
def timeline():
    load_cache(); decades = sorted(list(set(p['_decade'] for p in DB_CACHE)), reverse=True)
    cards = []
    for d in decades:
        items = [p for p in DB_CACHE if p['_decade']==d]
        cards.append({'id':f"d_{d}", 'title':d, 'subtitle':f"{len(items)} items", 'url':f"/timeline/decade/{d}", 'heroes':items, 'tags': get_top_tags(items)})
    for c in cards: c['comp_hash'] = get_composite_hash(c['id'], c['heroes'])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Timeline", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb="Decades", cards=cards, manifests={c['id']:build_manifest(c['heroes']) for c in cards})

@app.route('/timeline/decade/<decade>')
def timeline_decade(decade):
    load_cache(); years = sorted(list(set(p['_year'] for p in DB_CACHE if p['_decade'] == decade)), reverse=True)
    cards = []
    for y in years:
        items = [p for p in DB_CACHE if p['_year']==y]
        cards.append({'id':f"y_{y}", 'title':y, 'subtitle':f"{len(items)} items", 'url':f"/timeline/year/{y}", 'heroes':items, 'tags': get_top_tags(items)})
    for c in cards: c['comp_hash'] = get_composite_hash(c['id'], c['heroes'])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"The {decade}", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb=f"<a href='/timeline'>Timeline</a> / {decade}", cards=cards, manifests={c['id']:build_manifest(c['heroes']) for c in cards})

@app.route('/timeline/year/<year>')
def timeline_year(year):
    load_cache(); m_map = defaultdict(list)
    for p in [p for p in DB_CACHE if p['_year'] == year]:
        m_map[p['_month']].append(p)
    cards = []
    for m_code in sorted(m_map.keys()):
        imgs = m_map[m_code]; m_name = imgs[0]['_month_name']
        cards.append({'id':f"m_{year}_{m_code}", 'title':m_name, 'subtitle':f"{len(imgs)} items", 'url':f"/timeline/month/{year}/{m_code}", 'heroes':imgs, 'tags': get_top_tags(imgs)})
    for c in cards: c['comp_hash'] = get_composite_hash(c['id'], c['heroes'])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=year, active_tab="timeline", banner_img="hero-timeline.png", breadcrumb=f"<a href='/timeline'>Timeline</a> / <a href='/timeline/decade/{year[:3]}0s'>{year[:3]}0s</a> / {year}", cards=cards, manifests={c['id']:build_manifest(c['heroes']) for c in cards})

@app.route('/timeline/month/<year>/<month>')
def timeline_month(year, month):
    load_cache(); imgs = [p for p in DB_CACHE if p['_year'] == year and p['_month'] == month]
    m_name = imgs[0]['_month_name'] if imgs else "Month"
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title=f"{m_name} {year}", active_tab="timeline", banner_img="hero-timeline.png", breadcrumb=f"<a href='/timeline'>Timeline</a> / <a href='/timeline/decade/{year[:3]}0s'>{year[:3]}0s</a> / <a href='/timeline/year/{year}'>{year}</a> / {m_name}", photos=imgs, manifests={'main_gallery':build_manifest(imgs)})

@app.route('/undated')
def undated():
    load_cache(); f_map = defaultdict(list)
    for p in UNDATED_CACHE: f_map[p['_folder_group']].append(p)
    cards = [{'id':f"u_{f}", 'title':f, 'subtitle':f"{len(imgs)} items", 'url':f"/undated/{f}", 'heroes':imgs, 'tags': get_top_tags(imgs)} for f, imgs in sorted(f_map.items())]
    for c in cards: c['comp_hash'] = get_composite_hash(c['id'], c['heroes'])
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
    cards = []
    for f in sorted(list(unique_subs)):
        items = [p for p in all_m if p['_web_path'].startswith(prefix+f+'/')]
        cards.append({'id':f"f_{f}", 'title':f, 'subtitle':f"{len(items)} items", 'url':f"/folder/{prefix+f}", 'heroes':items, 'tags': get_top_tags(items)})
    for c in cards: c['comp_hash'] = get_composite_hash(c['id'], c['heroes'])
    return render_template_string(HTML_TEMPLATE, theme_color=THEME_COLOR, page_title="Explorer", active_tab="file", banner_img="hero-files.png", breadcrumb="Root" if not sub else f"<a href='/folder'>Root</a> / {sub.replace('/', ' / ')}", cards=cards, photos=direct_files, manifests={**{c['id']:build_manifest(c['heroes']) for c in cards}, 'main_gallery':build_manifest(direct_files)})

@app.route('/tags')
@app.route('/tags/<tag>')
def tags(tag=None):
    load_cache()
    if not tag:
        cards = [{'id':f"t_{t}", 'title':f"#{t}", 'subtitle':f"{len(imgs)} items", 'url':f"/tags/{t}", 'heroes':imgs, 'tags': []} for t, imgs in sorted(GLOBAL_TAGS.items())]
        for c in cards: c['comp_hash'] = get_composite_hash(c['id'], c['heroes'])
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