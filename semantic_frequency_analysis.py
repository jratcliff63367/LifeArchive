#!/usr/bin/env python3
"""
Analyze the Life Archive semantic sidecar database and emit:
1) human-readable frequency reports
2) ready-to-paste Python config blocks for the backend script

Default database location:
    C:\LifeArchive\semantic_scores.sqlite

This script is intentionally conservative:
- it does not guess categories
- it only reports what is actually present in the semantic sidecar
- it emits config lists you can manually trim or expand later

Example:
    python semantic_frequency_analysis.py
    python semantic_frequency_analysis.py --top-n 20 --min-count 10
    python semantic_frequency_analysis.py --output semantic_frequency_report.txt
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = r"C:\LifeArchive\semantic_scores.sqlite"

BOOLEAN_FIELDS = [
    "contains_people",
    "contains_animals",
    "contains_text",
    "is_document_like",
    "is_screenshot_like",
    "is_landscape_like",
    "is_food_like",
    "is_indoor_like",
    "is_outdoor_like",
]


@dataclass
class LabelStats:
    image_count: int = 0
    occurrence_count: int = 0
    top1_count: int = 0
    score_total: float = 0.0
    score_max: float = 0.0

    def add(self, score: float, is_top1: bool) -> None:
        self.occurrence_count += 1
        self.score_total += score
        self.score_max = max(self.score_max, score)
        if is_top1:
            self.top1_count += 1

    @property
    def avg_score(self) -> float:
        return self.score_total / self.occurrence_count if self.occurrence_count else 0.0


def normalize_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = " ".join(text.split())
    return text


def friendly_name(field_name: str) -> str:
    name = field_name
    for prefix in ("contains_", "is_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    if name.endswith("_like"):
        name = name[:-5]
    return name.replace("_", " ").title()


def load_rows(db_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT
                sha1,
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
                is_outdoor_like
            FROM semantic_scores
            """
        ).fetchall()


