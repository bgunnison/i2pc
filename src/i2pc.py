import os
import sys
import json
import time
import hashlib
import fnmatch
import argparse
import difflib
import signal
import shutil
import base64
import io
from datetime import datetime
from pathlib import Path
import re
import math
from typing import Optional

try:
    import requests  # type: ignore
except Exception:
    requests = None


class NavigationError(Exception):
    def __init__(self, segment: str, message: str | None = None):
        super().__init__(message or f"Could not find shell item segment: {segment}")
        self.segment = segment


class AbortedError(Exception):
    pass


def _norm_text(s: str) -> str:
    return str(s).strip().lower().replace("\u2019", "'").replace("\u2018", "'")


def load_config(path: Path) -> dict:
    """Load JSON config with friendly error messages.
    - UTF-8 with BOM tolerated
    - On JSON errors, prints a concise message with line/column and a code snippet
    """
    try:
        text = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        print(f"ERROR: Missing config file: {path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Cannot read {path}: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Build a small snippet with caret
        lines = text.splitlines()
        ln = e.lineno or 1
        col = e.colno or 1
        start = max(1, ln - 1)
        end = min(len(lines), ln + 1)
        print(f"ERROR: Invalid JSON in {path.name} at line {ln}, column {col}: {e.msg}", file=sys.stderr)
        for i in range(start, end + 1):
            try:
                prefix = ">>" if i == ln else "  "
                print(f"{prefix} {i:>4}: {lines[i-1]}", file=sys.stderr)
                if i == ln:
                    caret = " " * (col + 6) + "^"
                    print(caret, file=sys.stderr)
            except Exception:
                pass
        print("Hints: JSON does not allow trailing commas, comments, or single quotes.", file=sys.stderr)
        print("       Remove any trailing comma after the last item/object and try again.", file=sys.stderr)
        sys.exit(1)


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b''):
            h.update(chunk)
    return h.hexdigest()


def sha256_file_cancellable(path: Path, chunk_size: int = 4 * 1024 * 1024, should_abort=None) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while True:
            if callable(should_abort) and should_abort():
                raise AbortedError("Hashing aborted")
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_verified(verified_path: Path) -> dict:
    entries = {}
    if verified_path.exists():
        with verified_path.open('r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t', 1)
                if len(parts) == 2:
                    digest, relpath = parts
                    entries[relpath] = digest
    return entries


def append_verified(verified_path: Path, relpath: str, digest: str) -> None:
    with verified_path.open('a', encoding='utf-8') as f:
        f.write(f"{digest}\t{relpath}\n")


