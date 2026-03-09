# Life Archive Backend Baseline Technical Specification

Version: 1.1  
Status: Baseline backend plus first feature delta  
Purpose: This spec describes the **current backend behavior that must now be reproduced**, using the current SQLite database and archive layout produced by the ingestor. It intentionally stays close to the existing backend, but now adds one bounded feature: **multi-select on photo-grid pages with batch application of the existing rotate actions**. Any feature not explicitly described here is out of scope.

---

## 1. Objective

Build a Flask-based local web server that reproduces the current working "Life Archive" backend.

The backend must:

- Read the existing `archive_index.db` database.
- Serve original media files from the managed archive root.
- Serve cached thumbnails from `_thumbs`.
- Generate and cache 4x4 composite card images in `_thumbs/_composites`.
- Provide four top-level views:
  - Timeline
  - Undated
  - Explorer
  - Tags
- Provide a full-screen lightbox with keyboard navigation.
- Provide a right-click context menu for rotate-right and rotate-left.
- Provide the current minimal sidebar shell in the lightbox.

This backend is a **photo browser and curator shell**, not yet a full metadata editor.

---

## 2. Explicit non-goals

Do **not** implement any of the following in this baseline version:

- Maps page
- Videos page
- AI descriptions or AI tags
- Face recognition tables or UI
- Geographic reverse lookup UI
- Batch edit operations other than applying the existing rotate actions to the current selection
- Delete endpoint
- Add/remove tag endpoint
- Change date endpoint
- By-day calendar view
- Hero image selection by GPS, clustering, burst analysis, interest scoring, or ranking

Even if older design notes mention those features, they are not part of the baseline backend.

---

## 3. Runtime and dependencies

### 3.1 Language and framework

- Python 3.x
- Flask
- Pillow
- sqlite3 from the Python standard library

### 3.2 Required global Pillow setting

Set globally, before loading large images:

```python
Image.MAX_IMAGE_PIXELS = None
```

This is required to avoid warnings or failures on very large scans and panoramas.

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

Create `COMPOSITE_DIR` if it does not exist.

---

## 5. Database schema assumptions

### 5.1 Required table: `media`

The baseline backend assumes this table already exists and is populated by the ingestor.

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

### 5.2 Internal table: `composite_cache`

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

### 5.3 No schema migration beyond this

Do not require any other tables.

If future tables exist, ignore them.

---

## 6. Data loading and in-memory caches

At startup, and also on every route render in the existing implementation, the backend loads the active photo set from SQLite into memory.

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

If `final_dt` starts with `0000`, classify as undated.

For undated items, derive `_folder_group` as:

- split `_web_path` on `/`
- use the immediate parent folder name, meaning `parts[-2]`
- if no parent exists, use `"Root"`

#### Date-derived fields for dated items

If `final_dt` does **not** start with `0000`, parse it using:

```python
datetime.strptime(final_dt, "%Y-%m-%d %H:%M:%S")
```

Then derive:

- `_year`: first 4 chars of `final_dt`
- `_month`: 2-digit month from parsed datetime, `01` through `12`
- `_month_name`: full English month name, for example `January`
- `_decade`: first 3 digits of year plus `0s`, for example `1990s`, `2000s`

### 6.3 Excluded tags

The baseline backend must exclude the following tags case-insensitively:

- `pictures`
- `photos`
- `photographs`
- `media`
- `images`
- `terrysbackup`
- `topaz-undated`

Also exclude any tag that is exactly a 4-digit year.

Recommended helper semantics:

```python
if tag.strip().lower() in TAG_EXCLUSIONS: exclude
if re.fullmatch(r"\d{4}", tag.strip().lower()): exclude
```

### 6.4 Global tag index

For every item, for every value in `_tags_list`, append the full item dict into `GLOBAL_TAGS[tag]`.

The tag index preserves the current ordering inherited from `DB_CACHE` or `UNDATED_CACHE` population, which ultimately comes from `final_dt DESC` query order.

---

## 7. Helper behavior

### 7.1 `get_top_tags(items, limit=3)`

For any list of media items, count occurrences of tags found in each item's `_tags_list`, and return the `limit` most common tags.

This is used only to display the small tag pills under cards.

### 7.2 `build_manifest(media_list)`

Return a list of dictionaries in the same order as `media_list`.

Each manifest entry must contain:

- `sha1`
- `path` = `_web_path`
- `filename` = `original_filename`

Example entry:

