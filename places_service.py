from __future__ import annotations

import html
import sqlite3
from urllib.parse import quote
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class PlacesContext:
    scope_type: str
    title: str
    breadcrumb: str
    scope_url: str


@dataclass
class PlaceNode:
    node_id: str
    level: str
    label: str
    parent_id: str | None
    photo_count: int = 0
    lat_sum: float = 0.0
    lon_sum: float = 0.0
    lat: float | None = None
    lon: float | None = None
    cover_sha1: str | None = None
    children: list[str] | None = None
    item_refs: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = []
        if self.item_refs is None:
            self.item_refs = []


class PlacesService:
    def __init__(self, archive_root: Path, geo_db_path: Path, chooser: Callable[[list[dict[str, Any]]], dict[str, Any] | None]) -> None:
        self.archive_root = Path(archive_root)
        self.geo_db_path = Path(geo_db_path)
        self.choose_best_item = chooser

    def places_get_view(
        self,
        context: PlacesContext,
        items: list[dict[str, Any]],
        selected_node_id: str | None = None,
        gallery_limit: int = 18,
    ) -> dict[str, Any]:
        geo_records = self._load_geo_records(items)
        payload = self._build_place_hierarchy(geo_records)
        nodes: dict[str, PlaceNode] = payload["nodes"]
        root_id = payload["root_id"]
        if not nodes:
            return {
                "context": context,
                "selected_node_id": root_id,
                "selected_node": None,
                "sidebar_html": "<div class='places-empty'>No geotagged photos exist in this scope yet.</div>",
                "gallery_items": [],
                "selected_path": [],
                "stats": {"geotagged_count": 0, "leaf_count": 0},
                "leaf_cards": [],
            }

        resolved_selected = selected_node_id if selected_node_id in nodes else self._default_selected_node(nodes, root_id)
        selected_node = nodes[resolved_selected]
        gallery_items = sorted(selected_node.item_refs or [], key=self._hero_sort_key, reverse=True)[:gallery_limit]
        leaf_cards = self._build_leaf_cards(nodes, selected_node)
        return {
            "context": context,
            "selected_node_id": resolved_selected,
            "selected_node": self._serialize_node(selected_node),
            "sidebar_html": self._render_sidebar_html(nodes, root_id, resolved_selected),
            "gallery_items": gallery_items,
            "selected_path": [self._serialize_node(nodes[nid]) for nid in self._path_to_root(nodes, resolved_selected)],
            "stats": {
                "geotagged_count": payload["geotagged_count"],
                "leaf_count": payload["leaf_count"],
            },
            "leaf_cards": leaf_cards,
        }

    def places_get_preview(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        geo_records = self._load_geo_records(items)
        if not geo_records:
            return {"place_name": "No GPS data", "photo_count": 0, "cover_image": None, "lat": None, "lon": None}
        payload = self._build_place_hierarchy(geo_records)
        nodes: dict[str, PlaceNode] = payload["nodes"]
        selected_id = self._default_selected_node(nodes, payload["root_id"])
        node = nodes[selected_id]
        return {
            "place_name": node.label,
            "photo_count": node.photo_count,
            "cover_image": node.cover_sha1,
            "lat": node.lat,
            "lon": node.lon,
        }

    def _hero_sort_key(self, item: dict[str, Any]) -> tuple[float, str]:
        hero = float(item.get("_hero_score") or 0.0)
        return (hero, str(item.get("final_dt") or ""))

    def _load_geo_records(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not items:
            return []
        item_by_sha1 = {str(item.get("sha1")): item for item in items if item.get("sha1")}
        if not item_by_sha1:
            return []
        records: list[dict[str, Any]] = []
        if self.geo_db_path.exists():
            with sqlite3.connect(self.geo_db_path) as conn:
                conn.row_factory = sqlite3.Row
                sha1s = list(item_by_sha1.keys())
                for start in range(0, len(sha1s), 800):
                    chunk = sha1s[start:start+800]
                    marks = ",".join("?" for _ in chunk)
                    sql = f"""
                    SELECT pg.sha1, pg.coord_key, pg.source_lat, pg.source_lon,
                           gc.country, gc.state, gc.county, gc.city, gc.town, gc.village,
                           gc.hamlet, gc.suburb, gc.place_name, gc.formatted, gc.road,
                           gc.lat_rounded, gc.lon_rounded
                    FROM photo_geo pg
                    JOIN geo_cache gc ON gc.coord_key = pg.coord_key
                    WHERE pg.sha1 IN ({marks})
                    """
                    for row in conn.execute(sql, chunk).fetchall():
                        rec = dict(row)
                        base = item_by_sha1.get(str(rec["sha1"]))
                        if not base:
                            continue
                        rec["item"] = base
                        records.append(rec)
        seen = {str(r["sha1"]) for r in records}
        for sha1, item in item_by_sha1.items():
            if sha1 in seen:
                continue
            try:
                lat = float(item.get("latitude"))
                lon = float(item.get("longitude"))
                if abs(lat) <= 0.000001 and abs(lon) <= 0.000001:
                    continue
            except Exception:
                continue
            records.append({
                "sha1": sha1,
                "coord_key": f"{lat:.3f},{lon:.3f}",
                "source_lat": lat,
                "source_lon": lon,
                "country": "Unknown Country",
                "state": "Unknown Region",
                "county": "",
                "city": "",
                "town": "",
                "village": "",
                "hamlet": "",
                "suburb": "",
                "place_name": f"{lat:.3f}, {lon:.3f}",
                "formatted": f"{lat:.3f}, {lon:.3f}",
                "road": "",
                "lat_rounded": lat,
                "lon_rounded": lon,
                "item": item,
            })
        for rec in records:
            rec["country_label"] = self._clean_label(rec.get("country"), "Unknown Country")
            rec["region_label"] = self._clean_label(rec.get("state") or rec.get("county"), "Unknown Region")
            rec["city_label"] = self._clean_label(
                rec.get("city") or rec.get("town") or rec.get("village") or rec.get("hamlet") or rec.get("suburb") or rec.get("county") or rec.get("state"),
                "Miscellaneous",
            )
            rec["place_label"] = self._clean_label(rec.get("place_name") or rec.get("road") or rec.get("formatted"), "Pinned Place")
        return records

    def _build_place_hierarchy(self, geo_records: list[dict[str, Any]]) -> dict[str, Any]:
        nodes: dict[str, PlaceNode] = {}
        root_id = "root"
        nodes[root_id] = PlaceNode(root_id, "root", "World", None)

        def ensure(node_id: str, level: str, label: str, parent_id: str) -> PlaceNode:
            node = nodes.get(node_id)
            if node is None:
                node = PlaceNode(node_id=node_id, level=level, label=label, parent_id=parent_id)
                nodes[node_id] = node
                if node_id not in nodes[parent_id].children:
                    nodes[parent_id].children.append(node_id)
            elif node_id not in nodes[parent_id].children:
                nodes[parent_id].children.append(node_id)
            return node

        leaf_count = 0
        for rec in geo_records:
            country_id = f"country::{rec['country_label']}"
            region_id = f"region::{rec['country_label']}::{rec['region_label']}"
            city_id = f"city::{rec['country_label']}::{rec['region_label']}::{rec['city_label']}"
            place_id = f"place::{rec['coord_key']}"

            ensure(country_id, "country", rec["country_label"], root_id)
            ensure(region_id, "region", rec["region_label"], country_id)
            ensure(city_id, "city", rec["city_label"], region_id)
            place_node = ensure(place_id, "place", rec["place_label"], city_id)
            place_node.item_refs.append(rec["item"])
            leaf_count += 1

            lat = self._safe_float(rec.get("source_lat") or rec.get("lat_rounded"))
            lon = self._safe_float(rec.get("source_lon") or rec.get("lon_rounded"))
            for nid in [root_id, country_id, region_id, city_id, place_id]:
                node = nodes[nid]
                node.photo_count += 1
                node.lat_sum += lat
                node.lon_sum += lon
                node.item_refs.append(rec["item"]) if nid != place_id else None

        for node in nodes.values():
            if node.photo_count > 0:
                node.lat = node.lat_sum / node.photo_count
                node.lon = node.lon_sum / node.photo_count
                choice = self.choose_best_item(node.item_refs or [])
                node.cover_sha1 = str(choice.get("sha1")) if choice else None
            node.item_refs = self._dedupe_items(node.item_refs or [])
            node.children.sort(key=lambda child_id: (-nodes[child_id].photo_count, nodes[child_id].label.lower()))

        return {
            "nodes": nodes,
            "root_id": root_id,
            "geotagged_count": len(geo_records),
            "leaf_count": len([n for n in nodes.values() if n.level == "place"]),
        }

    def _dedupe_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            sha1 = str(item.get("sha1") or "")
            if not sha1 or sha1 in seen:
                continue
            seen.add(sha1)
            out.append(item)
        return out

    def _render_sidebar_html(self, nodes: dict[str, PlaceNode], root_id: str, selected_node_id: str) -> str:
        root = nodes[root_id]
        bits = ["<div class='places-tree'>"]
        for child_id in root.children:
            bits.append(self._render_node(nodes, child_id, selected_node_id, depth=0))
        bits.append("</div>")
        return "".join(bits)

    def _render_node(self, nodes: dict[str, PlaceNode], node_id: str, selected_node_id: str, depth: int) -> str:
        node = nodes[node_id]
        selected = self._path_set(nodes, selected_node_id)
        is_active = node_id == selected_node_id
        is_open = node_id in selected or depth < 1
        icon = {
            "country": "🌍",
            "region": "🗺️",
            "city": "🏙️",
            "place": "📍",
        }.get(node.level, "•")
        cls = ["places-node", f"level-{html.escape(node.level)}"]
        if is_active:
            cls.append("active")
        if is_open:
            cls.append("open")
        href = f"?node={quote(node_id, safe='')}"
        html_bits = [
            f"<div class='{' '.join(cls)}' style='--depth:{depth};'>",
            f"<a class='places-node-link' href='{href}'>",
            f"<span class='places-node-icon'>{icon}</span>",
            f"<span class='places-node-label'>{html.escape(node.label)}</span>",
            f"<span class='places-node-count'>({node.photo_count})</span>",
            "</a>",
        ]
        if node.children and is_open:
            html_bits.append("<div class='places-node-children'>")
            for child_id in node.children:
                html_bits.append(self._render_node(nodes, child_id, selected_node_id, depth + 1))
            html_bits.append("</div>")
        html_bits.append("</div>")
        return "".join(html_bits)

    def _path_to_root(self, nodes: dict[str, PlaceNode], node_id: str) -> list[str]:
        path: list[str] = []
        cur = node_id
        while cur and cur in nodes:
            if cur != "root":
                path.append(cur)
            cur = nodes[cur].parent_id or ""
        return list(reversed(path))

    def _path_set(self, nodes: dict[str, PlaceNode], node_id: str) -> set[str]:
        return set(self._path_to_root(nodes, node_id) + ([node_id] if node_id else []))

    def _default_selected_node(self, nodes: dict[str, PlaceNode], root_id: str) -> str:
        cur = root_id
        while nodes[cur].children:
            cur = nodes[cur].children[0]
        return cur

    def _build_leaf_cards(self, nodes: dict[str, PlaceNode], selected_node: PlaceNode) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for child_id in selected_node.children[:12]:
            child = nodes[child_id]
            cards.append(self._serialize_node(child))
        return cards

    def _serialize_node(self, node: PlaceNode) -> dict[str, Any]:
        return {
            "node_id": node.node_id,
            "node_q": quote(node.node_id, safe=''),
            "level": node.level,
            "label": node.label,
            "photo_count": node.photo_count,
            "cover_sha1": node.cover_sha1,
            "lat": node.lat,
            "lon": node.lon,
            "child_count": len(node.children),
        }

    @staticmethod
    def _clean_label(value: Any, fallback: str) -> str:
        text = str(value or "").strip()
        if not text:
            return fallback
        return text

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default
