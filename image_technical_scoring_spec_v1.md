# Image Technical Scoring Script Spec v1.0

## Purpose
This script computes **technical image quality metrics** for every active image in the archive and writes the results to its own sidecar SQLite database. It does **not** decide whether an image should be culled. It does **not** modify the main archive database. Its only job is to compute deterministic, rerunnable technical signals that other systems can consume.

Primary downstream consumers:
- hero-card dynamic selection
- future auto-culling pipeline
- future representative-image selection for day, tag, place, and people views
- future quality-based filtering or sorting in the backend

## Scope
This script evaluates technical properties only, such as:
- blur / sharpness
- exposure sanity
- contrast
- resolution / pixel count
- possible compression or noise indicators
- orientation sanity where detectable

It does **not** attempt to infer:
- aesthetic appeal
- composition quality
- subject importance
- face identity
- semantic scene meaning
- captions

## Inputs
### Required input database
`archive_index.db`

### Required table
`media`

### Required columns
- `sha1`
- `rel_fqn`
- `is_deleted`

### Image file root
The script reads the actual image file from the archive root using `rel_fqn`.

## Output database
`technical_scores.sqlite`

This database is fully disposable and may be deleted and regenerated at any time.

## Output schema
### Table: `technical_scores`
- `sha1 TEXT PRIMARY KEY`
- `width INTEGER`
- `height INTEGER`
- `pixel_count INTEGER`
- `sharpness_score REAL`
- `blur_score REAL`
- `exposure_score REAL`
- `contrast_score REAL`
- `noise_score REAL NULL`
- `compression_artifact_score REAL NULL`
- `technical_score REAL`
- `warnings TEXT DEFAULT ''`
- `scorer_name TEXT`
- `scorer_version TEXT`
- `scored_at TEXT`

### Table: `technical_score_runs`
- `run_id INTEGER PRIMARY KEY AUTOINCREMENT`
- `started_at TEXT`
- `completed_at TEXT NULL`
- `scorer_version TEXT`
- `images_attempted INTEGER`
- `images_scored INTEGER`
- `images_failed INTEGER`
- `notes TEXT DEFAULT ''`

## Processing rules
1. Read active images from `archive_index.db` where `is_deleted = 0`.
2. Resolve the image path from `rel_fqn`.
3. Open the image safely.
4. Compute technical metrics.
5. Write or replace the row in `technical_scores` for that `sha1`.
6. Never modify `archive_index.db`.

## Rerun behavior
The script should support two modes:

### Incremental mode
Default behavior. Skip any image that already has a score row for the current scorer version.

### Full rebuild mode
Delete or overwrite all rows and recompute from scratch.

## Minimum technical metrics
### 1. Dimensions
- width
- height
- pixel count

### 2. Sharpness / blur
Use a classical OpenCV metric such as variance of Laplacian.

Guidance:
- store the raw sharpness measurement
- derive a normalized blur or sharpness score from it
- do not hardcode brittle thresholds into the DB layer

### 3. Exposure sanity
Use histogram-based measurements to detect severe underexposure or overexposure.

The stored score should reward images whose luminance distribution is in a usable range.

### 4. Contrast
Use grayscale histogram spread or another simple contrast metric.

### 5. Optional noise / compression artifacts
If practical, estimate image degradation caused by noise or heavy JPEG compression.
This is optional in v1 but the schema should leave room for it.

## Normalization
Each metric should be normalized to a roughly comparable 0.0 to 1.0 scale before being combined.

The final `technical_score` should also be normalized to 0.0 to 1.0.

## Initial weighting guidance
A reasonable initial combined score could use:
- sharpness / blur: 40%
- exposure: 25%
- contrast: 20%
- resolution: 15%

These weights are not sacred and should be easy to revise later.

## Failure handling
If an image cannot be opened or scored:
- do not abort the run
- record the failure in logs or run summary
- optionally write a warning string for that image if partial metrics were available
- continue processing

## Logging requirements
The script must print heartbeat progress during long runs.
Progress should include:
- images examined
- images newly scored
- images skipped because already scored
- images failed
- elapsed time
- current file path

## Performance expectations
This script should be relatively fast and CPU-friendly.
It should be suitable for full-library rescoring of 100k+ images.

## Non-goals
- no image moving
- no culling decisions
- no face detection
- no caption generation
- no backend changes
- no writes to `archive_index.db`

## Acceptance criteria
- creates `technical_scores.sqlite`
- scores all active images without modifying the main archive DB
- supports incremental reruns
- survives corrupt or unreadable files without aborting
- produces stable deterministic scores for the same input image under the same scorer version