```json
{
  "sha1": "abc123...",
  "path": "Photos/2001/Trip/image001.jpg",
  "filename": "image001.jpg"
}
```

These manifests are serialized to JSON and consumed by the front-end lightbox JavaScript.

---

## 8. Composite card generation

### 8.1 Intent

Each card in Timeline, Undated, Explorer, and Tags uses a 400x400 composite image made from up to 16 child images.

### 8.2 Current baseline hero selection rule

Use the **first 16 items** from the candidate `media_list`.

There is no ranking, no geography, no burst suppression, and no scoring.

```python
heroes = media_list[:16]
```

### 8.3 Composite identity and caching

The composite cache is keyed by an arbitrary logical `path_key`, such as:

- `d_2000s`
- `y_2004`
- `m_2004_12`
- `u_FolderName`
- `f_SubFolder`
- `t_Florida`

For a given `path_key`:

1. Check `composite_cache` for an existing `composite_hash`
2. If the hash exists and `COMPOSITE_DIR/<composite_hash>.jpg` exists on disk, reuse it
3. Otherwise build a new composite

### 8.4 Composite hash

Compute an MD5 hash from the concatenation of the selected 16 SHA1 values, in order.

```python
hashlib.md5("".join(sha1s).encode()).hexdigest()
```

Use that hash as the composite JPEG filename.

### 8.5 Tile source preference

For each hero image:

1. Prefer `_thumbs/<sha1>.jpg` if it exists
2. Otherwise fall back to the original archived file at `ARCHIVE_ROOT/<rel_fqn>`

### 8.6 Tile processing

Build a black or very dark 400x400 RGB canvas.

For each selected image, in index order 0 through 15:

- Open the tile image
- Apply `ImageOps.exif_transpose(img)`
- Resize in-place with `thumbnail((100, 100))`
- Center the resized tile into its 100x100 cell
- Paste into a 4x4 grid using row-major order

Cell placement:

- column = `i % 4`
- row = `i // 4`
- origin x = `column * 100`
- origin y = `row * 100`

Centering inside the cell:

- `paste_x = cell_x + (100 - tile_width) // 2`
- `paste_y = cell_y + (100 - tile_height) // 2`

If any tile fails to open, skip it and continue.

### 8.7 Composite write

Save composite as JPEG to:

```text
COMPOSITE_DIR/<composite_hash>.jpg
```

Recommended quality: 85.

### 8.8 Composite cache row write

Persist the mapping:

- `path_key`
- comma-joined selected SHA1 list
- `composite_hash`

Note: the original working script uses `INSERT OR REPLACE` for this internal cache table. That is acceptable here because it does not affect user-authored metadata.

---

## 9. Top-level UI structure

### 9.1 Theme

Use a dark UI.

Required baseline palette:

- page background: `#0d0d0d`
- card background: `#1a1a1a`
- accent: `#bb86fc`
- secondary borders: dark gray around `#333`

### 9.2 Typography

Use Inter from Google Fonts.

Navigation and headers should appear bold and modern.

### 9.3 Navigation bar

Sticky top navigation bar with links:

- Timeline
- Undated
- Explorer
- Tags

The active tab must be visually highlighted with the accent color and a bottom border.

### 9.4 Hero banner

Each major page shows a wide banner near the top using an image from `_web_layout/assets`.

Use these filenames:

- Timeline pages: `hero-timeline.png`
- Undated pages: `hero-undated.png`
- Explorer pages: `hero-files.png`
- Tag pages: `hero-tags.png`

### 9.5 Page body modes

Pages render in one of two modes:

- **Card grid mode** for decade/year/month groups, folder groups, and tags index
- **Photo grid mode** for actual images within a single group

---

## 10. Card model

Each card must include:

- `id` unique within page
- `title`
- `subtitle` like `"741 items"`
- `url` destination when clicked
- `heroes` source list for composite generation
- `tags` top 0 to 3 tags
- `comp_hash` resolved composite JPEG hash

### 10.1 Card visual behavior

- Card is clickable
- Hover slightly raises card and highlights border with accent color
- Top area is a square preview using the 4x4 composite
- Bottom area shows title, subtitle, and tag pills

### 10.2 Composite click behavior

The square composite image itself must also support opening the lightbox directly to one of the underlying 16 images.

This is done by attaching a manifest id to the preview and then mapping click position to a tile index in JavaScript.

Specifically:

- The preview element receives `data-manifest="<card-id>"`
- On click, compute relative x/y within the preview
- Convert to 4x4 tile cell
- Open the corresponding index in that card's manifest

If the clicked tile index exceeds the number of entries in the manifest, clamp or ignore safely.

---

## 11. Photo grid model

When rendering actual images within a group, use a responsive grid of thumbnail cards.

Each photo tile must:

- show the 400px cached thumbnail from `/thumbs/<sha1>.jpg`
- be clickable to open the lightbox at that image index
- support right-click context menu for rotation
- include a stable DOM id `thumb-<sha1>` so it can be refreshed after rotate
- expose a visible checkbox affordance, styled similarly to Google Photos selection circles, for selecting this tile without opening the lightbox

The photo grid uses the manifest id `main_gallery`.

### 11.1 Scope of selection

Selection exists only on **photo-grid pages**, meaning pages that render actual thumbnails rather than composite cards.

This includes:

- `/timeline/month/<year>/<month>`
- `/undated/<folder>`
- `/folder` and `/folder/<path:sub>` for direct files shown in the current folder
- `/tags/<tag>`

Selection does **not** exist on card-grid pages such as decade, year, undated-group, explorer-folder-group, or tags index pages.

### 11.2 Selection model

The frontend maintains an in-memory selection set of SHA1 values for the **current page only**.

Required behavior:

- each photo tile has a checkbox hit target in the upper-left corner
- clicking the checkbox toggles selection for that tile
- clicking the thumbnail image itself still opens the lightbox and does **not** implicitly toggle selection
- selected tiles must have a clear visual selected state, for example a highlighted border and checked checkbox
- selection is page-local only, not global across routes

A simple JavaScript `Set` keyed by SHA1 is sufficient. No backend persistence is needed.

### 11.3 Selection lifecycle rules

Selection must be cleared in all of the following cases:

- when the user presses `Escape` while one or more tiles are selected
- when the user navigates to a different route or page view
- when the page is freshly loaded or reloaded

Because selection is page-local and entirely client-side, changing pages automatically acts as deselect due to loss of context.

### 11.4 Escape key priority

The `Escape` key now has priority behavior:

1. If the current page has one or more selected thumbnails, `Escape` clears the current selection and does nothing else.
2. Otherwise, if the lightbox is open, `Escape` closes the lightbox.
3. Otherwise, `Escape` has no effect.

This rule is required so the user can reliably deselect without accidentally closing unrelated UI first.

### 11.5 DOM requirements for selection

Each photo tile must expose enough DOM state for client-side selection management.

Minimum required semantics:

- tile root element has a stable marker such as `data-sha1="<sha1>"`
- tile root can receive a CSS class such as `selected`
- checkbox element can be toggled independently of the image click handler

The implementation may use either a real checkbox input or a styled clickable element that behaves like one, but it must present as a checkbox-style selection affordance to the user.

---

## 12. Lightbox behavior

### 12.1 Core behavior

The backend uses one reusable full-screen lightbox overlay.

It must support:

- open from any gallery or card composite
- close button
- next and previous nav zones
- arrow-key navigation
- ESC to close
- toggle sidebar with the `E` key

### 12.2 Manifest model

The frontend receives a JSON object named `manifests`.

This is a dictionary:

- key = manifest id, for example `main_gallery` or `d_2000s`
- value = list of manifest entries from `build_manifest(...)`

The lightbox keeps two pieces of state:

- current manifest id
- current image index

### 12.3 Displayed image

The lightbox `<img>` source is:

```text
/media/<manifest_entry.path>
```

Append a cache-busting query string timestamp after rotate operations.

### 12.4 Sidebar contents

The baseline sidebar is a minimal shell, not a real metadata panel.

It must at least display:

- file name of the current image
- a notes text area

The sidebar may slide in and out from the right.

No persistence is required for the notes area in this baseline version.

---

## 13. Context menu and rotate behavior

### 13.1 Right-click support

A custom context menu must appear on right-click for:

- photo thumbnails in gallery mode
- the currently open lightbox image

The menu still only needs two actions:

- Rotate Right (90)
- Rotate Left (270)

### 13.1.1 Selection-aware menu targeting

The right-click menu must become selection-aware on photo-grid pages.

Required behavior:

- if **two or more** thumbnails are currently selected, right-clicking any thumbnail opens the existing custom menu and the selected operation applies to **all currently selected SHA1s**
- if **zero or one** thumbnail is currently selected, right-clicking a thumbnail behaves exactly like the current baseline and targets only the thumbnail under the pointer
- if the lightbox is open and the user right-clicks the lightbox image, the current baseline single-image behavior is preserved; lightbox right-click does not need batch behavior in this feature

This rule preserves baseline behavior for ordinary use while enabling batch rotate only when there is a meaningful current selection.

### 13.2 Rotate endpoint

Preserve the existing endpoint:

```text
POST /api/rotate/<sha1>
```

Request body JSON must include:

```json
{
  "degrees": 90
}
```

or

```json
{
  "degrees": 270
}
```

For this feature, the request body may also include an optional batch list:

```json
{
  "degrees": 90,
  "sha1_list": ["abc", "def", "ghi"]
}
```

Selection-aware endpoint semantics:

- if `sha1_list` is present and contains 2 or more values, rotate all listed SHA1 values
- otherwise rotate only the route SHA1
- the route SHA1 remains required for backward compatibility with the baseline frontend

### 13.3 Rotate lookup

For each target SHA1, find `rel_fqn` from the `media` table.

If any SHA1 is not found, the simplest acceptable behavior is either:

- fail the whole request with HTTP 404, or
- skip invalid SHA1s and return only the successfully rotated list

Preferred behavior for this feature: fail only if the primary route SHA1 is missing, but otherwise process valid entries from `sha1_list` and ignore duplicates.

Before processing, normalize the target list by:

- removing duplicates while preserving order
- falling back to `[route_sha1]` if no usable batch list was supplied

### 13.4 Rotation semantics

For each target SHA1:

- open the original media file from disk
- preserve existing EXIF bytes if present
- map requested degrees to Pillow transpose methods like this:
  - `90` means visual rotate-right, implemented with `Image.ROTATE_270`
  - `270` means visual rotate-left, implemented with `Image.ROTATE_90`
- save back to the same file path as JPEG, preserving EXIF bytes when possible

Recommended quality: 95.

### 13.5 Thumbnail regeneration after rotate

After rotating each original file, regenerate `_thumbs/<sha1>.jpg` from the updated original.

Use the current thumbnail logic:

- open rotated original
- optionally apply `ImageOps.exif_transpose`
- resize to fit within 400x400 using `thumbnail((400,400))`
- save as RGB JPEG

### 13.6 Front-end refresh behavior after rotate

After successful rotate on a photo-grid page:

- refresh every affected thumbnail DOM node `thumb-<sha1>` with a cache-busting timestamp
- if the lightbox is open on one of the rotated images, reload the current full-size image with cache-busting timestamp
- leave selection intact after the rotate operation; rotate is not itself a deselect action

The backend still does **not** need to proactively invalidate every affected composite card.
That omission remains acceptable for this version.

Return JSON in a form that supports both single and batch refresh. A minimal acceptable response is:

```json
{
  "status": "ok",
  "updated": ["abc", "def", "ghi"]
}
```

For backward compatibility, if only one image was rotated, `updated` may contain a single SHA1.

---

## 14. Regression constraints for this feature delta

The multi-select feature must **not** change any of the following baseline behaviors:

- Timeline grouping and route structure
- Undated grouping and route structure
- Explorer grouping and route structure
- Tags grouping and route structure
- Composite card generation and click-to-lightbox behavior
- Lightbox navigation with left/right arrows
- Existing single-image right-click rotate behavior when no meaningful selection exists
- Existing banner images, dark theme, and page layout structure
- Existing database schema assumptions, other than accepting an optional `sha1_list` field in rotate requests

No delete, tag edit, date edit, or persistence of selection state is part of this feature.

---

## 15. Routes and page behavior

### 14.1 `/` and `/timeline`

These are the same page.

Behavior:

- load cache
- group all dated items by `_decade`
- sort decades descending, newest first
- create one card per decade
- card title = decade string like `2000s`
- card subtitle = `<count> items`
- card URL = `/timeline/decade/<decade>`
- card tags = `get_top_tags(items)`
- banner = `hero-timeline.png`
- active nav tab = `timeline`
- breadcrumb text = `Decades`

### 14.2 `/timeline/decade/<decade>`

Behavior:

- load cache
- filter dated items where `_decade == decade`
- group by `_year`
- sort years descending
- create one card per year
- card URL = `/timeline/year/<year>`
- banner = `hero-timeline.png`
- active tab = `timeline`
- breadcrumb = `Timeline / <decade>` where `Timeline` links to `/timeline`

