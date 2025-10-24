# i2pc — iPhone to PC Media Copier

A small Python app for Windows that copies media from a USB‑connected iPhone to a local folder, computes a hash for each copied file, and records the file path and hash in `verified.txt` under the destination directory. Configuration is via `config.json`.

## Usage

- Install Python 3.10+ on Windows.
- Install dependency:
  - `pip install -r requirements.txt`
- Connect your iPhone and unlock it. Approve the "Trust This Computer" prompt on the iPhone.
- Edit `config.json`:
  - `source_names`: Shell path segments to your iPhone photos root, e.g. `["Apple iPhone", "Internal Storage"]`.
  - `destination`: Local folder where files will be copied.
  - `include_patterns`: File globs to include (defaults provided).
  - `subfolders`: Recurse into subfolders.
  - `preserve_subfolders`: Keep the immediate parent folder name under destination.
  - `verified_file`: Name of the verification ledger (default `verified.txt`).
- Run the app (REPL):
  - `python src/i2pc.py`
  - Commands:
    - `copy`   — Copy all photos to `destination` (uses fast-skip per config).
    - `verify` — Recompute SHA-256 for each destination file and rebuild `verified.txt`.
    - `update` — Copy new or size-changed files; keep both by auto-numbering (Windows-style).
    - `date`   — Create a `date` directory containing files sorted by date.
    - `location` — Create a `location` directory grouping files with GPS into `Country/State/City[/YYYY-MM]`. Uses reverse geocoding; ignores files with no GPS.
    - `remdupe` — Delete duplicate files.
    - `iinfo *` — Show file info for all files on the iPhone, or choose a subset via `*.jpg` (for example).
    - `pcinfo *` — Show file info for all files in destination, or choose a subset via `*.jpg` (for example).
    - `quit`   — Exit.

## What “verification” means here

- After each file is copied via the Windows Shell (supports MTP), the app computes a SHA‑256 hash of the copied file and appends an entry to `verified.txt` as `<sha256>\t<relative_path>`.
- If the source item exposes its size, the app compares the destination file size to the source size to detect incomplete copies.
- Full byte‑for‑byte comparison against the MTP source stream is not performed (Shell COM does not expose an easy source stream). Size checks plus hashing of the destination provide practical assurance.

## Notes

- If `pywin32` is not installed, install it with `pip install pywin32`.
- The app uses Windows Shell COM and works only on Windows.
- If a filename collision occurs, Windows may auto‑rename the copied file (e.g., `IMG_0001 (2).JPG`). The app detects the new file by monitoring destination changes.

## verified.txt format

- One entry per line: `<sha256>\t<relative_path>`
- Relative paths are recorded using forward slashes.

## Reference views (browsable links)

- Enable optional reference directories under your destination by adding to `config.json`:
  - `"reference_views": ["date"]` to build a `date/` folder with subfolders `YYYY-MM-DD` containing links to your media.
  - Optionally set `"reference_link_type"` to one of `"hardlink"` (default), `"symlink"`, or `"copy"` (fallback when links fail).
- Notes:
  - Date is derived from EXIF `DateTimeOriginal` when possible (JPEG); otherwise file modified time is used.
  - Hardlinks require the destination and view to be on the same NTFS volume. If link creation fails, the app falls back to copying as a last resort.

## Faster skipping (avoid copying when unchanged)

- Add `"fast_skip"` to `config.json` to control pre-copy skip checks when a destination file already exists:
  - `"ledger"`: Skip if the file path is present in `verified.txt` (assumes previously verified file is still valid).
  - `"size"`: Skip if the iPhone reports a `Size` that matches the destination file size.
  - `"ledger_or_size"` (default): Use either of the above to skip early.
  - `"none"`: Always perform staged copy + hash comparison.
- Note: Windows MTP does not support hashing files in-place on the device; a staged copy is required for byte-level comparison. The above options reduce transfers when files are likely unchanged.