def safe_json_loads(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def analyze(rows: list[sqlite3.Row]) -> dict[str, Any]:
    total_images = len(rows)

    boolean_counts: dict[str, int] = {field: 0 for field in BOOLEAN_FIELDS}

    label_stats: dict[str, LabelStats] = defaultdict(LabelStats)
    label_images_seen: dict[str, set[str]] = defaultdict(set)

    tag_counts = Counter()
    tag_images_seen: dict[str, set[str]] = defaultdict(set)

    scene_type_counts = Counter()

    for row in rows:
        sha1 = str(row["sha1"])

        scene_type = normalize_token(row["scene_type"])
        if scene_type:
            scene_type_counts[scene_type] += 1

        for field in BOOLEAN_FIELDS:
            try:
                if int(row[field] or 0) == 1:
                    boolean_counts[field] += 1
            except Exception:
                pass

        top_labels = safe_json_loads(row["top_labels_json"], [])
        if isinstance(top_labels, list):
            for idx, item in enumerate(top_labels):
                if not isinstance(item, dict):
                    continue
                label = normalize_token(item.get("label"))
                if not label:
                    continue
                score = 0.0
                try:
                    score = float(item.get("score") or 0.0)
                except Exception:
                    score = 0.0
                if sha1 not in label_images_seen[label]:
                    label_images_seen[label].add(sha1)
                    label_stats[label].image_count += 1
                label_stats[label].add(score, is_top1=(idx == 0))

        ai_tags = safe_json_loads(row["ai_tags_json"], [])
        if isinstance(ai_tags, list):
            for tag in ai_tags:
                norm = normalize_token(tag)
                if not norm:
                    continue
                tag_counts[norm] += 1
                if sha1 not in tag_images_seen[norm]:
                    tag_images_seen[norm].add(sha1)

    tag_image_counts = {tag: len(images) for tag, images in tag_images_seen.items()}

    return {
        "total_images": total_images,
        "boolean_counts": boolean_counts,
        "label_stats": label_stats,
        "tag_counts": tag_counts,
        "tag_image_counts": tag_image_counts,
        "scene_type_counts": scene_type_counts,
    }


def pct(count: int, total: int) -> float:
    return (100.0 * count / total) if total else 0.0


def format_report(analysis: dict[str, Any], top_n: int, min_count: int) -> str:
    total_images = analysis["total_images"]
    boolean_counts = analysis["boolean_counts"]
    label_stats: dict[str, LabelStats] = analysis["label_stats"]
    tag_counts: Counter = analysis["tag_counts"]
    tag_image_counts: dict[str, int] = analysis["tag_image_counts"]
    scene_type_counts: Counter = analysis["scene_type_counts"]

    lines: list[str] = []
    lines.append("Life Archive semantic sidecar frequency analysis")
    lines.append("=" * 64)
    lines.append(f"Total images analyzed: {total_images}")
    lines.append("")

    lines.append("Semantic booleans")
    lines.append("-" * 64)
    for field in BOOLEAN_FIELDS:
        count = boolean_counts.get(field, 0)
        lines.append(f"{field:24s} {count:8d}  ({pct(count, total_images):6.2f}%)")
    lines.append("")

    lines.append(f"Scene type frequencies (min_count={min_count}, top_n={top_n})")
    lines.append("-" * 64)
    ranked_scene_types = sorted(
        (
            (scene_type, count)
            for scene_type, count in scene_type_counts.items()
            if count >= min_count
        ),
        key=lambda item: (item[1], item[0]),
        reverse=True,
    )
    for scene_type, count in ranked_scene_types[:top_n]:
        lines.append(
            f"{scene_type:20s} {count:8d}  ({pct(count, total_images):6.2f}%)"
        )
    lines.append("")

    lines.append(f"Top semantic labels (all positions, min_count={min_count}, top_n={top_n})")
    lines.append("-" * 64)
    ranked_labels = sorted(
        (
            (label, stats)
            for label, stats in label_stats.items()
            if stats.image_count >= min_count
        ),
        key=lambda pair: (pair[1].image_count, pair[1].top1_count, pair[1].avg_score, pair[0]),
        reverse=True,
    )
    for label, stats in ranked_labels[:top_n]:
        lines.append(
            f"{label:20s} "
            f"images={stats.image_count:6d}  "
            f"occurrences={stats.occurrence_count:6d}  "
            f"top1={stats.top1_count:6d}  "
            f"avg_score={stats.avg_score:0.3f}  "
            f"max_score={stats.score_max:0.3f}"
        )
    lines.append("")

    lines.append(f"Top semantic labels (top-1 only, min_count={min_count}, top_n={top_n})")
    lines.append("-" * 64)
    ranked_top1 = sorted(
        (
            (label, stats)
            for label, stats in label_stats.items()
            if stats.top1_count >= min_count
        ),
        key=lambda pair: (pair[1].top1_count, pair[1].image_count, pair[1].avg_score, pair[0]),
        reverse=True,
    )
    for label, stats in ranked_top1[:top_n]:
        lines.append(
            f"{label:20s} "
            f"top1={stats.top1_count:6d}  "
            f"images={stats.image_count:6d}  "
            f"avg_score={stats.avg_score:0.3f}"
        )
    lines.append("")

    lines.append(f"Top AI tags (unique image frequency, min_count={min_count}, top_n={top_n})")
    lines.append("-" * 64)
    ranked_tags = sorted(
        (
            (tag, tag_image_counts[tag], tag_counts[tag])
            for tag in tag_image_counts
            if tag_image_counts[tag] >= min_count
        ),
        key=lambda item: (item[1], item[2], item[0]),
        reverse=True,
    )
    for tag, image_count, occ_count in ranked_tags[:top_n]:
        lines.append(
            f"{tag:20s} images={image_count:6d}  "
            f"occurrences={occ_count:6d}  "
            f"coverage={pct(image_count, total_images):6.2f}%"
        )
    lines.append("")

    lines.append("Suggested backend config block")
    lines.append("-" * 64)
    lines.append(build_config_block(analysis, top_n=top_n, min_count=min_count))

    return "\n".join(lines)


def build_config_block(analysis: dict[str, Any], top_n: int, min_count: int) -> str:
    boolean_counts = analysis["boolean_counts"]
    label_stats: dict[str, LabelStats] = analysis["label_stats"]
    tag_image_counts: dict[str, int] = analysis["tag_image_counts"]
    scene_type_counts: Counter = analysis["scene_type_counts"]

    enabled_booleans = [
        field for field in BOOLEAN_FIELDS
        if boolean_counts.get(field, 0) >= min_count
    ]

    ranked_labels = sorted(
        (
            label for label, stats in label_stats.items()
            if stats.image_count >= min_count
        ),
        key=lambda label: (
            label_stats[label].image_count,
            label_stats[label].top1_count,
            label_stats[label].avg_score,
            label,
        ),
        reverse=True,
    )[:top_n]

    ranked_tags = sorted(
        (
            tag for tag, count in tag_image_counts.items()
            if count >= min_count
        ),
        key=lambda tag: (tag_image_counts[tag], tag),
        reverse=True,
    )[:top_n]

    ranked_scene_types = sorted(
        (
            scene_type for scene_type, count in scene_type_counts.items()
            if count >= min_count
        ),
        key=lambda scene_type: (scene_type_counts[scene_type], scene_type),
        reverse=True,
    )[:top_n]

    lines: list[str] = []
    lines.append("# ---------------------------------------------------------------------------")
    lines.append("# Semantic sort config (generated from semantic_frequency_analysis.py)")
    lines.append("# You can manually add/remove items after pasting this into the backend.")
    lines.append("# ---------------------------------------------------------------------------")
    lines.append("SEMANTIC_BOOLEAN_SORT_FIELDS = [")
    for field in enabled_booleans:
        lines.append(f'    {{"key": "{field}", "label": "{friendly_name(field)}"}},')
    lines.append("]")
    lines.append("")
    lines.append("SEMANTIC_LABEL_SORT_KEYS = [")
    for label in ranked_labels:
        lines.append(f'    "{label}",')
    lines.append("]")
    lines.append("")

    lines.append("SEMANTIC_SCENE_TYPE_SORT_KEYS = [")
    for scene_type in ranked_scene_types:
        lines.append(f'    "{scene_type}",')
    lines.append("]")
    lines.append("")
    lines.append("SEMANTIC_TAG_SORT_KEYS = [")
    for tag in ranked_tags:
        lines.append(f'    "{tag}",')
    lines.append("]")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Path to semantic_scores.sqlite")
    parser.add_argument("--top-n", type=int, default=20, help="Top N labels/tags to emit")
    parser.add_argument("--min-count", type=int, default=10, help="Minimum image count to include")
    parser.add_argument("--output", default="", help="Optional output text file")
    parser.add_argument("--config-only", action="store_true", help="Print only the backend config block")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    rows = load_rows(db_path)
    analysis = analyze(rows)

    if args.config_only:
        output_text = build_config_block(analysis, top_n=args.top_n, min_count=args.min_count)
    else:
        output_text = format_report(analysis, top_n=args.top_n, min_count=args.min_count)

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(output_text, encoding="utf-8")
        print(f"Wrote: {out_path}")
    else:
        print(output_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
