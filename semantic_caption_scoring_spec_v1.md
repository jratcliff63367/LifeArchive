# Semantic Labeling and Caption Script Spec v1.0

## Purpose
This script generates lightweight semantic labels and optional captions for active archive images and writes them to a dedicated sidecar SQLite database.

This script is lower priority than technical, aesthetic, and face detection scoring, but it is useful for:
- future image description features
- future search and filtering
- later narrative assembly
- possible keeper-interest tie-breakers

## Scope
This script may generate:
- subject labels
- scene labels
- short captions
- optional caption confidence or model confidence

It does **not** decide culling actions and does **not** write to the main archive DB.

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
`semantic_scores.sqlite`

This database is fully disposable and may be deleted and regenerated at any time.

## Output schema
### Table: `semantic_scores`
- `sha1 TEXT PRIMARY KEY`
- `scene_labels TEXT DEFAULT ''`
- `subject_labels TEXT DEFAULT ''`
- `short_caption TEXT DEFAULT ''`
- `semantic_interest_score REAL NULL`
- `scorer_name TEXT`
- `model_name TEXT`
- `model_version TEXT`
- `scored_at TEXT`
- `warnings TEXT DEFAULT ''`

### Table: `semantic_score_runs`
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
3. Run a semantic model or labeler.
4. Write the resulting labels/caption into `semantic_scores`.
5. Never modify the main archive DB.

## Rerun behavior
### Incremental mode
Default behavior. Skip images already scored with the current model version.

### Full rebuild mode
Delete or overwrite all rows and recompute.

## Candidate implementations
Examples include:
- BLIP / BLIP2-style captioning
- object or scene classification models
- vision-language labeling models

The script must record the model name and version used.

## Data formatting
### `scene_labels`
Store as a delimiter-separated string or JSON array encoded as text.

### `subject_labels`
Store as a delimiter-separated string or JSON array encoded as text.

### `short_caption`
Single short sentence or phrase. Keep it compact.

### `semantic_interest_score`
Optional heuristic value indicating whether the image appears semantically meaningful rather than generic.

## Failure handling
If an image cannot be processed or a model call fails:
- do not abort the run
- continue processing the rest of the library
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
- no culling decisions
- no writes to `archive_index.db`
- no file movement
- no face identity recognition

## Acceptance criteria
- creates `semantic_scores.sqlite`
- stores captions and/or semantic labels independently of the main archive DB
- supports incremental reruns
- can later support backend description features without schema changes to the main archive DB
