import os
import sys
import math
import time
import json
import sqlite3
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageFile

Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

ARCHIVE_DB = r"C:\website-photos\archive_index.db"
IMAGE_ROOT = r"C:\website-photos"
OUTPUT_DB = r"C:\website-photos\aesthetic_scores.sqlite"

DEFAULT_INCREMENTAL = True
PROGRESS_INTERVAL = 5
COMMIT_INTERVAL = 100

MAX_ANALYSIS_DIM = 1024
UPSCALE_SMALL_IMAGES = False

# Stable production identifier for the fixed CLIP scorer.
# Keep this unchanged unless the scoring semantics materially change.
MODEL_VERSION = "clip_aesthetic_v3_batched"
SCORER_NAME = "clip_plus_heuristics"
MODEL_NAME = "open_clip_vit_b32_or_transformers_clip_b32"

REQUIRE_CLIP = False
PREFER_OPEN_CLIP = True

CUDA_BATCH_SIZE = 16
CPU_BATCH_SIZE = 4

WEIGHT_AESTHETIC = 0.35
WEIGHT_COMPOSITION = 0.30
WEIGHT_SUBJECT = 0.20
WEIGHT_INTEREST = 0.15

PROMPT_PAIRS = {
    "aesthetic": (
        [
            "a beautiful professional photograph",
            "a visually stunning photograph",
            "an aesthetically pleasing image",
            "a well-shot high quality photo",
            "an elegant artistic photograph",
        ],
        [
            "an ugly poorly shot photo",
            "a low quality unattractive image",
            "a badly composed snapshot",
            "a boring low quality photograph",
            "an unappealing messy image",
        ],
    ),
    "composition": (
        [
            "a well composed photograph",
            "a photograph with strong composition",
            "a balanced thoughtfully framed image",
            "a photograph with excellent visual balance",
            "a photograph with clear intentional framing",
        ],
        [
            "a badly composed photograph",
            "a poorly framed image",
            "an unbalanced cluttered photograph",
            "a photograph with awkward framing",
            "a compositionally weak snapshot",
        ],
    ),
    "subject_prominence": (
        [
            "a photograph with a clear main subject",
            "an image with a strong focal point",
            "a picture with a prominent subject",
            "a photo where the subject stands out clearly",
            "a visually focused photograph",
        ],
        [
            "a cluttered image with no clear subject",
            "a confusing busy photograph",
            "a diffuse scene without a focal point",
            "an image with too many competing subjects",
            "a visually unfocused photograph",
        ],
    ),
    "interest": (
        [
            "a memorable interesting photograph",
            "a meaningful keeper photo",
            "a compelling photograph worth saving",
            "an emotionally resonant image",
            "a striking and interesting picture",
        ],
        [
            "a forgettable uninteresting snapshot",
            "a mundane disposable photo",
            "a boring unremarkable image",
            "a photograph not worth keeping",
            "an emotionally flat ordinary snapshot",
        ],
    ),
}

CLIP_STATE = {
    "enabled": False,
    "device": "cpu",
    "backend": None,
    "model": None,
    "preprocess": None,
    "processor": None,
    "tokenizer": None,
    "text_features": {},
    "warnings": [],
}

# ------------------------------------------------------------
# UTILITIES
# ------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")



def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))



def safe_json(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return json.dumps(["json_encode_failed"], ensure_ascii=False)



def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))



def logistic_centered(x: float, center: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return sigmoid((x - center) / scale)



def open_image_rgb(path: str) -> Image.Image:
    with Image.open(path) as img:
        img = img.convert("RGB")
        return img.copy()



def pil_to_bgr_np(img: Image.Image) -> np.ndarray:
    arr = np.array(img)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)



def normalize_working_image(img: Image.Image) -> Tuple[Image.Image, float]:
    w, h = img.size
    max_dim = max(w, h)
    if max_dim <= 0:
        return img, 1.0
    if max_dim <= MAX_ANALYSIS_DIM and not UPSCALE_SMALL_IMAGES:
        return img, 1.0
    scale = MAX_ANALYSIS_DIM / float(max_dim)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    if new_w == w and new_h == h:
        return img, 1.0
    resample = Image.Resampling.LANCZOS if scale < 1.0 else Image.Resampling.BICUBIC
    return img.resize((new_w, new_h), resample), scale



