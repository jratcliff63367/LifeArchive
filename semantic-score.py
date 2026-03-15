#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
from PIL import Image, ImageFile

# ============================================================================
# CONFIG
# ============================================================================

ARCHIVE_DB = r"C:\website-photos\archive_index.db"
OUTPUT_DB = r"C:\website-photos\semantic_scores.sqlite"
FACE_DB = r"C:\website-photos\face_scores.sqlite"
IMAGE_ROOT = r"C:\website-photos"

# Set to an integer for smoke testing, or None to process everything.
LIMIT_IMAGES = None

# If True, delete and rebuild semantic sidecar content.
REBUILD_DATABASE = False

# If True, rows containing warnings from prior runs are eligible for retry.
RETRY_WARNING_ROWS = True-

# Image normalization.
MAX_ANALYSIS_DIM = 1024
UPSCALE_SMALL_IMAGES = False

# CLIP model.
CLIP_MODEL = "ViT-B-32"
CLIP_PRETRAINED = "openai"
DEVICE_PREFERENCE = "auto"   # "auto", "cuda", or "cpu"
CLIP_BATCH_SIZE = 16

# Progress / checkpointing.
HEARTBEAT_EVERY_N = 25
COMMIT_EVERY_N = 100

# If True, any model-load failure aborts.
REQUIRE_CLIP = True

# If a face row exists and face_count > 0, force contains_people = 1.
USE_FACE_SIDEcar_OVERRIDE = True

# ============================================================================
# IMPORTS THAT MAY NOT EXIST UNTIL INSTALLED
# ============================================================================

try:
    import torch
    import open_clip
except Exception as exc:
    print("ERROR: Missing required packages. Install into your venv with:")
    print(r"  .venv\Scripts\python -m pip install torch open_clip_torch pillow numpy")
    print(f"\nImport error: {exc}")
    raise

# Pillow safety / robustness for very large images.
Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True
warnings.filterwarnings("ignore", category=UserWarning, module="PIL")

# ============================================================================
# PROMPT BANKS
# ============================================================================

PROMPT_BANKS: dict[str, list[tuple[str, str]]] = {
    "scene_type": [
        ("portrait", "a portrait photo of a person"),
        ("group_photo", "a group photo of several people"),
        ("landscape", "a scenic landscape photo"),
        ("nature", "a nature photo outdoors"),
        ("city", "an urban city scene"),
        ("indoor_room", "an indoor room or interior scene"),
        ("food", "a photo of food or a meal"),
        ("animal", "a photo of an animal or pet"),
        ("vehicle", "a photo of a car truck motorcycle or vehicle"),
        ("document", "a photo of a document page receipt or printed paper"),
        ("screenshot", "a screenshot of a phone app computer screen or website"),
        ("object_closeup", "a close-up photo of an object"),
    ],
    # Positive people concept bucket. No negation prompts.
    "people_bucket": [
        ("person", "a photo of a person"),
        ("people", "a photo of people"),
        ("portrait", "a portrait photo of a person"),
        ("group_photo", "a group photo of several people"),
        ("family_photo", "a family photo"),
        ("couple_photo", "a photo of a couple"),
        ("friends_photo", "a photo of friends together"),
        ("candid_people", "a candid photo of people"),
    ],
    "animal_presence": [
        ("animals_yes", "a photo containing an animal or pet"),
        ("animals_no", "a photo without any animal"),
    ],
    "text_presence": [
        ("text_yes", "a text-heavy image with visible writing or printed text"),
        ("text_no", "an image without visible text"),
    ],
    "document_like": [
        ("document_yes", "a document scan receipt letter form or printed page"),
        ("document_no", "a normal photographic scene"),
    ],
    "screenshot_like": [
        ("screenshot_yes", "a screenshot of software a website a user interface or a phone screen"),
        ("screenshot_no", "a normal camera photo"),
    ],
    "landscape_like": [
        ("landscape_yes", "a wide scenic landscape nature or travel photo"),
        ("landscape_no", "not primarily a scenic landscape"),
    ],
    "food_like": [
        ("food_yes", "a photo of food on a plate table or restaurant setting"),
        ("food_no", "not a food photo"),
    ],
    "indoor_outdoor": [
        ("indoor", "an indoor scene inside a room building or house"),
        ("outdoor", "an outdoor scene in nature city street or open air"),
    ],
    "interest": [
        ("meaningful", "a meaningful memorable or interesting photograph"),
        ("ordinary", "an ordinary casual or unremarkable snapshot"),
        ("boring", "a boring low-information image"),
    ],
}

