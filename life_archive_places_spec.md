# LIFE ARCHIVE — PLACES SYSTEM TECHNICAL SPEC

## 1. Purpose

The Places System provides a hierarchical, context-aware geographic view over the existing photo dataset.

It must:
- Operate on filtered subsets of images (NOT global only)
- Build hierarchical place groupings
- Provide UI-ready payloads
- Integrate with existing clustering + ranking systems
- Remain fully decoupled from main backend logic

---

## 2. Core Design Principles

- Places is a VIEW, not a dataset
- Fully context-aware (timeline, folder, day, etc.)
- Hierarchical but derived from fuzzy GPS clustering
- Strong separation of concerns (own module)
- Safe for iteration without backend regression

---

## 3. Module Architecture

File: places_service.py

Public API:

- places_get_view(context, db_conn)
- places_get_children(context, node_id, db_conn)
- places_get_preview(context, item_id, db_conn)

Private/Internal (not exposed):

- _build_place_hierarchy(images)
- _cluster_gps_points(images)
- _resolve_place_names(cluster)
- _select_cover_image(cluster)
- _build_map_payload(cluster)

---

## 4. Context Input Model

Context is provided by backend and defines the active dataset.

Example:

{
  "scope_type": "month",
  "date_range": ["2024-03-01", "2024-03-31"],
  "folder": null,
  "tags": [],
  "filters": {
    "exclude_deleted": true
  }
}

---

## 5. Data Flow

1. Backend resolves current image set
2. Pass image IDs + context → Places module
3. Places module:
   - extracts GPS data
   - clusters into places
   - builds hierarchy
   - selects best image per place
   - returns structured payload

---

## 6. Place Hierarchy Model

Hierarchy is derived, not fixed.

Example:

World
  → Country
    → Region
      → City
        → Place Cluster

Cluster Definition:
- group of GPS points within spatial threshold
- may represent:
  - landmark
  - event
  - repeated location

---

## 7. Place Node Schema

{
  "node_id": "uuid",
  "name": "Alcatraz Island",
  "level": "place",
  "photo_count": 12,
  "cover_image_id": "abc123",
  "lat": 37.8267,
  "lon": -122.4230,
  "bounding_box": {...},
  "children": []
}

---

## 8. Cover Image Selection

Use existing ranking system:

- select best image within cluster
- MUST respect:
  - face expression priority
  - people weighting
  - technical/aesthetic fallback

---

## 9. Map Payload

{
  "pins": [
    {
      "node_id": "...",
      "lat": ...,
      "lon": ...,
      "photo_count": ...,
      "cover_image_id": "..."
    }
  ]
}

---

## 10. UI Integration

Hero card button:

[ Tags ] [ Faces ] [ 📍 Places ]

Behavior:
- Clicking Places transforms current view into geographic hierarchy
- Context is preserved (month, folder, etc.)

---

## 11. Preview API

places_get_preview(context, item_id):

Returns:

{
  "place_name": "...",
  "photo_count": ...,
  "cover_image": "...",
  "lat": ...,
  "lon": ...
}

Used for:
- hover previews
- quick navigation hints

---

## 12. Future Enhancements

- Identity-aware clustering (same people constraint)
- Semantic naming ("Waterfall hike")
- Trip detection (multi-day location clusters)
- Time × Place hybrid navigation

---

## 13. Non-Goals (for now)

- Perfect geocoding accuracy
- External API dependency
- Real-time map tile rendering optimization

---

## 14. Implementation Notes

- Must be deterministic
- Must not mutate original image dataset
- Should support caching (optional later)
- Should be testable in isolation

---

## 15. Ground Truth Rule

Places must reflect how humans remember:

- "Where was I?"
- "What happened there?"

Not:
- raw GPS coordinates
