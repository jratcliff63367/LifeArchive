# Life Archive Backend Technical Specification

Version: 1.3  
Status: Current backend behavior plus next approved selection-management features  
Purpose: This spec is the canonical contract for reproducing the **current working backend** and then extending it with the next bounded feature set: **Select All**, **Move to Trash**, **Move to Stash**, and **Empty Trash**.

This spec reflects the current backend represented by the latest working script, including:
- Timeline views: decade → year → month grid → day calendar → day gallery/lightbox
- Undated
- Explorer
- Tags
- Photo-grid multi-select with checkbox overlays and batch rotate
- Lightbox and current sidebar shell

It also adds the next requested feature set, but does **not** yet introduce any other roadmap items.

---

## 1. Objective

Build a Flask-based local web server that reproduces the current working "Life Archive" backend and adds selection-management operations for file triage.

The backend must:

- Read the existing `archive_index.db` database.
- Serve original media files from the managed archive root.
- Serve cached thumbnails from `_thumbs`.
- Generate and cache 4x4 composite card images in `_thumbs/_composites`.
- Provide top-level views:
  - Timeline
  - Undated
  - Explorer
  - Tags
- Provide timeline drilldown pages:
  - decade view
  - year view
  - month grid view
  - month day-calendar view
  - day gallery/lightbox view
- Provide photo-grid multi-select with checkboxes.
- Provide Select All behavior and `Ctrl+A` on photo-grid pages.
- Provide a right-click context menu whose rotate operations are selection-aware.
- Provide three new selection-aware content operations:
  - Move to Trash
  - Move to Stash
  - Empty Trash
- Provide the current minimal sidebar shell in the lightbox.

This backend is a **photo browser, curator shell, and triage tool**, not yet a full metadata editor.

---

## 2. Explicit non-goals

Do **not** implement any of the following in this version:

- Maps page
- Videos page
- AI descriptions or AI tags
- Face recognition tables or UI
- Geographic reverse lookup UI
- Add/remove tag endpoint
- Change date endpoint
- Crop endpoint
- Restore-from-trash endpoint
- Restore-from-stash endpoint
- Empty stash endpoint
- Multi-archive routing UI beyond stash/trash
- Hero image selection by GPS, clustering, burst analysis, interest scoring, or ranking

Even if older design notes mention those features, they are not part of this version.

---

## 3. Runtime and dependencies

### 3.1 Language and framework

- Python 3.x
- Flask
- Pillow
- sqlite3 from the Python standard library
- Standard-library file operations: `os`, `shutil`, `pathlib`, `hashlib`, `json`, `logging`

### 3.2 Required global Pillow setting

Set globally before loading large images:

```python
Image.MAX_IMAGE_PIXELS = None
```

### 3.3 Logging

Suppress normal Flask request logging from Werkzeug so console output stays quiet.

Example behavior:

```python
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)
```

---

## 4. Archive root layout

The backend operates against a single archive root, here called `ARCHIVE_ROOT`.

Example:

```text
ARCHIVE_ROOT/
    archive_index.db
    _thumbs/
        <sha1>.jpg
        _composites/
            <md5>.jpg
    _trash/
        ... preserved relative original hierarchy ...
    _stash/
        ... preserved relative original hierarchy ...
    _web_layout/
        assets/
            hero-timeline.png
            hero-undated.png
            hero-files.png
            hero-tags.png
    ... actual archived media hierarchy ...
```

### 4.1 Required derived paths

The server must derive these paths from `ARCHIVE_ROOT`:

- `DB_PATH = ARCHIVE_ROOT/archive_index.db`
- `THUMB_DIR = ARCHIVE_ROOT/_thumbs`
- `COMPOSITE_DIR = ARCHIVE_ROOT/_thumbs/_composites`
- `ASSETS_DIR = ARCHIVE_ROOT/_web_layout/assets`
- `TRASH_DIR = ARCHIVE_ROOT/_trash`
- `STASH_DIR = ARCHIVE_ROOT/_stash`

Create these directories if they do not exist:

- `COMPOSITE_DIR`
- `TRASH_DIR`
- `STASH_DIR`

