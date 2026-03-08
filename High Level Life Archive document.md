## 

# **The Life Archive: Master Design and Functional Specification**

**Version:** 1.5 | **Status:** Final Detailed Narrative Specification

The **Life Archive** is a high-performance, private software ecosystem designed to act as a **Semantic Historian**. It transforms an unorganized digital landfill into a structured, navigable history of a human life. By leveraging a suite of independent, idempotent batch tools to extract intelligence, the system surfaces significance through geographic clustering and interest scoring while ensuring that human curation remains the definitive "Gold Standard."

---

## **1\. The Abstract: The Logic of the Historian**

The system operates across three tiers: a **File/Database Foundation**, a **Batch Toolchain**, and a **Curation UI**. The "Historian" philosophy dictates that the software evaluates content rather than just displaying it. It identifies the "statistical peaks" of a collection—using geographic clustering and aesthetic scoring—to represent decades, years, and months through visual summaries. It is designed for total privacy, local-first performance, and long-term data portability.

---

## **2\. The Python Toolchain: Independent Batch Utilities**

The archive’s intelligence is populated by standalone Python scripts. Every tool is **Idempotent**: it can be run indefinitely against 100k+ assets, surgically identifying "deltas" (new or changed assets) and ignoring established records to maintain database integrity.

### ingest-photos.py **(The Gatekeeper)**

This script manages the movement of media into the archive and establishes the identity of every item.

* **Identity Logic:** Uses a SHA1 hash to fingerprint every file. If a hash exists, it is a duplicate and is ignored.  
* **Self-Healing:** If a file is moved on disk, the script detects the missing path and updates the database pointer to the new location.  
* **Timestamp Hierarchy:** Resolves dates via a strict hierarchy: (1) Valid GPS Coordinates, (2) "Undated" path override, (3) Year Extraction Regex, (4) EXIF metadata, and (5) OS File Modification Time.

### process-faces.py **(The Identity Engine)**

* **Detection & Clustering:** Scans images for human faces, generates mathematical embeddings, and clusters them into anonymous identities (e.g., "Person 42").  
* **Naming Workflow:** Populates detections (bounding boxes) and identities (mappings). This powers the UI workflow where the author assigns a human name to a cluster, globally updating the social graph.

### process-geography.py **(The Context Engine)**

* **Reverse Geocoding:** Translates raw GPS coordinates into a full hierarchy: Country, State, City, Street Address, and **Nearest Business Name**.  
* **Place Aliasing:** Manages the logic for "Place Aliases," allowing the user to define coordinate bounding boxes as named locations (e.g., "The Colorado House").

### process-interest.py **(The Curator)**

* **Interest Scoring:** Assigns a score (0–100) based on technical clarity, exposure, and the presence of faces.  
* **Redundancy Clustering:** Identifies "bursts" (photos taken within seconds) and similar compositions. It selects a "Keyframe" for visual cards and suppresses the remaining duplicates to avoid repetitive UI grids.

### process-ai.py **(The Semantic Worker)**

* **AI Narrative:** Generates a one-sentence visual summary of the image content.  
* **Auto-Tagging:** Performs object classification (e.g., "Cat," "Mountain," "Porsche") for a dedicated machine-tag column.  
* **Domain Isolation:** Ensures all machine logic remains in ai\_description and ai\_tags, never mixing with human notes.

### process-videos.py **(The Video Worker)**

* **Visual Proxy:** Iterates through the isolated ./Videos hierarchy and generates **poster-frame thumbnails** for every video file, allowing them to be browsed visually within the Explorer tab.

### archive-backend.py **(The Curator/API)**

* **API Layer:** A Flask-based service that serves the dashboard, manages the **Metadata Cache** for card performance, and handles curation commands like rotations and notes.

---

## **3\. The Visual Intelligence: The "Hero 16" Algorithm**

The system represents folders, months, years, and decades through **4x4 Composite Cards**. The selection of these 16 representative images follows a specific algorithmic narrative:

