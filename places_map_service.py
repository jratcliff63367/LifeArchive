from __future__ import annotations

from typing import Any


class PlacesMapService:
    """
    First-pass local map payload builder for the Places page.

    No external APIs, tiles, or map services are required.
    """

    LEVEL_ZOOM = {
        "country": 2.2,
        "state": 3.2,
        "province": 3.2,
        "region": 3.2,
        "county": 4.4,
        "city": 8.0,
        "town": 8.5,
        "village": 9.0,
        "hamlet": 9.2,
        "suburb": 9.6,
        "road": 11.0,
        "place": 10.5,
        "coord": 10.5,
    }

    def build_map_view(self, selected_node: dict[str, Any] | None, context_title: str) -> dict[str, Any]:
        if not selected_node:
            return {
                "title": context_title or "Places",
                "subtitle": "No geotagged place selected",
                "level": "root",
                "center_lat": 20.0,
                "center_lon": 0.0,
                "zoom": 1.4,
                "marker_lat": None,
                "marker_lon": None,
            }

        lat = self._safe_float(selected_node.get("lat"))
        lon = self._safe_float(selected_node.get("lon"))
        level = str(selected_node.get("level") or "place")
        title = str(selected_node.get("label") or context_title or "Places")
        photo_count = int(selected_node.get("photo_count") or 0)
        subtitle = f"{photo_count} geotagged photo" + ("" if photo_count == 1 else "s")

        coord_text = None
        if lat is not None and lon is not None:
            coord_text = f"{lat:.4f}, {lon:.4f}"

        return {
            "title": title,
            "subtitle": subtitle,
            "level": level,
            "center_lat": lat if lat is not None else 20.0,
            "center_lon": lon if lon is not None else 0.0,
            "zoom": self._zoom_for_level(level),
            "marker_lat": lat,
            "marker_lon": lon,
            "coord_text": coord_text,
        }

    def _zoom_for_level(self, level: str) -> float:
        return float(self.LEVEL_ZOOM.get(level, 4.2))

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except Exception:
            return None
