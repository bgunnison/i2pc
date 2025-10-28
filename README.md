# i2pc — iPhone to PC Media Copier

A Python app for Windows that copies media from a USB‑connected iPhone to a local folder.
Features include sorting media by date and location. Media can also be sorted by category using AI. 
 

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
  - Do not store secrets here. Put your `OPENAI_API_KEY` in `private.json` (see below).
- Run the app:
  - `python src/i2pc.py`
  - Commands:
    - `copy`   — Copy all photos to `destination` (uses fast-skip per config).
    - `verify` — Recompute SHA-256 for each destination file and rebuild `verified.txt`.
    - `update` — Copy new or size-changed files; keep both by auto-numbering (Windows-style).
    - `date`   — Create/update a `date` directory containing files sorted by date.
    - `category` — Create a `category` directory grouping JPG/PNG/HEIC by content using an AI model. Requires `OPENAI_API_KEY` in `private.json`, and in `aicategorize.json` provide a JSON object containing the model and a system message. Each image is thumbnailed before sending. On timeout or API error, the photo is placed under `category/errored`; true AI unknowns go under `category/unknown`.
    - `location` — Create/update a `location` directory grouping files with GPS into `Country/State/City[/YYYY-MM]`.
    - `remdupe` — Delete duplicate files.
    - `iinfo *` — Show file info for all files on the iPhone, or choose a subset via `*.jpg` (for example).
    - `pcinfo *` — Show file info for all files in destination, or choose a subset via `*.jpg` (for example).
    - `verbose [on|off]` — Toggle verbose debug output (prints AI request metadata and payload sizes; never prints API key).
    - `quit`   — Exit.

## What “verification” means here

- After each file is copied via the Windows Shell (supports MTP), the app computes a SHA‑256 hash of the copied file and appends an entry to `verified.txt` as `<sha256>\t<relative_path>`.
- If the source item exposes its size, the app compares the destination file size to the source size to detect incomplete copies.

## Notes

- If `pywin32` is not installed, install it with `pip install pywin32`.
- The app uses Windows Shell COM and works only on Windows.
- If a filename collision occurs, Windows may auto‑rename the copied file (e.g., `IMG_0001 (2).JPG`). The app detects the new file by monitoring destination changes.

- Category view:
  - Put your OpenAI key in `private.json` as `{ "OPENAI_API_KEY": "sk-..." }`. Optionally set `"aicategory_timeout_s"` (seconds) in `config.json`.
  - In `aicategorize.json`, provide JSON with the model and a system message.
  - The model returns a single word category, the media is copied as a link to a directory.

- Destination scanning:
  - The app scans only the top-level files in your destination (media) directory when verifying or building views (date, location, category). It does not recurse into subdirectories. Generated view directories are excluded automatically.

## Secrets

- Create `private.json` at the repo root and add:
  - `{ "OPENAI_API_KEY": "sk-..." }`
- `private.json` is listed in `.gitignore` and is not committed.

## Reference views (browsable links)

  - Optionally set `"reference_link_type"` to one of `"hardlink"` (default), `"symlink"`, or `"copy"` (fallback when links fail).
- Notes:
  - Date is derived from EXIF `DateTimeOriginal` when possible (JPEG); otherwise file modified time is used.
  - Hardlinks require the destination and view to be on the same NTFS volume. If link creation fails, the app falls back to copying as a last resort.
