LIFE ARCHIVE PROJECT CONTEXT

Version: 1.0
Purpose: Restore full context for the Life Archive system in any new ChatGPT conversation.

PROJECT OVERVIEW

The Life Archive is a personal software system designed to ingest, organize, analyze, and curate a very large personal photo collection (~100k+ images) accumulated over decades across many backups.

The system consolidates these images into a single archive and provides tooling for:

• browsing
• tagging
• clustering
• scoring
• automatic culling
• face detection
• narrative life reconstruction

The project is implemented primarily in Python with SQLite databases and a local Flask web UI.

The system is designed to be modular, with multiple independent scripts producing sidecar databases that feed the backend viewer.

PRIMARY DESIGN PRINCIPLES

Scripts should be independent

Each analysis pipeline writes its own sidecar SQLite database

The backend viewer dynamically combines data from these databases

No script should depend heavily on another script

Scripts should be rerunnable and idempotent

Databases should be rebuildable without destroying the archive

MAIN SYSTEM COMPONENTS

The system currently consists of:

1. Photo ingestion pipeline

Script:

ingest-photos.py

Purpose:

• scan multiple source directories
• copy images into unified archive
• detect duplicates
• extract EXIF metadata
• build main archive index
• generate thumbnails

Source directories example:

SOURCES = [
F:\Terry's Pictures for website
F:\GoogleTakeout\jratcliffscarab\Takeout\Google Photos
]

Destination archive:

C:\website-test
2. Main archive database
archive_index.db

Primary table:

media

Key fields include:

• sha1
• rel_fqn (relative file path)
• timestamp
• gps coordinates
• width
• height
• tags
• deleted flag

This database represents the authoritative index of the archive.

3. Backend web viewer

Script:

archive-backend.py

Framework:

Flask

Provides UI views:

Timeline
Undated
Explorer
Tags
Lightbox

Features include:

• browsing images by time
• tag-based browsing
• lightbox viewer
• multi-selection
• stash and trash functionality
• hero cards

FILE STRUCTURE

Project code directory:

C:\Users\jratc\python-code

Contains scripts such as:

archive-backend.py
ingest-photos.py
technical-image-score.py
export_archive_debug_json.py

Python virtual environment:

C:\Users\jratc\python-code\.venv

All dependencies must be installed in this venv.

ARCHIVE DIRECTORY STRUCTURE
C:\website-test
    archive_index.db
    thumbnails\
    images\

Images are referenced by relative paths stored in the database.

CURRENT DEVELOPMENT STAGE

The ingestion pipeline is working and has successfully processed a massive dataset of legacy photos.

The current focus is building image analysis pipelines that generate metadata useful for:

• automatic culling
• hero card selection
• representative thumbnails
• clustering
• face detection
• semantic labeling

IMAGE SCORING ARCHITECTURE

Instead of a single scoring script, the system will use multiple specialized scoring pipelines.

Each pipeline writes to its own SQLite sidecar database.

Examples:

technical_scores.sqlite
face_scores.sqlite
aesthetic_scores.sqlite
semantic_scores.sqlite

These are later combined dynamically by the backend.

TECHNICAL IMAGE SCORING

Current script:

technical-image-score.py

Output database:

technical_scores.sqlite

Fields produced:

• width
• height
• sharpness
• contrast
• brightness
• edge_density
• resolution_score
• technical_score

This script uses OpenCV to compute basic image quality metrics.

Purpose:

• detect blurry images
• detect low detail images
• detect poor exposure
• provide baseline scoring for culling

Dependencies:

opencv-python
numpy
pillow

Installed in:

python-code\.venv
FUTURE SCORING PIPELINES

Additional analysis pipelines planned:

FACE DETECTION

Database:

face_scores.sqlite

Data stored:

• face count
• bounding boxes
• face size ratio
• confidence scores

This data will support:

• face clustering
• portrait detection
• family photo prioritization

AESTHETIC SCORING

Database:

aesthetic_scores.sqlite

Uses ML models to estimate:

• composition quality
• subject prominence
• aesthetic appeal

SEMANTIC LABELING

Database:

semantic_scores.sqlite

Contains:

• scene labels
• object detection
• short captions

HERO CARD SYSTEM

Hero cards are used by the UI to represent groups of photos.

Each card displays 16 images in a 4x4 grid.

Hero cards are generated dynamically, not precomputed.

Selection logic uses:

• image scores
• diversity
• cluster filtering

AUTO CULLING PIPELINE

Future script:

auto_cull.py

Purpose:

Automatically identify redundant images in clusters of temporally and geographically similar photos.

Approach:

cluster images by location + timestamp

rank images by quality score

retain top images

move lower-ranked images to:

_cull
BACKEND DEBUGGING FEATURES

The lightbox viewer will eventually display debugging metadata such as:

Sharpness
Contrast
Edge density
Technical score
Face count

This allows visual inspection of scoring accuracy.

DEVELOPMENT WORKFLOW

Typical workflow:

ingest images

build archive_index.db

generate thumbnails

run scoring scripts

run backend viewer

inspect results

tune scoring algorithms

CURRENT TASK

The technical scoring script is currently running over the entire archive.

Expected runtime:

10–30 minutes depending on CPU

Next step after completion:

• inspect generated database
• possibly export to JSON for debugging
• integrate debug data into lightbox UI

LONG TERM GOALS

The Life Archive will eventually support:

• face recognition and clustering
• narrative timeline generation
• life event detection
• location-based map browsing
• automatic story generation

The ultimate goal is a hierarchical life narrative interface that synthesizes decades of personal data.

IMPORTANT NOTES FOR CHATGPT

When continuing development:

assume all scripts live in C:\Users\jratc\python-code

assume archive root is C:\website-test

assume .venv virtual environment is used

assume SQLite databases are the main data layer

prefer independent scripts over tightly coupled systems

avoid changes that break existing ingestion or backend workflows

END CONTEXT FILE

Paste this file into any new conversation to restore full project context.