GENERAL_LABELS: list[tuple[str, str]] = [
    ("people", "a photo of one or more people"),
    ("portrait", "a portrait photo of a person"),
    ("group", "a group photo of several people"),
    ("selfie", "a selfie photo"),
    ("animal", "a photo of a pet or animal"),
    ("dog", "a photo of a dog"),
    ("cat", "a photo of a cat"),
    ("bird", "a photo of a bird"),
    ("landscape", "a scenic landscape photo"),
    ("nature", "a nature photo outdoors"),
    ("mountain", "a photo of a mountain"),
    ("beach", "a photo of a beach or ocean shoreline"),
    ("waterfall", "a photo of a waterfall"),
    ("garden", "a photo of plants flowers garden or farm"),
    ("city", "an urban city street or skyline scene"),
    ("building", "a photo of a building or architecture"),
    ("landmark", "a travel landmark monument or famous place"),
    ("food", "a photo of food or a meal"),
    ("drink", "a photo of a drink beverage or coffee"),
    ("fruit", "a photo of fruit or produce"),
    ("bananas", "a photo of bananas"),
    ("vehicle", "a photo of a vehicle car truck bus or motorcycle"),
    ("indoor", "an indoor room or interior"),
    ("outdoor", "an outdoor scene"),
    ("table", "a photo of a table tabletop or counter"),
    ("night", "a night photo or dark evening scene"),
    ("sunset", "a sunset or sunrise sky photo"),
    ("document", "a document page receipt printed paper or letter"),
    ("text", "an image with a lot of visible text"),
    ("screenshot", "a screenshot of an app website or computer screen"),
    ("object_closeup", "a close-up photo of an object"),
]

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class CandidateRow:
    sha1: str
    rel_fqn: str

@dataclass
class CLIPContext:
    device: str
    model_name: str
    model_version: str
    model: Any
    preprocess: Any
    tokenizer: Any
    general_label_features: Any
    bank_features: dict[str, Any]

# ============================================================================
# UTILITIES
# ============================================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))

def choose_device() -> str:
    pref = DEVICE_PREFERENCE.lower().strip()
    if pref == "cpu":
        return "cpu"
    if pref == "cuda":
        return "cuda"
    return "cuda" if torch.cuda.is_available() else "cpu"

def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=False)

def normalize_working_image(pil_img: Image.Image) -> Image.Image:
    img = pil_img.convert("RGB")
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    long_side = max(w, h)
    if long_side <= MAX_ANALYSIS_DIM and not UPSCALE_SMALL_IMAGES:
        return img
    scale = MAX_ANALYSIS_DIM / float(long_side)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    if (new_w, new_h) == (w, h):
        return img
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)

def cosine_probs(image_features: Any, text_features: Any) -> np.ndarray:
    logits = image_features @ text_features.T
    probs = torch.softmax(logits, dim=-1)
    return probs.detach().float().cpu().numpy()

def bool_from_pair(prob_yes: float, prob_no: float, threshold: float = 0.50) -> int:
    yes_norm = prob_yes / max(1e-8, (prob_yes + prob_no))
    return 1 if yes_norm >= threshold else 0

def score_from_interest_probs(meaningful: float, ordinary: float, boring: float) -> float:
    score = (meaningful * 1.0) + (ordinary * 0.45) + (boring * 0.0)
    denom = max(1e-8, meaningful + ordinary + boring)
    return clamp01(score / denom)

def derive_scene_type(scene_probs_row: np.ndarray) -> str:
    best_idx = int(np.argmax(scene_probs_row))
    return PROMPT_BANKS["scene_type"][best_idx][0]

