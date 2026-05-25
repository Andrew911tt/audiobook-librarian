"""
File Mover — Audiobook Librarian

Executes the physical organisation of the library:
  • Creates clean target folders  (Output / Author / [Series /] Title)
  • Transfers files using Copy (shutil.copy2) or Move (shutil.move)
  • Renames audio/ebook files to  [Book Title] - [Original Filename].[ext]
    so they are identifiable outside their folder and collisions are avoided
  • Renames cover images to cover.jpg / cover.png
  • Writes a metadata.opf file containing ASIN / ISBN so Audiobookshelf
    can match the item precisely without polluting the folder name

MAX_PATH defence: if a prefixed destination path would exceed 240 characters
the title prefix is silently dropped and the original filename is used.

Dry-run guarantee: nothing is written until execute_move() is called.
"""

import logging
import re as _re
import shutil
import sqlite3
from pathlib import Path
from typing import Callable, Optional

import database

log = logging.getLogger(__name__)

# Image extensions that get renamed to cover.*
_COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Maximum characters allowed in the full destination path (Windows MAX_PATH
# is 260; we reserve 20 chars of headroom for metadata.opf and other writes).
_MAX_DST_PATH = 240

# Maximum characters taken from the book title when building the filename prefix.
_TITLE_PREFIX_MAX = 60

# Characters illegal in Windows filenames.
_ILLEGAL_FILENAME_CHARS = _re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# OPF template — minimal but valid EPUB OPF 2.0
_OPF_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf"
         unique-identifier="uid"
         version="2.0">

  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">

    <dc:title>{title}</dc:title>
    <dc:creator opf:role="aut">{author}</dc:creator>
{narrator_tag}
    <dc:language>en</dc:language>
{series_tags}
{id_tags}
  </metadata>

</package>
"""


# ---------------------------------------------------------------------------
# OPF generation
# ---------------------------------------------------------------------------

def _escape_xml(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _build_opf(book: dict) -> str:
    title    = _escape_xml(book.get("best_title",  "") or "Unknown")
    author   = _escape_xml(book.get("best_author", "") or "Unknown")
    narrator = _escape_xml(book.get("narrator",    "") or "")
    series   = _escape_xml(book.get("series_name", "") or "")
    seq      = _escape_xml(book.get("series_sequence", "") or "")
    isbn     = _escape_xml(book.get("isbn",        "") or "")
    asin     = _escape_xml(book.get("asin",        "") or "")

    narrator_tag = (
        f'    <dc:creator opf:role="nrt">{narrator}</dc:creator>'
        if narrator else ""
    )

    series_tags_lines = []
    if series:
        series_tags_lines.append(
            f'    <meta name="calibre:series" content="{series}"/>'
        )
    if seq:
        series_tags_lines.append(
            f'    <meta name="calibre:series_index" content="{seq}"/>'
        )
    series_tags = "\n".join(series_tags_lines)

    id_tags_lines = []
    if asin:
        id_tags_lines.append(
            f'    <dc:identifier id="uid" opf:scheme="ASIN">{asin}</dc:identifier>'
        )
    if isbn:
        id_tags_lines.append(
            f'    <dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier>'
        )
    if not id_tags_lines:
        id_tags_lines.append(
            f'    <dc:identifier id="uid">{_escape_xml(book.get("parent_folder",""))}</dc:identifier>'
        )
    id_tags = "\n".join(id_tags_lines)

    return _OPF_TEMPLATE.format(
        title=title,
        author=author,
        narrator_tag=narrator_tag,
        series_tags=series_tags,
        id_tags=id_tags,
    )


# ---------------------------------------------------------------------------
# File transfer helpers
# ---------------------------------------------------------------------------

def _transfer_file(src: Path, dst: Path, mode: str) -> str:
    """
    Transfer a single file according to *mode*.

    mode values:
      'copy' — shutil.copy2(), source untouched (default)
      'move' — shutil.move(), source deleted after transfer

    Returns one of: 'copied', 'moved'.
    """
    if mode == "move":
        shutil.move(str(src), str(dst))
        return "moved"
    else:  # copy (default)
        shutil.copy2(src, dst)
        return "copied"


def _cover_dest_name(src: Path) -> str:
    """Return 'cover.jpg' or 'cover.png' based on source extension."""
    ext = src.suffix.lower()
    return "cover.png" if ext == ".png" else "cover.jpg"


def _make_title_prefix(title: str) -> str:
    """
    Return a safe, length-capped filename prefix derived from *title*.

    e.g.  "The Hunt for Red October"  →  "The Hunt for Red October - "
          ""                           →  ""  (no prefix added)
    """
    if not title:
        return ""
    safe = _ILLEGAL_FILENAME_CHARS.sub("", title).strip(". ")
    if not safe:
        return ""
    if len(safe) > _TITLE_PREFIX_MAX:
        safe = safe[:_TITLE_PREFIX_MAX].rstrip()
    return f"{safe} - "


# ---------------------------------------------------------------------------
# Post-move cleanup
# ---------------------------------------------------------------------------

def _remove_empty_dirs(folder: Path) -> None:
    """
    After a move, delete *folder* if it is empty, then walk up the tree
    deleting each parent directory that becomes empty — stopping when a
    non-empty directory is reached or the filesystem root is hit.
    """
    target = folder
    while True:
        try:
            if not target.is_dir():
                break
            contents = list(target.iterdir())
            if contents:
                log.debug(f"Source folder not empty, leaving: {target}")
                break
            target.rmdir()
            log.info(f"Removed empty source folder: {target}")
            parent = target.parent
            if parent == target:
                break
            target = parent
        except OSError as e:
            log.warning(f"Could not remove folder {target}: {e}")
            break


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def execute_move(
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    mode: str = "copy",
) -> dict:
    """
    Organise the library by transferring every non-duplicate book in the
    books table from its source folder to its target_abs_path.

    For each book:
      1. Create the target directory.
      2. Transfer every file from source → target using *mode*.
         Audio / ebook files are prefixed with the book title.
         Cover images are renamed to cover.jpg / cover.png.
         If the prefixed path would exceed _MAX_DST_PATH characters the
         prefix is dropped and the original filename is used instead.
      3. Write metadata.opf into the target folder.

    Args:
        on_progress: callable(current, total, label) for UI updates.
        mode: 'copy' (default) or 'move'.

    Returns:
        {
          'total':              number of books attempted,
          'ok':                 books fully transferred,
          'skipped':            books with no valid source or target path,
          'errors':             books where at least one file failed,
          'duplicates_skipped': books excluded because they are duplicates,
          'mode':               the transfer mode used,
          'files_copied':       count of copied files,
          'files_moved':        count of moved files,
        }
    """
    all_books = database.fetch_books(limit=10000)
    dup_count = sum(1 for b in all_books if b["is_duplicate"])
    books     = [b for b in all_books if not b["is_duplicate"]]
    if dup_count:
        log.info(f"execute_move: skipping {dup_count} duplicate book group(s).")

    total   = len(books)
    ok      = 0
    skipped = 0
    errors  = 0
    copied  = 0
    moved   = 0
    folders_removed = 0

    # Fetch source file lists only — never re-transfer already-organised files
    with sqlite3.connect(str(database.DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        all_files_rows = conn.execute(
            "SELECT parent_folder, abs_path, file_name, extension, file_type "
            "FROM files "
            "WHERE is_library_file = 0"
        ).fetchall()

    from collections import defaultdict
    files_by_folder: dict[str, list] = defaultdict(list)
    for f in all_files_rows:
        files_by_folder[f["parent_folder"]].append(f)

    for idx, book in enumerate(books, 1):
        folder     = book["parent_folder"]
        target_str = book["target_abs_path"]
        title      = book["best_title"] or Path(folder).name

        if on_progress:
            on_progress(idx, total, title)

        if not folder or not target_str:
            log.warning(f"Skipping book with missing path: {title!r}")
            skipped += 1
            continue

        src_dir = Path(folder)
        dst_dir = Path(target_str)

        if not src_dir.is_dir():
            log.warning(f"Source folder not found, skipping: {src_dir}")
            skipped += 1
            continue

        # ── Create target directory ──────────────────────────────────
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error(f"Cannot create target dir {dst_dir}: {e}")
            errors += 1
            continue

        book_files   = files_by_folder.get(folder, [])
        book_error   = False
        cover_done   = False
        title_prefix = _make_title_prefix(book["best_title"] or Path(folder).name)

        for file_row in book_files:
            src_file = Path(file_row["abs_path"])
            ext      = file_row["extension"].lower()
            ftype    = file_row["file_type"]

            # ── Determine destination filename ───────────────────────
            if ftype == "image" and ext in _COVER_EXTS and not cover_done:
                # First cover → standardised name (no title prefix)
                dst_name   = _cover_dest_name(src_file)
                cover_done = True
            elif ftype == "image" and ext in _COVER_EXTS:
                # Additional covers → keep original name
                dst_name = file_row["file_name"]
            else:
                # Audio / ebook / other → prefix with book title
                orig     = file_row["file_name"]
                dst_name = title_prefix + orig if title_prefix else orig

                # MAX_PATH defence: if the full path would exceed the limit,
                # silently fall back to the un-prefixed filename.
                if title_prefix and len(str(dst_dir / dst_name)) > _MAX_DST_PATH:
                    dst_name = orig
                    log.warning(
                        f"Title prefix skipped (path too long): {orig!r}"
                    )

            dst_file = dst_dir / dst_name

            # Skip if already there (re-run safety)
            if dst_file.exists():
                log.debug(f"Already exists, skipping: {dst_file.name}")
                continue

            if not src_file.exists():
                log.warning(f"Source file missing: {src_file}")
                continue

            try:
                method = _transfer_file(src_file, dst_file, mode)
                if method == "moved":
                    moved += 1
                else:
                    copied += 1
                log.debug(f"{method}: {src_file.name} → {dst_dir.name}/{dst_file.name}")
            except Exception as e:
                log.error(f"Transfer failed [{src_file.name}]: {e}")
                book_error = True

        # ── Write metadata.opf ───────────────────────────────────────
        opf_path = dst_dir / "metadata.opf"
        try:
            opf_path.write_text(_build_opf(dict(book)), encoding="utf-8")
            log.debug(f"Wrote metadata.opf → {dst_dir.name}")
        except Exception as e:
            log.error(f"metadata.opf write failed [{dst_dir.name}]: {e}")
            book_error = True

        if book_error:
            errors += 1
        else:
            ok += 1

        # ── Remove empty source folder after a move ──────────────────
        if mode == "move" and not book_error:
            _remove_empty_dirs(src_dir)
            folders_removed += 1

    log.info(
        f"execute_move complete [{mode}] — {ok} ok, {skipped} skipped, "
        f"{errors} errors, {dup_count} duplicates excluded | "
        f"{copied} copied, {moved} moved, "
        f"{folders_removed} source folders removed"
    )
    return {
        "total":              total,
        "ok":                 ok,
        "skipped":            skipped,
        "errors":             errors,
        "duplicates_skipped": dup_count,
        "mode":               mode,
        "files_copied":       copied,
        "files_moved":        moved,
        "folders_removed":    folders_removed,
    }