1. **Temporal Pruning:** The candidate pool is filtered for "bursts." If multiple images share the same one-minute window, only the image with the highest interest\_score is retained.  
2. **Geographic Hashing:** Remaining images are assigned a geographic hash to create a map of unique locations within the current context.  
3. **Significant Location Selection:**  
   * **If \> 16 Locations:** The system uses a **K-Means++ clustering algorithm** to find the 16 most statistically significant geographic centroids. One representative image is chosen for each.  
   * **If \< 16 Locations:** The system enters a **Round-Robin loop**. It picks the highest-scored image from Location A, then Location B, until 16 slots are filled.  
4. **Representative Tie-Breaking:** For each location, the script checks the database for an interest\_score. If null, it assigns a score using an aesthetic metric and caches it.  
5. **Composite Assembly:** The 16 images are combined into a single canvas. This process is **Rotation-Aware**, using EXIF-transpose logic to ensure correct orientation.  
6. **Sub-Image Navigation:** Clicking a sub-image on a 4x4 card launches the **Lightbox** directly to that photo, allowing the user to scroll specifically through those 16 "Hero" images.

---

## **4\. UI Mechanics & Behavioral Logic**

### **Global Interaction (Google Photos Model)**

* **Selection Mechanics:** Every thumbnail features a circular **checkbox icon** in the top-left corner.  
  * **Idle:** The icon is a subtle, semi-transparent outline.  
  * **Selected:** Upon clicking the icon (or the image in selection mode), the circle fills with **Accent Violet (\#bb86fc)** and displays a white checkmark. The thumbnail receives a 3px violet border and a slight dimming overlay.  
* **The Escape Reset:** Hitting the **ESC key** acts as a global circuit breaker, instantly clearing the Selection Manifest and resetting the grid to its idle state.

### **Contextual Menu Logic**

The right-click menu is context-aware and operates on the current selection manifest:

* **Valid Contexts (Actionable Layer):** Thumbnail grids (Month, Tag, Explorer folders) and the Lightbox. Options include: *Rotate 90° CW, Rotate 90° CCW, Add Tag, Remove Tag, Change Date, Delete.*  
* **Invalid Contexts (Navigational Layer):** On high-level Decade or Year pages, right-clicking a 4x4 card provides **Administrative Actions** only (e.g., "Open in Explorer," "Recalculate Hero 16"), not image-level mutations like "Rotate."  
* **Empty Space:** Clicking the background offers "Select All" or "Clear Selection."

### **Navigation and Hierarchy**

* **The Banner System:** Every top-level page features a unique, wide-aspect hero banner: hero-timeline.png, hero-undated.png, hero-tags.png, hero-files.png, hero-maps.png, and hero-videos.png.  
* **Contextual Nav:** Clicking a tag on a card (e.g., "\#Hiking") filters the view **relative to that card's parent scope**.

### **The "By Day" Calendar View**

Accessed via a button in the Month view, this provides a **Google Calendar-style month layout**.

* **Visuals:** Days containing photos are highlighted with a representative thumbnail, a photo count, and location indicators.  
* **Direct-Jump:** Clicking a highlighted day bypasses the grid and jumps **directly into the Lightbox** for all photos taken on that specific day.

---

## **5\. The Lightbox & Curation Sidebar**

The Lightbox is an edge-to-edge immersive view. Pressing **'E'** slides out the **Curation Panel** from the right.

* **Block 1 (Identity):** Original Filename, **SHA1 Hash**, and Full Relative Path.  
* **Block 2 (Temporal):** Resolved Date \+ **Date Source Label** (e.g., "Source: GPS-Verified").  
* **Block 3 (Geographic):** Full hierarchy including Country, State, City, Street Address, and the **Nearest Business Name**.  
* **Block 4 (AI Metadata):** AI narrative description and detected object tags, rendered in an **italicized gray system font**.  
* **Block 5 (Human Narrative):** Large textarea for **Notes/Captions** and a field for **Custom Tags**, rendered in a **bold white primary font**.

### **System Side-Effects**

Any manual edit (rotation, date change, tag assignment) that affects an image's presentation automatically **invalidates and regenerates the 4x4 composite card** for its parent month, year, or decade to ensure visual consistency across the entire archive.

