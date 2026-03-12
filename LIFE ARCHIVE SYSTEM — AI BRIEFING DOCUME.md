We are continuing development of my "Life Archive" system.

I have a project briefing document that contains the complete architecture and current development state.

Please read and treat the following document as the authoritative system context before answering anything.

After reading it, confirm that you understand the architecture and ask what task we should continue next.

--- BEGIN PROJECT CONTEXT ---
LIFE ARCHIVE SYSTEM — AI BRIEFING DOCUMENT

Version: 1.0
Author: John Ratcliff
Purpose: Restore full development context for AI assistants.

PROJECT DESCRIPTION

The Life Archive is a personal software system for consolidating, analyzing, and curating a very large photo archive accumulated over decades (~100k+ images across multiple backups).

The system ingests photos from many source directories and builds a unified archive with a web-based browsing interface and a set of analysis pipelines.

Primary goals:

• unify scattered photo collections
• eliminate duplicates
• automatically score image quality
• cluster related images
• automatically cull redundant photos
• identify faces and people
• extract semantic information
• generate meaningful summaries of a life narrative

TECHNOLOGY STACK

Language:

Python

Backend:

Flask

Database:

SQLite

Image processing:

OpenCV
Pillow
NumPy

Future ML libraries:

Torch
MediaPipe
Transformers

Environment:

Python virtual environment (.venv)
PROJECT DIRECTORY STRUCTURE

Python scripts:

C:\Users\jratc\python-code

Contains:

archive-backend.py
ingest-photos.py
technical-image-score.py
export_archive_debug_json.py

Virtual environment:

python-code\.venv

All Python dependencies must be installed inside this environment.

ARCHIVE STORAGE

Archive root:

C:\website-test

Structure:

archive_index.db
images\
thumbnails\

Images are stored in folders referenced by relative paths in the database.

MAIN DATABASE

Database:

archive_index.db

Primary table:

media

Important fields:

sha1
rel_fqn
timestamp
gps_lat
gps_lon
width
height
tags
is_deleted

This database is the authoritative index of the archive.

INGESTION PIPELINE

Script:

ingest-photos.py

Purpose:

• scan multiple source directories
• copy images into archive
• deduplicate images
• extract EXIF metadata
• populate archive_index.db
• generate thumbnails

Example sources:

F:\Terry's Pictures for website
F:\GoogleTakeout\jratcliffscarab\Takeout\Google Photos

Destination:

C:\website-test
BACKEND WEB VIEWER

Script:

archive-backend.py

Framework:

Flask

UI pages:

Timeline
Undated
Explorer
Tags
Lightbox

Features:

• browsing images by time
• tag-based browsing
• lightbox viewer
• multi-selection
• stash and trash functionality
• hero cards

HERO CARD SYSTEM

Hero cards display a 4x4 grid of images representing a group.

Hero cards are generated dynamically, not precomputed.

Future hero selection logic will use:

• image quality scores
• diversity rules
• cluster analysis

IMAGE SCORING ARCHITECTURE

Instead of one monolithic analysis pipeline, the system uses multiple independent scripts.

Each script writes to its own sidecar SQLite database.

Advantages:

• modular design
• scripts can be rerun independently
• backend can dynamically combine signals

CURRENT SCORING SCRIPT

Script:

technical-image-score.py

Output database:

technical_scores.sqlite

Metrics computed:

sharpness
contrast
brightness
edge_density
resolution_score
technical_score

Purpose:

• detect blurry images
• detect low detail images
• detect poorly exposed images
• provide baseline quality ranking

Dependencies:

opencv-python
numpy
pillow
FUTURE SCORING PIPELINES
Face detection

Database:

face_scores.sqlite

Data stored:

face_count
face_bounding_boxes
face_area_ratio
confidence_scores
Aesthetic scoring

Database:

aesthetic_scores.sqlite

Uses ML models to evaluate:

composition
visual appeal
subject prominence
Semantic labeling

Database:

semantic_scores.sqlite

Extracts:

scene labels
objects
captions
AUTO-CULLING SYSTEM

Future script:

auto_cull.py

Purpose:

automatically remove redundant images from clusters.

Workflow:

1 cluster images by time and location
2 rank images using quality scores
3 keep best images
4 move lower-ranked images to

_cull
BACKEND DEBUGGING PLANS

The lightbox viewer will eventually display analysis metadata for debugging.

Example overlay:

Sharpness
Contrast
Edge density
Technical score
Face count

This allows visual validation of scoring algorithms.

DEVELOPMENT PRINCIPLES

Important design constraints:

1 scripts should remain independent
2 analysis data stored in sidecar databases
3 backend should compute views dynamically
4 archive database should remain authoritative
5 analysis pipelines must be rerunnable

CURRENT DEVELOPMENT STATUS

The ingestion pipeline has successfully processed a very large dataset of historical photos.

The technical scoring script is currently running to generate baseline image quality metrics.

Next steps:

1 inspect generated scoring database
2 export data for debugging if necessary
3 integrate debug metadata into lightbox
4 build additional scoring pipelines

LONG TERM VISION

The Life Archive system will ultimately support:

• face recognition
• event detection
• timeline reconstruction
• location-based browsing
• narrative summaries of life events

The goal is to transform decades of scattered digital photos into a coherent hierarchical life archive.

END OF BRIEFING FILE

--- END PROJECT CONTEXT ---
