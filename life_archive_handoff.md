# Life Archive Project -- Full Context Handoff

## Overview

This project is a **local-first photo archive system** designed to
organize, browse, and analyze a very large personal image collection
(\~100,000+ images, expanding toward full life archive scale).

The system is NOT just a photo browser. It is intended to evolve into a
**hierarchical life archive**, eventually incorporating: - Photos -
Location history (Google Takeout) - Calendar data - Fitness data - Other
personal datasets

Primary goal: \> Build a fast, intuitive, timeline + location +
tag-based exploration system for an entire life.

------------------------------------------------------------------------

## Current Architecture

### Backend

Main backend script: - `life_archive_backend_baseline.py`

Supporting services: - `places_service.py` - `places_map_service.py`

### Key Features

1.  Timeline Navigation
    -   Decade → Year → Month → Day
    -   Calendar-based browsing
2.  Places System
    -   Photos grouped by geographic location
    -   Derived from EXIF + geotag enrichment
3.  Tag System
    -   Derived from folder names
    -   Context-sensitive filtering
4.  Composite Tile System (CRITICAL)
    -   Each node renders a **4x4 (16 image) composite**
    -   Server-generated image
    -   Prevents UI overload and improves performance
5.  Lightbox
    -   Displays selected images
    -   Supports rotation and deletion marking

------------------------------------------------------------------------

## Data Pipeline

### Ingestion Script (previously developed)

Responsibilities: 1. Copy images from multiple sources 2. Deduplicate by
content 3. Preserve folder hierarchy 4. Extract EXIF metadata 5. Store
metadata in SQLite

### Source Strategy

-   Multiple input sources (Google Takeout, backups, etc.)
-   First source = canonical
-   Later sources = fill gaps only

------------------------------------------------------------------------

## Current State (IMPORTANT)

-   \~100,000 images loaded
-   Google Takeout fully processed
-   Geotagging complete using OpenCage
-   System is FUNCTIONALLY WORKING

------------------------------------------------------------------------

## Major Problem

### Performance Bottleneck

When clicking a **Place with large number of photos**: - UI freezes -
Delay measured in **minutes**

This is the current critical issue.

------------------------------------------------------------------------

## Root Cause Hypothesis

The system is doing **too much computation at query time**, likely
including:

-   Large SQL queries
-   Sorting / grouping in Python
-   Composite tile generation
-   Possibly repeated computations that never change

Key insight from user:

> "There is no operation that changes the hierarchy."

Meaning: - Folder structure is static - Place hierarchy is static after
geotagging - Timeline hierarchy is static

Therefore: \> Everything expensive should be **precomputed and cached**

------------------------------------------------------------------------

## Planned Optimization Strategy

### Phase 1: Instrumentation (CURRENT TASK)

Goal: \> Identify EXACT bottlenecks using aggressive logging

Approach: - Add timing logs to every major phase - Print timestamps and
durations - Make delays obvious

#### Logging Targets

Inside place click handling:

-   DB query time
-   Result processing time
-   Tile selection time
-   Image loading time
-   Composite generation time

Example logging style:

    [PLACE LOAD] Start
    [DB QUERY] 2.4s
    [GROUPING] 15.2s
    [SELECT 16 IMAGES] 0.8s
    [COMPOSITE BUILD] 42.6s
    [TOTAL] 61.3s

User explicitly requested: \> "Print an obnoxious amount of information"

------------------------------------------------------------------------

## Phase 2: Precomputation

Once bottleneck identified, move to:

### Precompute Everything Static

Candidates:

#### 1. Place Buckets

-   Precompute:
    -   place → list of image IDs
-   Store in DB or cache file

#### 2. Timeline Buckets

-   year/month/day → image IDs

#### 3. Tag Buckets

-   tag → image IDs

#### 4. Composite Tiles (VERY IMPORTANT)

-   Pre-generate 4x4 tiles
-   Store as images on disk
-   Only rebuild when data changes

#### 5. "Best 16 Images" Selection

-   Precompute representative images per node
-   Possibly using:
    -   random sampling
    -   timestamp spread
    -   GPS clustering (future)

------------------------------------------------------------------------

## External Tools Integrated

### 1. Duplicate Photo Cleaner

-   Used outside pipeline
-   Removes near-duplicates

### 2. Topaz AI

-   Used for:
    -   Upscaling
    -   Enhancing images

These are **manual preprocessing tools**, not part of backend.

------------------------------------------------------------------------

## Geotagging System

-   Uses OpenCage API (paid tier)
-   Current rate: 15 requests/sec
-   Already processed full dataset

### Known Edge Case

"Unknown Country"

Planned solution: - Add water-body resolution using Natural Earth
dataset - Secondary lookup only when country missing

------------------------------------------------------------------------

## Storage

-   SQLite database
-   Stores:
    -   file paths
    -   timestamps
    -   GPS data
    -   tags

------------------------------------------------------------------------

## Design Philosophy

### 1. Local-first

-   No cloud dependency
-   All processing local

### 2. Deterministic

-   Same input → same structure
-   Avoid runtime randomness

### 3. Precompute over compute-on-demand

-   Shift cost to ingestion phase
-   Make UI instant

### 4. Visual-first UX

-   Composite tiles instead of raw grids
-   Reduce cognitive load

------------------------------------------------------------------------

## Known Constraints

-   Very large dataset (100k+ images)
-   Single user system (can be opinionated)
-   Windows environment
-   Python backend
-   Performance matters more than elegance

------------------------------------------------------------------------

## Immediate Next Step

### Implement Logging

Modify: - `places_service.py` - `places_map_service.py` - relevant
backend handlers

Add: - high-resolution timing (time.perf_counter) - logs at every stage

User will: 1. Run system 2. Click slow place 3. Capture logs 4. Provide
output

Then: → We optimize based on real data

------------------------------------------------------------------------

## Future Directions

-   GPS clustering (k-means for representative images)
-   Map view UI
-   Cross-dataset correlation (calendar + photos)
-   "Life narrative" reconstruction

------------------------------------------------------------------------

## Summary

You now have: - Fully ingested dataset (\~100k images) - Working
backend - Known performance bottleneck - Clear plan: log → identify →
precompute → cache

The system is **architecturally sound**, but currently: \> Doing
expensive work at runtime that should be done once.

Fix that, and this becomes extremely fast.
