"""
Directory walker and scan orchestrator.

Calls extractors.extract() for each file, writes results to database.

Two roots can be scanned in one pass:
  source_dir   — the user's raw/incoming library  (is_library_file = 0)
  output_root  — the already-organised output      (is_library_file = 1)

Files in output_root are treated as "truth": at equal quality they win
the deduplication contest over source files.  If a source file has a
higher quality score than the matching library file the source wins
instead (upgrade detection in flag_duplicates).

Dry-run only — no files are moved or renamed.
"""

import datetime
import logging
import os
import re
import threading
from pathlib import Path
from typing import Callable, Optional

from extractors import compute_hash, extract
import database

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

AUDIO_EXTS  = frozenset({".mp3", ".m4a", ".m4b", ".m4r", ".flac", ".aac",
                          ".ogg", ".opus", ".wav", ".wma", ".aiff", ".aif"})
EBOOK_EXTS  = frozenset({".epub", ".pdf", ".mobi", ".azw", ".azw3",
                          ".cbz", ".cbr", ".djvu"})
IMAGE_EXTS  = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif",
                          ".bmp", ".tiff", ".tif"})
IGNORE_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini",
                           ".nomedia", ".gitignore", "albumartwork",
                           "metadata.opf"})

# Pattern-based ignores: catches name variants the static set can't cover.
IGNORE_NAME_PATTERNS = [
    # ── album-art family ──────────────────────────────────────────────
    # Always cruft — never the canonical cover file.  Catches every
    # variant with or without a " (N)" copy suffix:
    #   album-art.jpg, album-art (1).jpg, album-art (10).png,
    #   albumart.jpg, album_art (1).jpeg, albumartwork (2).png,
    #   AlbumArt.png, Album-Artwork (3).jpg
    re.compile(
        r"^album[-_ ]?art(work)?(\s*\(\d+\))?(\.\w+)?$",
        re.IGNORECASE,
    ),

    # ── duplicate-copy artifacts of common cover/image names ─────────
    # Matches the WINDOWS-style copy suffix " (N)" on common cover
    # filenames, so the canonical "cover.jpg" is KEPT but stray copies
    # like "cover (1).jpg" / "folder (2).png" are skipped.
    #   cover (1).jpg, cover (10).png, Cover (2).jpeg
    #   folder (1).jpg, front (1).png, back (2).png
    #   thumb (1).jpg, thumbnail (3).png
    #   poster (1).jpg, artwork (1).png, image (1).jpg
    #   cover-art (1).jpg, front-cover (1).png, back_cover (2).jpg
    re.compile(
        r"^("
        r"cover([-_ ]?art)?"
        r"|folder"
        r"|front([-_ ]?cover)?"
        r"|back([-_ ]?cover)?"
        r"|thumb(nail)?"
        r"|poster"
        r"|artwork"
        r"|image"
        r")"
        r"\s*\(\d+\)"            # REQUIRED " (N)" copy suffix
        r"(\.\w+)?$",
        re.IGNORECASE,
    ),
]


def _classify(path: Path) -> Optional[str]:
    """Return file_type string or None if the file should be ignored."""
    name = path.name.lower()
    if name in IGNORE_NAMES or name.startswith("."):
        return None
    if any(p.match(name) for p in IGNORE_NAME_PATTERNS):
        return None
    ext = path.suffix.lower()
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in EBOOK_EXTS:
        return "ebook"
    if ext in IMAGE_EXTS:
        return "image"
    return "other"


# ---------------------------------------------------------------------------
# Stop flag
# ---------------------------------------------------------------------------

_stop_event = threading.Event()


def request_stop() -> None:
    _stop_event.set()


# Alias used by main.py
stop_scan = request_stop


# ---------------------------------------------------------------------------
# Internal per-directory walker
# ---------------------------------------------------------------------------

_STATS_EVERY = 25   # send stats update every N files