def any_match(name: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def get_shell():
    try:
        import win32com.client  # type: ignore
    except ImportError as e:
        print("ERROR: pywin32 is required. Please install with: pip install pywin32", file=sys.stderr)
        raise
    return win32com.client.Dispatch("Shell.Application")


def navigate_by_names(shell, names: list[str]):
    # Be resilient to different root locations and apostrophe variants
    def _traverse_from(root_folder, segs: list[str]):
        folder = root_folder
        for seg in segs:
            found = None
            items = folder.Items()
            for i in range(items.Count):
                item = items.Item(i)
                if _norm_text(getattr(item, 'Name', '')) == _norm_text(seg):
                    found = item
                    break
            if not found:
                # Raise to indicate which segment could not be found from this root
                raise NavigationError(seg)
            folder = found.GetFolder
        return folder

    # Allow users to include a leading "This PC" segment; drop it.
    synonyms_this_pc = {"this pc", "computer", "my computer"}
    cleaned = list(names)
    if cleaned:
        first = _norm_text(cleaned[0])
        if first in synonyms_this_pc or first in {"::{20d04fe0-3aea-1069-a2d8-08002b30309d}", "shell:mycomputerfolder"}:
            cleaned = cleaned[1:]

    roots = [
        # Prefer "This PC" root for MTP devices like iPhone
        shell.NameSpace('shell:MyComputerFolder'),
        # Fallback to Desktop
        shell.NameSpace(0),
    ]
    last_error = None
    for root in roots:
        try:
            result = _traverse_from(root, cleaned)
            if result is not None:
                return result
        except NavigationError as e:
            last_error = e
            continue
        except Exception as e:
            last_error = e
            continue
    if isinstance(last_error, NavigationError):
        # Surface the failing segment for friendlier messaging upstream
        raise last_error
    if last_error:
        raise RuntimeError(f"Could not navigate from any root: {last_error}")
    # If we get here, nothing matched
    raise NavigationError(cleaned[0] if cleaned else '(empty)')


def print_friendly_navigation_help(segment: str, names: list[str]) -> None:
    print("How to fix:", file=sys.stderr)
    print("- Unlock the iPhone and tap 'Trust This Computer'.", file=sys.stderr)
    print("- Ensure Apple Mobile Device Support is installed (iTunes/Apple Devices).", file=sys.stderr)
    print("- Verify it appears under 'This PC' in Explorer.", file=sys.stderr)
    print("- In PowerShell, list device names: Get-ChildItem 'shell:MyComputerFolder' | Select Name", file=sys.stderr)
    print("- Use the exact device name in config.json 'source_names'.", file=sys.stderr)
    print("- Note: Curly apostrophes (’) and straight (') are normalized by the app.", file=sys.stderr)
    if names:
        print(f"- Your configured path: {names}", file=sys.stderr)
    print(f"- Segment not found: {segment}", file=sys.stderr)


def list_child_names(shell_folder) -> list[str]:
    items = shell_folder.Items()
    return [str(items.Item(i).Name) for i in range(items.Count)]


def suggest_names(candidates: list[str], target: str) -> list[str]:
    return difflib.get_close_matches(target, candidates, n=5, cutoff=0.4)


def device_segment_present(shell, first_segment: str) -> bool:
    try:
        pc = shell.NameSpace('shell:MyComputerFolder')
        if not pc:
            return False
        names = list_child_names(pc)
        return any(_norm_text(n) == _norm_text(first_segment) for n in names)
    except Exception:
        return False


def list_files(shell_folder, include_patterns: list[str], recursive: bool):
    items = shell_folder.Items()
    # Snapshot items because the COM collection is dynamic
    snapshot = [items.Item(i) for i in range(items.Count)]
    for item in snapshot:
        try:
            is_folder = bool(getattr(item, 'IsFolder'))
        except Exception:
            is_folder = False
        if is_folder:
            if recursive:
                sub_folder = item.GetFolder
                yield from list_files(sub_folder, include_patterns, recursive)
            continue
        name = str(item.Name)
        if any_match(name, include_patterns):
            yield item


def diff_new_files(dir_path: Path, before: set[str]) -> list[str]:
    try:
        after = set(os.listdir(dir_path))
    except FileNotFoundError:
        after = set()
    new = sorted(after - before)
    return new


def _parse_dimensions(text: str) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    t = str(text)
    # Normalize separators: "4032 x 3024", "4032×3024", "4032 X 3024"
    for sep in ['×', 'x', 'X']:
        if sep in t:
            parts = t.replace(' ', '').split(sep)
            if len(parts) >= 2:
                try:
                    w = int(''.join(ch for ch in parts[0] if ch.isdigit()))
                    h = int(''.join(ch for ch in parts[1] if ch.isdigit()))
                    return w, h
                except Exception:
                    return None, None
    # Fallback: find two large integers in text
    digits = [int(''.join(c for c in tok if c.isdigit())) for tok in t.replace('x', ' ').replace('×', ' ').split() if any(c.isdigit() for c in tok)]
    if len(digits) >= 2:
        return digits[0], digits[1]
    return None, None


# -------------------- Reference Views (Hardlink Indexes) --------------------

def _is_media_file(name: str, patterns: list[str]) -> bool:
    return any_match(name, patterns)


def _iter_media_files(root: Path, patterns: list[str], exclude_dirs: set[Path]):
    root = root.resolve()
    ex_norm = {p.resolve() for p in exclude_dirs}
    for base, dirs, files in os.walk(root):
        base_path = Path(base)
        # Skip excluded roots
        if any(base_path == ex or str(base_path).startswith(str(ex) + os.sep) for ex in ex_norm):
            # Do not descend further
            dirs[:] = []
            continue
        # Skip i2pc temp folder
        dirs[:] = [d for d in dirs if d != '.i2pc_tmp']
        for fname in files:
            if fname == 'verified.txt':
                continue
            if _is_media_file(fname, patterns):
                yield base_path / fname


def _iter_media_files_shallow(root: Path, patterns: list[str], exclude_dirs: set[Path]):
    """Yield only files in the immediate root directory (no recursion),
    skipping excluded directories. Views are assumed to be excluded upstream.
    """
    root = root.resolve()
    try:
        for entry in root.iterdir():
            try:
                if entry.is_file() and _is_media_file(entry.name, patterns):
                    yield entry
            except Exception:
                continue
    except FileNotFoundError:
        return


def _get_date_key_for_path(path: Path) -> str:
    """Return YYYY-MM-DD for the media's date.
    Tries EXIF DateTimeOriginal via Pillow if available; falls back to mtime.
    """
    # Attempt EXIF via Pillow
    date_taken = None
    try:
        from PIL import Image  # type: ignore
        from PIL.ExifTags import TAGS  # type: ignore
        with Image.open(path) as im:
            exif = getattr(im, '_getexif', None)
            if callable(exif):
                data = exif() or {}
                for tag, value in data.items():
                    name = TAGS.get(tag, tag)
                    if name == 'DateTimeOriginal' and value:
                        # Formats like '2023:10:21 14:33:22'
                        try:
                            value = str(value)
                            value = value.replace('\x00', '').strip()
                            dt = datetime.strptime(value, '%Y:%m:%d %H:%M:%S')
                            date_taken = dt
                            break
                        except Exception:
                            pass
    except Exception:
        # Pillow not installed or image w/o EXIF; ignore
        pass

    if not date_taken:
        try:
            ts = path.stat().st_mtime
            date_taken = datetime.fromtimestamp(ts)
        except Exception:
            date_taken = datetime.now()
    return date_taken.strftime('%Y-%m-%d')


def _ensure_empty_dir(path: Path) -> None:
    if path.exists():
        # Safety: only delete if inside destination tree and not a symlink
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            try:
                path.unlink()
            except Exception:
                pass
    path.mkdir(parents=True, exist_ok=True)


def _unique_name(dst_dir: Path, base_name: str) -> Path:
    candidate = dst_dir / base_name
    if not candidate.exists():
        return candidate
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    i = 1
    while True:
        cand = dst_dir / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def build_reference_view_date(dest_root: Path, patterns: list[str], link_type: str = 'hardlink', view_name: str = 'date') -> None:
    view_root = dest_root / view_name
    # Rebuild view fresh to avoid stale entries
    _ensure_empty_dir(view_root)

    exclude = {view_root}
    # Create map date_key -> list[Path]
    buckets: dict[str, list[Path]] = {}
    for f in _iter_media_files_shallow(dest_root, patterns, exclude_dirs=exclude):
        key = _get_date_key_for_path(f)
        buckets.setdefault(key, []).append(f)

    # Materialize links
    for key, files in sorted(buckets.items()):
        out_dir = view_root / key
        out_dir.mkdir(parents=True, exist_ok=True)
        for src in files:
            dst = _unique_name(out_dir, src.name)
            try:
                if link_type == 'symlink':
                    os.symlink(src, dst)
                elif link_type == 'copy':
                    shutil.copy2(src, dst)
                else:
                    # default: hardlink
                    os.link(src, dst)
            except Exception:
                # Fallback to copy as last resort to keep view usable
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    # give up on this file
                    pass


def build_reference_views(dest_root: Path, views: list[str], patterns: list[str], link_type: str = 'hardlink') -> None:
    supported = {
        'date': build_reference_view_date,
    }
    for v in views:
        fn = supported.get(v.lower())
        if not fn:
            print(f"Reference view '{v}' not supported. Skipping.")
            continue
        print(f"Building reference view: {v}...")
        fn(dest_root, patterns, link_type=link_type, view_name=v)


# -------------------- Remove Duplicates (by name sans ' (n)') --------------------

_DUPLICATE_SUFFIX_RE = re.compile(r"^(?P<stem>.*?)(?: \((?P<num>\d+)\))?(?P<ext>\.[^.]+)?$", re.IGNORECASE)


def _normalize_dup_key(filename: str) -> str:
    """Normalize a filename by stripping a trailing ' (n)' before the extension, case-insensitive.
    Returns lowercased normalized name for grouping (stem + ext).
    """
    name = str(filename)
    m = _DUPLICATE_SUFFIX_RE.match(name)
    if not m:
        return name.lower()
    stem = m.group('stem') or ''
    ext = m.group('ext') or ''
    # If there was a numeric suffix, drop it
    norm = f"{stem}{ext}"
    return norm.lower()


def cmd_remdupe(cfg: dict) -> None:
    destination = Path(cfg.get('destination'))
    include_patterns = cfg.get('include_patterns') or ["*.jpg", "*.jpeg", "*.png", "*.heic", "*.mov", "*.mp4"]
    reference_views = cfg.get('reference_views', []) or []
    ensure_dir(destination)

    # Collect files, skipping reference views and temp dir
    exclude_dirs: set[Path] = set()
    for v in reference_views:
        if v:
            exclude_dirs.add(destination / v)
    exclude_dirs.add(destination / '.i2pc_tmp')

    groups: dict[str, list[Path]] = {}
    for f in _iter_media_files_shallow(destination, include_patterns, exclude_dirs):
        groups.setdefault(_normalize_dup_key(f.name), []).append(f)

    to_delete: list[Path] = []
    hashed_cache: dict[Path, str] = {}

    for key, paths in groups.items():
        if len(paths) < 2:
            continue
        # Identify canonical: prefer file without numeric suffix (num==0). If none, skip group.
        with_nums = []
        canonical = None
        for p in paths:
            m = _DUPLICATE_SUFFIX_RE.match(p.name)
            num = int(m.group('num')) if (m and m.group('num')) else 0
            if num == 0 and canonical is None:
                canonical = p
            else:
                with_nums.append((num, p))
        if not canonical:
            # No base name present; skip to avoid accidental deletions
            continue
        # Compute canonical hash once
        try:
            can_hash = hashed_cache.get(canonical)
            if not can_hash:
                can_hash = sha256_file_cancellable(canonical)
                hashed_cache[canonical] = can_hash
        except Exception as e:
            print(f"WARNING: failed to hash {canonical}: {e}", file=sys.stderr)
            continue
        # Compare and mark suffixed duplicates with equal hash
        for num, p in with_nums:
            try:
                h = hashed_cache.get(p)
                if not h:
                    h = sha256_file_cancellable(p)
                    hashed_cache[p] = h
                if h == can_hash:
                    to_delete.append(p)
            except Exception as e:
                print(f"WARNING: failed to hash {p}: {e}", file=sys.stderr)
                continue

    if not to_delete:
        print("remdupe: no duplicates found to delete.")
        return

    # Delete without prompt (default force)
    removed = 0
    for p in to_delete:
        try:
            rel = p.relative_to(destination).as_posix()
            p.unlink(missing_ok=True)  # type: ignore[arg-type]
            removed += 1
            print(f"deleted: {rel}")
        except Exception as e:
            print(f"ERROR deleting {p}: {e}", file=sys.stderr)

    print(f"remdupe: removed {removed} duplicate file(s).")


# -------------------- Location View (Country/State/City[/YYYY-MM]) --------------------

_US_STATE_TO_CODE = {
    'alabama': 'AL','alaska': 'AK','arizona': 'AZ','arkansas': 'AR','california': 'CA','colorado': 'CO','connecticut': 'CT','delaware': 'DE','florida': 'FL','georgia': 'GA','hawaii': 'HI','idaho': 'ID','illinois': 'IL','indiana': 'IN','iowa': 'IA','kansas': 'KS','kentucky': 'KY','louisiana': 'LA','maine': 'ME','maryland': 'MD','massachusetts': 'MA','michigan': 'MI','minnesota': 'MN','mississippi': 'MS','missouri': 'MO','montana': 'MT','nebraska': 'NE','nevada': 'NV','new hampshire': 'NH','new jersey': 'NJ','new mexico': 'NM','new york': 'NY','north carolina': 'NC','north dakota': 'ND','ohio': 'OH','oklahoma': 'OK','oregon': 'OR','pennsylvania': 'PA','rhode island': 'RI','south carolina': 'SC','south dakota': 'SD','tennessee': 'TN','texas': 'TX','utah': 'UT','vermont': 'VT','virginia': 'VA','washington': 'WA','west virginia': 'WV','wisconsin': 'WI','wyoming': 'WY','district of columbia': 'DC'
}


def _sanitize_segment(s: str) -> str:
    s = (s or '').strip().replace('/', '_').replace(' ', '_').replace(',', '')
    return s if s else 'Unknown'


def _exif_gps_for_local(path: Path):
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as im:
            exif = getattr(im, '_getexif', None)
            if not callable(exif):
                return None
            data = exif() or {}
            gps = data.get(34853) or data.get('GPSInfo')
            if not gps:
                return None
            def _rat_to_float(r):
                try:
                    return float(r[0]) / float(r[1]) if hasattr(r, '__len__') else float(r)
                except Exception:
                    try:
                        return float(r)
                    except Exception:
                        return None
            def _dms_to_deg(dms, ref):
                try:
                    d = _rat_to_float(dms[0]); m = _rat_to_float(dms[1]); s = _rat_to_float(dms[2])
                    if None in (d, m, s):
                        return None
                    sign = -1 if ref in ('S','W') else 1
                    return sign * (d + m/60.0 + s/3600.0)
                except Exception:
                    return None
            lat_ref = gps.get(1) or gps.get('GPSLatitudeRef')
            lat_dms = gps.get(2) or gps.get('GPSLatitude')
            lon_ref = gps.get(3) or gps.get('GPSLongitudeRef')
            lon_dms = gps.get(4) or gps.get('GPSLongitude')
            if lat_ref and lat_dms and lon_ref and lon_dms:
                lat = _dms_to_deg(lat_dms, lat_ref if isinstance(lat_ref, str) else str(lat_ref))
                lon = _dms_to_deg(lon_dms, lon_ref if isinstance(lon_ref, str) else str(lon_ref))
                if lat is not None and lon is not None:
                    return (lat, lon)
    except Exception:
        return None
    return None


def _reverse_geocode(lat: float, lon: float, geocoder, cache: dict, timeout_s: float = 5.0) -> tuple[str, str, str] | None:
    # Round to 3 decimals (~110m) to reuse results
    key = (round(lat, 3), round(lon, 3))
    if key in cache:
        return cache[key]
    try:
        loc = geocoder.reverse((lat, lon), language='en', exactly_one=True, timeout=timeout_s)
        if not loc or not getattr(loc, 'raw', None):
            cache[key] = None
            return None
        addr = loc.raw.get('address', {})
        cc = (addr.get('country_code') or '').upper()
        if not cc:
            cc = 'XX'
        state = addr.get('state') or ''
        if cc == 'US':
            code = _US_STATE_TO_CODE.get(state.strip().lower()) or state
        else:
            code = state
        city = addr.get('city') or addr.get('town') or addr.get('village') or addr.get('municipality') or addr.get('suburb') or ''
        parts = (
            cc,
            _sanitize_segment(code),
            _sanitize_segment(city),
        )
        res = parts
        cache[key] = res
        return res
    except Exception:
        cache[key] = None
        return None


def build_location_view(dest_root: Path, patterns: list[str], link_type: str = 'hardlink', view_name: str = 'location') -> None:
    view_root = dest_root / view_name
    _ensure_empty_dir(view_root)

    try:
        from geopy.geocoders import Nominatim  # type: ignore
    except Exception:
        print("ERROR: geopy is required for reverse geocoding. Install with: pip install geopy", file=sys.stderr)
        return
    geocoder = Nominatim(user_agent='i2pc/1.0', timeout=5)

    # Stream build as we go, restructuring per-location when needed
    include_patterns = patterns
    exclude_dirs: set[Path] = {view_root, dest_root / '.i2pc_tmp'}
    geo_cache: dict[tuple[float,float], tuple[str,str,str] | None] = {}
    # Track per-location state
    loc_state: dict[tuple[str,str,str], dict] = {}

    print("Scanning local files for GPS (location view)...")
    scanned = 0
    gps_found = 0
    unique_locs = 0
    for f in _iter_media_files_shallow(dest_root, include_patterns, exclude_dirs):
        scanned += 1
        # Only consider images that are likely to have EXIF GPS
        if f.suffix.lower() not in ('.jpg', '.jpeg', '.heic', '.heif', '.png'):
            continue
        gps = _exif_gps_for_local(f)
        if not gps:
            continue
        gps_found += 1
        lat, lon = gps
        # Reverse geocode with simple cache and per-request timeout
        key = (round(lat, 3), round(lon, 3))
        if key not in geo_cache:
            print(f"  geocoding {lat:.3f},{lon:.3f} (unique #{len(geo_cache)+1})...")
            loc = _reverse_geocode(lat, lon, geocoder, geo_cache, timeout_s=5.0)
            # Respect Nominatim rate limits (~1 req/sec)
            try:
                time.sleep(1.0)
            except Exception:
                pass
        else:
            loc = geo_cache.get(key)
        if not loc:
            continue
        if loc not in loc_state:
            base_dir = view_root / _sanitize_segment(loc[0]) / _sanitize_segment(loc[1]) / _sanitize_segment(loc[2])
            loc_state[loc] = {
                'base': base_dir,
                'days': set(),
                'use_month': False,
            }
            unique_locs += 1
        st = loc_state[loc]
        # Determine date keys
        day_key = _get_date_key_for_path(f)  # YYYY-MM-DD
        month_key = day_key[:7]
        # If this is a new distinct day and we already had a day, trigger restructure to month folders
        if not st['use_month']:
            if st['days'] and (day_key not in st['days']):
                # Restructure: move existing files in base into YYYY-MM subfolders
                base_dir = st['base']
                try:
                    for entry in list(base_dir.iterdir()) if base_dir.exists() else []:
                        if entry.is_file():
                            dkey = _get_date_key_for_path(entry)
                            mkey = dkey[:7]
                            out_dir = base_dir / mkey
                            out_dir.mkdir(parents=True, exist_ok=True)
                            new_path = _unique_name(out_dir, entry.name)
                            try:
                                os.replace(str(entry), str(new_path))
                            except Exception:
                                try:
                                    shutil.copy2(entry, new_path)
                                    entry.unlink(missing_ok=True)  # type: ignore[arg-type]
                                except Exception:
                                    pass
                except Exception:
                    pass
                st['use_month'] = True
        # Now place current file
        out_dir = st['base'] if not st['use_month'] else (st['base'] / month_key)
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = _unique_name(out_dir, f.name)
        try:
            if link_type == 'symlink':
                os.symlink(f, dst)
            elif link_type == 'copy':
                shutil.copy2(f, dst)
            else:
                os.link(f, dst)
        except Exception:
            try:
                shutil.copy2(f, dst)
            except Exception:
                pass
        st['days'].add(day_key)

        # Periodic progress update
        if scanned % 50 == 0:
            print(f"  progress: scanned {scanned}, with GPS {gps_found}, unique locations {unique_locs}")

    total_linked = 0
    try:
        # Sum files created under view_root
        for base, _, files in os.walk(view_root):
            total_linked += len(files)
    except Exception:
        pass
    print(f"Location view built: {unique_locs} location(s), {total_linked} entries.")


def cmd_location(cfg: dict):
    destination = Path(cfg.get('destination'))
    include_patterns = cfg.get('include_patterns') or ["*.jpg", "*.jpeg", "*.png", "*.heic", "*.mov", "*.mp4"]
    link_type = str(cfg.get('reference_link_type', 'hardlink')).lower()
    ensure_dir(destination)
    print("Building location reference view...")
    build_location_view(destination, include_patterns, link_type=link_type, view_name='location')
    print("Location view ready.")


# -------------------- Category View (AI-assisted, JPG only) --------------------

def _load_ai_category_inputs(repo_root: Path) -> tuple[Optional[str], Optional[str]]:
    """Strictly load model and system prompt from aicategorize.json.
    Expects JSON with at least: { "model": "...", "messages": [ {"role":"system","content":"..."}, ... ] }
    Returns (model, prompt) or (None, None) on failure.
    """
    path = (repo_root / 'aicategorize.json')
    if not path.exists():
        return None, None
    try:
        obj = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(obj, dict):
            return None, None
        model = obj.get('model') if isinstance(obj.get('model'), str) else None
        prompt = None
        msgs = obj.get('messages')
        if isinstance(msgs, list) and msgs:
            first = msgs[0]
            if isinstance(first, dict) and first.get('role') == 'system' and isinstance(first.get('content'), str):
                prompt = first['content']
        if model and prompt and model.strip() and prompt.strip():
            return model.strip(), prompt.strip()
        return None, None
    except Exception:
        return None, None


def _make_thumbnail_bytes(path: Path, max_size: int = 256) -> Optional[bytes]:
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as im:
            im.thumbnail((max_size, max_size))
            out = io.BytesIO()
            im.convert('RGB').save(out, format='JPEG', quality=80)
            return out.getvalue()
    except Exception:
        return None


def _call_openai_category(api_key: str, model: str, prompt: str, thumb_jpeg: bytes, timeout_s: float = 20.0, max_retries: int = 5, proxies: Optional[dict] = None, verbose: bool = False) -> tuple[Optional[str], Optional[str]]:
    if not requests:
        return None, "requests-not-installed"
    url = 'https://api.openai.com/v1/chat/completions'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    b64 = base64.b64encode(thumb_jpeg).decode('ascii')
    body = {
        'model': model,
        'temperature': 0,
        'messages': [
            { 'role': 'system', 'content': prompt },
            { 'role': 'user', 'content': [
                { 'type': 'text', 'text': 'Categorize this photo.' },
                { 'type': 'image_url', 'image_url': { 'url': f'data:image/jpeg;base64,{b64}' } }
            ]}
        ]
    }
    # Verbose preview of request (safe; no API key)
    if verbose:
        try:
            body_json = json.dumps(body)
            body_len = len(body_json)
            print(f"DEBUG AI POST /v1/chat/completions | model={model} temp=0 body_bytes={body_len}", flush=True)
            print(f"DEBUG system_len={len(prompt or '')}", flush=True)
            # Print full system prompt for inspection (may be long)
            print(f"DEBUG system_full: {(prompt or '').replace('\r','')}", flush=True)
            print(f"DEBUG image_b64_len={len(b64)} proxies={'yes' if proxies else 'no'}", flush=True)
        except Exception:
            pass
    delay = 1.0
    last_err = None
    for attempt in range(1, max_retries+1):
        try:
            resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=timeout_s, proxies=proxies)
            if resp.status_code == 200:
                data = resp.json()
                try:
                    content = data['choices'][0]['message']['content']
                    if not content:
                        return None, "empty-response"
                    # Accept plain text category or a small JSON object {"category":"..."}
                    cat = None
                    try:
                        obj = json.loads(content)
                        if isinstance(obj, dict) and 'category' in obj and isinstance(obj['category'], str):
                            cat = obj['category']
                    except Exception:
                        cat = content.strip()
                    if isinstance(cat, str) and cat.strip():
                        return cat.strip(), None
                except Exception:
                    return None, "parse-error"
            elif resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            else:
                # Short friendly message
                try:
                    j = resp.json()
                    msg = j.get('error', {}).get('message') if isinstance(j, dict) else None
                except Exception:
                    msg = None
                short = (msg or resp.text or '').strip().replace('\n', ' ')
                return None, f"HTTP {resp.status_code}: {short[:200]}"
        except requests.exceptions.Timeout:
            last_err = "timeout"
            continue
        except Exception as e:
            last_err = f"{e.__class__.__name__}: {str(e)[:200]}"
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
    return None, (f"retry-exhausted: {last_err}" if last_err else "retry-exhausted")