def derive_top_labels(general_probs_row: np.ndarray, top_n: int = 6) -> list[dict[str, Any]]:
    order = np.argsort(-general_probs_row)[:top_n]
    out: list[dict[str, Any]] = []
    for i in order:
        out.append({
            "label": GENERAL_LABELS[int(i)][0],
            "score": round(float(general_probs_row[int(i)]), 6),
        })
    return out

def derive_ai_tags(
    scene_type: str,
    top_labels: list[dict[str, Any]],
    contains_people: int,
    contains_animals: int,
    contains_text: int,
    is_document_like: int,
    is_screenshot_like: int,
    is_landscape_like: int,
    is_food_like: int,
    is_indoor_like: int,
    is_outdoor_like: int,
    semantic_score: float,
) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()

    def add(tag: str) -> None:
        t = tag.strip().lower()
        if not t or t in seen:
            return
        seen.add(t)
        tags.append(t)

    add(scene_type)

    for item in top_labels:
        if float(item["score"]) >= 0.08:
            add(str(item["label"]))

    if contains_people:
        add("people")
    if contains_animals:
        add("animals")
    if contains_text:
        add("text")
    if is_document_like:
        add("document")
    if is_screenshot_like:
        add("screenshot")
    if is_landscape_like:
        add("landscape")
        add("travel")
    if is_food_like:
        add("food")
    if is_indoor_like:
        add("indoor")
    if is_outdoor_like:
        add("outdoor")

    if semantic_score >= 0.70:
        add("interesting")
    elif semantic_score <= 0.35:
        add("low_information")

    if is_document_like or is_screenshot_like:
        tags = [t for t in tags if t not in {"travel", "landscape"}]

    return tags[:12]

