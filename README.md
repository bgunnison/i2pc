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
- Run the app:
  - `python -m src.i2pc` or `python src/i2pc.py`

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