def _call_openai_category_batch(api_key: str, model: str, prompt: str, items: list[tuple[str, bytes]], timeout_s: float = 40.0, max_retries: int = 5, proxies: Optional[dict] = None, verbose: bool = False) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Send a single chat.completions request with multiple thumbnails.
    items: list of (id, thumb_jpeg)
    Returns (labels_by_id, error)
    """
    if not requests:
        return None, "requests-not-installed"
    url = 'https://api.openai.com/v1/chat/completions'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    user_content = []
    for iid, tb in items:
        b64 = base64.b64encode(tb).decode('ascii')
        user_content.append({ 'type': 'text', 'text': f'id:{iid}' })
        user_content.append({ 'type': 'image_url', 'image_url': { 'url': f'data:image/jpeg;base64,{b64}' } })
    body = {
        'model': model,
        'temperature': 0,
        'messages': [
            { 'role': 'system', 'content': prompt },
            { 'role': 'user', 'content': user_content }
        ]
    }
    if verbose:
        try:
            body_json = json.dumps(body)
            print(f"DEBUG AI BATCH POST | model={model} items={len(items)} body_bytes={len(body_json)}", flush=True)
            print(f"DEBUG system_len={len(prompt or '')}", flush=True)
        except Exception:
            pass
    delay = 1.0
    last_err = None
    for attempt in range(1, max_retries+1):
        try:
            resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=timeout_s, proxies=proxies)
            if resp.status_code == 200:
                data = resp.json()
                try:
                    content = data['choices'][0]['message']['content']
                    if not content:
                        return None, "empty-response"
                    labels: dict[str, str] = {}
                    # Expect JSON like {"results":[{"id":"a001","label":"word"}, ...]}
                    parsed = None
                    try:
                        parsed = json.loads(content)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict) and isinstance(parsed.get('results'), list):
                        for r in parsed['results']:
                            if isinstance(r, dict) and isinstance(r.get('id'), str) and isinstance(r.get('label'), str):
                                labels[r['id']] = r['label']
                        return labels, None
                    # Fallback simple parse: split plain text by lines "id:label"
                    lines = [ln.strip() for ln in str(content).splitlines() if ln.strip()]
                    for ln in lines:
                        if ':' in ln:
                            iid, lab = ln.split(':', 1)
                            labels[iid.strip()] = lab.strip()
                    if labels:
                        return labels, None
                    return None, "unrecognized-response"
                except Exception:
                    return None, "parse-error"
            elif resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            else:
                try:
                    j = resp.json()
                    msg = j.get('error', {}).get('message') if isinstance(j, dict) else None
                except Exception:
                    msg = None
                short = (msg or resp.text or '').strip().replace('\n', ' ')
                return None, f"HTTP {resp.status_code}: {short[:200]}"
        except requests.exceptions.Timeout:
            last_err = "timeout"
            continue
        except Exception as e:
            last_err = f"{e.__class__.__name__}: {str(e)[:200]}"
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
    return None, (f"retry-exhausted: {last_err}" if last_err else "retry-exhausted")


def _test_openai_api_connectivity(api_key: str, timeout_s: float = 5.0, proxies: Optional[dict] = None) -> tuple[bool, Optional[str]]:
    """Quick connectivity/auth test to OpenAI API. Returns (ok, reason)."""
    if not requests:
        return False, "requests-not-installed"
    url = 'https://api.openai.com/v1/models'
    headers = { 'Authorization': f'Bearer {api_key}' }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout_s, proxies=proxies)
        if resp.status_code == 200:
            return True, None
        try:
            j = resp.json() if resp.content else {}
            msg = (j.get('error', {}) or {}).get('message') if isinstance(j, dict) else None
        except Exception:
            msg = None
        short = (msg or resp.text or '').strip().replace('\n',' ')
        return False, f"HTTP {resp.status_code}: {short[:200]}"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except Exception as e:
        return False, f"{e.__class__.__name__}: {str(e)[:200]}"


def _sanitize_category(name: str) -> str:
    s = (name or '').strip()
    if not s:
        return 'unknown'
    s = s.replace('/', '_').replace('\\', '_').replace(':', '_')
    s = s.replace(' ', '_').replace(',', '')
    return s[:80]


def cmd_category(cfg: dict, query: str = ""):
    destination = Path(cfg.get('destination'))
    link_type = str(cfg.get('reference_link_type', 'hardlink')).lower()
    repo_root = Path.cwd()
    # Strict inputs: model and prompt from aicategorize.json; key from config OPENAI_API_KEY
    model, prompt = _load_ai_category_inputs(repo_root)
    # Read private key only from private.json (not from config.json)
    def _load_private_key(root: Path) -> Optional[str]:
        p = root / 'private.json'
        if not p.exists():
            return None
        try:
            obj = json.loads(p.read_text(encoding='utf-8-sig'))
            if isinstance(obj, dict):
                key = obj.get('OPENAI_API_KEY')
                if isinstance(key, str) and key.strip():
                    return key.strip()
        except Exception:
            return None
        return None
    api_key = _load_private_key(repo_root)
    ensure_dir(destination)
    if not api_key:
        print("ERROR: OPENAI_API_KEY missing in private.json", file=sys.stderr)
        return
    if not requests:
        print("ERROR: requests package not installed. pip install requests", file=sys.stderr)
        return
    if not (model and prompt):
        print("ERROR: aicategorize.json missing or invalid (require {\"model\":\"...\", \"messages\":[{\"role\":\"system\",\"content\":\"...\"}]})", file=sys.stderr)
        return

    view_root = destination / 'category'
    _ensure_empty_dir(view_root)

    include_patterns = ["*.jpg", "*.jpeg"]
    exclude_dirs: set[Path] = {view_root, destination / '.i2pc_tmp'}

    # Optional HTTPS proxy from config
    proxy_url = cfg.get('https_proxy')
    proxies = {'https': str(proxy_url)} if proxy_url else None
    # Connection preflight with friendly status
    line = f"Using model: {model}, testing connection"
    if proxies:
        line += " via proxy"
    print(line + "...", end="", flush=True)
    ok, reason = _test_openai_api_connectivity(api_key, timeout_s=5.0, proxies=proxies)
    if not ok:
        print("")
        print(f"ERROR: Cannot reach OpenAI API ({reason}). Check VPN/proxy/firewall or set HTTPS_PROXY. Aborting.", file=sys.stderr)
        return
    print(", connection OK...")
    count = 0
    unknown = 0
    errors = 0
    print("Scanning local JPGs for categorization...")
    # Collect files
    files: list[Path] = [f for f in _iter_media_files_shallow(destination, include_patterns, exclude_dirs)]
    # Optional pattern filtering like pcinfo/info (glob on filename)
    q = (query or "").strip()
    if q:
        pats = [p.strip() for p in q.split() if p.strip()]
        if pats:
            filtered = []
            for f in files:
                name = f.name
                if any(fnmatch.fnmatch(name, p) for p in pats):
                    filtered.append(f)
            files = filtered
            if VERBOSE:
                print(f"DEBUG filter: patterns={pats} matched={len(files)}", flush=True)
    batch_size = int(cfg.get('aicategory_batch_size', 8))
    for i in range(0, len(files)):
        pass
    # Process batches
    for start in range(0, len(files), batch_size):
        batch = files[start:start+batch_size]
        items: list[tuple[str, bytes]] = []
        id_to_path: dict[str, Path] = {}
        id_to_rel: dict[str, str] = {}
        # Build thumbnails
        for idx, f in enumerate(batch, start=1):
            iid = f"a{start+idx:03d}"
            rel = f.relative_to(destination).as_posix()
            try:
                tb = _make_thumbnail_bytes(f, max_size=256)
            except Exception:
                tb = None
            if VERBOSE:
                print(f"DEBUG file: {rel} thumb_bytes={len(tb) if tb else 0}", flush=True)
            if not tb:
                # mark as unknown later
                id_to_path[iid] = f
                id_to_rel[iid] = rel
                continue
            items.append((iid, tb))
            id_to_path[iid] = f
            id_to_rel[iid] = rel
        if not items:
            # Nothing usable in this batch; print unknowns for those with no thumbs
            for iid, rel in id_to_rel.items():
                count += 1
                unknown += 1
                errors += 1
                print(f"[{count}] {rel} -> unknown (thumb-failed)")
            continue
        # Call batch API
        tmo = cfg.get('aicategory_timeout_s')
        timeout_s = float(tmo) if tmo is not None else 20.0
        labels, err = _call_openai_category_batch(api_key, model, prompt, items, timeout_s=timeout_s, proxies=proxies, verbose=VERBOSE)
        # Assign results
        for iid, rel in id_to_rel.items():
            f = id_to_path[iid]
            label = labels.get(iid) if labels else None
            cat = (label or '').strip() if label else ''
            if not cat:
                unknown += 1
                cat_dir = view_root / 'unknown'
            else:
                cat_dir = view_root / _sanitize_category(cat)
            cat_dir.mkdir(parents=True, exist_ok=True)
            dst = _unique_name(cat_dir, f.name)
            try:
                if link_type == 'symlink':
                    os.symlink(f, dst)
                elif link_type == 'copy':
                    shutil.copy2(f, dst)
                else:
                    os.link(f, dst)
            except Exception:
                try:
                    shutil.copy2(f, dst)
                except Exception:
                    pass
            count += 1
            if not cat:
                errors += 1
                print(f"[{count}] {rel} -> unknown ({err or 'no-label'})")
            else:
                print(f"[{count}] {rel} -> {cat}")

        if count % 25 == 0:
            print(f"  progress: categorized {count} files (unknown {unknown}, errors {errors})...")
    print(f"Category view ready. total={count} unknown={unknown} errors={errors}")


# -------------------- Update Mode Copy (keep both on size difference) --------------------

def copy_single_update(shell, parent_folder, item, dest_root: Path, preserve_subfolders: bool, skip_existing: bool, progress=None, should_abort=None, unknown_behavior: str = 'skip', size_source: str = 'auto', size_tolerance_bytes: int = 8192):
    rel_dir = ''
    try:
        parent = item.GetFolder
        rel_dir = str(parent.Title)
    except Exception:
        rel_dir = ''

    dest_dir = dest_root / rel_dir if (preserve_subfolders and rel_dir) else dest_root
    ensure_dir(dest_dir)

    name = str(item.Name)
    target_path = dest_dir / name

    # Determine source size if possible
    src_size, src_exact = get_item_size_best(parent_folder, item, source_mode=size_source)

    final_target = target_path
    if skip_existing and target_path.exists():
        try:
            dest_size = target_path.stat().st_size
        except Exception:
            dest_size = None
        if src_size is not None and dest_size is not None:
            if src_exact:
                if dest_size == src_size:
                    return 'skipped-same-size', target_path
            else:
                if abs(dest_size - src_size) <= max(0, int(size_tolerance_bytes)):
                    return 'skipped-same-size', target_path
        if src_size is None:
            # If source size is unavailable, honor unknown_behavior
            ub = (unknown_behavior or 'skip').lower()
            if ub == 'copy_unique':
                final_target = _unique_name(dest_dir, name)
            elif ub == 'copy_replace':
                final_target = target_path
            else:
                return 'skipped-unknown-size', target_path
        # Size differs: keep both with unique name
        final_target = _unique_name(dest_dir, name)

    # Stage copy
    stage_root = dest_dir / '.i2pc_tmp'
    ensure_dir(stage_root)
    # Ensure staging dir is empty to avoid UI or collisions
    try:
        for entry in stage_root.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                pass
    except Exception:
        pass
    before = set()
    dest_folder = get_shell().NameSpace(str(stage_root))
    if dest_folder is None:
        raise RuntimeError(f"Could not open destination folder via Shell: {stage_root}")
    FOF_RENAMEONCOLLISION = 0x0008
    FOF_NOCONFIRMMKDIR = 0x0200
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_NOERRORUI = 0x0400
    flags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR | FOF_RENAMEONCOLLISION

    if callable(should_abort) and should_abort():
        raise AbortedError("Aborted before update copy stage")
    dest_folder.CopyHere(item, flags)

    # Wait for new file in stage
    start = time.time()
    new_name = None
    while time.time() - start < 300:
        if callable(should_abort) and should_abort():
            raise AbortedError("Aborted during update stage wait")
        new_files = diff_new_files(stage_root, before)
        if new_files:
            if name in new_files:
                new_name = name
            else:
                candidates = [stage_root / n for n in new_files]
                candidates = [p for p in candidates if p.is_file()]
                if candidates:
                    newest = max(candidates, key=lambda p: p.stat().st_mtime)
                    new_name = newest.name
            if new_name:
                break
        time.sleep(0.3)
    if not new_name:
        raise RuntimeError("Could not determine staged file for update mode")

    staged_file = stage_root / new_name

    # Optional size wait
    if src_size is not None and src_exact:
        wait_start = time.time()
        while time.time() - wait_start < 300:
            if callable(should_abort) and should_abort():
                try:
                    if staged_file.exists():
                        staged_file.unlink()
                except Exception:
                    pass
                raise AbortedError("Aborted during update size wait")
            try:
                if staged_file.stat().st_size >= src_size and staged_file.stat().st_size > 0:
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.3)

    # Finalize move to unique or target
    try:
        os.replace(str(staged_file), str(final_target))
    except Exception:
        try:
            if final_target.exists():
                # In rare race, choose another name
                final_target = _unique_name(final_target.parent, final_target.name)
            os.rename(str(staged_file), str(final_target))
        except Exception as e:
            raise RuntimeError(f"Failed to finalize update copy to {final_target}: {e}")

    # Determine copied status
    if not target_path.exists():
        status = 'copied-new'
    elif final_target.name != target_path.name:
        status = 'copied-unique'
    else:
        status = 'copied-replaced'
    return (status, final_target)


# -------------------- Verify (rebuild verification ledger) --------------------

def verify_destination(dest_root: Path, verified_path: Path, patterns: list[str], exclude_view_names: list[str] | None = None, should_abort=None) -> tuple[int, int]:
    exclude_dirs: set[Path] = set()
    if exclude_view_names:
        for v in exclude_view_names:
            if v:
                exclude_dirs.add(dest_root / v)
    exclude_dirs.add(dest_root / '.i2pc_tmp')

    tmp_path = verified_path.with_suffix('.tmp')
    written = 0
    errors = 0
    try:
        with tmp_path.open('w', encoding='utf-8') as out:
            for f in _iter_media_files_shallow(dest_root, patterns, exclude_dirs):
                if callable(should_abort) and should_abort():
                    raise AbortedError("Aborted during verify")
                digest = sha256_file_cancellable(f, should_abort=should_abort)
                rel = f.relative_to(dest_root).as_posix()
                out.write(f"{digest}\t{rel}\n")
                written += 1
        os.replace(str(tmp_path), str(verified_path))
    except AbortedError:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        raise
    except Exception:
        errors += 1
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
    return written, errors


# -------------------- REPL Commands --------------------

def _init_shell_safely():
    shell = get_shell()
    return shell


def _navigate_source(shell, source_names: list[str]):
    try:
        src_folder = navigate_by_names(shell, source_names)
    except NavigationError as e:
        # Build a user-friendly error with suggestions
        seg = getattr(e, 'segment', str(e))
        pc = None
        try:
            pc = shell.NameSpace('shell:MyComputerFolder')
        except Exception:
            pc = None
        pc_names = []
        try:
            pc_names = list_child_names(pc) if pc else []
        except Exception:
            pc_names = []
        suggestions = []
        try:
            suggestions = suggest_names(pc_names, seg) if (pc_names and seg) else []
        except Exception:
            suggestions = []
        msg_lines = [
            f"Could not find '{seg}' under 'This PC'.",
            "Tips:",
            "- Unlock the iPhone and tap 'Trust This Computer'.",
            "- Open File Explorer and confirm the exact device name.",
            "- Update config.json 'source_names' to match the exact device name.",
        ]
        if source_names:
            msg_lines.append(f"- Configured path: {source_names}")
        if pc_names:
            msg_lines.append(f"- Visible under 'This PC': {pc_names}")
        if suggestions:
            msg_lines.append(f"- Closest matches: {suggestions}")
        raise RuntimeError("\n".join(msg_lines))
    # Single concise success line
    try:
        joined = " ".join([str(s) for s in (source_names or [])])
        if joined:
            print(f"Found {joined}")
    except Exception:
        pass
    return src_folder


def cmd_copy(cfg: dict):
    destination = Path(cfg.get('destination'))
    include_patterns = cfg.get('include_patterns') or ["*.jpg", "*.jpeg", "*.png", "*.heic", "*.mov", "*.mp4"]
    preserve_subfolders = bool(cfg.get('preserve_subfolders', True))
    recursive = bool(cfg.get('subfolders', True))
    skip_existing = bool(cfg.get('skip_existing', True))
    verified_filename = cfg.get('verified_file', 'verified.txt')
    fast_skip = str(cfg.get('fast_skip', 'ledger_or_size')).lower()
    source_names = cfg.get('source_names') or []

    ensure_dir(destination)
    verified_path = destination / verified_filename
    verified_set = read_verified(verified_path)

    aborted = {'flag': False}
    def _sigint(signum, frame):
        aborted['flag'] = True
        print("Ctrl-C: stopping after current step...", file=sys.stderr)
    try:
        signal.signal(signal.SIGINT, _sigint)
    except Exception:
        pass

    shell = _init_shell_safely()
    src_folder = _navigate_source(shell, source_names)
    print("Scanning source files...")
    copied = skipped = errors = 0
    idx = 0
    update_unknown = str(cfg.get('update_unknown_size', 'skip')).lower()
    size_source = str(cfg.get('update_size_source', 'exact')).lower()
    size_tol = int(cfg.get('update_size_tolerance_bytes', 8192))
    for parent_folder, item in list_files_with_parent(src_folder, include_patterns, recursive):
        idx += 1
        if aborted['flag']:
            break
        name = str(item.Name)
        def _progress(stage, info=None):
            if stage == 'finalize-start':
                print(f"[{idx}] Copying...")
            elif stage == 'finalize-finished':
                target = (info or {}).get('target')
                print(f"[{idx}] Copy complete: {Path(target).name if target else ''}")
        try:
            status, dest_file = copy_single(
                shell, item, destination, preserve_subfolders, skip_existing,
                verified_set, verified_path, progress=_progress,
                should_abort=(lambda: aborted['flag']), fast_skip=fast_skip)
            if status in ('copied','replaced'):
                copied += 1
            else:
                skipped += 1
        except AbortedError:
            break
        except Exception as e:
            errors += 1
            print(f"[{idx}] ERROR: {e}", file=sys.stderr)
    print(f"Done. copied={copied} skipped={skipped} errors={errors}")


def cmd_update(cfg: dict):
    destination = Path(cfg.get('destination'))
    include_patterns = cfg.get('include_patterns') or ["*.jpg", "*.jpeg", "*.png", "*.heic", "*.mov", "*.mp4"]
    preserve_subfolders = bool(cfg.get('preserve_subfolders', True))
    recursive = bool(cfg.get('subfolders', True))
    skip_existing = True
    source_names = cfg.get('source_names') or []

    ensure_dir(destination)

    aborted = {'flag': False}
    def _sigint(signum, frame):
        aborted['flag'] = True
        print("Ctrl-C: stopping after current step...", file=sys.stderr)
    try:
        signal.signal(signal.SIGINT, _sigint)
    except Exception:
        pass

    shell = _init_shell_safely()
    src_folder = _navigate_source(shell, source_names)
    print("Scanning source files...")
    copied = skipped = errors = 0
    idx = 0
    update_unknown = str(cfg.get('update_unknown_size', 'skip')).lower()
    size_source = str(cfg.get('update_size_source', 'auto')).lower()
    size_tol = int(cfg.get('update_size_tolerance_bytes', 8192))
    for parent_folder, item in list_files_with_parent(src_folder, include_patterns, recursive):
        idx += 1
        if aborted['flag']:
            break
        name = str(item.Name)
        try:
            # Pre-check metadata against existing file if present to avoid any copy
            # Determine destination path consistent with preserve_subfolders=False (update uses same rule as copy_single_update)
            # We only consider immediate parent title when preserve_subfolders=True
            preserve_subfolders = bool(cfg.get('preserve_subfolders', True))
            rel_dir = ''
            try:
                parent = item.GetFolder
                rel_dir = str(parent.Title)
            except Exception:
                rel_dir = ''
            dest_dir = destination / rel_dir if (preserve_subfolders and rel_dir) else destination
            ensure_dir(dest_dir)
            target_path = dest_dir / name
            if target_path.exists():
                dev_meta = get_device_metadata(parent_folder, item)
                pc_meta = get_pc_metadata(target_path)
                if metadata_considered_same(dev_meta, pc_meta):
                    skipped += 1
                    print(f"[{idx}] iPhone: {name}, same by metadata")
                    continue
            status, dest_file = copy_single_update(
                shell, parent_folder, item, destination, preserve_subfolders, skip_existing,
                should_abort=(lambda: aborted['flag']), unknown_behavior=update_unknown,
                size_source=size_source, size_tolerance_bytes=size_tol)
            if status in ('copied-new','copied-unique','copied-replaced'):
                copied += 1
                if status == 'copied-new':
                    print(f"[{idx}] iPhone: {name}, new -> copied")
                elif status == 'copied-unique':
                    print(f"[{idx}] iPhone: {name}, -> copied as {Path(dest_file).name}")
                else:
                    print(f"[{idx}] iPhone: {name}, replaced")
            else:
                skipped += 1
                if status == 'skipped-same-size':
                    print(f"[{idx}] iPhone: {name}, same as local")
                elif status == 'skipped-unknown-size':
                    print(f"[{idx}] iPhone: {name}, size unavailable; skipped")
                else:
                    print(f"[{idx}] iPhone: {name}, skipped")
        except AbortedError:
            break
        except Exception as e:
            errors += 1
            print(f"[{idx}] ERROR: {e}", file=sys.stderr)
    print(f"Update done. copied={copied} skipped={skipped} errors={errors}")


def cmd_verify(cfg: dict):
    destination = Path(cfg.get('destination'))
    include_patterns = cfg.get('include_patterns') or ["*.jpg", "*.jpeg", "*.png", "*.heic", "*.mov", "*.mp4"]
    verified_filename = cfg.get('verified_file', 'verified.txt')
    reference_views = cfg.get('reference_views', []) or []
    ensure_dir(destination)
    verified_path = destination / verified_filename

    aborted = {'flag': False}
    def _sigint(signum, frame):
        aborted['flag'] = True
        print("Ctrl-C: stopping after current step...", file=sys.stderr)
    try:
        signal.signal(signal.SIGINT, _sigint)
    except Exception:
        pass

    print("Verifying destination files (rebuilding ledger)...")
    written, errors = verify_destination(destination, verified_path, include_patterns, exclude_view_names=reference_views, should_abort=(lambda: aborted['flag']))
    print(f"Verify complete. entries={written} errors={errors}")


def cmd_date(cfg: dict):
    destination = Path(cfg.get('destination'))
    include_patterns = cfg.get('include_patterns') or ["*.jpg", "*.jpeg", "*.png", "*.heic", "*.mov", "*.mp4"]
    link_type = str(cfg.get('reference_link_type', 'hardlink')).lower()
    ensure_dir(destination)
    print("Building date reference view...")
    build_reference_views(destination, ['date'], include_patterns, link_type=link_type)
    print("Date view ready.")


def repl(cfg: dict):
    print("i2pc REPL. Commands: copy, verify, update, date, location, category, remdupe, iinfo, pcinfo, verbose, help, quit")
    while True:
        try:
            raw = input("> ")
            line = raw.strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print("^C")
            break
        if not line:
            continue
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in ('quit','exit','q'): break
        if cmd in ('help','h','?'):
            print("Commands:\n  copy       - Copy all photos\n  verify     - Rebuild verification ledger\n  update     - Copy new or size-changed files (keep both)\n  date       - Create a date directory containing files sorted by date.\n  location   - Create a location directory grouping by Country/State/City and, when needed, by YYYY-MM. (GPS-only; local files)\n  category * - Categorize JPGs matching a pattern (e.g., IMG_1234.JPG or *.jpg); builds the category directory\n  remdupe    - Remove duplicate files\n  iinfo *    - Show file info for all files on the iPhone, or choose a subset via *.jpg (for example)\n  pcinfo *   - Show file info for all files in destination, or choose a subset via *.jpg (for example)\n  verbose [on|off] - Toggle verbose debug output (shows AI request metadata; never prints API key)\n  quit       - Exit")
            continue
        try:
            if cmd == 'copy':
                cmd_copy(cfg)
            elif cmd == 'verify':
                cmd_verify(cfg)
            elif cmd == 'update':
                cmd_update(cfg)
            elif cmd == 'date':
                cmd_date(cfg)
            elif cmd == 'location':
                cmd_location(cfg)
            elif cmd == 'category':
                cmd_category(cfg, arg)
            elif cmd == 'remdupe':
                cmd_remdupe(cfg)
            elif cmd in ('info','iinfo'):
                cmd_info(cfg, arg)
            elif cmd == 'pcinfo':
                cmd_pcinfo(cfg, arg)
            elif cmd == 'verbose':
                global VERBOSE
                a = (arg or '').strip().lower()
                if a in ('on','1','true','yes'):
                    VERBOSE = True
                elif a in ('off','0','false','no'):
                    VERBOSE = False
                else:
                    VERBOSE = not VERBOSE
                print(f"Verbose is now {'ON' if VERBOSE else 'OFF'}")
            
            else:
                print("Unknown command. Type 'help' for options.")
        except AbortedError:
            print("Operation aborted.")
        except KeyboardInterrupt:
            print("Operation interrupted.")
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)


def wait_for_copy_completion(src_item, dest_dir: Path, timeout_s: int = 300) -> Path:
    size_expected = None
    try:
        _sz = int(getattr(src_item, 'Size'))
        if _sz > 0:
            size_expected = _sz
    except Exception:
        size_expected = None

    started = time.time()
    prev_new = set()
    while time.time() - started < timeout_s:
        new_files = set(diff_new_files(dest_dir, before=set()))  # list all; we'll pick latest mtime
        if new_files:
            # Choose the most recently modified file as candidate
            candidates = [dest_dir / n for n in new_files]
            candidates = [p for p in candidates if p.is_file()]
            if candidates:
                candidate = max(candidates, key=lambda p: p.stat().st_mtime)
                # If size is known, wait for it to reach expected size or stabilize
                if size_expected is not None:
                    # wait until size == expected
                    try:
                        if candidate.stat().st_size >= size_expected:
                            return candidate
                    except FileNotFoundError:
                        pass
                else:
                    # size unknown: wait for 1 second of no growth
                    try:
                        size1 = candidate.stat().st_size
                        time.sleep(1.0)
                        size2 = candidate.stat().st_size
                        if size1 == size2 and size2 > 0:
                            return candidate
                    except FileNotFoundError:
                        pass
        time.sleep(0.3)
    raise TimeoutError("Timed out waiting for copy to finish")


def copy_single(shell, item, dest_root: Path, preserve_subfolders: bool, skip_existing: bool, verified_set: dict, verified_path: Path, progress=None, should_abort=None, fast_skip: str = 'none'):
    # Determine destination subdir based on parent folder display path
    rel_dir = ''
    try:
        parent = item.GetFolder
        # Build a relative dir name chain starting from the source root's immediate children
        # We only take the immediate parent folder name for simplicity
        rel_dir = str(parent.Title)
    except Exception:
        rel_dir = ''

    dest_dir = dest_root / rel_dir if (preserve_subfolders and rel_dir) else dest_root
    ensure_dir(dest_dir)
    if callable(should_abort) and should_abort():
        raise AbortedError("Aborted before starting copy")
    if callable(progress):
        try:
            progress('dest-prepared', {'dest_dir': str(dest_dir)})
        except Exception:
            pass

    # Target file path and relative key
    candidate_rel = (Path(rel_dir) / str(item.Name)).as_posix() if (preserve_subfolders and rel_dir) else Path(str(item.Name)).as_posix()
    target_path = dest_dir / Path(str(item.Name))
    if callable(should_abort) and should_abort():
        raise AbortedError("Aborted before staging")
    if callable(progress):
        try:
            progress('dest-check', {'exists': target_path.exists(), 'target': str(target_path)})
        except Exception:
            pass

    # Fast-skip checks before any data transfer
    # Rel path for ledger and target path for file checks
    rel_for_log = (Path(rel_dir) / target_path.name).as_posix() if (preserve_subfolders and rel_dir) else target_path.name
    if skip_existing and target_path.exists():
        # Option 1: If already verified before, trust ledger and skip
        if fast_skip in ('ledger', 'ledger_or_size') and rel_for_log in verified_set:
            return 'skipped-identical', target_path
        # Option 2: If source exposes size and matches destination file size, skip
        if fast_skip in ('size', 'ledger_or_size'):
            try:
                _sz = int(getattr(item, 'Size'))
                if _sz > 0 and target_path.stat().st_size == _sz:
                    return 'skipped-identical', target_path
            except Exception:
                pass

    # Use a staging directory to copy source content before deciding overwrite
    stage_root = dest_dir / '.i2pc_tmp'
    ensure_dir(stage_root)
    # Ensure staging dir is empty to avoid UI or collisions
    try:
        for entry in stage_root.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                pass
    except Exception:
        pass
    # Pre-snapshot now empty
    before = set()
    dest_folder = get_shell().NameSpace(str(stage_root))
    if dest_folder is None:
        raise RuntimeError(f"Could not open destination folder via Shell: {stage_root}")

    FOF_RENAMEONCOLLISION = 0x0008
    FOF_NOCONFIRMMKDIR = 0x0200
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_NOERRORUI = 0x0400
    flags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR | FOF_RENAMEONCOLLISION

    if callable(should_abort) and should_abort():
        raise AbortedError("Aborted before stage fetch")
    if callable(progress):
        try:
            progress('stage-fetch-start', {'stage_dir': str(stage_root)})
        except Exception:
            pass
    dest_folder.CopyHere(item, flags)

    # Identify the new file and wait for completion
    # Try for up to 5 minutes
    start = time.time()
    new_name = None
    while time.time() - start < 300:
        if callable(should_abort) and should_abort():
            raise AbortedError("Aborted while waiting for staged file")
        new_files = diff_new_files(stage_root, before)
        if new_files:
            # Prefer the original name if present, otherwise pick newest
            if str(item.Name) in new_files:
                new_name = str(item.Name)
            else:
                # pick most recent
                candidates = [stage_root / n for n in new_files]
                candidates = [p for p in candidates if p.is_file()]
                if candidates:
                    newest = max(candidates, key=lambda p: p.stat().st_mtime)
                    new_name = newest.name
            if new_name:
                break
        time.sleep(0.3)
    if not new_name:
        raise RuntimeError("Could not determine copied file name.")

    staged_file = stage_root / new_name
    if callable(should_abort) and should_abort():
        # Attempt cleanup of partially staged file
        try:
            if staged_file.exists():
                staged_file.unlink()
        except Exception:
            pass
        raise AbortedError("Aborted after staging")
    if callable(progress):
        try:
            progress('stage-fetch-finished', {'staged_file': str(staged_file)})
        except Exception:
            pass

    # If source size known, wait for sizes to match
    try:
        _sz = int(getattr(item, 'Size'))
        src_size = _sz if _sz > 0 else None
    except Exception:
        src_size = None

    if src_size is not None:
        if callable(should_abort) and should_abort():
            # Cleanup and abort
            try:
                if staged_file.exists():
                    staged_file.unlink()
            except Exception:
                pass
            raise AbortedError("Aborted during size wait")
        if callable(progress):
            try:
                progress('size-verify-start', {'expected_bytes': src_size})
            except Exception:
                pass
        wait_start = time.time()
        while time.time() - wait_start < 300:
            try:
                if staged_file.stat().st_size >= src_size and staged_file.stat().st_size > 0:
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.3)
        if callable(progress):
            try:
                progress('size-verified', {'bytes': staged_file.stat().st_size})
            except Exception:
                pass

    # Compute hash and write verified
    if callable(should_abort) and should_abort():
        try:
            if staged_file.exists():
                staged_file.unlink()
        except Exception:
            pass
        raise AbortedError("Aborted before hashing")
    if callable(progress):
        try:
            progress('hashing-start', {'path': str(dest_file)})
        except Exception:
            pass
    digest = sha256_file_cancellable(staged_file, should_abort=should_abort)
    if callable(progress):
        try:
            progress('hashing-done', {'sha256': digest})
        except Exception:
            pass
    # rel_for_log is already computed above

    # Determine existing digest for target (prefer ledger, else compute if file exists)
    existing_digest = None
    if rel_for_log in verified_set:
        existing_digest = verified_set[rel_for_log]
    elif target_path.exists():
        # compute current destination digest
        if callable(should_abort) and should_abort():
            raise AbortedError("Aborted before existing digest compute")
        if callable(progress):
            try:
                progress('hashing-start', {'path': str(target_path)})
            except Exception:
                pass
        existing_digest = sha256_file_cancellable(target_path, should_abort=should_abort)

    # Compare and decide overwrite
    if existing_digest is not None and existing_digest == digest:
        # Identical; remove staged copy and ensure ledger entry exists
        try:
            staged_file.unlink(missing_ok=True)  # type: ignore[arg-type]
        except TypeError:
            # Python < 3.8 compatibility for missing_ok
            try:
                if staged_file.exists():
                    staged_file.unlink()
            except Exception:
                pass
        if rel_for_log not in verified_set:
            if callable(progress):
                try:
                    progress('verify-record', {'entry': rel_for_log})
                except Exception:
                    pass
            append_verified(verified_path, rel_for_log, digest)
            verified_set[rel_for_log] = digest
        return 'skipped-identical', target_path

    # Move/replace into final destination
    had_existing = target_path.exists()
    if callable(should_abort) and should_abort():
        # Do not finalize; remove staged copy
        try:
            if staged_file.exists():
                staged_file.unlink()
        except Exception:
            pass
        raise AbortedError("Aborted before finalize")
    if callable(progress):
        try:
            progress('finalize-start', {'target': str(target_path)})
        except Exception:
            pass
    try:
        os.replace(str(staged_file), str(target_path))
    except Exception:
        # Fallback: try rename then remove old
        try:
            if target_path.exists():
                target_path.unlink()
            os.rename(str(staged_file), str(target_path))
        except Exception as e:
            raise RuntimeError(f"Failed to finalize copy to {target_path}: {e}")

    # Record verification for final file
    if rel_for_log not in verified_set or verified_set.get(rel_for_log) != digest:
        if callable(progress):
            try:
                progress('verify-record', {'entry': rel_for_log})
            except Exception:
                pass
        append_verified(verified_path, rel_for_log, digest)
        verified_set[rel_for_log] = digest

    # Final size verification if possible (on finalized file)
    if src_size is not None:
        dest_size = target_path.stat().st_size
        if dest_size != src_size:
            raise RuntimeError(f"Size mismatch after copy: src={src_size} dest={dest_size} for {target_path}")

    if callable(progress):
        try:
            progress('finalize-finished', {'target': str(target_path)})
        except Exception:
            pass
    return ('replaced' if had_existing else 'copied'), target_path


def main():
    parser = argparse.ArgumentParser(description='iPhone to PC Media Copier (i2pc) — REPL')
    args, _ = parser.parse_known_args()
    repo_root = Path.cwd()
    cfg_path = repo_root / 'config.json'
    if not cfg_path.exists():
        print(f"Missing config.json at {cfg_path}. Please create it from the template in README (see Usage).")
        sys.exit(1)

    cfg = load_config(cfg_path)

    

    # Start interactive REPL
    repl(cfg)


def list_files_with_parent(shell_folder, include_patterns: list[str], recursive: bool):
    """Yield tuples of (parent_folder, item) to enable property lookups via GetDetailsOf."""
    items = shell_folder.Items()
    snapshot = [items.Item(i) for i in range(items.Count)]
    for item in snapshot:
        try:
            is_folder = bool(getattr(item, 'IsFolder'))
        except Exception:
            is_folder = False
        if is_folder:
            if recursive:
                sub_folder = item.GetFolder
                yield from list_files_with_parent(sub_folder, include_patterns, recursive)
            continue
        name = str(item.Name)
        if any_match(name, include_patterns):
            yield (shell_folder, item)


def _find_details_index(folder, header: str) -> int | None:
    # Enumerate columns to find header name
    try:
        for i in range(0, 512):
            h = folder.GetDetailsOf(None, i)
            if not h:
                # Some shells return empty for many columns; continue
                continue
            if str(h).strip().lower() == header.strip().lower():
                return i
    except Exception:
        pass
    return None


def _parse_size_text(s: str) -> int | None:
    if not s:
        return None
    txt = str(s).replace('\xa0', ' ').strip()
    # Common forms: "1.5 MB", "1,234 KB", "123 bytes", "1,234,567"
    units = {
        'b': 1,
        'byte': 1,
        'bytes': 1,
        'kb': 1024,
        'mb': 1024**2,
        'gb': 1024**3,
        'tb': 1024**4,
    }
    parts = txt.lower().split()
    try:
        if len(parts) == 1:
            # Pure number with separators
            num = parts[0].replace(',', '').replace('.', '')
            return int(num)
        if len(parts) >= 2:
            num_str = parts[0].replace(',', '.').replace(' ', '')
            unit = parts[1].strip().rstrip('.')
            factor = units.get(unit)
            if factor:
                val = float(num_str)
                return int(val * factor)
            # Fallback when like "1,234 bytes"
            if unit in ('byte', 'bytes'):
                num = parts[0].replace(',', '').replace('.', '')
                return int(num)
    except Exception:
        return None
    return None


def get_item_size_bytes(parent_folder, item) -> int | None:
    """Return exact file size in bytes if available via item.Size; otherwise None.
    We avoid parsing localized 'Size' strings to prevent false mismatches.
    """
    try:
        _sz = int(getattr(item, 'Size'))
        if _sz > 0:
            return _sz
    except Exception:
        pass
    return None


def _parse_size_text_with_exactness(s: str) -> tuple[int | None, bool]:
    """Parse a localized Size string. Returns (bytes, is_exact).
    Exact when it states bytes explicitly; approximate for KB/MB/GB values.
    """
    if not s:
        return None, False
    txt = str(s).replace('\xa0', ' ').strip()
    parts = txt.lower().split()
    try:
        if len(parts) == 1:
            # Plain integer text — treat as exact bytes
            num = parts[0].replace(',', '').replace('.', '')
            return int(num), True
        if len(parts) >= 2:
            num_raw = parts[0]
            unit = parts[1].strip().rstrip('.')
            # Explicit bytes
            if unit in ('byte', 'bytes'):
                num = num_raw.replace(',', '').replace('.', '')
                return int(num), True
            # Approximate binary units
            units = {'kb': 1024, 'mb': 1024**2, 'gb': 1024**3, 'tb': 1024**4}
            factor = units.get(unit)
            if factor:
                num_str = num_raw.replace(',', '.').replace(' ', '')
                val = float(num_str)
                return int(val * factor), False
    except Exception:
        return None, False
    return None, False


def get_item_size_best(parent_folder, item, source_mode: str = 'auto') -> tuple[int | None, bool]:
    """Return (size_bytes, is_exact) using configured source mode.
    source_mode: 'exact' -> only item.Size; 'details' -> only details; 'auto' -> prefer exact, else details.
    """
    # exact bytes via attribute
    exact = get_item_size_bytes(parent_folder, item)
    if source_mode in ('exact', 'auto') and exact:
        return exact, True
    if source_mode in ('details', 'auto'):
        try:
            idx = _find_details_index(parent_folder, 'Size')
            if idx is not None:
                val = parent_folder.GetDetailsOf(item, idx)
                size_bytes, is_exact = _parse_size_text_with_exactness(val)
                if size_bytes:
                    return size_bytes, bool(is_exact)
        except Exception:
            pass
    return None, False


def enumerate_item_details(parent_folder, item, max_cols: int = 256) -> list[tuple[str, str]]:
    """Enumerate Shell detail columns for an item, returning non-empty (header, value) pairs.
    Includes 'Name' and 'Folder' when available.
    """
    details: list[tuple[str, str]] = []
    try:
        name = str(getattr(item, 'Name', ''))
        if name:
            details.append(('Name', name))
    except Exception:
        pass
    try:
        folder_title = str(getattr(parent_folder, 'Title', ''))
        if folder_title:
            details.append(('Folder', folder_title))
    except Exception:
        pass
    # Enumerate headers and values
    try:
        for i in range(0, max_cols):
            try:
                header = parent_folder.GetDetailsOf(None, i)
                hdr = str(header).strip() if header is not None else ''
                if not hdr:
                    continue
                value = parent_folder.GetDetailsOf(item, i)
                val = str(value).strip() if value is not None else ''
                if val:
                    # Avoid duplicating Name/Folder
                    if hdr.lower() in ('name', 'folder'):  # localized duplicates may still appear
                        continue
                    details.append((hdr, val))
            except Exception:
                continue
    except Exception:
        pass
    return details


def get_device_metadata(parent_folder, item) -> dict:
    """Return metadata from device via Shell details: name, ext, date_taken_str, dims (w,h)."""
    meta = {
        'name': '', 'ext': '', 'date_taken_str': '', 'width': None, 'height': None
    }
    try:
        nm = str(getattr(item, 'Name', ''))
        meta['name'] = nm
        meta['ext'] = Path(nm).suffix.lower()
    except Exception:
        pass
    # scan details
    details = enumerate_item_details(parent_folder, item, max_cols=128)
    for k, v in details:
        kl = k.strip().lower()
        if 'date taken' in kl or ('date' in kl and 'taken' in kl):
            meta['date_taken_str'] = v
        elif 'dimension' in kl:
            w, h = _parse_dimensions(v)
            meta['width'], meta['height'] = w, h
    return meta


def get_pc_metadata(path: Path) -> dict:
    meta = {'name': path.name, 'ext': path.suffix.lower(), 'date_taken': None, 'width': None, 'height': None}
    try:
        from PIL import Image  # type: ignore
        from PIL.ExifTags import TAGS  # type: ignore
        with Image.open(path) as im:
            try:
                meta['width'], meta['height'] = im.size
            except Exception:
                pass
            exif = getattr(im, '_getexif', None)
            if callable(exif):
                data = exif() or {}
                # Map EXIF tag id for DateTimeOriginal
                date_val = None
                subsec = None
                for tag, value in data.items():
                    name = TAGS.get(tag, tag)
                    if name == 'DateTimeOriginal' and value:
                        date_val = str(value).replace('\x00', '').strip()
                    elif name == 'SubsecTimeOriginal' and value:
                        subsec = str(value).strip()
                if date_val:
                    if subsec:
                        meta['date_taken'] = f"{date_val}.{subsec}"
                    else:
                        meta['date_taken'] = date_val
    except Exception:
        # Pillow not installed or not an image; leave partials
        pass
    return meta


def metadata_considered_same(device_meta: dict, pc_meta: dict) -> bool:
    """Strict comparison with no fallbacks/approximations.
    - Requires normalized extension match AND exact dimensions match on both sides.
    - If any required field is missing, returns False (do not skip).
    """
    def norm_ext(e: str) -> str:
        e = (e or '').lower()
        if e in ('.jpeg', '.jpg'):
            return '.jpg'
        if e in ('.heic', '.heif'):
            return '.heic'
        return e
    if norm_ext(device_meta.get('ext')) != norm_ext(pc_meta.get('ext')):
        return False
    dw, dh = device_meta.get('width'), device_meta.get('height')
    pw, ph = pc_meta.get('width'), pc_meta.get('height')
    if None in (dw, dh, pw, ph):
        return False
    return int(dw) == int(pw) and int(dh) == int(ph)


def cmd_info(cfg: dict, query: str):
    if not query:
        print("Please provide a file name. Examples: info IMG_1234.JPG, info IMG_1234, info *.MOV, info *.JPG")
        return
    destination = Path(cfg.get('destination'))
    include_patterns = cfg.get('include_patterns') or ["*.jpg", "*.jpeg", "*.png", "*.heic", "*.mov", "*.mp4"]
    recursive = bool(cfg.get('subfolders', True))
    source_names = cfg.get('source_names') or []

    shell = _init_shell_safely()
    src_folder = _navigate_source(shell, source_names)

    # Search strategy:
    # - If pattern contains * or ?, use glob.
    # - Otherwise, do exact name match (case-insensitive), or exact stem match if no extension given.
    # Simple pattern handling: one token, unlimited results
    q = query.strip()
    use_glob = ('*' in q) or ('?' in q)
    q_low = q.lower()
    q_has_ext = ('.' in q and not q.endswith('.'))
    matched = 0
    max_matches = None
    print(f"Searching for: {q}")
    for parent_folder, item in list_files_with_parent(src_folder, include_patterns, recursive):
        try:
            name = str(getattr(item, 'Name', ''))
        except Exception:
            name = ''
        name_low = name.lower()
        stem_low = Path(name).stem.lower()
        is_match = False
        if use_glob:
            try:
                is_match = fnmatch.fnmatch(name, q)
            except Exception:
                is_match = False
        else:
            if q_has_ext:
                is_match = (name_low == q_low)
            else:
                is_match = (stem_low == q_low) or (name_low == q_low)
        if not is_match:
            continue
        matched += 1
        print(f"[{matched}] {name}")
        details = enumerate_item_details(parent_folder, item)
        # Build a case-insensitive map of first occurrences
        detail_map_ci: dict[str, tuple[str,str]] = {}
        for k, v in details:
            kl = k.lower()
            if kl not in detail_map_ci:
                detail_map_ci[kl] = (k, v)
        indent = "      "
        printed_dims = False
        printed_gps = False
        def _print_exact(key_ci: str):
            kv = detail_map_ci.get(key_ci)
            if kv:
                print(f"{indent}{kv[0]}: {kv[1]}")
        def _print_first_by_contains(substr_ci: str, label: str | None = None):
            for kl, (origk, val) in detail_map_ci.items():
                if substr_ci in kl:
                    print(f"{indent}{(label or origk)}: {val}")
                    return
        # Ordered output per user preference
        _print_exact('folder')
        # Type can be labeled 'Type' or 'Item type'
        if 'type' in detail_map_ci:
            _print_exact('type')
        elif 'item type' in detail_map_ci:
            print(f"{indent}{detail_map_ci['item type'][0]}: {detail_map_ci['item type'][1]}")
        _print_exact('size')
        # Dimensions: try 'Dimensions', else any pair of width/height fields commonly exposed
        if detail_map_ci.get('dimensions'):
            _print_exact('dimensions')
            printed_dims = True
        else:
            # try common alternatives from Shell columns
            for wkey, hkey in (
                ('frame width', 'frame height'),
                ('image width', 'image height'),
                ('width', 'height'),
            ):
                if wkey in detail_map_ci and hkey in detail_map_ci:
                    print(f"{indent}Dimensions: {detail_map_ci[wkey][1]} x {detail_map_ci[hkey][1]}")
                    printed_dims = True
                    break
        _print_exact('name')
        # Modified/Created can appear as 'Date modified'/'Date created'
        if 'modified' in detail_map_ci:
            _print_exact('modified')
        else:
            _print_first_by_contains('modified', 'Modified')
        if 'created' in detail_map_ci:
            _print_exact('created')
        else:
            _print_first_by_contains('created', 'Created')
        # GPS fields (try explicit GPS labels, then fall back to generic latitude/longitude if present)
        if 'gps latitude' in detail_map_ci or any('latitude' in k for k in detail_map_ci.keys()):
            if 'gps latitude' in detail_map_ci:
                _print_exact('gps latitude')
            else:
                _print_first_by_contains('latitude', 'GPS Latitude')
            printed_gps = True
        if 'gps longitude' in detail_map_ci or any('longitude' in k for k in detail_map_ci.keys()):
            if 'gps longitude' in detail_map_ci:
                _print_exact('gps longitude')
            else:
                _print_first_by_contains('longitude', 'GPS Longitude')
            printed_gps = True
        if 'gps altitude' in detail_map_ci:
            _print_exact('gps altitude')
            printed_gps = True

        # No fallbacks: rely solely on device Shell details unless explicitly requested
        _print_exact('supported')
        _print_exact('title')
        if max_matches is not None and matched >= max_matches:
            break
    if matched == 0:
        print("No exact match found.")
        if not use_glob and len(q) >= 3:
            # Offer up to 5 prefix suggestions
            print("Suggestions (prefix match):")
            sugg = 0
            for parent_folder, item in list_files_with_parent(src_folder, include_patterns, recursive):
                try:
                    name = str(getattr(item, 'Name', ''))
                except Exception:
                    name = ''
                name_low = name.lower()
                stem_low = Path(name).stem.lower()
                if name_low.startswith(q_low) or stem_low.startswith(q_low):
                    print(f"- {name}")
                    sugg += 1
                    if sugg >= 5:
                        break
            if sugg == 0:
                print("(none)")
        print("Tip: Use wildcards to broaden search, e.g., info *.JPG or info *6528*.")


def cmd_pcinfo(cfg: dict, query: str):
    destination = Path(cfg.get('destination'))
    include_patterns = cfg.get('include_patterns') or ["*.jpg", "*.jpeg", "*.png", "*.heic", "*.mov", "*.mp4"]
    reference_views = cfg.get('reference_views', []) or []
    recursive = bool(cfg.get('subfolders', True))
    ensure_dir(destination)

    # Build exclude set for generated/reference dirs
    exclude_dirs: set[Path] = set()
    for v in reference_views:
        if v:
            exclude_dirs.add(destination / v)
    exclude_dirs.add(destination / '.i2pc_tmp')

    q = (query or '*').strip()
    use_glob = ('*' in q) or ('?' in q)
    q_low = q.lower()
    q_has_ext = ('.' in q and not q.endswith('.'))

    matched = 0
    print(f"Searching destination for: {q}")
    for f in _iter_media_files_shallow(destination, include_patterns, exclude_dirs):
        name = f.name
        name_low = name.lower()
        stem_low = f.stem.lower()
        is_match = False
        if use_glob:
            try:
                is_match = fnmatch.fnmatch(name, q)
            except Exception:
                is_match = False
        else:
            if q in ('', '*'):
                is_match = True
            elif q_has_ext:
                is_match = (name_low == q_low)
            else:
                is_match = (stem_low == q_low) or (name_low == q_low)
        if not is_match:
            continue
        matched += 1
        rel = f.relative_to(destination).as_posix()
        print(f"[{matched}] {rel}")
        indent = "      "
        # Basic file info
        try:
            st = f.stat()
            print(f"{indent}Size: {st.st_size} bytes")
            mtime = datetime.fromtimestamp(st.st_mtime)
            print(f"{indent}Modified: {mtime.isoformat(sep=' ', timespec='seconds')}")
        except Exception:
            pass
        # Dimensions for images if Pillow is available
        try:
            from PIL import Image  # type: ignore
            if f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.heic', '.heif'):
                with Image.open(f) as im:
                    w, h = im.size
                    print(f"{indent}Dimensions: {w} x {h}")
        except Exception:
            pass
    if matched == 0:
        print("No matches.")

if __name__ == '__main__':
    main()