### 14.3 `/timeline/year/<year>`

Behavior:

- load cache
- filter dated items where `_year == year`
- group by `_month`
- sort month codes ascending (`01` to `12`)
- derive display month name from the first item in each month bucket
- create one card per month
- card title = full month name
- card URL = `/timeline/month/<year>/<month>`
- banner = `hero-timeline.png`
- active tab = `timeline`
- breadcrumb = `Timeline / <decade> / <year>` with links back

### 14.4 `/timeline/month/<year>/<month>`

Behavior:

- load cache
- filter dated items where `_year == year and _month == month`
- render photo grid mode, not card mode
- preserve current order from `DB_CACHE`
- manifest id = `main_gallery`
- page title = `<MonthName> <Year>`
- banner = `hero-timeline.png`
- active tab = `timeline`
- breadcrumb = `Timeline / <decade> / <year> / <MonthName>` with links back

### 14.5 `/undated`

Behavior:

- load cache
- group `UNDATED_CACHE` by `_folder_group`
- sort groups alphabetically by folder name
- create one card per group
- card URL = `/undated/<folder_group>`
- banner = `hero-undated.png`
- active tab = `undated`
- breadcrumb = `Undated`

### 14.6 `/undated/<folder>`

Behavior:

- load cache
- filter undated items where `_folder_group == folder`
- render photo grid mode
- manifest id = `main_gallery`
- banner = `hero-undated.png`
- active tab = `undated`
- breadcrumb = `Undated / <folder>` where `Undated` links to `/undated`

### 14.7 `/folder` and `/folder/<path:sub>`

This is the Explorer view.

Behavior:

- load cache
- combine all visible media: `DB_CACHE + UNDATED_CACHE`
- determine `prefix = sub + "/"` if `sub` is non-empty, else empty string
- scan all items whose `_web_path` starts with `prefix`
- for each matching path:
  - if remaining suffix contains `/`, treat the first path segment as a direct subfolder
  - otherwise treat the media item as a direct file in this folder

The page must therefore show:

- one card per direct child subfolder
- one photo tile per direct media file in the current folder

Subfolder cards:

- title = folder name only
- subtitle = total count of all items under that subtree
- URL = `/folder/<prefix + folder_name>`

Page presentation:

- if `sub` empty, breadcrumb = `Root`
- otherwise breadcrumb = `Root / ...` with slash-separated folder path
- banner = `hero-files.png`
- active tab = `file`

Note: even though the nav text says `EXPLORER`, the active-tab identifier in the working script is `file`. Preserve behavior or implement equivalent highlighting logic.

### 14.8 `/tags`

Behavior:

- load cache
- sort all tag names alphabetically
- create one card per tag
- card title = `#<tag>`
- card subtitle = `<count> items`
- card URL = `/tags/<tag>`
- card tags list should be empty on tag cards
- banner = `hero-tags.png`
- active tab = `tags`
- breadcrumb = `All Tags`

### 14.9 `/tags/<tag>`

Behavior:

- load cache
- lookup `GLOBAL_TAGS.get(tag, [])`
- render photo grid mode using those items in current stored order
- manifest id = `main_gallery`
- banner = `hero-tags.png`
- active tab = `tags`
- breadcrumb = `Tags / <tag>` where `Tags` links to `/tags`

### 14.10 `/media/<path:p>`

Serve the original file from disk.

Behavior:

- URL-decode `p`
- replace web slashes with OS path separators
- join with `ARCHIVE_ROOT`
- normalize path
- if file exists, return `send_file(...)`
- else return 404 text `Not Found`

### 14.11 `/thumbs/<f>`

Serve a thumbnail file directly from `THUMB_DIR`.

### 14.12 `/composite/<h>.jpg`

Serve a composite JPEG directly from `COMPOSITE_DIR`.

### 14.13 `/assets/<path:f>`

Serve banner images and other static UI assets from `ASSETS_DIR`.

---

## 16. HTML template requirements

A single `render_template_string(...)` template is sufficient for the baseline.

The template must be capable of rendering both:

- card-grid pages
- photo-grid pages

It must accept these conceptual inputs:

- `theme_color`
- `page_title`
- `active_tab`
- `banner_img`
- `breadcrumb`
- `cards` optional
- `photos` optional
- `manifests`

### 15.1 Card-grid rendering

When `cards` exists:

