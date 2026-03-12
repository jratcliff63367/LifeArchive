# Image Scoring Sidecar Specs Index

This bundle contains one separate design spec for each planned scoring/enrichment script:

1. `image_technical_scoring_spec_v1.md`
2. `image_aesthetic_scoring_spec_v1.md`
3. `face_detection_scoring_spec_v1.md`
4. `semantic_caption_scoring_spec_v1.md`

## Architectural note
These scripts are intentionally independent.
Each script:
- reads `archive_index.db`
- writes only to its own sidecar SQLite database
- can be deleted and rerun independently
- does not modify the main archive DB

## Dynamic backend behavior
The backend should continue to build hero cards dynamically. These sidecar DBs exist to provide reusable selection signals, not to precompute fixed hero-card results.

## Immediate priority order
Suggested implementation order:
1. technical scoring
2. aesthetic/composition scoring
3. face detection scoring
4. semantic labeling/captioning

This order reflects likely immediate value for culling and hero-card selection.
