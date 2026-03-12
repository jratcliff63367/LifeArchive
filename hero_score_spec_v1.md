Life Archive
Hero Score Calculation
Design Specification v1.0

Author: John Ratcliff / AI assistant collaboration
Purpose: Define how images are ranked for hero card selection.

1. Purpose

The Hero Score ranks images by how visually interesting and representative they are.

It is used for:

• selecting the representative image from each geographic cluster
• filling remaining hero slots when fewer than 16 locations exist
• selecting representative images for timelines, folders, or tags

Hero Score is not used for culling.

Culling uses a separate metric.

2. Design Goals

The score should prefer images that are:

• visually interesting
• emotionally meaningful
• representative of human activity
• aesthetically strong

The score should de-emphasize images that are:

• technically poor
• boring or low-information
• screenshots or documents
• duplicates

The algorithm should be:

• deterministic
• fast
• explainable
• tunable

3. Input Signals

Hero Score is computed from signals stored in sidecar databases.

Signal	Source
technical_score	technical_scores.sqlite
face_count	face_scores.sqlite
largest_face_area_ratio	face_scores.sqlite
aesthetic_score	aesthetic_scores.sqlite
semantic_labels	semantic_scores.sqlite

Not all signals must exist initially.

The score must work even with partial data.

4. Signal Philosophy

Signals fall into two categories:

Technical Quality

Ensures image is usable.

Examples:

• sharpness
• contrast
• exposure
• blur detection

This is represented by:

technical_score

Technical score acts mostly as a quality floor.

Interest Signals

Signals that the image is meaningful or visually engaging.

Examples:

• presence of people
• number of faces
• subject prominence
• composition quality
• semantic richness

These drive hero selection.

5. Base Hero Score Formula

Initial implementation:

hero_score =
    (technical_score * 0.30) +
    (face_score * 0.30) +
    (aesthetic_score * 0.25) +
    (semantic_score * 0.15)

Weights will be tuned during testing.

6. Face Score

Faces are one of the strongest indicators of meaningful images.

Compute:

face_score =
    log(1 + face_count) * 0.6 +
    largest_face_area_ratio * 0.4

This favors:

• images containing people
• images where faces are prominent

But avoids extreme bias toward large group photos.

7. Aesthetic Score

Derived from aesthetic model or composition metrics.

Possible inputs:

• rule-of-thirds alignment
• subject salience
• color contrast
• scene composition

Output normalized to:

0.0 – 1.0
8. Semantic Score

Measures how meaningful the scene appears.

Examples of high semantic interest:

• people
• animals
• landmarks
• events
• activities

Examples of low semantic interest:

• blank walls
• screenshots
• documents

Score range:

0.0 – 1.0
9. Technical Score

Already computed by:

technical-image-score.py

Signals include:

• sharpness
• edge density
• brightness
• contrast

This prevents blurry or unusable images from winning.

10. Special Boost Rules

Certain patterns should increase the hero score.

People Boost
if face_count > 0:
    hero_score += 0.10

Images with people are usually more meaningful.

Prominent Face Boost
if largest_face_area_ratio > 0.15:
    hero_score += 0.10

Large faces often indicate portraits.

Semantic Landmark Boost

Future rule example:

if semantic_label in ["landmark", "mountain", "monument"]:
    hero_score += 0.05
11. Penalty Rules
Very Low Technical Score
if technical_score < 0.25:
    hero_score *= 0.5

Avoid blurry or poorly exposed images.

Screenshot / Document Penalty

If semantic labels indicate:

text
document
screenshot

Apply:

hero_score *= 0.4
12. Normalization

Final hero score should be normalized to:

0.0 – 1.0

This allows consistent ranking.

13. Ranking Process

Given a set of candidate images:

compute hero_score
sort descending
select top image

This is used:

• within geographic clusters
• when filling remaining hero card slots

14. Testing Methodology

Hero score will be tuned using curated test sets.

Procedure:

select representative subsets of images

run scoring algorithm

inspect results visually

adjust weights and rules

repeat

Perfect ranking is not required.

Goal:

choose a strong representative image
15. Expected Behavior

Examples of images likely to score highly:

• smiling group photo
• portrait with clear subject
• scenic landmark photo
• meaningful moment

Images likely to score poorly:

• blurry images
• screenshots
• random textures
• poorly exposed shots

16. Relationship to Culling

Hero Score and Cull Score are separate metrics.

Metric	Purpose
Hero Score	choose representative images
Cull Score	choose best frame in burst cluster

Hero score values do not need to be perfect.

Cull score must be more precise.

17. Future Improvements

Possible additional signals:

• smile detection
• eye-open detection
• subject motion
• scene rarity
• diversity weighting

These can be added later without changing the architecture.

18. Summary

Hero Score combines:

technical quality
faces
aesthetic composition
semantic interest

It is designed to select representative, meaningful images rather than technically perfect ones.

The score will be tuned through iterative testing.