- render responsive card grid
- each card preview uses `/composite/<comp_hash>.jpg`
- card body shows title, subtitle, tag pills
- tag pills link to `/tags/<tag>`

### 15.2 Photo-grid rendering

When `photos` exists:

- render responsive photo grid
- each tile uses `/thumbs/<sha1>.jpg`
- each tile must attach its manifest index and SHA1 for lightbox and rotate

---

## 16. Front-end JavaScript requirements

Implement JavaScript sufficient to reproduce current interaction behavior.

### 16.1 Required functions

Equivalent logic is required for:

- `openLB(manifestId, index)`
- `closeLB()`
- `changeImg(delta)`
- `updateLB()`
- `toggleSidebar()`
- `handleCompositeClick(event, manifestId)`
- `handleContextMenu(event, sha1)`
- `rotateImage(degrees)`

### 16.2 Keyboard behavior

Global key handling:

- `Escape` closes the lightbox
- `ArrowRight` advances if lightbox active
- `ArrowLeft` goes back if lightbox active
- `e` toggles sidebar if lightbox active or globally, matching existing loose behavior

### 16.3 Context menu dismissal

Clicking elsewhere hides the custom context menu.

---

## 17. Sorting and ordering rules

These rules matter and must be reproduced.

- Base media order comes from SQL `ORDER BY final_dt DESC`
- Timeline decades sorted descending
- Timeline years sorted descending
- Timeline months sorted ascending by month code
- Undated folder groups sorted alphabetically
- Explorer subfolders sorted alphabetically
- Tags index sorted alphabetically by tag name
- Items inside any photo grid preserve their existing filtered order, which comes from the original query order unless regrouped differently

---

## 18. Error handling and tolerance

The baseline server should be forgiving.

- Missing assets should not crash the server
- Missing thumbnails should fall back where possible in composite generation
- Individual corrupt images in composites should be skipped, not fatal
- Missing media path on `/media/...` should return 404, not crash
- Missing SHA1 on rotate should return JSON error 404

---

## 19. Baseline acceptance criteria

A backend implementation is considered correct if all of the following are true.

### 19.1 Timeline

- `/timeline` shows one composite card per decade
- clicking a decade card opens the year list
- clicking a year card opens the month list
- clicking a month card opens a gallery of thumbnails
- clicking a composite tile opens the corresponding image in the lightbox

### 19.2 Undated

- `/undated` groups undated images by immediate parent folder
- clicking a group opens a gallery of those images

### 19.3 Explorer

- `/folder` shows direct child subfolders as cards and direct files as thumbnails
- drilling into subfolders works recursively

### 19.4 Tags

- `/tags` shows one card per available tag
- `/tags/<tag>` shows only images with that tag

### 19.5 Lightbox

- opens from any gallery
- arrow keys navigate
- ESC closes
- `E` toggles sidebar

### 19.6 Rotate

- right-click thumbnail or lightbox image shows rotate menu
- rotation updates original file on disk
- thumbnail refreshes immediately

### 19.7 Visual style

- dark theme
- sticky nav
- square composite cards
- banner image per major page
- cards and grids broadly match the current existing UI

---

## 20. Implementation notes for future specs

When extending this system later, the next spec should be written as a delta against this baseline, not as a replacement.

Any future feature spec should explicitly state one of:

- `NEW`: feature does not exist in baseline
- `CHANGE`: replace a baseline behavior
- `KEEP`: baseline behavior remains unchanged

That prevents the next coding LLM from accidentally rebuilding the app around aspirational notes instead of the working current system.



---

## 17. Acceptance tests for multi-select

1. Open a photo-grid page such as `/timeline/month/2005/12`; each thumbnail tile shows a checkbox-style selection affordance.
2. Click the checkbox on two thumbnails; both become visibly selected and the lightbox does not open.
3. Press `Escape`; both selections clear and no route change occurs.
4. Select two thumbnails, then right-click one of the selected tiles and choose Rotate Right; both thumbnails refresh after the operation.
5. Select two thumbnails, then right-click an unselected tile; the selected set still determines the batch target if two or more images are selected.
6. With no selection, right-click a thumbnail and rotate it; behavior matches the original single-image flow.
7. Navigate from one photo-grid page to another; the new page starts with no selection.
8. Open the lightbox and right-click the current lightbox image; single-image rotate still works as before.
9. Card-grid pages such as `/timeline` or `/timeline/decade/2000s` do not show selection checkboxes.