def _walk_root(
    root: Path,
    is_library_file: int,
    stats: dict,
    on_progress: Callable[[int, str], None],
    on_stats: Callable[[dict], None],
    skip_subtree: Optional[Path] = None,
    skipped_folders: Optional[set] = None,
) -> None:
    """
    Walk *root* recursively, extract metadata, and upsert every file into
    the database.

    Args:
        root:             Directory to walk.
        is_library_file:  1 if this root is the organised output folder,
                          0 if it is the raw source directory.
        stats:            Shared mutable stats dict (mutated in place).
        on_progress:      Progress callback(count, filename).
        on_stats:         Stats callback(stats_dict).
        skip_subtree:     Optional path to exclude (prevents double-scanning
                          when output_root is nested inside source or vice versa).
        skipped_folders:  Set of folder path strings the user has tagged as "skip".
                          Any directory that matches exactly or is a sub-directory
                          of a skipped path will be silently ignored.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)

        # Skip hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        # Skip the other root if it is nested inside this one
        if skip_subtree:
            try:
                current.relative_to(skip_subtree)
                log.debug(f"Skipping nested subtree: {current}")
                dirnames.clear()
                continue
            except ValueError:
                pass

        # Skip user-tagged folders (exact match or any sub-path).
        # Normalise both sides so '/' vs '\' doesn't matter.
        if skipped_folders:
            current_norm = str(current).replace("/", os.sep).rstrip(os.sep)
            for s in skipped_folders:
                s_norm = s.replace("/", os.sep).rstrip(os.sep)
                if current_norm == s_norm or current_norm.startswith(s_norm + os.sep):
                    log.debug(f"Skipping user-ignored folder: {current}")
                    dirnames.clear()
                    filenames = []
                    break
            if not filenames and not dirnames:
                continue

        if _stop_event.is_set():
            log.info("Scan stopped by user request.")
            return

        for filename in filenames:
            if _stop_event.is_set():
                return

            filepath  = current / filename
            file_type = _classify(filepath)

            if file_type is None:
                continue

            try:
                st = filepath.stat()
            except OSError as e:
                log.warning(f"Cannot access [{filename}]: {e}")
                continue

            try:
                extraction = extract(filepath, file_type)
            except Exception as e:
                log.error(f"Extraction error [{filename}]: {e}")
                extraction = None

            # Only hash audio files — avoids slow hashing of large image/ebook files
            file_hash = compute_hash(filepath) if file_type == "audio" else None

            record = {
                "abs_path":         str(filepath),
                "parent_folder":    str(filepath.parent),
                "file_name":        filename,
                "extension":        filepath.suffix.lower(),
                "file_type":        file_type,
                "file_size":        st.st_size,
                "file_hash":        file_hash,
                "extracted_title":  extraction.title            if extraction else "",
                "extracted_author": extraction.author           if extraction else "",
                "duration_seconds": extraction.duration_seconds if extraction else None,
                "bitrate":          extraction.bitrate          if extraction else None,
                "confidence_score": extraction.confidence       if extraction else 0,
                "is_duplicate":     0,
                "is_library_file":  is_library_file,
                "file_modified":    datetime.datetime.fromtimestamp(
                                        st.st_mtime).isoformat(),
            }

            try:
                database.upsert(record)
            except Exception as e:
                log.error(f"DB write failed [{filename}]: {e}")

            stats["total"] += 1
            stats[file_type] = stats.get(file_type, 0) + 1

            if on_progress:
                on_progress(stats["total"], filename)

            if stats["total"] % _STATS_EVERY == 0:
                if on_stats:
                    on_stats(dict(stats))


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan(
    root_path: str,
    on_progress: Optional[Callable[[int, str], None]] = None,
    on_stats:    Optional[Callable[[dict], None]]     = None,
    clear_first: bool = True,
    output_root: Optional[str] = None,
) -> dict:
    """
    Walk root_path (and optionally output_root) recursively, extract
    metadata, and write every file to the database.

    Files under root_path   → is_library_file = 0  (source)
    Files under output_root → is_library_file = 1  (organised library)

    Args:
        root_path:    Source directory to scan.
        on_progress:  Optional callback(count, current_filename).
        on_stats:     Optional callback(stats_dict).
        clear_first:  Clear the database before scanning (default True).
        output_root:  Optional organised library directory to scan alongside source.

    Returns the final stats dict.
    """
    _stop_event.clear()
    database.init()

    if clear_first:
        database.clear()

    _on_progress = on_progress or (lambda c, n: None)
    _on_stats    = on_stats    or (lambda s: None)

    source = Path(root_path)
    output = Path(output_root) if output_root else None

    stats = {"total": 0, "audio": 0, "ebook": 0, "image": 0, "other": 0}

    # ── Scan source directory ────────────────────────────────────────────
    source_skip = None
    if output and output.is_dir():
        try:
            output.relative_to(source)
            source_skip = output
            log.info(
                f"output_root is inside source_dir — will skip {output} "
                f"during source scan."
            )
        except ValueError:
            pass

    # Load user-tagged skip list once — shared across both walk passes
    user_skips = database.get_skipped_folders()
    if user_skips:
        log.info(f"Skip list active: {len(user_skips)} folder(s) will be ignored.")

    log.info(f"Scanning source: {source}")
    _walk_root(source, 0, stats, _on_progress, _on_stats,
               skip_subtree=source_skip, skipped_folders=user_skips)

    if _stop_event.is_set():
        _on_stats(dict(stats))
        return stats

    # ── Scan output / library directory ─────────────────────────────────
    if output and output.is_dir() and output != source:
        output_skip = None
        try:
            source.relative_to(output)
            output_skip = source
            log.info(
                f"source_dir is inside output_root — will skip {source} "
                f"during library scan."
            )
        except ValueError:
            pass

        log.info(f"Scanning library (output_root): {output}")
        _walk_root(output, 1, stats, _on_progress, _on_stats,
                   skip_subtree=output_skip, skipped_folders=user_skips)
    elif output_root:
        log.debug(
            f"output_root not scanned: "
            f"{'same as source' if output == source else 'directory not found'}"
        )

    _on_stats(dict(stats))
    log.info(
        f"Scan finished — {stats['total']} files "
        f"({stats['audio']} audio, {stats['ebook']} ebook, "
        f"{stats['image']} image, {stats['other']} other)"
    )
    return stats


# ---------------------------------------------------------------------------
# Non-destructive re-compare against an existing output folder
# ---------------------------------------------------------------------------

def refresh_library_match(
    output_root: str,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> None:
    """
    Walk output_root and mark every file found there as is_library_file=1
    without clearing the database first.  Then re-runs duplicate detection
    and syncs the books table duplicate flags.

    Used by the "↻ Re-Compare with Output" button — lets the user update
    which source books are superseded by the library without doing a full
    rescan of the source directory.
    """
    _stop_event.clear()
    out_path = Path(output_root)
    if not out_path.exists() or not out_path.is_dir():
        log.warning(f"refresh_library_match: invalid output root {output_root!r}")
        return

    log.info(f"Refreshing library matches against: {output_root}")

    _on_progress = on_progress or (lambda c, n: None)
    _on_stats    = lambda s: None

    stats = {"total": 0, "audio": 0, "ebook": 0, "image": 0, "other": 0}
    _walk_root(out_path, 1, stats, _on_progress, _on_stats)

    log.info(
        f"refresh_library_match: {stats['total']} files walked "
        f"({stats['audio']} audio)"
    )

    # Pass 1 & 2: re-evaluate raw extracted metadata against library files
    database.flag_duplicates()
    # Pass 3: compare corrected books metadata (best_author/best_title) against
    # library files — catches books whose metadata was wrong at scan time but
    # has since been corrected by the sync tools
    database.match_books_against_library()
    # Sync the books table is_duplicate flags from the files table
    database.sync_book_duplicates()
