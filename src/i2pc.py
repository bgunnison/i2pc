import os
import sys
import json
import time
import hashlib
import fnmatch
import argparse
import difflib
import signal
from pathlib import Path


class NavigationError(Exception):
    def __init__(self, segment: str, message: str | None = None):
        super().__init__(message or f"Could not find shell item segment: {segment}")
        self.segment = segment


def _norm_text(s: str) -> str:
    return str(s).strip().lower().replace("\u2019", "'").replace("\u2018", "'")


def load_config(path: Path) -> dict:
    with path.open('r', encoding='utf-8') as f:
        cfg = json.load(f)
    return cfg


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b''):
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


def copy_single(shell, item, dest_root: Path, preserve_subfolders: bool, skip_existing: bool, verified_set: dict, verified_path: Path, progress=None):
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
    if callable(progress):
        try:
            progress('dest-prepared', {'dest_dir': str(dest_dir)})
        except Exception:
            pass

    # Target file path and relative key
    candidate_rel = (Path(rel_dir) / str(item.Name)).as_posix() if (preserve_subfolders and rel_dir) else Path(str(item.Name)).as_posix()
    target_path = dest_dir / Path(str(item.Name))
    if callable(progress):
        try:
            progress('dest-check', {'exists': target_path.exists(), 'target': str(target_path)})
        except Exception:
            pass

    # Use a staging directory to copy source content before deciding overwrite
    stage_root = dest_dir / '.i2pc_tmp'
    ensure_dir(stage_root)
    # Pre-snapshot staging dir
    before = set(os.listdir(stage_root)) if stage_root.exists() else set()
    dest_folder = get_shell().NameSpace(str(stage_root))
    if dest_folder is None:
        raise RuntimeError(f"Could not open destination folder via Shell: {stage_root}")

    FOF_NOCONFIRMMKDIR = 0x0200
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_NOERRORUI = 0x0400
    flags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR

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
    if callable(progress):
        try:
            progress('hashing-start', {'path': str(dest_file)})
        except Exception:
            pass
    digest = sha256_file(staged_file)
    if callable(progress):
        try:
            progress('hashing-done', {'sha256': digest})
        except Exception:
            pass
    rel_for_log = (Path(rel_dir) / target_path.name).as_posix() if (preserve_subfolders and rel_dir) else target_path.name

    # Determine existing digest for target (prefer ledger, else compute if file exists)
    existing_digest = None
    if rel_for_log in verified_set:
        existing_digest = verified_set[rel_for_log]
    elif target_path.exists():
        # compute current destination digest
        if callable(progress):
            try:
                progress('hashing-start', {'path': str(target_path)})
            except Exception:
                pass
        existing_digest = sha256_file(target_path)

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
    parser = argparse.ArgumentParser(description='iPhone to PC Media Copier (i2pc)')
    parser.add_argument('--probe', action='store_true', help='Probe and print available names under This PC; do not copy')
    parser.add_argument('--list-only', action='store_true', help='List matched source files without copying')
    args, _ = parser.parse_known_args()
    repo_root = Path.cwd()
    cfg_path = repo_root / 'config.json'
    if not cfg_path.exists():
        print(f"Missing config.json at {cfg_path}. Please create it from the template in README (see Usage).")
        sys.exit(1)

    cfg = load_config(cfg_path)
    source_names = cfg.get('source_names') or []
    include_patterns = cfg.get('include_patterns') or ["*.jpg", "*.jpeg", "*.png", "*.heic", "*.mov", "*.mp4"]
    destination = Path(cfg.get('destination'))
    preserve_subfolders = bool(cfg.get('preserve_subfolders', True))
    recursive = bool(cfg.get('subfolders', True))
    skip_existing = bool(cfg.get('skip_existing', True))
    verified_filename = cfg.get('verified_file', 'verified.txt')

    if not source_names or not isinstance(source_names, list):
        print("config.json must include 'source_names': a list like ['Apple iPhone','Internal Storage','DCIM']", file=sys.stderr)
        sys.exit(2)

    ensure_dir(destination)
    verified_path = destination / verified_filename
    verified_set = read_verified(verified_path)

    # Log destination at start (stdout)
    print(f"Destination: {destination}")

    try:
        shell = get_shell()
    except ImportError:
        print("ERROR: Missing dependency 'pywin32'.", file=sys.stderr)
        print("How to fix:", file=sys.stderr)
        print("- Run: pip install -r requirements.txt", file=sys.stderr)
        print("- Or: pip install pywin32", file=sys.stderr)
        sys.exit(4)

    # Handle Ctrl-C gracefully
    aborted = {'flag': False}
    def _sigint_handler(signum, frame):
        aborted['flag'] = True
        try:
            print("Interrupted by user (Ctrl-C). Finishing current operation...", file=sys.stderr, flush=True)
        except Exception:
            pass
    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except Exception:
        pass
    # Optional probe to help users discover correct names
    if args.probe:
        pc = shell.NameSpace('shell:MyComputerFolder')
        pc_names = list_child_names(pc) if pc else []
        print("Under 'This PC':")
        for n in pc_names:
            print(f"- {n}")
        # If config has a first segment, offer suggestions
        if source_names:
            first = source_names[0]
            sugg = suggest_names(pc_names, first)
            if sugg:
                print(f"Suggestions for '{first}': {sugg}")
        # Exit after probe
        return

    try:
        src_folder = navigate_by_names(shell, source_names)
    except NavigationError as e:
        print(f"Failed to navigate to source via Shell namespace: {e}", file=sys.stderr)
        # Provide context and suggestions from current root
        pc = shell.NameSpace('shell:MyComputerFolder')
        pc_names = list_child_names(pc) if pc else []
        if pc_names:
            maybe = suggest_names(pc_names, getattr(e, 'segment', str(e)))
            if maybe:
                print(f"Did you mean one of: {maybe}", file=sys.stderr)
        print_friendly_navigation_help(getattr(e, 'segment', str(e)), source_names)
        sys.exit(3)
    except Exception as e:
        print(f"Failed to navigate to source via Shell namespace: {e}", file=sys.stderr)
        print("How to fix:", file=sys.stderr)
        print("- Ensure the iPhone is connected, unlocked, and trusted.", file=sys.stderr)
        print("- Verify it appears under 'This PC' and try again.", file=sys.stderr)
        sys.exit(3)

    try:
        files = list(list_files(src_folder, include_patterns, recursive))
    except KeyboardInterrupt:
        print("Interrupted while listing files.", file=sys.stderr)
        sys.exit(130)
    total = len(files)
    print(f"Found {total} file(s) to process.")
    if args.list_only:
        for idx, it in enumerate(files, start=1):
            name = str(getattr(it, 'Name', ''))
            print(f"[{idx}/{total}] IPhone: {name}", flush=True)
        return
    copied = 0
    skipped = 0
    errors = 0

    for idx, item in enumerate(files, start=1):
        if aborted['flag']:
            print("Stopping due to user interrupt.", file=sys.stderr)
            break
        name = str(item.Name)
        print(f"[{idx}/{total}] IPhone: {name}", flush=True)
        def _progress(stage, info=None):
            if stage == 'dest-prepared':
                # Quiet
                pass
            elif stage == 'dest-check':
                exists = bool((info or {}).get('exists'))
                if exists:
                    print(f"[{idx}/{total}] Comparing", flush=True)
            elif stage == 'stage-fetch-start':
                # Quiet
                pass
            elif stage == 'stage-fetch-finished':
                # Quiet
                pass
            elif stage == 'finalize-start':
                print(f"[{idx}/{total}] Copying...", flush=True)
            elif stage == 'finalize-finished':
                target = (info or {}).get('target')
                if target:
                    print(f"[{idx}/{total}] Copy complete: {Path(target).name}", flush=True)
                else:
                    print(f"[{idx}/{total}] Copy complete", flush=True)
            elif stage in ('size-verify-start','size-verified','hashing-start','hashing-done','verify-record'):
                # Quiet to keep output concise per user preference
                pass

        try:
            status, dest_file = copy_single(
                shell,
                item,
                destination,
                preserve_subfolders,
                skip_existing,
                verified_set,
                verified_path,
                progress=_progress,
            )
            if status == 'copied':
                copied += 1
            elif status == 'replaced':
                copied += 1
            elif status == 'skipped-identical':
                skipped += 1
                print(f"[{idx}/{total}] Skipped (identical)", flush=True)
            else:
                skipped += 1
                print(f"[{idx}/{total}] Skipped (already verified)", flush=True)
        except KeyboardInterrupt:
            aborted['flag'] = True
            print("Interrupted by user.", file=sys.stderr, flush=True)
            break
        except Exception as e:
            errors += 1
            print(f"[{idx}/{total}] ERROR: {e}", file=sys.stderr, flush=True)
            # If the device seems to have disappeared, stop early with guidance
            if source_names:
                first_seg = source_names[0]
                if not device_segment_present(shell, first_seg):
                    print("The source device no longer appears under 'This PC'. It may have been disconnected or locked.", file=sys.stderr)
                    print("Tips:", file=sys.stderr)
                    print("- Ensure the cable is connected and the iPhone is unlocked.", file=sys.stderr)
                    print("- Reconnect the device and run the tool again.", file=sys.stderr)
                    break

    print(f"Done. copied={copied} skipped={skipped} errors={errors}")
    if aborted.get('flag'):
        sys.exit(130)


if __name__ == '__main__':
    main()