### 4.2 Reserved top-level folders

The backend must treat these as reserved internal folders and exclude them from normal archive browsing unless explicitly required:

- `_thumbs`
- `_web_layout`
- `_trash`
- `_stash`

Files moved to `_trash` or `_stash` are **not part of the active archive corpus**.

---

## 5. Database schema assumptions

### 5.1 Required table: `media`

The backend assumes this table already exists and is populated by the ingestor.

Required columns:

- `sha1 TEXT PRIMARY KEY`
- `rel_fqn TEXT`
- `original_filename TEXT`
- `path_tags TEXT`
- `final_dt TEXT`
- `dt_source TEXT`
- `is_deleted INTEGER`
- `custom_notes TEXT`
- `custom_tags TEXT`

### 5.2 Required table: `composite_cache`

The backend must create this table if it does not already exist:

```sql
CREATE TABLE IF NOT EXISTS composite_cache (
    path_key TEXT PRIMARY KEY,
    sha1_list TEXT,
    composite_hash TEXT
)
```

Also create an index for timeline reads:

```sql
CREATE INDEX IF NOT EXISTS idx_media_dt ON media(final_dt)
```

### 5.3 No additional required schema changes for this feature

The new trash/stash behavior must work against the existing schema.

Use these semantics:

- Active files remain represented in `media` with `is_deleted = 0`.
- Files moved to trash or stash must be removed from the active archive by updating `is_deleted = 1`.
- `rel_fqn` must be updated to the new physical path under `_trash` or `_stash`.
- No additional columns are required for this version.

Note: this means both stash and trash are hidden from active browsing using the same database-level active/inactive mechanism. Their distinction is encoded in filesystem location, not a separate DB column.

---

## 6. Data loading and in-memory caches

At startup, and also on every route render in the current implementation, the backend loads the active photo set from SQLite into memory.

Maintain these three global in-memory structures:

- `DB_CACHE`: dated items only
- `UNDATED_CACHE`: undated items only
- `GLOBAL_TAGS`: mapping of tag string -> list of media items

### 6.1 Media query

Load media with this semantic behavior:

- Only include rows where `is_deleted = 0`
- Sort rows by `final_dt DESC`

Equivalent SQL:

```sql
SELECT * FROM media WHERE is_deleted = 0 ORDER BY final_dt DESC
```

This query is the reason moved-to-trash and moved-to-stash files disappear from all active views.

### 6.2 Per-item derived fields

Each loaded row becomes a mutable dictionary with these derived fields added.

#### `_web_path`

Convert `rel_fqn` to a forward-slash web path:

