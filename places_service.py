from __future__ import annotations

import html
import sqlite3
from urllib.parse import quote, urlencode
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
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
        gallery_items = self._build_gallery_items(selected_node.item_refs or [], gallery_limit=gallery_limit, selected_node=selected_node, context=context)
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

    def _build_gallery_items(self, items: list[dict[str, Any]], gallery_limit: int = 18, selected_node: PlaceNode | None = None, context: PlacesContext | None = None) -> list[dict[str, Any]]:
        deduped = self._dedupe_items(items)
        if not deduped:
            return []
        clusters = self._cluster_items_by_time(deduped, threshold_seconds=20)
        gallery: list[dict[str, Any]] = []
        for cluster in clusters:
            representative = self.choose_best_item(cluster) or sorted(cluster, key=self._hero_sort_key, reverse=True)[0]
            dt = self._parse_dt(representative.get('final_dt'))
            rep = dict(representative)
            title = (f"{dt.strftime('%B')} {dt.day}, {dt.year}" if dt else str(representative.get('_month_name') or 'Photo'))
            if dt is None and representative.get('_year'):
                title = f"{representative.get('_month_name') or 'Photo'} {representative.get('_year')}"
            subtitle = f"{len(cluster)} photo" + ('' if len(cluster) == 1 else 's')
            rep['_places_title'] = title
            rep['_places_subtitle'] = subtitle
            rep['_places_cluster_size'] = len(cluster)
            rep['_places_href'] = self._build_bucket_href(cluster, representative, title, selected_node=selected_node, context=context)
            gallery.append(rep)
        gallery.sort(key=self._hero_sort_key, reverse=True)
        return gallery[:gallery_limit]

    def _build_bucket_href(self, cluster: list[dict[str, Any]], representative: dict[str, Any], label: str, selected_node: PlaceNode | None = None, context: PlacesContext | None = None) -> str:
        sha1s = [str(item.get('sha1') or '').strip() for item in cluster if str(item.get('sha1') or '').strip()]
        if not sha1s:
            return '#'
        selected_sha1 = str(representative.get('sha1') or sha1s[0])
        params = {
            'ids': ','.join(sha1s),
            'sha1': selected_sha1,
            'label': label,
            'place': (selected_node.label if selected_node else 'Places'),
            'back': (f"{context.scope_url}?node={quote(selected_node.node_id, safe='')}" if context and selected_node else '/places'),
        }
        return '/places_lightbox?' + urlencode(params)

    def _parse_dt(self, value: Any) -> datetime | None:
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        for parser in (datetime.fromisoformat,):
            try:
                return parser(text.replace('Z', '+00:00'))
            except Exception:
                pass
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y:%m:%d %H:%M:%S'):
            try:
                return datetime.strptime(text, fmt)
            except Exception:
                pass
        return None

    def _cluster_items_by_time(self, items: list[dict[str, Any]], threshold_seconds: int = 20) -> list[list[dict[str, Any]]]:
        stamped: list[tuple[datetime | None, dict[str, Any]]] = [(self._parse_dt(item.get('final_dt')), item) for item in items]
        with_dt = [(dt, item) for dt, item in stamped if dt is not None]
        without_dt = [item for dt, item in stamped if dt is None]
        with_dt.sort(key=lambda pair: pair[0])
        clusters: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        prev_dt: datetime | None = None
        for dt, item in with_dt:
            if not current:
                current = [item]
                prev_dt = dt
                continue
            if prev_dt is not None and (dt - prev_dt).total_seconds() <= threshold_seconds:
                current.append(item)
            else:
                clusters.append(current)
                current = [item]
            prev_dt = dt
        if current:
            clusters.append(current)
        for item in without_dt:
            clusters.append([item])
        return clusters

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

        self._consolidate_leaf_siblings(nodes, root_id)

        for node in nodes.values():
            if node.photo_count > 0:
                node.lat = node.lat_sum / node.photo_count
                node.lon = node.lon_sum / node.photo_count
                choice = self.choose_best_item(node.item_refs or [])
                node.cover_sha1 = str(choice.get("sha1")) if choice else None
            node.item_refs = self._dedupe_items(node.item_refs or [])
            node.children.sort(key=lambda child_id: (-nodes[child_id].photo_count, nodes[child_id].label.lower()))

        visible_ids = self._reachable_node_ids(nodes, root_id)

        return {
            "nodes": nodes,
            "root_id": root_id,
            "geotagged_count": len(geo_records),
            "leaf_count": len([nid for nid in visible_ids if nodes[nid].level == "place"]),
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


    def _consolidate_leaf_siblings(self, nodes: dict[str, PlaceNode], root_id: str) -> None:
        reachable = self._reachable_node_ids(nodes, root_id)
        for parent_id in list(reachable):
            parent = nodes.get(parent_id)
            if not parent or not parent.children:
                continue

            leaf_ids = [child_id for child_id in parent.children if self._is_leaf(nodes, child_id)]
            if len(leaf_ids) < 2:
                continue

            grouped: dict[str, list[str]] = defaultdict(list)
            for child_id in leaf_ids:
                child = nodes[child_id]
                grouped[self._normalize_place_label(child.label)].append(child_id)

            if not any(len(group) > 1 for group in grouped.values()):
                continue

            new_children: list[str] = []
            seen_leaf_ids: set[str] = set()
            for child_id in parent.children:
                if child_id in seen_leaf_ids:
                    continue

                child = nodes[child_id]
                if not self._is_leaf(nodes, child_id):
                    new_children.append(child_id)
                    continue

                group = grouped.get(self._normalize_place_label(child.label), [child_id])
                if len(group) == 1:
                    new_children.append(child_id)
                    seen_leaf_ids.add(child_id)
                    continue

                canonical_id = max(group, key=lambda nid: (nodes[nid].photo_count, len(nodes[nid].item_refs or []), nid))
                canonical = nodes[canonical_id]

                merged_items: list[dict[str, Any]] = []
                merged_count = 0
                merged_lat_sum = 0.0
                merged_lon_sum = 0.0
                for gid in group:
                    gnode = nodes[gid]
                    merged_items.extend(gnode.item_refs or [])
                    merged_count += gnode.photo_count
                    merged_lat_sum += gnode.lat_sum
                    merged_lon_sum += gnode.lon_sum

                canonical.item_refs = self._dedupe_items(merged_items)
                canonical.photo_count = len(canonical.item_refs)
                if canonical.photo_count <= 0 and merged_count > 0:
                    canonical.photo_count = merged_count
                canonical.lat_sum = merged_lat_sum
                canonical.lon_sum = merged_lon_sum
                canonical.lat = canonical.lat_sum / canonical.photo_count if canonical.photo_count > 0 else None
                canonical.lon = canonical.lon_sum / canonical.photo_count if canonical.photo_count > 0 else None
                choice = self.choose_best_item(canonical.item_refs or [])
                canonical.cover_sha1 = str(choice.get("sha1")) if choice else None

                for gid in group:
                    seen_leaf_ids.add(gid)

                new_children.append(canonical_id)

            parent.children = new_children

    def _reachable_node_ids(self, nodes: dict[str, PlaceNode], root_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [root_id]
        while stack:
            node_id = stack.pop()
            if node_id in seen or node_id not in nodes:
                continue
            seen.add(node_id)
            stack.extend(nodes[node_id].children or [])
        return seen

    def _is_leaf(self, nodes: dict[str, PlaceNode], node_id: str) -> bool:
        node = nodes.get(node_id)
        return bool(node) and not (node.children or [])

    @staticmethod
    def _normalize_place_label(label: str) -> str:
        return " ".join(str(label or "").strip().lower().split())

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
