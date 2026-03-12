# Face Detection and Face Feature Scoring Script Spec v1.0

## Purpose
This script detects faces in active archive images and writes both summary-level face signals and per-face bounding box details into a dedicated sidecar SQLite database.

This is a **face detection** pipeline, not a face recognition pipeline. It is intended to support:
- image scoring
- future culling logic
- future people-page work
- future face clustering/recognition work
- potential UI overlays or representative face crops later

## Scope
This script is responsible for:
- detecting whether faces exist in an image
- counting faces
- capturing face bounding boxes
- estimating face prominence
- capturing simple face-quality signals if available, such as eyes-open or pose estimates

It does **not** assign names or identities to faces.

## Inputs
### Required input database
`archive_index.db`

### Required table
`media`

### Required columns
- `sha1`
- `rel_fqn`
- `is_deleted`

## Output database
`face_scores.sqlite`

This database is fully disposable and may be deleted and regenerated at any time.

## Output schema
### Table: `image_face_summary`
- `sha1 TEXT PRIMARY KEY`
- `face_count INTEGER`
- `largest_face_area_ratio REAL`
- `has_prominent_face INTEGER`
- `eyes_open_face_count INTEGER NULL`
- `primary_face_confidence REAL NULL`
- `face_interest_score REAL`
- `scorer_name TEXT`
- `model_name TEXT`
- `model_version TEXT`
- `scored_at TEXT`
- `warnings TEXT DEFAULT ''`

### Table: `image_faces`
- `sha1 TEXT NOT NULL`
- `face_index INTEGER NOT NULL`
- `x1 REAL`
- `y1 REAL`
- `x2 REAL`
- `y2 REAL`
- `bbox_area_ratio REAL`
- `confidence REAL`
- `left_eye_open_score REAL NULL`
- `right_eye_open_score REAL NULL`
- `eyes_open_score REAL NULL`
- `yaw REAL NULL`
- `pitch REAL NULL`
- `roll REAL NULL`
- PRIMARY KEY (`sha1`, `face_index`)

### Table: `face_score_runs`
- `run_id INTEGER PRIMARY KEY AUTOINCREMENT`
- `started_at TEXT`
- `completed_at TEXT NULL`
- `model_name TEXT`
- `model_version TEXT`
- `images_attempted INTEGER`
- `images_scored INTEGER`
- `images_failed INTEGER`
- `notes TEXT DEFAULT ''`

## Processing rules
1. Read active images from `archive_index.db` where `is_deleted = 0`.
2. Resolve physical image paths from `rel_fqn`.
3. Run face detection.
4. Write one summary row in `image_face_summary`.
5. Write zero or more rows in `image_faces`.
6. Never modify `archive_index.db`.

## Rerun behavior
### Incremental mode
Default behavior. Skip images already processed with the current model version.

### Full rebuild mode
Delete or overwrite all face rows and recompute.

## Candidate libraries
Suitable candidates include:
- MediaPipe
- InsightFace
- other equivalent face-detection libraries

The chosen library must expose or allow derivation of bounding box coordinates. If the library naturally provides additional useful metrics such as eye openness or pose, those should be captured.

## Required derived fields
### `face_count`
Number of detected faces.

### `largest_face_area_ratio`
Area of the largest face bounding box divided by total image area.

### `has_prominent_face`
Boolean-like flag indicating whether a face is large enough to matter visually in the frame.

### `face_interest_score`
A derived per-image face score intended for downstream use in keeper scoring. This may consider:
- number of faces
- prominence of the largest face
- whether eyes appear open
- whether the face is frontal enough to be meaningful

## Coordinate conventions
Bounding boxes must use image-space coordinates consistent with the original decoded image.
Record them as floating-point or integer pixel coordinates, but use one convention consistently.

## Failure handling
If a given image cannot be processed:
- do not abort the run
- continue processing remaining images
- log the failure in run summary

## Logging requirements
The script must emit heartbeat progress for long runs, including:
- images examined
- images scored
- images skipped
- images failed
- elapsed time
- current file path

## Non-goals
- no identity recognition
- no clustering faces across images
- no person naming
- no file moving
- no writes to `archive_index.db`

## Acceptance criteria
- creates `face_scores.sqlite`
- stores both summary face data and detailed bounding boxes
- supports incremental reruns
- survives bad images without aborting the run
- provides reusable face-derived signals for culling and hero-card selection later