- Replace backslashes (`\`) with `/`

This is the path used in media URLs and explorer grouping.

#### `_tags_list`

Compute from:

- `path_tags`
- `custom_tags`

Concatenate them with a comma, split on commas, trim whitespace, exclude invalid tags, de-duplicate, title-case, then sort.

Equivalent logic:

1. Build `raw_tags = f"{path_tags or ''},{custom_tags or ''}"`
2. Split on `,`
3. Trim whitespace on each token
4. Discard empty strings
5. Discard excluded tags
6. Convert remaining tags to title case
7. Remove duplicates using a set
8. Sort the final list alphabetically

#### `_folder_group` for undated items

For undated rows only, set `_folder_group` to the first path component of `_web_path`, unless missing.

#### `_year`, `_month`, `_month_name`, `_decade`

For rows whose `final_dt` is not zero-date:

- `_year` = first 4 chars of `final_dt`
- `_month` = chars 6-7 of `final_dt`
- `_month_name` = localized English month label (January, February, ...)
- `_decade` = e.g. `1990s`, `2000s`

For rows whose `final_dt == "0000-00-00 00:00:00"`, do not set these fields.

### 6.3 Excluded path tags

Exclude these tags before final `_tags_list` creation:

- `pictures`
- `photos`
- `media`
- `images`
- `terrysbackup`
- `topaz-undated`

Also exclude pure 4-digit year tags such as `1997`, `2004`, etc.

### 6.4 Undated classification rule

Rows belong in `UNDATED_CACHE` only when:

```python
final_dt == "0000-00-00 00:00:00"
```

Rows do **not** become undated merely because their path contains the string `undated`. That classification must already have been done by the ingestor.

---

## 7. Composite card generation

### 7.1 Purpose

Composite cards are used for:

- decade cards
- year cards
- month cards
- explorer cards
- tag cards
- undated folder cards
- calendar day cells when a representative thumbnail is needed

### 7.2 Selection algorithm for composite source images

For the current working backend, composite image selection remains intentionally simple:

- Use the first 16 images from the media list as already ordered by the page/query.
- Do **not** use GPS clustering, burst suppression, interest scoring, or ranking.

Equivalent logic:

```python
media_list[:16]
```

### 7.3 Composite layout

- 4x4 tile grid
- Render to a single JPEG cached in `COMPOSITE_DIR`
- Cache key derived from the list of source SHA1 values and a composite version string

### 7.4 Composite invalidation

Any operation that materially changes visible image orientation or membership of a page must invalidate affected composite images.

At minimum, invalidate after:

- single rotate
- batch rotate
- move to trash
- move to stash
- empty trash

It is acceptable to invalidate conservatively by clearing or version-bumping all composites rather than surgically removing only the touched ones.

---

## 8. Top-level navigation

The top nav remains:

- Timeline
- Undated
- Explorer
- Tags

These top-level tabs remain stable and must not be renamed.

---

## 9. Timeline views

### 9.1 Timeline root

Route:

```text
/timeline
```

Shows decade cards.

### 9.2 Decade view

Route:

```text
/timeline/decade/<decade>
```

Shows year cards for that decade.

### 9.3 Year view

Route:

```text
/timeline/year/<year>
```

Shows month cards for that year.

### 9.4 Month grid view

Route:

```text
/timeline/month/<year>/<month>
```

Shows the standard photo grid for the selected month.

Must include an action control labeled:

- `Day View`

which links to:

```text
/timeline/month/<year>/<month>/days
```

### 9.5 Month day-calendar view

Route:

```text
/timeline/month/<year>/<month>/days
```

Displays a Sunday-first month calendar.

Each populated day cell must show:

- a representative thumbnail
- day number
- photo count
- up to 3 top tags
- a simple place-style label derived from top tags

Each populated cell links to:

```text
/timeline/month/<year>/<month>/day/<day>
```

### 9.6 Day gallery view

Route:

```text
/timeline/month/<year>/<month>/day/<day>
```

Displays all photos for that day in the standard photo-grid/lightbox UI.

Lightbox navigation stays inside that day’s photo list only.

---

## 10. Undated view

Route:

```text
/undated
```

Displays cards grouped by `_folder_group` using only `UNDATED_CACHE`.

If `UNDATED_CACHE` is empty, the page will appear empty except for the header. This is correct behavior.

---

## 11. Explorer view

### 11.1 Root explorer

Route:

```text
/folder
```

Shows cards for first-level folders.

### 11.2 Nested explorer

Route:

```text
/folder/<path:subpath>
```

Shows child folder cards and/or direct photo grid depending on contents.

Explorer grouping is based on `_web_path`, not timestamps.

---

## 12. Tags view

### 12.1 Tag root

Route:

```text
/tags
```

Displays tag cards using the current existing mode.

### 12.2 Tag detail

Route:

```text
/tags/<tag>
```

Displays the standard photo grid for that tag.

### 12.3 Tag-mode note

A future compact tag-pill mode has been discussed but is not part of this version.

---

## 13. Photo-grid behavior

The standard photo-grid UI is used on:

- month grid pages
- day gallery pages
- explorer leaf pages
- tag detail pages
- any other direct-photo pages in the current backend

### 13.1 Standard interactions

Each photo card supports:

- checkbox selection overlay
- click to open lightbox
- right-click custom context menu

### 13.2 Selection state

Selection is page-local and client-side only.

Selection must be cleared when:

- the user presses `Escape`
- the user navigates to another page/view
- a completed batch operation chooses to clear selection after refresh

### 13.3 Select All

Add a visible `Select All` action on photo-grid pages.

Behavior:

- selects all currently visible photo cards on the page
- does not select hidden/off-page items
- updates checkbox states and selection count immediately

### 13.4 `Ctrl+A`

On photo-grid pages, pressing `Ctrl+A` must perform the same action as `Select All`.

Rules:

- it applies only to the current visible photo-grid page
- if keyboard focus is in a text input or textarea, do not override the browser’s normal select-all behavior inside that field
- it must not cross page boundaries or persist after navigation

### 13.5 Escape

`Escape` must clear current selection first.

If the lightbox is open, the existing lightbox-close behavior may remain, but selection clearing must still be honored in a sensible order.

---

## 14. Lightbox behavior

The lightbox remains full-screen and supports:

- previous / next navigation
- keyboard arrow navigation
- current minimal sidebar shell
- right-click context menu

No new metadata editor is added in this version.

---

## 15. Context menu behavior

### 15.1 Current items

The existing context menu already supports:

- Rotate Right
- Rotate Left

### 15.2 New items

Add these menu items:

- Move to Trash
- Move to Stash

Additionally, provide an `Empty Trash` control in the UI, but **not** as a per-image context menu item. It is a higher-level archive action.

### 15.3 Selection-aware semantics

If multiple items are selected and the right-click target is one of the selected items, the menu operations apply to the full current selection.

If no meaningful multi-selection exists, right-click behaves as it does currently for a single image.

---

## 16. Rotate operations

### 16.1 Single rotate

Existing behavior remains:

- rotate master image on disk
- regenerate thumbnail
- invalidate composites
- refresh UI

### 16.2 Batch rotate

Existing selection-aware batch rotate remains.

Endpoint semantics may remain:

```text
POST /api/rotate_batch
```

with a JSON payload like:

```json
{
  "sha1_list": ["...", "..."],
  "degrees": 90
}
```

The backend may choose to reload the page after completion. That is acceptable and currently preferred for safety.

---

## 17. Trash and stash behavior

### 17.1 Purpose

Trash and stash are reversible file-triage operations.

- **Trash** means the file is no longer wanted in the active archive, but can still be recovered until trash is emptied.
- **Stash** means the file is valuable but does not belong in the current archive context.

### 17.2 Physical move rules

Given an active file such as:

```text
pictures/vacation/beach.jpg
```

Move to Trash must physically move it to:

```text
_trash/pictures/vacation/beach.jpg
```

Move to Stash must physically move it to:

```text
_stash/pictures/vacation/beach.jpg
```

Preserve the relative path beneath the archive root.

Create intermediate directories as needed.

### 17.3 Name collision handling

If a destination file already exists in trash or stash at the target relative path, the backend must avoid destructive overwrite.

Acceptable behavior:

- append a suffix such as `_1`, `_2`, etc., before the extension
- then update `rel_fqn` to the actual destination path used

### 17.4 Database updates

After a successful move to trash or stash:

- update `rel_fqn` to the new relative path under `_trash` or `_stash`
- set `is_deleted = 1`
- commit immediately
- invalidate affected composites
- refresh active in-memory caches or reload page data so the files disappear from active views

### 17.5 Visibility rules

Files in `_trash` or `_stash` must not appear in:

- Timeline
- Day View
- Undated
- Explorer
- Tags
- lightbox sequences drawn from active pages

### 17.6 Scope of move operations

Move to Trash and Move to Stash must operate on:

- the current single image if no meaningful multi-selection exists
- the full current selected set if multi-selection applies

### 17.7 Recommended endpoints

One acceptable design is:

```text
POST /api/move_to_trash
POST /api/move_to_stash
```

with payloads like:

```json
{
  "sha1_list": ["...", "..."]
}
```

The backend may return JSON and then reload the page client-side.

---

## 18. Empty Trash behavior

### 18.1 Purpose

`Empty Trash` is the only destructive bulk operation in this version.

There is **no** `Empty Stash` operation.

### 18.2 Behavior

When triggered, the backend must:

- recursively delete all files under `_trash`
- remove corresponding DB rows from `media`, or otherwise make them permanently non-recoverable
- invalidate affected composites
- refresh active caches

Preferred DB behavior for this version:

- delete the matching `media` rows whose `rel_fqn` begins with `_trash\\` or `_trash/`

This keeps the archive DB clean.

### 18.3 Confirmation requirement

This action must require an explicit confirmation step in the UI. A simple confirmation dialog is acceptable.

### 18.4 Recommended endpoint

One acceptable design is:

```text
POST /api/empty_trash
```

No payload is required.

---

## 19. Selection bar / page actions

A selection bar is already present in the current backend for batch rotate.

Extend it as needed to support:

- selected count display
- Select All
- Rotate Left
- Rotate Right
- Move to Trash
- Move to Stash

It is acceptable for `Empty Trash` to live outside the selection bar as a page-level action.

---

## 20. UI refresh behavior after mutation

After any successful mutating operation:

- single rotate
- batch rotate
- move to trash
- move to stash
- empty trash

it is acceptable and preferred to do a full page reload rather than attempting a brittle partial DOM patch.

This is consistent with the current backend’s preference for correctness over fancy incremental updates.

---

## 21. Regression constraints

These are strict non-regression requirements.

The following must remain unchanged unless explicitly required by this spec:

- top nav labels and routes
- timeline grouping behavior
- year and decade card behavior
- month grid behavior
- day-calendar behavior
- day gallery behavior
- Undated grouping behavior
- Explorer grouping behavior
- tag derivation and sort behavior
- lightbox arrow navigation
- single-image rotate behavior
- current minimal sidebar shell
- database dependence on existing `media` schema

The new feature work must minimize diff size and avoid unrelated rewrites.

---

## 22. Acceptance tests

### 22.1 Existing views still work

1. Open `/timeline`; decade cards render.
2. Open a decade page; year cards render.
3. Open a year page; month cards render.
4. Open a month page; photo grid renders and `Day View` button is present.
5. Open `/timeline/month/<year>/<month>/days`; calendar renders.
6. Click a populated day; day gallery renders and lightbox works.
7. Open `/undated`; undated cards render if zero-date rows exist.
8. Open `/folder`; explorer cards render.
9. Open `/tags`; tag cards render.
10. Open a tag detail page; photo grid renders.

### 22.2 Selection behavior

1. On a photo-grid page, click one checkbox; selection count becomes 1.
2. Click additional checkboxes; count updates.
3. Press `Escape`; selection clears.
4. Navigate away; selection is gone on the next page.
5. Press `Ctrl+A`; all visible photo cards become selected.
6. Click `Select All`; all visible photo cards become selected.

### 22.3 Rotate behavior

1. Select multiple photos.
2. Use right-click Rotate Right on one selected photo.
3. All selected photos rotate.
4. Thumbnails and composites refresh correctly after reload.

### 22.4 Move to Trash

1. Select one or more photos on a photo-grid page.
2. Invoke `Move to Trash`.
3. Physical files appear under `_trash/...` with preserved relative paths.
4. Corresponding DB rows are updated: `rel_fqn` under `_trash`, `is_deleted = 1`.
5. Photos disappear from active view after reload.

### 22.5 Move to Stash

1. Select one or more photos on a photo-grid page.
2. Invoke `Move to Stash`.
3. Physical files appear under `_stash/...` with preserved relative paths.
4. Corresponding DB rows are updated: `rel_fqn` under `_stash`, `is_deleted = 1`.
5. Photos disappear from active view after reload.

### 22.6 Empty Trash

1. After moving files to trash, trigger `Empty Trash`.
2. Confirm the destructive action.
3. `_trash` contents are deleted.
4. Matching DB rows are removed.
5. Trash does not reappear after backend restart.

---

## 23. Implementation guidance

When a coding LLM uses this spec, it should:

- start from the latest working backend behavior, not from scratch
- preserve existing routes and templates wherever possible
- add the smallest number of new endpoints necessary
- prefer explicit helper functions for move and delete operations
- avoid broad refactors unrelated to this feature

This feature is intentionally bounded. Implement only what is described here.