def chunked(seq, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

# ------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aesthetic_scores(
            sha1 TEXT PRIMARY KEY,
            aesthetic_score REAL,
            composition_score REAL,
            subject_prominence_score REAL,
            interest_score REAL,
            saliency_score REAL,
            overall_aesthetic_score REAL,
            scorer_name TEXT,
            model_name TEXT,
            model_version TEXT,
            scored_at TEXT,
            warnings TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aesthetic_score_runs(
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            completed_at TEXT NULL,
            model_name TEXT,
            model_version TEXT,
            images_attempted INTEGER,
            images_scored INTEGER,
            images_failed INTEGER,
            notes TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_aesthetic_scores_model_version ON aesthetic_scores(model_version)"
    )
    conn.commit()



def begin_run(conn: sqlite3.Connection, notes: str) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO aesthetic_score_runs(
            started_at, completed_at, model_name, model_version,
            images_attempted, images_scored, images_failed, notes
        ) VALUES (?, NULL, ?, ?, 0, 0, 0, ?)
        """,
        (utc_now_iso(), MODEL_NAME, MODEL_VERSION, notes),
    )
    conn.commit()
    return int(cur.lastrowid)



def finish_run(conn: sqlite3.Connection, run_id: int, attempted: int, scored: int, failed: int) -> None:
    conn.execute(
        """
        UPDATE aesthetic_score_runs
        SET completed_at=?, images_attempted=?, images_scored=?, images_failed=?
        WHERE run_id=?
        """,
        (utc_now_iso(), attempted, scored, failed, run_id),
    )
    conn.commit()

# ------------------------------------------------------------
# CLIP SUPPORT
# ------------------------------------------------------------


def _torch_imports():
    import torch
    return torch



def _normalize_torch_embeddings(tensor):
    return tensor / tensor.norm(dim=-1, keepdim=True).clamp(min=1e-12)



def _precompute_prompt_features_open_clip(torch, model, tokenizer, device: str):
    text_features = {}
    with torch.no_grad():
        for metric_name, (positive_prompts, negative_prompts) in PROMPT_PAIRS.items():
            pos_tokens = tokenizer(positive_prompts).to(device)
            neg_tokens = tokenizer(negative_prompts).to(device)
            pos_features = model.encode_text(pos_tokens)
            neg_features = model.encode_text(neg_tokens)
            pos_features = _normalize_torch_embeddings(pos_features)
            neg_features = _normalize_torch_embeddings(neg_features)
            text_features[metric_name] = {
                "pos": pos_features.detach().cpu().numpy().astype(np.float32),
                "neg": neg_features.detach().cpu().numpy().astype(np.float32),
            }
    return text_features



def _precompute_prompt_features_transformers(torch, model, processor, device: str):
    text_features = {}
    with torch.no_grad():
        for metric_name, (positive_prompts, negative_prompts) in PROMPT_PAIRS.items():
            pos_inputs = processor(text=positive_prompts, return_tensors="pt", padding=True, truncation=True)
            neg_inputs = processor(text=negative_prompts, return_tensors="pt", padding=True, truncation=True)
            pos_inputs = {k: v.to(device) for k, v in pos_inputs.items()}
            neg_inputs = {k: v.to(device) for k, v in neg_inputs.items()}
            pos_features = model.get_text_features(**pos_inputs)
            neg_features = model.get_text_features(**neg_inputs)
            pos_features = _normalize_torch_embeddings(pos_features)
            neg_features = _normalize_torch_embeddings(neg_features)
            text_features[metric_name] = {
                "pos": pos_features.detach().cpu().numpy().astype(np.float32),
                "neg": neg_features.detach().cpu().numpy().astype(np.float32),
            }
    return text_features



def init_clip() -> None:
    try:
        torch = _torch_imports()
    except Exception as exc:
        msg = f"CLIP imports failed: {exc}"
        CLIP_STATE["warnings"].append(msg)
        if REQUIRE_CLIP:
            raise RuntimeError(msg) from exc
        print(f"[Warning] {msg}")
        return

    device = "cuda" if torch.cuda.is_available() else "cpu"
    CLIP_STATE["device"] = device

    errors = []

    if PREFER_OPEN_CLIP:
        try:
            import open_clip
            print(f"Loading CLIP via open_clip on {device}...")
            model, _, preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai", device=device)
            tokenizer = open_clip.get_tokenizer("ViT-B-32")
            model.eval()
            text_features = _precompute_prompt_features_open_clip(torch, model, tokenizer, device)
            CLIP_STATE.update({
                "enabled": True,
                "backend": "open_clip",
                "model": model,
                "preprocess": preprocess,
                "tokenizer": tokenizer,
                "text_features": text_features,
            })
            print("CLIP ready via open_clip.")
            return
        except Exception as exc:
            errors.append(f"open_clip_init_failed: {exc}")

    try:
        from transformers import CLIPModel, AutoProcessor
        print(f"Loading CLIP via transformers on {device}...")
        processor = AutoProcessor.from_pretrained("openai/clip-vit-base-patch32", use_fast=True)
        model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
        model.to(device)
        model.eval()
        text_features = _precompute_prompt_features_transformers(torch, model, processor, device)
        CLIP_STATE.update({
            "enabled": True,
            "backend": "transformers",
            "model": model,
            "processor": processor,
            "text_features": text_features,
        })
        print("CLIP ready via transformers.")
        return
    except Exception as exc:
        errors.append(f"transformers_init_failed: {exc}")

    msg = "; ".join(errors) if errors else "clip_backend_unavailable"
    CLIP_STATE["warnings"].append(msg)
    if REQUIRE_CLIP:
        raise RuntimeError(msg)
    print(f"[Warning] CLIP unavailable; continuing heuristic-only. {msg}")



def compute_clip_metric_scores_batch(images_rgb: List[Image.Image]) -> Tuple[List[Dict[str, float]], List[List[str]]]:
    if not CLIP_STATE["enabled"]:
        return [{} for _ in images_rgb], [list(CLIP_STATE["warnings"]) for _ in images_rgb]

    torch = _torch_imports()
    model = CLIP_STATE["model"]
    device = CLIP_STATE["device"]
    text_features = CLIP_STATE["text_features"]

    try:
        with torch.no_grad():
            if CLIP_STATE["backend"] == "open_clip":
                preprocess = CLIP_STATE["preprocess"]
                batch = torch.stack([preprocess(img) for img in images_rgb]).to(device)
                image_features = model.encode_image(batch)
            else:
                processor = CLIP_STATE["processor"]
                inputs = processor(images=images_rgb, return_tensors="pt")
                inputs = {k: v.to(device) for k, v in inputs.items()}
                image_features = model.get_image_features(**inputs)

            image_features = _normalize_torch_embeddings(image_features)
            image_vectors = image_features.detach().cpu().numpy().astype(np.float32)

        all_results: List[Dict[str, float]] = []
        for image_vec in image_vectors:
            result: Dict[str, float] = {}
            for metric_name, blobs in text_features.items():
                pos_sims = np.matmul(blobs["pos"], image_vec)
                neg_sims = np.matmul(blobs["neg"], image_vec)
                raw_margin = float(np.mean(pos_sims) - np.mean(neg_sims))
                result[metric_name] = clamp01(logistic_centered(raw_margin, 0.0, 0.03))
            all_results.append(result)
        return all_results, [[] for _ in images_rgb]
    except Exception as exc:
        warning = f"clip_inference_failed: {exc}"
        if REQUIRE_CLIP:
            raise
        return [{} for _ in images_rgb], [[warning] for _ in images_rgb]

# ------------------------------------------------------------
# HEURISTICS
# ------------------------------------------------------------


def spectral_residual_saliency(gray: np.ndarray) -> np.ndarray:
    gray_f = gray.astype(np.float32)
    fft = np.fft.fft2(gray_f)
    log_amp = np.log(np.abs(fft) + 1e-8)
    phase = np.angle(fft)
    avg_log_amp = cv2.blur(log_amp, (3, 3))
    spectral_residual = log_amp - avg_log_amp
    saliency_fft = np.exp(spectral_residual + 1j * phase)
    saliency = np.abs(np.fft.ifft2(saliency_fft)) ** 2
    saliency = cv2.GaussianBlur(saliency.astype(np.float32), (9, 9), 2.5)
    saliency -= saliency.min()
    maxv = saliency.max()
    if maxv > 1e-8:
        saliency /= maxv
    else:
        saliency[:] = 0.0
    return saliency.astype(np.float32)



def compute_entropy(gray: np.ndarray) -> float:
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
    total = float(hist.sum())
    if total <= 0:
        return 0.0
    p = hist / total
    p = p[p > 0]
    return float(-np.sum(p * np.log2(p)))



def compute_colorfulness(bgr: np.ndarray) -> float:
    b, g, r = cv2.split(bgr.astype(np.float32))
    rg = np.abs(r - g)
    yb = np.abs(0.5 * (r + g) - b)
    std_rg, mean_rg = np.std(rg), np.mean(rg)
    std_yb, mean_yb = np.std(yb), np.mean(yb)
    return float(np.sqrt(std_rg ** 2 + std_yb ** 2) + 0.3 * np.sqrt(mean_rg ** 2 + mean_yb ** 2))



def compute_sharpness(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())



def compute_edge_density(gray: np.ndarray) -> float:
    edges = cv2.Canny(gray, 100, 200)
    return float(np.mean(edges > 0))



def compute_saturation(hsv: np.ndarray) -> float:
    return float(np.mean(hsv[:, :, 1].astype(np.float32)) / 255.0)



def compute_brightness(gray: np.ndarray) -> float:
    return float(np.mean(gray) / 255.0)



def compute_contrast(gray: np.ndarray) -> float:
    return float(np.std(gray) / 64.0)



def saliency_mass_properties(saliency: np.ndarray) -> Dict[str, float]:
    h, w = saliency.shape[:2]
    if h <= 0 or w <= 0:
        return {
            "cx": 0.5, "cy": 0.5, "q90_ratio": 0.0,
            "subject_bbox_area_ratio": 0.0, "edge_touch_ratio": 1.0,
            "balance_score": 0.0, "rule_of_thirds_score": 0.0,
        }

    mass = saliency.astype(np.float64)
    total = float(mass.sum())
    if total <= 1e-12:
        return {
            "cx": 0.5, "cy": 0.5, "q90_ratio": 0.0,
            "subject_bbox_area_ratio": 0.0, "edge_touch_ratio": 1.0,
            "balance_score": 0.0, "rule_of_thirds_score": 0.0,
        }

    ys, xs = np.mgrid[0:h, 0:w]
    cx = float((mass * xs).sum() / total) / max(1.0, (w - 1))
    cy = float((mass * ys).sum() / total) / max(1.0, (h - 1))

    q90 = float(np.quantile(mass, 0.90))
    mask90 = (mass >= q90).astype(np.uint8)
    q90_ratio = float(mask90.mean())

    ys90, xs90 = np.where(mask90 > 0)
    if len(xs90) > 0:
        x0, x1 = int(xs90.min()), int(xs90.max())
        y0, y1 = int(ys90.min()), int(ys90.max())
        bbox_area = max(1, (x1 - x0 + 1) * (y1 - y0 + 1))
        bbox_area_ratio = bbox_area / float(w * h)
        edge_band_x = max(1, int(round(w * 0.05)))
        edge_band_y = max(1, int(round(h * 0.05)))
        edge_mask = np.zeros_like(mask90)
        edge_mask[:, :edge_band_x] = 1
        edge_mask[:, w - edge_band_x:] = 1
        edge_mask[:edge_band_y, :] = 1
        edge_mask[h - edge_band_y:, :] = 1
        edge_touch_ratio = float(np.sum(mask90 * edge_mask) / max(1, np.sum(mask90)))
    else:
        bbox_area_ratio = 0.0
        edge_touch_ratio = 1.0

    left_mass = float(mass[:, : max(1, w // 2)].sum())
    right_mass = float(mass[:, w // 2 :].sum())
    top_mass = float(mass[: max(1, h // 2), :].sum())
    bottom_mass = float(mass[h // 2 :, :].sum())
    lr_balance = 1.0 - abs(left_mass - right_mass) / max(total, 1e-8)
    tb_balance = 1.0 - abs(top_mass - bottom_mass) / max(total, 1e-8)
    balance_score = clamp01((lr_balance + tb_balance) * 0.5)

    thirds = [(1/3,1/3),(2/3,1/3),(1/3,2/3),(2/3,2/3)]
    d = min(math.sqrt((cx-tx)**2 + (cy-ty)**2) for tx, ty in thirds)
    rule_of_thirds_score = clamp01(1.0 - (d / 0.4715))

    return {
        "cx": cx,
        "cy": cy,
        "q90_ratio": q90_ratio,
        "subject_bbox_area_ratio": bbox_area_ratio,
        "edge_touch_ratio": edge_touch_ratio,
        "balance_score": balance_score,
        "rule_of_thirds_score": rule_of_thirds_score,
    }



def compute_heuristic_scores(img_rgb: Image.Image) -> Tuple[Dict[str, float], List[str]]:
    warnings: List[str] = []
    try:
        working_img, scale = normalize_working_image(img_rgb)
        if abs(scale - 1.0) > 1e-6:
            warnings.append(f"normalized_scale={scale:.6f}")
    except Exception as exc:
        return {}, [f"image_normalize_failed: {exc}"]

    try:
        bgr = pil_to_bgr_np(working_img)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        saliency = spectral_residual_saliency(gray)
    except Exception as exc:
        return {}, [f"opencv_analysis_failed: {exc}"]

    brightness = compute_brightness(gray)
    contrast = clamp01(compute_contrast(gray))
    sharpness = clamp01(logistic_centered(compute_sharpness(gray), center=120.0, scale=80.0))
    entropy = clamp01(logistic_centered(compute_entropy(gray), center=6.5, scale=1.0))
    colorfulness = clamp01(logistic_centered(compute_colorfulness(bgr), center=35.0, scale=18.0))
    saturation = clamp01(compute_saturation(hsv) / 0.65)
    edge_density = clamp01(compute_edge_density(gray) / 0.18)
    exposure_score = 1.0 - min(1.0, abs(brightness - 0.5) / 0.5)

    mass = saliency_mass_properties(saliency)
    saliency_score = clamp01(logistic_centered(float(np.mean(saliency)), center=0.22, scale=0.08))

    composition_score = clamp01(
        0.35 * mass["rule_of_thirds_score"] +
        0.35 * mass["balance_score"] +
        0.15 * (1.0 - mass["edge_touch_ratio"]) +
        0.15 * exposure_score
    )

    subject_prominence_score = clamp01(
        0.45 * clamp01(logistic_centered(mass["subject_bbox_area_ratio"], center=0.18, scale=0.10)) +
        0.35 * clamp01(1.0 - min(1.0, mass["q90_ratio"] / 0.25)) +
        0.20 * (1.0 - mass["edge_touch_ratio"])
    )

    aesthetic_score = clamp01(
        0.22 * sharpness +
        0.18 * contrast +
        0.14 * exposure_score +
        0.12 * saturation +
        0.12 * colorfulness +
        0.12 * composition_score +
        0.10 * entropy
    )

    interest_score = clamp01(
        0.30 * subject_prominence_score +
        0.20 * saliency_score +
        0.20 * entropy +
        0.15 * colorfulness +
        0.15 * edge_density
    )

    return {
        "aesthetic_score": aesthetic_score,
        "composition_score": composition_score,
        "subject_prominence_score": subject_prominence_score,
        "interest_score": interest_score,
        "saliency_score": saliency_score,
        "heur_brightness": brightness,
        "heur_contrast": contrast,
        "heur_sharpness": sharpness,
        "heur_entropy": entropy,
        "heur_colorfulness": colorfulness,
        "heur_saturation": saturation,
    }, warnings

# ------------------------------------------------------------
# SCORE BLENDING / UPSERT
# ------------------------------------------------------------


def blend_scores(heur: Dict[str, float], clip: Dict[str, float]) -> Dict[str, float]:
    def blended(metric: str, heur_weight: float = 0.45, clip_weight: float = 0.55) -> float:
        h = heur.get(metric, 0.0)
        c = clip.get(metric)
        if c is None:
            return clamp01(h)
        return clamp01(heur_weight * h + clip_weight * c)

    aesthetic_score = blended("aesthetic_score")
    composition_score = blended("composition_score")
    subject_prominence_score = blended("subject_prominence_score")
    interest_score = blended("interest_score")
    saliency_score = clamp01(heur.get("saliency_score", 0.0))

    overall = clamp01(
        WEIGHT_AESTHETIC * aesthetic_score +
        WEIGHT_COMPOSITION * composition_score +
        WEIGHT_SUBJECT * subject_prominence_score +
        WEIGHT_INTEREST * interest_score
    )

    return {
        "aesthetic_score": aesthetic_score,
        "composition_score": composition_score,
        "subject_prominence_score": subject_prominence_score,
        "interest_score": interest_score,
        "saliency_score": saliency_score,
        "overall_aesthetic_score": overall,
    }



def upsert_score(conn: sqlite3.Connection, sha1: str, score_row: Dict[str, float], warnings: List[str]) -> None:
    conn.execute(
        """
        INSERT INTO aesthetic_scores(
            sha1, aesthetic_score, composition_score, subject_prominence_score,
            interest_score, saliency_score, overall_aesthetic_score,
            scorer_name, model_name, model_version, scored_at, warnings
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sha1) DO UPDATE SET
            aesthetic_score=excluded.aesthetic_score,
            composition_score=excluded.composition_score,
            subject_prominence_score=excluded.subject_prominence_score,
            interest_score=excluded.interest_score,
            saliency_score=excluded.saliency_score,
            overall_aesthetic_score=excluded.overall_aesthetic_score,
            scorer_name=excluded.scorer_name,
            model_name=excluded.model_name,
            model_version=excluded.model_version,
            scored_at=excluded.scored_at,
            warnings=excluded.warnings
        """,
        (
            sha1,
            score_row.get("aesthetic_score"),
            score_row.get("composition_score"),
            score_row.get("subject_prominence_score"),
            score_row.get("interest_score"),
            score_row.get("saliency_score"),
            score_row.get("overall_aesthetic_score"),
            SCORER_NAME,
            MODEL_NAME,
            MODEL_VERSION,
            utc_now_iso(),
            safe_json(warnings),
        ),
    )

# ------------------------------------------------------------
# MAIN WORKER
# ------------------------------------------------------------


def parse_args(argv: List[str]) -> Tuple[bool, Optional[int]]:
    incremental = DEFAULT_INCREMENTAL
    limit = None
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--rebuild":
            incremental = False
        elif arg == "--incremental":
            incremental = True
        elif arg == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
            i += 1
        i += 1
    return incremental, limit



def build_todo_rows(archive_conn: sqlite3.Connection, incremental: bool, limit: Optional[int]) -> List[sqlite3.Row]:
    limit_sql = f" LIMIT {int(limit)}" if limit is not None and limit > 0 else ""

    if incremental:
        print("Reading active archive rows needing score update...")
        query = f"""
            SELECT m.sha1, m.rel_fqn
            FROM media m
            LEFT JOIN sidecar.aesthetic_scores s ON s.sha1 = m.sha1
            WHERE m.is_deleted = 0
              AND (
                    s.sha1 IS NULL
                 OR s.model_version <> ?
                 OR s.warnings LIKE '%clip_model_load_failed%'
                 OR s.warnings LIKE '%clip_inference_failed%'
                 OR s.warnings LIKE '%CLIP unavailable%'
                 OR s.warnings LIKE '%transformers_init_failed%'
                 OR s.warnings LIKE '%open_clip_init_failed%'
                 OR s.warnings LIKE '%clip_backend_unavailable%'
              )
            ORDER BY m.rowid
            {limit_sql}
        """
        return archive_conn.execute(query, (MODEL_VERSION,)).fetchall()

    print("Reading all active archive rows...")
    query = f"""
        SELECT sha1, rel_fqn
        FROM media
        WHERE is_deleted = 0
        ORDER BY rowid
        {limit_sql}
    """
    return archive_conn.execute(query).fetchall()



def main() -> int:
    incremental, limit = parse_args(sys.argv)

    print("Opening archive database...")
    archive_conn = sqlite3.connect(ARCHIVE_DB)
    archive_conn.row_factory = sqlite3.Row

    print("Opening output database...")
    out_conn = sqlite3.connect(OUTPUT_DB)
    out_conn.row_factory = sqlite3.Row
    init_db(out_conn)

    attach_path = OUTPUT_DB.replace("'", "''")
    archive_conn.execute(f"ATTACH DATABASE '{attach_path}' AS sidecar")

    init_clip()
    clip_enabled = CLIP_STATE["enabled"]
    device = CLIP_STATE["device"]
    batch_size = CUDA_BATCH_SIZE if device == "cuda" else CPU_BATCH_SIZE

    notes = [
        f"incremental={incremental}",
        f"clip_enabled={clip_enabled}",
        f"clip_backend={CLIP_STATE['backend']}",
        f"device={device}",
        f"batch_size={batch_size}",
        f"max_analysis_dim={MAX_ANALYSIS_DIM}",
    ]
    if CLIP_STATE["warnings"]:
        notes.append(f"clip_warnings={safe_json(CLIP_STATE['warnings'])}")

    run_id = begin_run(out_conn, "; ".join(notes))
    rows = build_todo_rows(archive_conn, incremental, limit)
    total = len(rows)
    print(f"Found {total} images to score")

    attempted = 0
    scored = 0
    failed = 0
    skipped = 0
    last_progress = time.time()
    started = time.time()
    pending: List[Dict[str, object]] = []

    def flush_batch() -> None:
        nonlocal attempted, scored, failed, skipped, pending
        if not pending:
            return

        clip_images = [x["working_img"] for x in pending if x.get("working_img") is not None]
        clip_results: List[Dict[str, float]] = []
        clip_warnings: List[List[str]] = []
        if clip_images:
            clip_results, clip_warnings = compute_clip_metric_scores_batch(clip_images)

        clip_iter = 0
        for item in pending:
            attempted += 1
            if item.get("error"):
                failed += 1
                upsert_score(out_conn, item["sha1"], {
                    "aesthetic_score": 0.0,
                    "composition_score": 0.0,
                    "subject_prominence_score": 0.0,
                    "interest_score": 0.0,
                    "saliency_score": 0.0,
                    "overall_aesthetic_score": 0.0,
                }, [item["error"]])
                continue

            heur = item["heur"]
            warnings = list(item["warnings"])
            clip = {}
            if item.get("working_img") is not None:
                clip = clip_results[clip_iter] if clip_iter < len(clip_results) else {}
                if clip_iter < len(clip_warnings):
                    warnings.extend(clip_warnings[clip_iter])
                clip_iter += 1

            merged = blend_scores(heur, clip)
            upsert_score(out_conn, item["sha1"], merged, warnings)
            scored += 1

        pending = []

    for idx, row in enumerate(rows, start=1):
        sha1 = row["sha1"]
        rel_fqn = row["rel_fqn"]
        abs_path = os.path.join(IMAGE_ROOT, rel_fqn)

        try:
            img = open_image_rgb(abs_path)
            working_img, scale = normalize_working_image(img)
            heur, heur_warnings = compute_heuristic_scores(working_img)
            warnings = list(heur_warnings)
            if abs(scale - 1.0) > 1e-6:
                warnings.append(f"normalized_scale={scale:.6f}")
            pending.append({
                "sha1": sha1,
                "rel_fqn": rel_fqn,
                "working_img": working_img,
                "heur": heur,
                "warnings": warnings,
            })
        except Exception as exc:
            pending.append({
                "sha1": sha1,
                "rel_fqn": rel_fqn,
                "working_img": None,
                "heur": {},
                "warnings": [],
                "error": f"image_open_or_analysis_failed: {exc}",
            })

        if len(pending) >= batch_size:
            flush_batch()

        if idx % COMMIT_INTERVAL == 0:
            out_conn.commit()
            print("[Checkpoint] committed")

        now = time.time()
        if idx == total or (now - last_progress) >= PROGRESS_INTERVAL:
            elapsed = max(1e-6, now - started)
            rate = attempted / elapsed if attempted > 0 else 0.0
            remaining = max(0, total - idx)
            eta_secs = remaining / rate if rate > 1e-6 else 0.0
            eta_min = eta_secs / 60.0
            pct = (idx / total * 100.0) if total else 100.0
            current_path = abs_path
            print(
                f"[Progress] {idx}/{total} | {pct:.1f}% | scored={scored} failed={failed} skipped={skipped} | "
                f"{rate:.2f} img/sec | ETA {eta_min:.1f}m"
            )
            print(f"Current: {current_path}")
            last_progress = now

    flush_batch()
    out_conn.commit()
    finish_run(out_conn, run_id, attempted, scored, failed)
    print(f"Done. attempted={attempted} scored={scored} failed={failed} skipped={skipped}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted by user.")
        raise
    except Exception:
        traceback.print_exc()
        raise
