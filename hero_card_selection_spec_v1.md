Life Archive
Hero Card Composite Image Selection
Design Specification v1.0

Author: John Ratcliff / AI assistant collaboration
Purpose: Define the algorithm used to select the 16 images used in a hero card composite (4×4 grid).

1. Goals

The hero card should visually summarize a large set of candidate images (timeline view, folder view, tag view, etc.).

The system should:

• represent major geographic locations in the dataset
• prefer visually interesting images
• avoid overwhelming representation of a single location
• ensure non-GPS images are not ignored
• scale efficiently to tens of thousands of photos

The algorithm must remain fast enough to run dynamically when hero cards are generated.

2. Key Principles

Hero card selection is not the same as culling.

Culling

Used for near-duplicate photos taken at the same time/location.

Goal:
Select the single best image in a cluster.

Primary criteria:

technical quality

blur

exposure

eyes open

face quality

Hero Selection

Used to summarize large heterogeneous sets of images.

Goal:
Select representative and interesting images.

Primary criteria:

geographic diversity

interest level

people present

composition

semantic richness

Perfect ranking is not required.
Any strong representative image is acceptable.

3. Inputs

Candidate image set from a UI context such as:

timeline node

directory

tag

search result

Candidate size may range from:

10 → 50,000+ images

Each image may have:

Signal	Source
GPS coordinates	EXIF
hero score	derived metric
face count	face_scores.sqlite
technical score	technical_scores.sqlite
semantic labels	semantic_scores.sqlite

Only GPS and hero score are required for the core algorithm.

4. Hero Card Size

Hero card consists of:

16 images
4 × 4 composite grid
5. Handling Non-GPS Images

Images without GPS coordinates must not be excluded.

These include:

• older digital photos
• scanned photos
• historical archives

Rule

If non-GPS images exist, reserve one hero slot for them.

reserved_slots = 1
gps_slots = 16 - reserved_slots

If no non-GPS images exist:

gps_slots = 16

The best non-GPS image is selected using hero score.

6. GPS Clustering Strategy

Hero selection is primarily place-based.

Images are grouped using k-means++ clustering on GPS coordinates.

This produces clusters representing major locations.

Example clusters:

hotel
beach
waterfall
museum
restaurant district
airport
7. Large Dataset Optimization

Very large datasets may contain many duplicate coordinates (e.g., thousands of photos at home).

To avoid unnecessary clustering work, coordinates are pre-bucketed.

Preprocessing Step

GPS coordinates are hashed into a spatial grid.

Example:

lat_bucket = round(latitude, 3)
lon_bucket = round(longitude, 3)

This reduces thousands of identical points to one representative point.

Each bucket stores:

centroid_lat
centroid_lon
image_count
best_hero_image

This dramatically reduces clustering workload.

8. Clustering Procedure

After GPS bucketing:

bucket_count = number of GPS buckets
Case A — Few Locations

If:

bucket_count <= gps_slots

Then no clustering is required.

Each bucket becomes one hero candidate.

Case B — Many Locations

If:

bucket_count > gps_slots

Run:

k-means++ clustering
k = gps_slots

Clustering operates on bucket centroids, not raw photos.

Each resulting cluster represents a geographic region.

9. Selecting Images from Clusters

For each cluster:

gather all images belonging to that cluster

compute or retrieve hero scores

select the image with the highest hero score

This becomes the cluster representative image.

10. Filling Remaining Slots

Sometimes there are fewer than 16 clusters.

Example:

3 locations

In that case:

selected_images = cluster representatives

Remaining slots are filled with additional images using hero score.

Selection rules:

choose next highest hero-score image

prefer images from clusters already selected

avoid near-duplicates if possible

This produces visual variety when only a few locations exist.

11. Final Assembly

Selected images:

≤ 16 cluster representatives
+ non-GPS representative (if present)
+ filler images if needed

These images are arranged into a 4×4 composite grid.

The composite is rebuilt whenever:

candidate set changes

underlying scores change

archive metadata changes

12. Performance Considerations

Clustering complexity:

O(N × K × iterations)

Typical parameters:

N ≈ 100–500 GPS buckets
K ≤ 16
iterations ≈ 10–20

This is fast enough for dynamic execution in Python.

The dominant cost is typically:

• database queries
• score sorting

Not clustering itself.

13. Expected Behavior Examples
Example A — Vacation
10 excursions

Result:

≈ 10 clusters
≈ 10 representative images
+ 6 filler images

Produces a good geographic summary.

Example B — Mostly One Location
3000 photos at home

Result:

1 location cluster
+ filler images from that location

Produces visually diverse images from the same place.

Example C — Historical Archive
many scanned images without GPS

Result:

1 reserved non-GPS hero image
+ GPS clusters if present

Ensures legacy images are represented.

14. Future Improvements

Possible enhancements:

• semantic diversity enforcement
• face diversity
• time diversity
• duplicate suppression
• interest-weighted clustering

These are optional refinements and not required for v1.

15. Implementation Dependencies

The algorithm requires:

archive_index.db
technical_scores.sqlite
face_scores.sqlite
aesthetic_scores.sqlite
semantic_scores.sqlite

However the minimum requirement is:

GPS coordinates
hero score
16. Summary

Hero card generation is based on three core ideas:

Geographic clustering

Best image per cluster

Fallback variety when locations are few

This produces visually meaningful summaries of large photo collections while remaining computationally efficient.