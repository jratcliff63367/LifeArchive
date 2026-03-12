# Image Aesthetic and Composition Scoring Script Spec v1.0

## Purpose
This script computes **aesthetic and composition-oriented scores** for active archive images and writes them to a dedicated sidecar SQLite database.

This script is intended to capture the more subjective notion of whether an image feels like a keeper. It should complement the purely technical scoring script, not replace it.

Primary downstream consumers:
- hero-card dynamic selection
- representative image selection for day, month, tag, place, and future people views
- future auto-culling pipeline

## Scope
This script may use heavier ML models than the technical scoring script.
It is responsible for estimating:
- aesthetic appeal
- composition quality
- subject salience / prominence
- image interestingness

It does **not** decide final culling actions.
It does **not** do face identity recognition.

## Inputs
### Required input database
`archive_index.db`

### Required table
`media`

### Required columns
- `sha1`
- `rel_fqn`
- `is_deleted`

### Optional supporting databases
This script should not require any other sidecar DB in v1.
It should be independently runnable.

## Output database
`aesthetic_scores.sqlite`

This database is fully disposable and may be deleted and regenerated at any time.

## Output schema
### Table: `aesthetic_scores`
- `sha1 TEXT PRIMARY KEY`
- `aesthetic_score REAL`
- `composition_score REAL`
- `subject_prominence_score REAL`
- `interest_score REAL`
- `saliency_score REAL NULL`
- `overall_aesthetic_score REAL`
- `scorer_name TEXT`
- `model_name TEXT`
- `model_version TEXT`
- `scored_at TEXT`
- `warnings TEXT DEFAULT ''`

### Table: `aesthetic_score_runs`
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
2. Resolve the physical image path from `rel_fqn`.
3. Open the image safely.
4. Run the aesthetic/composition model pipeline.
5. Write or replace the row in `aesthetic_scores` for that `sha1`.
6. Never modify the main archive DB.

## Rerun behavior
### Incremental mode
Default behavior. Skip images already scored with the current model version.

### Full rebuild mode
Delete or overwrite all scores and recompute.

## Candidate implementation approaches
The script may use one or more of the following:
- CLIP-based aesthetic predictor
- NIMA-like image aesthetic model
- saliency-based composition heuristics
- lightweight object-detection-derived subject prominence estimates

The implementation must record the model name and version used.

## Minimum output meanings
### `aesthetic_score`
A general measure of how visually appealing the image appears.

### `composition_score`
A measure of framing, balance, or arrangement quality.

### `subject_prominence_score`
A measure of whether the image appears to contain a clear primary subject rather than diffuse clutter.

### `interest_score`
A rough measure of whether the image looks meaningful, memorable, or keeper-like. This may be partly model-derived and partly heuristic.

### `overall_aesthetic_score`
Weighted combination of the above scores, normalized to 0.0 to 1.0.

## Initial weighting guidance
A reasonable initial combined score could be:
- aesthetic_score: 35%
- composition_score: 30%
- subject_prominence_score: 20%
- interest_score: 15%

These weights must be easy to revise in code later.

## Failure handling
If a model call fails or an image cannot be scored:
- do not abort the run
- log the failure
- continue processing

## Logging requirements
The script must emit heartbeat progress for long runs, including:
- images examined
- images scored
- images skipped
- images failed
- elapsed time
- current file path

## Performance considerations
This script may be significantly slower than the technical scorer.
It should still support incremental reruns cleanly so repeated experimentation is practical.

## Non-goals
- no file moving
- no culling action
- no face identity recognition
- no writes to `archive_index.db`
- no hero card precomputation

## Acceptance criteria
- creates `aesthetic_scores.sqlite`
- scores active images without modifying the archive DB
- stores model metadata and versioning
- supports incremental reruns
- produces reusable dynamic selection signals for backend and future culling