# ============================================================================
# DATABASE
# ============================================================================

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS semantic_scores (
            sha1 TEXT PRIMARY KEY,
            semantic_score REAL,
            scene_type TEXT,
            top_labels_json TEXT,
            ai_tags_json TEXT,
            contains_people INTEGER,
            contains_animals INTEGER,
            contains_text INTEGER,
            is_document_like INTEGER,
            is_screenshot_like INTEGER,
            is_landscape_like INTEGER,
            is_food_like INTEGER,
            is_indoor_like INTEGER,
            is_outdoor_like INTEGER,
            scorer_name TEXT,
            model_name TEXT,
            model_version TEXT,
            scored_at TEXT,
            warnings TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS semantic_score_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            completed_at TEXT NULL,
            model_name TEXT,
            model_version TEXT,
            images_attempted INTEGER DEFAULT 0,
            images_scored INTEGER DEFAULT 0,
            images_failed INTEGER DEFAULT 0,
            notes TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semantic_scores_model_version ON semantic_scores(model_version)")
    conn.commit()

def rebuild_db(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS semantic_scores")
    conn.execute("DROP TABLE IF EXISTS semantic_score_runs")
    conn.commit()
    init_db(conn)

def begin_run(conn: sqlite3.Connection, model_name: str, model_version: str, notes: str = "") -> int:
    cur = conn.execute("""
        INSERT INTO semantic_score_runs (
            started_at, completed_at, model_name, model_version, images_attempted, images_scored, images_failed, notes
        ) VALUES (?, NULL, ?, ?, 0, 0, 0, ?)
    """, (utc_now_iso(), model_name, model_version, notes))
    conn.commit()
    return int(cur.lastrowid)

def update_run(conn: sqlite3.Connection, run_id: int, attempted: int, scored: int, failed: int) -> None:
    conn.execute("""
        UPDATE semantic_score_runs
        SET images_attempted = ?, images_scored = ?, images_failed = ?
        WHERE run_id = ?
    """, (attempted, scored, failed, run_id))

def finish_run(conn: sqlite3.Connection, run_id: int, attempted: int, scored: int, failed: int) -> None:
    conn.execute("""
        UPDATE semantic_score_runs
        SET completed_at = ?, images_attempted = ?, images_scored = ?, images_failed = ?
        WHERE run_id = ?
    """, (utc_now_iso(), attempted, scored, failed, run_id))
    conn.commit()

def upsert_semantic_row(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute("""
        INSERT INTO semantic_scores (
            sha1,
            semantic_score,
            scene_type,
            top_labels_json,
            ai_tags_json,
            contains_people,
            contains_animals,
            contains_text,
            is_document_like,
            is_screenshot_like,
            is_landscape_like,
            is_food_like,
            is_indoor_like,
            is_outdoor_like,
            scorer_name,
            model_name,
            model_version,
            scored_at,
            warnings
        ) VALUES (
            :sha1,
            :semantic_score,
            :scene_type,
            :top_labels_json,
            :ai_tags_json,
            :contains_people,
            :contains_animals,
            :contains_text,
            :is_document_like,
            :is_screenshot_like,
            :is_landscape_like,
            :is_food_like,
            :is_indoor_like,
            :is_outdoor_like,
            :scorer_name,
            :model_name,
            :model_version,
            :scored_at,
            :warnings
        )
        ON CONFLICT(sha1) DO UPDATE SET
            semantic_score        = excluded.semantic_score,
            scene_type            = excluded.scene_type,
            top_labels_json       = excluded.top_labels_json,
            ai_tags_json          = excluded.ai_tags_json,
            contains_people       = excluded.contains_people,
            contains_animals      = excluded.contains_animals,
            contains_text         = excluded.contains_text,
            is_document_like      = excluded.is_document_like,
            is_screenshot_like    = excluded.is_screenshot_like,
            is_landscape_like     = excluded.is_landscape_like,
            is_food_like          = excluded.is_food_like,
            is_indoor_like        = excluded.is_indoor_like,
            is_outdoor_like       = excluded.is_outdoor_like,
            scorer_name           = excluded.scorer_name,
            model_name            = excluded.model_name,
            model_version         = excluded.model_version,
            scored_at             = excluded.scored_at,
            warnings              = excluded.warnings
    """, row)

def attach_sidecar(archive_conn: sqlite3.Connection, output_db: str) -> None:
    archive_conn.execute("ATTACH DATABASE ? AS sidecar", (output_db,))

def read_candidate_rows(archive_conn: sqlite3.Connection, model_version: str, limit_images: int | None) -> list[CandidateRow]:
    base_limit = ""
    if limit_images is not None:
        base_limit = f" LIMIT {int(limit_images)}"

    if REBUILD_DATABASE:
        query = f"""
            SELECT m.sha1, m.rel_fqn
            FROM media m
            WHERE m.is_deleted = 0
            ORDER BY m.rowid
            {base_limit}
        """
        rows = archive_conn.execute(query).fetchall()
        return [CandidateRow(sha1=r["sha1"], rel_fqn=r["rel_fqn"]) for r in rows]

    print("Reading active archive rows needing score update...")
    if RETRY_WARNING_ROWS:
        query = f"""
            SELECT m.sha1, m.rel_fqn
            FROM media m
            LEFT JOIN sidecar.semantic_scores s
              ON s.sha1 = m.sha1 AND s.model_version = ?
            WHERE m.is_deleted = 0
              AND (
                    s.sha1 IS NULL
                    OR s.warnings <> ''
                  )
            ORDER BY m.rowid
            {base_limit}
        """
    else:
        query = f"""
            SELECT m.sha1, m.rel_fqn
            FROM media m
            LEFT JOIN sidecar.semantic_scores s
              ON s.sha1 = m.sha1 AND s.model_version = ?
            WHERE m.is_deleted = 0
              AND s.sha1 IS NULL
            ORDER BY m.rowid
            {base_limit}
        """
    rows = archive_conn.execute(query, (model_version,)).fetchall()
    return [CandidateRow(sha1=r["sha1"], rel_fqn=r["rel_fqn"]) for r in rows]

def load_face_summary_map() -> dict[str, dict[str, Any]]:
    if not USE_FACE_SIDEcar_OVERRIDE:
        return {}
    if not os.path.exists(FACE_DB):
        return {}
    try:
        with sqlite3.connect(FACE_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM image_face_summary").fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            out[str(d.get("sha1"))] = d
        return out
    except Exception:
        return {}

# ============================================================================
# CLIP LOADING / EMBEDDINGS
# ============================================================================

def build_text_features(model: Any, tokenizer: Any, device: str, prompts: list[str]) -> Any:
    with torch.no_grad():
        tokens = tokenizer(prompts).to(device)
        feats = model.encode_text(tokens)
        feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats

def load_clip() -> CLIPContext:
    device = choose_device()
    print(f"Loading CLIP via open_clip on {device}...")
    model, _, preprocess = open_clip.create_model_and_transforms(
        CLIP_MODEL,
        pretrained=CLIP_PRETRAINED,
        device=device,
    )
    tokenizer = open_clip.get_tokenizer(CLIP_MODEL)
    model.eval()

    model_name = f"open_clip:{CLIP_MODEL}"
    model_version = f"{CLIP_PRETRAINED}|semantic-fast-v2-peoplefix"

    general_prompts = [prompt for _, prompt in GENERAL_LABELS]
    general_label_features = build_text_features(model, tokenizer, device, general_prompts)

    bank_features: dict[str, Any] = {}
    for bank_name, entries in PROMPT_BANKS.items():
        prompts = [prompt for _, prompt in entries]
        bank_features[bank_name] = build_text_features(model, tokenizer, device, prompts)

    print("CLIP ready via open_clip.")
    return CLIPContext(
        device=device,
        model_name=model_name,
        model_version=model_version,
        model=model,
        preprocess=preprocess,
        tokenizer=tokenizer,
        general_label_features=general_label_features,
        bank_features=bank_features,
    )

def encode_image_batch(ctx: CLIPContext, pil_images: list[Image.Image]) -> Any:
    tensor_batch = torch.stack([ctx.preprocess(img) for img in pil_images]).to(ctx.device)
    with torch.no_grad():
        feats = ctx.model.encode_image(tensor_batch)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats

# ============================================================================
# IMAGE PROCESSING
# ============================================================================

def open_image_for_analysis(path: str) -> Image.Image:
    with Image.open(path) as img:
        img.load()
        img = img.convert("RGB")
    return normalize_working_image(img)

def process_batch(ctx: CLIPContext, batch_rows: list[CandidateRow], face_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    valid_rows: list[CandidateRow] = []
    valid_images: list[Image.Image] = []
    results: list[dict[str, Any]] = []

    for row in batch_rows:
        full_path = os.path.join(IMAGE_ROOT, row.rel_fqn)
        try:
            valid_images.append(open_image_for_analysis(full_path))
            valid_rows.append(row)
        except Exception:
            results.append({
                "sha1": row.sha1,
                "semantic_score": 0.0,
                "scene_type": "unknown",
                "top_labels_json": "[]",
                "ai_tags_json": "[]",
                "contains_people": 0,
                "contains_animals": 0,
                "contains_text": 0,
                "is_document_like": 0,
                "is_screenshot_like": 0,
                "is_landscape_like": 0,
                "is_food_like": 0,
                "is_indoor_like": 0,
                "is_outdoor_like": 0,
                "scorer_name": "semantic-fast-openclip",
                "model_name": ctx.model_name,
                "model_version": ctx.model_version,
                "scored_at": utc_now_iso(),
                "warnings": "open_failed",
            })

    if valid_rows:
        image_features = encode_image_batch(ctx, valid_images)
        general_probs = cosine_probs(image_features, ctx.general_label_features)
        scene_probs = cosine_probs(image_features, ctx.bank_features["scene_type"])
        people_bucket_probs = cosine_probs(image_features, ctx.bank_features["people_bucket"])
        animal_probs = cosine_probs(image_features, ctx.bank_features["animal_presence"])
        text_probs = cosine_probs(image_features, ctx.bank_features["text_presence"])
        document_probs = cosine_probs(image_features, ctx.bank_features["document_like"])
        screenshot_probs = cosine_probs(image_features, ctx.bank_features["screenshot_like"])
        landscape_probs = cosine_probs(image_features, ctx.bank_features["landscape_like"])
        food_probs = cosine_probs(image_features, ctx.bank_features["food_like"])
        indoor_outdoor_probs = cosine_probs(image_features, ctx.bank_features["indoor_outdoor"])
        interest_probs = cosine_probs(image_features, ctx.bank_features["interest"])

        for j, row in enumerate(valid_rows):
            scene_type = derive_scene_type(scene_probs[j])

            # Positive concept bucket for people.
            people_score = float(np.max(people_bucket_probs[j]))
            ap = animal_probs[j]
            tp = text_probs[j]
            dp = document_probs[j]
            sp = screenshot_probs[j]
            lp = landscape_probs[j]
            fp = food_probs[j]
            iop = indoor_outdoor_probs[j]
            ip = interest_probs[j]

            contains_people = 1 if people_score >= 0.20 else 0
            contains_animals = bool_from_pair(ap[0], ap[1], 0.52)
            contains_text = bool_from_pair(tp[0], tp[1], 0.52)
            is_document_like = bool_from_pair(dp[0], dp[1], 0.54)
            is_screenshot_like = bool_from_pair(sp[0], sp[1], 0.54)
            is_landscape_like = bool_from_pair(lp[0], lp[1], 0.53)
            is_food_like = bool_from_pair(fp[0], fp[1], 0.53)
            is_indoor_like = 1 if iop[0] >= iop[1] else 0
            is_outdoor_like = 1 if iop[1] > iop[0] else 0

            # Consistency overrides.
            if scene_type in {"portrait", "group_photo"}:
                contains_people = 1

            face_row = face_map.get(row.sha1)
            if face_row:
                try:
                    face_count = int(face_row.get("face_count") or 0)
                except Exception:
                    face_count = 0
                if face_count > 0:
                    contains_people = 1

            semantic_score = score_from_interest_probs(
                meaningful=float(ip[0]),
                ordinary=float(ip[1]),
                boring=float(ip[2]),
            )

            if contains_people:
                semantic_score += 0.10
            if contains_animals:
                semantic_score += 0.06
            if is_landscape_like:
                semantic_score += 0.07
            if scene_type in {"group_photo", "portrait", "landscape", "nature", "food", "animal"}:
                semantic_score += 0.05
            if is_document_like:
                semantic_score *= 0.45
            if is_screenshot_like:
                semantic_score *= 0.35
            if contains_text and not contains_people and not contains_animals and not is_food_like:
                semantic_score *= 0.80

            semantic_score = clamp01(semantic_score)

            top_labels = derive_top_labels(general_probs[j], top_n=6)
            ai_tags = derive_ai_tags(
                scene_type=scene_type,
                top_labels=top_labels,
                contains_people=contains_people,
                contains_animals=contains_animals,
                contains_text=contains_text,
                is_document_like=is_document_like,
                is_screenshot_like=is_screenshot_like,
                is_landscape_like=is_landscape_like,
                is_food_like=is_food_like,
                is_indoor_like=is_indoor_like,
                is_outdoor_like=is_outdoor_like,
                semantic_score=semantic_score,
            )

            warnings_list: list[str] = []
            if face_row and int(face_row.get("face_count") or 0) > 0:
                warnings_list.append("people_from_face_override")
            elif scene_type in {"portrait", "group_photo"} and contains_people:
                warnings_list.append("people_from_scene_override")

            results.append({
                "sha1": row.sha1,
                "semantic_score": round(float(semantic_score), 6),
                "scene_type": scene_type,
                "top_labels_json": safe_json(top_labels),
                "ai_tags_json": safe_json(ai_tags),
                "contains_people": int(contains_people),
                "contains_animals": int(contains_animals),
                "contains_text": int(contains_text),
                "is_document_like": int(is_document_like),
                "is_screenshot_like": int(is_screenshot_like),
                "is_landscape_like": int(is_landscape_like),
                "is_food_like": int(is_food_like),
                "is_indoor_like": int(is_indoor_like),
                "is_outdoor_like": int(is_outdoor_like),
                "scorer_name": "semantic-fast-openclip",
                "model_name": ctx.model_name,
                "model_version": ctx.model_version,
                "scored_at": utc_now_iso(),
                "warnings": "|".join(warnings_list),
            })

    return results

# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    print("Opening archive database...")
    archive_conn = sqlite3.connect(ARCHIVE_DB)
    archive_conn.row_factory = sqlite3.Row

    print("Opening output database...")
    out_conn = sqlite3.connect(OUTPUT_DB)
    out_conn.row_factory = sqlite3.Row

    if REBUILD_DATABASE:
        rebuild_db(out_conn)
    else:
        init_db(out_conn)

    attach_sidecar(archive_conn, OUTPUT_DB)

    print("Loading face summary sidecar map...")
    face_map = load_face_summary_map()
    if face_map:
        print(f"Loaded {len(face_map)} face summary rows.")
    else:
        print("No usable face summary sidecar found, or it is empty.")

    try:
        ctx = load_clip()
    except Exception as exc:
        print(f"[ERROR] CLIP model load failed: {exc}")
        if REQUIRE_CLIP:
            raise
        return 1

    limit_images = LIMIT_IMAGES
    candidates = read_candidate_rows(archive_conn, ctx.model_version, limit_images)
    print(f"Found {len(candidates)} images to score")

    run_id = begin_run(out_conn, ctx.model_name, ctx.model_version)

    total = len(candidates)
    attempted = 0
    scored = 0
    failed = 0
    skipped = 0
    start_time = time.time()

    batch: list[CandidateRow] = []
    for idx, cand in enumerate(candidates, start=1):
        batch.append(cand)

        flush = len(batch) >= CLIP_BATCH_SIZE or idx == total
        if flush and batch:
            try:
                rows = process_batch(ctx, batch, face_map)
                for row in rows:
                    upsert_semantic_row(out_conn, row)
                    if row["warnings"] == "open_failed":
                        failed += 1
                    else:
                        scored += 1
                attempted += len(batch)
            except Exception:
                attempted += len(batch)
                failed += len(batch)
                tb = traceback.format_exc()
                print(f"[ERROR] Batch failed for {len(batch)} images:\n{tb}")
                for row in batch:
                    upsert_semantic_row(out_conn, {
                        "sha1": row.sha1,
                        "semantic_score": 0.0,
                        "scene_type": "unknown",
                        "top_labels_json": "[]",
                        "ai_tags_json": "[]",
                        "contains_people": 0,
                        "contains_animals": 0,
                        "contains_text": 0,
                        "is_document_like": 0,
                        "is_screenshot_like": 0,
                        "is_landscape_like": 0,
                        "is_food_like": 0,
                        "is_indoor_like": 0,
                        "is_outdoor_like": 0,
                        "scorer_name": "semantic-fast-openclip",
                        "model_name": ctx.model_name,
                        "model_version": ctx.model_version,
                        "scored_at": utc_now_iso(),
                        "warnings": "batch_failed",
                    })
            batch = []

        if attempted > 0 and (attempted % HEARTBEAT_EVERY_N == 0 or idx == total):
            elapsed = max(1e-6, time.time() - start_time)
            rate = attempted / elapsed
            eta_sec = ((total - attempted) / max(1e-6, rate)) if attempted < total else 0.0
            pct = (attempted / total * 100.0) if total else 100.0
            current_full = os.path.join(IMAGE_ROOT, cand.rel_fqn)
            print(
                f"[Progress] {attempted}/{total} | {pct:.1f}% | "
                f"scored={scored} failed={failed} skipped={skipped} | "
                f"{rate:.2f} img/sec | ETA {eta_sec/60:.1f}m"
            )
            print(f"Current: {current_full}")

        if attempted > 0 and attempted % COMMIT_EVERY_N == 0:
            update_run(out_conn, run_id, attempted, scored, failed)
            out_conn.commit()
            print("[Checkpoint] committed")

    update_run(out_conn, run_id, attempted, scored, failed)
    finish_run(out_conn, run_id, attempted, scored, failed)

    out_conn.close()
    archive_conn.close()

    elapsed = time.time() - start_time
    print("=" * 80)
    print(f"Completed semantic fast scoring in {elapsed/60:.2f} minutes")
    print(f"Attempted={attempted} Scored={scored} Failed={failed} Skipped={skipped}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
