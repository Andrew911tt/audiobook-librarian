"""
SQLite persistence layer for the library manifest.

Schema:
  id, abs_path, parent_folder, file_name, extension, file_type,
  file_size, file_hash, extracted_title, extracted_author,
  duration_seconds, bitrate, confidence_score, is_duplicate,
  file_modified, scanned_at
"""

import csv
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

from errors import RateLimitError, TokenLimitError

log = logging.getLogger(__name__)

DB_PATH: Path = Path.home() / ".bookorganizer" / "library_manifest.db"

_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS files (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    abs_path         TEXT    UNIQUE NOT NULL,
    parent_folder    TEXT    DEFAULT '',
    file_name        TEXT    NOT NULL,
    extension        TEXT    DEFAULT '',
    file_type        TEXT    DEFAULT 'other',
    file_size        INTEGER DEFAULT 0,
    file_hash        TEXT,
    extracted_title  TEXT    DEFAULT '',
    extracted_author TEXT    DEFAULT '',
    duration_seconds REAL,
    bitrate          INTEGER,
    confidence_score INTEGER DEFAULT 0,
    is_duplicate     INTEGER DEFAULT 0,
    is_library_file  INTEGER DEFAULT 0,
    file_modified    TEXT    DEFAULT '',
    scanned_at       TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_title_author
    ON files (LOWER(TRIM(extracted_title)), LOWER(TRIM(extracted_author)));

CREATE INDEX IF NOT EXISTS idx_hash
    ON files (file_hash)
    WHERE file_hash IS NOT NULL;
"""

# Created separately — after migration ensures the column exists
_IDX_PARENT = "CREATE INDEX IF NOT EXISTS idx_parent ON files (parent_folder);"

_BOOKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_folder    TEXT    UNIQUE NOT NULL,
    best_author      TEXT    DEFAULT '',
    best_title       TEXT    DEFAULT '',
    series_name      TEXT    DEFAULT '',
    series_sequence  TEXT    DEFAULT '',
    narrator         TEXT    DEFAULT '',
    isbn             TEXT    DEFAULT '',
    asin             TEXT    DEFAULT '',
    file_count       INTEGER DEFAULT 0,
    total_duration   REAL    DEFAULT 0,
    confidence_score INTEGER DEFAULT 0,
    is_duplicate     INTEGER DEFAULT 0,
    manual_review    INTEGER DEFAULT 0,
    local_title      TEXT    DEFAULT '',
    local_author     TEXT    DEFAULT '',
    local_series     TEXT    DEFAULT '',
    local_sequence   TEXT    DEFAULT '',
    local_id         TEXT    DEFAULT '',
    local_synced     INTEGER DEFAULT 0,
    llm_title        TEXT    DEFAULT '',
    llm_author       TEXT    DEFAULT '',
    llm_series       TEXT    DEFAULT '',
    llm_sequence     TEXT    DEFAULT '',
    llm_id           TEXT    DEFAULT '',
    llm_synced       INTEGER DEFAULT 0,
    llm_source       TEXT    DEFAULT '',
    cat_title        TEXT    DEFAULT '',
    cat_author       TEXT    DEFAULT '',
    cat_series       TEXT    DEFAULT '',
    cat_sequence     TEXT    DEFAULT '',
    cat_isbn         TEXT    DEFAULT '',
    cat_asin         TEXT    DEFAULT '',
    cat_synced       INTEGER DEFAULT 0,
    cat_source       TEXT    DEFAULT '',
    user_title       TEXT    DEFAULT '',
    user_author      TEXT    DEFAULT '',
    user_series      TEXT    DEFAULT '',
    user_sequence    TEXT    DEFAULT '',
    user_narrator    TEXT    DEFAULT '',
    user_isbn        TEXT    DEFAULT '',
    user_asin        TEXT    DEFAULT '',
    user_edited      INTEGER DEFAULT 0,
    user_edited_at   TEXT    DEFAULT '',
    target_abs_path  TEXT    DEFAULT '',
    updated_at       TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_books_author
    ON books (LOWER(TRIM(best_author)));
"""

_SKIPPED_SCHEMA = """
CREATE TABLE IF NOT EXISTS skipped_folders (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT    UNIQUE NOT NULL,
    added_at TEXT    DEFAULT (datetime('now'))
);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def init(db_path: Optional[Path] = None) -> None:
    global DB_PATH
    if db_path:
        DB_PATH = Path(db_path)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(_SCHEMA)
        # Migration: add columns if upgrading from an older DB
        existing = {row[1] for row in c.execute("PRAGMA table_info(files)")}
        if "parent_folder" not in existing:
            c.execute("ALTER TABLE files ADD COLUMN parent_folder TEXT DEFAULT ''")
            log.info("Migrated DB: added parent_folder column")
        if "is_library_file" not in existing:
            c.execute("ALTER TABLE files ADD COLUMN is_library_file INTEGER DEFAULT 0")
            log.info("Migrated DB: added is_library_file column")
        # Create parent_folder index after ensuring the column exists
        c.execute(_IDX_PARENT)
        # Books table
        c.executescript(_BOOKS_SCHEMA)
        # Skipped folders table
        c.executescript(_SKIPPED_SCHEMA)
        # Migration: add columns if upgrading from an older books table
        books_cols = {row[1] for row in c.execute("PRAGMA table_info(books)")}
        for col in ("series_name", "series_sequence", "narrator", "isbn", "asin"):
            if col not in books_cols:
                c.execute(f"ALTER TABLE books ADD COLUMN {col} TEXT DEFAULT ''")
                log.info(f"Migrated books table: added {col} column")
        if "is_duplicate" not in books_cols:
            c.execute("ALTER TABLE books ADD COLUMN is_duplicate INTEGER DEFAULT 0")
            log.info("Migrated books table: added is_duplicate column")
        if "manual_review" not in books_cols:
            c.execute("ALTER TABLE books ADD COLUMN manual_review INTEGER DEFAULT 0")
            log.info("Migrated books table: added manual_review column")
        for col, defval in [
            ("local_title",        "TEXT    DEFAULT ''"),
            ("local_author",       "TEXT    DEFAULT ''"),
            ("local_series",       "TEXT    DEFAULT ''"),
            ("local_sequence",     "TEXT    DEFAULT ''"),
            ("local_id",           "TEXT    DEFAULT ''"),
            ("local_synced",       "INTEGER DEFAULT 0"),
            ("audible_runtime_min","INTEGER DEFAULT 0"),
            ("google_title",       "TEXT    DEFAULT ''"),
            ("google_author",      "TEXT    DEFAULT ''"),
            ("google_isbn",        "TEXT    DEFAULT ''"),
            ("metadata_source",    "TEXT    DEFAULT ''"),
            ("llm_title",          "TEXT    DEFAULT ''"),
            ("llm_author",         "TEXT    DEFAULT ''"),
            ("llm_series",         "TEXT    DEFAULT ''"),
            ("llm_sequence",       "TEXT    DEFAULT ''"),
            ("llm_id",             "TEXT    DEFAULT ''"),
            ("llm_synced",         "INTEGER DEFAULT 0"),
            ("llm_source",         "TEXT    DEFAULT ''"),
            ("cat_title",          "TEXT    DEFAULT ''"),
            ("cat_author",         "TEXT    DEFAULT ''"),
            ("cat_series",         "TEXT    DEFAULT ''"),
            ("cat_sequence",       "TEXT    DEFAULT ''"),
            ("cat_isbn",           "TEXT    DEFAULT ''"),
            ("cat_asin",           "TEXT    DEFAULT ''"),
            ("cat_synced",         "INTEGER DEFAULT 0"),
            ("cat_source",         "TEXT    DEFAULT ''"),
            ("user_title",         "TEXT    DEFAULT ''"),
            ("user_author",        "TEXT    DEFAULT ''"),
            ("user_series",        "TEXT    DEFAULT ''"),
            ("user_sequence",      "TEXT    DEFAULT ''"),
            ("user_narrator",      "TEXT    DEFAULT ''"),
            ("user_isbn",          "TEXT    DEFAULT ''"),
            ("user_asin",          "TEXT    DEFAULT ''"),
            ("user_edited",        "INTEGER DEFAULT 0"),
            ("user_edited_at",     "TEXT    DEFAULT ''"),
        ]:
            if col not in books_cols:
                c.execute(f"ALTER TABLE books ADD COLUMN {col} {defval}")
                log.info(f"Migrated books table: added {col} column")

        # Migrate local_* → llm_* for existing rows (one-time, idempotent)
        if "local_synced" in books_cols and "llm_synced" in {
                row[1] for row in c.execute("PRAGMA table_info(books)")}:
            c.execute("""
                UPDATE books SET
                    llm_title    = COALESCE(NULLIF(llm_title,    ''), local_title),
                    llm_author   = COALESCE(NULLIF(llm_author,   ''), local_author),
                    llm_series   = COALESCE(NULLIF(llm_series,   ''), local_series),
                    llm_sequence = COALESCE(NULLIF(llm_sequence, ''), local_sequence),
                    llm_id       = COALESCE(NULLIF(llm_id,       ''), local_id),
                    llm_synced   = CASE WHEN llm_synced = 0 THEN local_synced ELSE llm_synced END,
                    llm_source   = CASE WHEN llm_source = '' THEN 'local' ELSE llm_source END
                WHERE local_synced != 0 AND llm_synced = 0
            """)
            log.info("Migrated local_* shadow columns → llm_*")
    log.info(f"Database ready: {DB_PATH}")


def clear() -> None:
    """Delete all rows from files AND books (keeps schema).

    Both tables are cleared together so that after a rescan the books table
    never contains stale groups from a previous scan.  Without this, the
    persistence layer (_startup_load) would restore old book groups on the
    next app launch even though the file data has changed.
    """
    with _conn() as c:
        c.execute("DELETE FROM files")
        c.execute("DELETE FROM books")
    log.info("Database cleared (files + books).")


# ---------------------------------------------------------------------------
# Skipped folders
# ---------------------------------------------------------------------------

def add_skipped_folder(path: str) -> None:
    """Mark a source folder as skipped — ignored by scanner and LLM sync."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO skipped_folders (path) VALUES (?)",
            (str(path),)
        )
    log.info(f"Skipped folder added: {path!r}")


def remove_skipped_folder(path: str) -> None:
    """Remove a folder from the skip list."""
    with _conn() as c:
        c.execute("DELETE FROM skipped_folders WHERE path = ?", (str(path),))
    log.info(f"Skipped folder removed: {path!r}")


def get_skipped_folders() -> set:
    """Return the set of all skipped folder paths (persisted across restarts)."""
    with _conn() as c:
        rows = c.execute("SELECT path FROM skipped_folders").fetchall()
    return {row["path"] for row in rows}


def purge_under_path(path: str) -> tuple:
    """
    Delete every files row and books row whose path is *path* itself
    or a sub-path of *path*.

    Matches both forward-slash and back-slash separators so paths stored
    in either style (e.g. SQLite-normalised or raw Windows paths) all get
    cleaned up.

    Returns (files_deleted, books_deleted).
    """
    path = str(path).rstrip("/\\")
    prefix_bs = path + "\\"
    prefix_fs = path + "/"
    with _conn() as c:
        files_deleted = c.execute(
            "DELETE FROM files "
            "WHERE abs_path = ? OR abs_path LIKE ? OR abs_path LIKE ? "
            "OR parent_folder = ? OR parent_folder LIKE ? OR parent_folder LIKE ?",
            (path,
             prefix_bs + "%", prefix_fs + "%",
             path,
             prefix_bs + "%", prefix_fs + "%"),
        ).rowcount
        books_deleted = c.execute(
            "DELETE FROM books "
            "WHERE parent_folder = ? OR parent_folder LIKE ? OR parent_folder LIKE ?",
            (path, prefix_bs + "%", prefix_fs + "%"),
        ).rowcount
    log.info(
        f"purge_under_path({path!r}): deleted {files_deleted} file row(s) "
        f"and {books_deleted} book row(s)."
    )
    return files_deleted, books_deleted


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert(record: dict) -> None:
    cols   = list(record.keys())
    q      = ", ".join("?" * len(cols))
    names  = ", ".join(cols)
    update = ", ".join(
        f"{c}=excluded.{c}" for c in cols if c != "abs_path"
    )
    sql = (
        f"INSERT INTO files ({names}) VALUES ({q}) "
        f"ON CONFLICT(abs_path) DO UPDATE SET {update}, "
        f"scanned_at=datetime('now')"
    )
    with _conn() as c:
        c.execute(sql, [record[k] for k in cols])


# ---------------------------------------------------------------------------
# Duplicate detection  (Window Functions — cross-folder aware)
# ---------------------------------------------------------------------------

def flag_duplicates() -> int:
    """
    Reset all is_duplicate flags, then re-evaluate using a Quality Score.

    Quality Score  =  FORMAT_SCORE  +  (bitrate / 32)
        FLAC  → format score 10
        M4B   → format score  8
        MP3   → format score  5
        other → format score  3

    Two passes:

    1. Title + Author (cross-folder, non-image files only)
       - Folder-groups are ranked by quality_score DESC, then by
         is_library_file DESC as a tiebreaker.
       - "Truth-Based Priority": at equal quality a Library folder (output_root
         file, is_library_file=1) beats a Source folder.
       - "Upgrade Detection": if a Source folder has a strictly higher quality
         score it outranks the Library folder — the Source is kept, the Library
         is flagged as duplicate.

    2. Hash-exact duplicates — same quality+library tiebreak ranking.

    Returns the count of rows flagged as duplicates.
    """
    _QUALITY_EXPR = """
        CASE extension
            WHEN '.flac' THEN 10
            WHEN '.m4b'  THEN  8
            WHEN '.mp3'  THEN  5
            ELSE 3
        END + COALESCE(bitrate, 0) / 32.0
    """

    with _conn() as conn:
        conn.execute("UPDATE files SET is_duplicate = 0")
        conn.commit()

        dup_ids: set[int] = set()

        # ── 1. Title + Author — cross-folder only, exclude images ────────
        rows = conn.execute(f"""
            WITH FolderGroups AS (
                SELECT
                    LOWER(TRIM(extracted_title))  AS norm_title,
                    LOWER(TRIM(extracted_author)) AS norm_author,
                    parent_folder,
                    MAX(is_library_file)          AS is_lib,
                    AVG({_QUALITY_EXPR})           AS quality_score,
                    SUM(file_size)                AS total_size,
                    GROUP_CONCAT(id)              AS ids
                FROM files
                WHERE TRIM(extracted_title)  != ''
                  AND TRIM(extracted_author) != ''
                  AND file_type != 'image'
                GROUP BY norm_title, norm_author, parent_folder
            ),
            Ranked AS (
                SELECT ids,
                       ROW_NUMBER() OVER (
                           PARTITION BY norm_title, norm_author
                           ORDER BY
                               quality_score DESC,
                               is_lib        DESC,
                               total_size    DESC
                       ) AS rn
                FROM FolderGroups
            )
            SELECT ids FROM Ranked WHERE rn > 1
        """).fetchall()

        for (id_csv,) in rows:
            for id_ in id_csv.split(","):
                dup_ids.add(int(id_))

        # ── 2. Hash-exact duplicates ──────────────────────────────────────
        rows = conn.execute(f"""
            WITH Ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY file_hash
                           ORDER BY
                               {_QUALITY_EXPR} DESC,
                               is_library_file DESC,
                               file_size       DESC
                       ) AS rn
                FROM files
                WHERE file_hash IS NOT NULL
            )
            SELECT id FROM Ranked WHERE rn > 1
        """).fetchall()

        for row in rows:
            dup_ids.add(row["id"])

        # ── Apply ─────────────────────────────────────────────────────────
        if dup_ids:
            placeholders = ",".join("?" * len(dup_ids))
            conn.execute(
                f"UPDATE files SET is_duplicate = 1 WHERE id IN ({placeholders})",
                list(dup_ids),
            )
            conn.commit()

        log.info(f"Duplicate detection complete — {len(dup_ids)} duplicates flagged.")
        return len(dup_ids)


def sync_book_duplicates() -> None:
    """
    Update books.is_duplicate to reflect the current files.is_duplicate flags.

    A book group is considered a duplicate when ALL of its audio files have
    been flagged as duplicates by flag_duplicates().  Call this after
    flag_duplicates() to keep the books table in sync without rebuilding it.
    """
    with _conn() as conn:
        conn.execute("""
            UPDATE books SET is_duplicate = (
                SELECT CASE
                    WHEN COUNT(*) > 0 AND MIN(is_duplicate) = 1 THEN 1
                    ELSE 0
                END
                FROM files
                WHERE files.parent_folder = books.parent_folder
                  AND files.file_type = 'audio'
            )
        """)
        conn.commit()
    log.info("Books duplicate flags synced from files table.")


def match_books_against_library() -> int:
    """
    Compare each source book's corrected metadata (best_author / best_title
    from the books table) against library files (is_library_file=1) in the
    files table.

    This catches books that slipped through the initial flag_duplicates() pass
    because their raw extracted metadata was wrong (e.g. title and author
    reversed), but have since been correctly identified by the sync tools.

    Any source book whose best_author + best_title matches a library file's
    extracted_author + extracted_title is flagged as is_duplicate=1 in the
    books table.

    Returns the count of newly flagged duplicates.
    """
    with _conn() as conn:
        # Build a set of (author, title) pairs present in the library
        library_rows = conn.execute("""
            SELECT LOWER(TRIM(extracted_author)) AS norm_author,
                   LOWER(TRIM(extracted_title))  AS norm_title
            FROM files
            WHERE is_library_file = 1
              AND TRIM(extracted_author) != ''
              AND TRIM(extracted_title)  != ''
            GROUP BY norm_author, norm_title
        """).fetchall()

        library_set = {(r["norm_author"], r["norm_title"]) for r in library_rows}

        if not library_set:
            log.info("match_books_against_library: no library files to compare against.")
            return 0

        # Check each non-duplicate source book against the library set
        source_books = conn.execute("""
            SELECT parent_folder,
                   LOWER(TRIM(best_author)) AS norm_author,
                   LOWER(TRIM(best_title))  AS norm_title
            FROM books
            WHERE is_duplicate = 0
              AND TRIM(best_author) != ''
              AND TRIM(best_title)  != ''
        """).fetchall()

        flagged = 0
        for book in source_books:
            if (book["norm_author"], book["norm_title"]) in library_set:
                conn.execute(
                    "UPDATE books SET is_duplicate = 1 WHERE parent_folder = ?",
                    (book["parent_folder"],)
                )
                flagged += 1
                log.info(
                    f"match_books_against_library: flagged as duplicate — "
                    f"{book['norm_author']!r} / {book['norm_title']!r}"
                )

        conn.commit()

    log.info(f"match_books_against_library: {flagged} book(s) flagged as duplicates.")
    return flagged


# ---------------------------------------------------------------------------
# Book groups — helpers
# ---------------------------------------------------------------------------

import re as _re

# Matches titles that start with a numeric series prefix: "01.01 - " or "06 - "
_SERIES_PREFIX_RE = _re.compile(r"^(\d+(?:\.\d+)?)\s*[-–]\s*(.+)$")


def _sanitize_path_component(s: str) -> str:
    """Strip characters that are illegal in Windows/Linux folder names."""
    s = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)
    s = s.strip(". ")
    return s or "Unknown"


def _extract_series(title: str) -> tuple:
    """
    Detect a numeric series prefix like '01.01 - ' or '06 - '.
    Returns (clean_title, series_sequence).
    series_name is always '' — the user sets it in the Edit dialog.
    """
    m = _SERIES_PREFIX_RE.match((title or "").strip())
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return (title or ""), ""


def build_target_path(output_root: str, author: str, title: str,
                      series_name: str, series_seq: str,
                      asin: str = "", isbn: str = "") -> str:
    """
    Generate a clean, human-readable Audiobookshelf-compatible target path.
      With series name + seq : Output / Author / Series / Book N - Title
      With seq only          : Output / Author / Book N - Title
      Plain                  : Output / Author / Title

    IDs (ASIN/ISBN) are stored in metadata.opf inside the folder,
    NOT appended to the folder name.
    Public so the Edit dialog can call it directly.
    """
    _MAX_FOLDER_PATH = 240

    root = Path(output_root)
    a    = _sanitize_path_component(author) or "Unknown Author"
    t    = _sanitize_path_component(title)  or "Unknown Title"

    if series_name and series_seq:
        s  = _sanitize_path_component(series_name)
        bk = f"Book {series_seq} - {t}"
        candidate = root / a / s / bk
        if len(str(candidate)) > _MAX_FOLDER_PATH:
            prefix    = f"Book {series_seq} - "
            available = _MAX_FOLDER_PATH - len(str(root / a / s)) - 1 - len(prefix)
            t_short   = t[:max(10, available)].rstrip()
            bk        = prefix + t_short
            log.warning(f"Path too long — title truncated: {t!r} → {t_short!r}")
        return str(root / a / s / bk)
    elif series_seq:
        bk = f"Book {series_seq} - {t}"
        candidate = root / a / bk
        if len(str(candidate)) > _MAX_FOLDER_PATH:
            prefix    = f"Book {series_seq} - "
            available = _MAX_FOLDER_PATH - len(str(root / a)) - 1 - len(prefix)
            t_short   = t[:max(10, available)].rstrip()
            bk        = prefix + t_short
            log.warning(f"Path too long — title truncated: {t!r} → {t_short!r}")
        return str(root / a / bk)

    candidate = root / a / t
    if len(str(candidate)) > _MAX_FOLDER_PATH:
        available = _MAX_FOLDER_PATH - len(str(root / a)) - 1
        t_short   = t[:max(10, available)].rstrip()
        log.warning(f"Path too long — title truncated: {t!r} → {t_short!r}")
        return str(root / a / t_short)
    return str(candidate)


def _pick_best(rows: list, field: str) -> str:
    """
    Return the best value for `field` across a list of sqlite3.Row objects.
    Priority: MAX confidence_score first, then longest string to break ties.
    Skips 'Cover Art' — that is an image placeholder, not a real author/title.
    """
    candidates = []
    for row in rows:
        val = (row[field] or "").strip()
        if not val or val == "Cover Art":
            continue
        candidates.append((row["confidence_score"] or 0, len(val), val))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]


# ---------------------------------------------------------------------------
# Book groups — public API
# ---------------------------------------------------------------------------

def build_book_groups(output_root_dir: str) -> int:
    """
    Groups the `files` table by parent_folder and populates the `books` table.

    For each folder:
      - best_author / best_title: file with MAX confidence_score;
        ties broken by longest string.
      - series_sequence: extracted from a numeric prefix in best_title.
      - file_count: total files in the folder.
      - total_duration: sum of duration_seconds for audio files only.
      - confidence_score: MAX confidence across all files in the folder.
      - target_abs_path: Audiobookshelf-format path (see build_target_path).

    Duplicate book groups (all audio files flagged as duplicates) are skipped.

    Returns the number of book groups written.
    """
    from itertools import groupby

    with _conn() as conn:
        conn.execute("DELETE FROM books")
        conn.commit()

        all_files = conn.execute("""
            SELECT parent_folder, extracted_title, extracted_author,
                   confidence_score, duration_seconds, file_type, is_duplicate
            FROM files
            WHERE parent_folder != ''
              AND is_library_file = 0
            ORDER BY parent_folder, confidence_score DESC
        """).fetchall()

        if not all_files:
            log.warning("build_book_groups: no files found in DB.")
            return 0

        count = 0
        for folder, file_iter in groupby(all_files, key=lambda r: r["parent_folder"]):
            files = list(file_iter)

            best_author = _pick_best(files, "extracted_author")
            raw_title   = _pick_best(files, "extracted_title")

            clean_title, series_seq = _extract_series(raw_title)
            series_name = ""

            file_count     = len(files)
            audio_files    = [r for r in files if r["file_type"] == "audio"]
            total_duration = sum((r["duration_seconds"] or 0) for r in audio_files)
            confidence     = max((r["confidence_score"] or 0) for r in files)
            target         = build_target_path(
                output_root_dir, best_author, clean_title, series_name, series_seq
            ) if output_root_dir else ""

            # Skip folders where every audio file is a duplicate
            is_dup = (
                len(audio_files) > 0
                and all(r["is_duplicate"] for r in audio_files)
            )
            if is_dup:
                log.debug(f"build_book_groups: skipping duplicate folder {folder!r}")
                continue

            conn.execute("""
                INSERT INTO books
                    (parent_folder, best_author, best_title, series_name,
                     series_sequence, file_count, total_duration,
                     confidence_score, target_abs_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(parent_folder) DO UPDATE SET
                    best_author=excluded.best_author,
                    best_title=excluded.best_title,
                    series_name=excluded.series_name,
                    series_sequence=excluded.series_sequence,
                    file_count=excluded.file_count,
                    total_duration=excluded.total_duration,
                    confidence_score=excluded.confidence_score,
                    target_abs_path=excluded.target_abs_path,
                    updated_at=datetime('now')
            """, (folder, best_author, clean_title, series_name, series_seq,
                  file_count, total_duration, confidence, target))
            count += 1

        conn.commit()

    log.info(f"build_book_groups: {count} book groups written.")
    return count


def update_book(parent_folder: str, updates: dict) -> None:
    """Persist manual edits to a books row."""
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [parent_folder]
    with _conn() as c:
        c.execute(
            f"UPDATE books SET {sets}, updated_at=datetime('now') "
            f"WHERE parent_folder=?",
            vals,
        )
    log.info(f"Book updated: {Path(parent_folder).name}")


def get_hash_duplicate_files() -> list[dict]:
    """
    Return all file records that are hash-exact duplicates AND flagged is_duplicate=1.

    These are the *lower-quality* copies identified by flag_duplicates() — the
    originals (is_duplicate=0) that share the same file_hash are the keepers.

    Returns a list of row dicts with keys: abs_path, file_hash, file_size.
    Returns an empty list if flag_duplicates() has not been run yet.
    """
    with _conn() as c:
        rows = c.execute("""
            SELECT abs_path, file_hash, file_size
            FROM files
            WHERE is_duplicate = 1
              AND file_hash IS NOT NULL
              AND file_hash IN (
                  SELECT file_hash
                  FROM files
                  WHERE file_hash IS NOT NULL
                  GROUP BY file_hash
                  HAVING COUNT(*) > 1
              )
            ORDER BY abs_path
        """).fetchall()
    return [dict(r) for r in rows]


def delete_file(abs_path: str) -> None:
    """Remove a single file record from the files table."""
    with _conn() as c:
        c.execute("DELETE FROM files WHERE abs_path = ?", (abs_path,))
    log.info(f"File deleted from DB: {Path(abs_path).name}")


def get_duplicate_book_source_files() -> list[dict]:
    """
    Return every SOURCE file (is_library_file=0) that belongs to a book group
    flagged as is_duplicate=1.

    These are the files the user wants to physically remove from disk after
    'Re-Compare with Output' or 'Flag Duplicates' confirms that the audiobook
    already exists in the organised library — otherwise the next scan will
    re-import them.

    Returns rows with keys: abs_path, parent_folder, file_size, file_type.
    """
    with _conn() as c:
        rows = c.execute("""
            SELECT f.abs_path, f.parent_folder, f.file_size, f.file_type
            FROM files f
            JOIN books b ON b.parent_folder = f.parent_folder
            WHERE b.is_duplicate = 1
              AND f.is_library_file = 0
            ORDER BY f.parent_folder, f.file_name
        """).fetchall()
    return [dict(r) for r in rows]


def get_duplicate_book_folders() -> list[str]:
    """Return the distinct source parent_folder paths of all duplicate-flagged books."""
    with _conn() as c:
        rows = c.execute("""
            SELECT DISTINCT b.parent_folder
            FROM books b
            WHERE b.is_duplicate = 1
        """).fetchall()
    return [r["parent_folder"] for r in rows]


def snapshot_duplicate_book_folders() -> set:
    """
    Return the set of parent_folder paths currently marked is_duplicate=1.
    Used before Re-Compare so the caller can diff afterwards to find only the
    books that THIS re-compare flagged (excluding ones that were already dups).
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT parent_folder FROM books WHERE is_duplicate = 1"
        ).fetchall()
    return {r["parent_folder"] for r in rows}


def unflag_duplicate_books(folders) -> int:
    """
    Clear is_duplicate=1 on the listed book rows.  Used when the user cancels
    out of the post-Re-Compare action dialog — they want those rows visible
    again rather than hidden as duplicates.

    Returns the number of book rows updated.
    """
    folders = [str(f) for f in folders]
    if not folders:
        return 0
    with _conn() as c:
        placeholders = ",".join("?" * len(folders))
        cur = c.execute(
            f"UPDATE books SET is_duplicate = 0 "
            f"WHERE parent_folder IN ({placeholders})",
            folders,
        )
        n = cur.rowcount
    log.info(f"unflag_duplicate_books: cleared is_duplicate on {n} book(s).")
    return n


def delete_book(parent_folder: str) -> None:
    """Remove a book group from the books table."""
    with _conn() as c:
        c.execute("DELETE FROM books WHERE parent_folder = ?", (parent_folder,))
    log.info(f"Book deleted from DB: {Path(parent_folder).name}")


def fetch_book(parent_folder: str):
    """Fetch a single row from the books table. Returns None if not found."""
    with _conn() as c:
        return c.execute("""
            SELECT parent_folder, best_author, best_title, series_name,
                   series_sequence, narrator, isbn, asin,
                   file_count, total_duration, audible_runtime_min,
                   confidence_score, is_duplicate, manual_review,
                   local_title, local_author, local_series, local_sequence,
                   local_id, local_synced,
                   llm_title, llm_author, llm_series, llm_sequence,
                   llm_id, llm_synced, llm_source,
                   cat_title, cat_author, cat_series, cat_sequence,
                   cat_isbn, cat_asin, cat_synced, cat_source,
                   user_title, user_author, user_series, user_sequence,
                   user_narrator, user_isbn, user_asin,
                   user_edited, user_edited_at,
                   google_title, google_author, google_isbn, metadata_source,
                   target_abs_path
            FROM books WHERE parent_folder = ?
        """, (parent_folder,)).fetchone()


def fetch_books(limit: int = 500, hide_duplicates: bool = False,
                review_only: bool = False) -> list:
    """Fetch the books table ordered by author → series → sequence → title."""
    clauses = []
    if hide_duplicates:
        clauses.append("is_duplicate = 0")
    if review_only:
        clauses.append("manual_review = 1")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as c:
        return c.execute(f"""
            SELECT parent_folder, best_author, best_title, series_name,
                   series_sequence, narrator, isbn, asin,
                   file_count, total_duration, audible_runtime_min,
                   confidence_score, is_duplicate, manual_review,
                   local_title, local_author, local_series, local_sequence,
                   local_id, local_synced,
                   llm_title, llm_author, llm_series, llm_sequence,
                   llm_id, llm_synced, llm_source,
                   cat_title, cat_author, cat_series, cat_sequence,
                   cat_isbn, cat_asin, cat_synced, cat_source,
                   user_title, user_author, user_series, user_sequence,
                   user_narrator, user_isbn, user_asin,
                   user_edited, user_edited_at,
                   google_title, google_author, google_isbn, metadata_source,
                   target_abs_path
            FROM books
            {where}
            ORDER BY LOWER(TRIM(best_author)),
                     LOWER(TRIM(series_name)),
                     series_sequence,
                     LOWER(TRIM(best_title))
            LIMIT ?
        """, (limit,)).fetchall()


def _get_sample_filenames_direct(parent_folder: str, n: int = 3) -> list:
    """Return up to n filenames from the given folder (opens its own connection)."""
    with _conn() as conn:
        return _get_sample_filenames(conn, parent_folder, n)


def _get_sample_filenames(conn, parent_folder: str, n: int = 3) -> list:
    """Return up to n audio file_names from the given folder, preferring audio."""
    rows = conn.execute(
        "SELECT file_name FROM files "
        "WHERE parent_folder = ? AND file_type = 'audio' "
        "LIMIT ?",
        (parent_folder, n),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT file_name FROM files WHERE parent_folder = ? LIMIT ?",
            (parent_folder, n),
        ).fetchall()
    return [r["file_name"] for r in rows]


def _build_llm_context(folder_path: str, sample_files: list) -> str:
    """
    Build a rich context string from the full folder path and sample filenames
    for use as LLM input.
    """
    p = Path(folder_path)
    parts = [pt for pt in p.parts if pt and pt not in ("/", "\\") and pt != p.anchor]

    lines = [f"Full path : {folder_path}"]
    if parts:
        lines.append(f"Folder    : {parts[-1]}")
    if len(parts) >= 2:
        lines.append(f"Parent    : {parts[-2]}")
    if len(parts) >= 3:
        lines.append(f"Grandparent: {parts[-3]}")

    for i, name in enumerate(sample_files, 1):
        lines.append(f"File {i}    : {name}")

    return "\n".join(lines)


def _sanitize_title_for_search(text: str) -> str:
    """
    Clean a raw best_title (or folder name) into a compact search term
    suitable for API queries.
    """
    if not text:
        return ""

    t = text
    t = _re.sub(r'\([^)]*\)', '', t)
    t = _re.sub(r'\[[^\]]*\]', '', t)
    t = _re.sub(r'\s*-\s*copy\s*$', '', t, flags=_re.IGNORECASE)
    t = _re.sub(r'^\d{1,4}(?:\.\d+)?\s*[-–]\s*', '', t)
    t = _re.sub(r'\s+\d{4}\s+\d{3}-\d{3}\s*$', '', t)
    t = _re.sub(r'\s+\d{3}-\d{3}\s*$', '', t)
    t = _re.sub(r'\s+\d{4}\s*$', '', t)
    t = _re.sub(r'\s{2,}', ' ', t)
    t = t.strip(' -–_,.()')

    return t


def _infer_output_root(target_path: str, author: str) -> str:
    """
    Return the output root — the directory that contains the author folder.
    Falls back to grandparent of the target path when the author is not found.
    """
    if not target_path:
        return ""
    p = Path(target_path)
    clean_author = _sanitize_path_component(author).lower()

    if clean_author:
        parts = p.parts
        for i, part in enumerate(parts):
            if part.lower() == clean_author:
                root_parts = parts[:i]
                if root_parts:
                    return str(Path(*root_parts))
                break

    try:
        return str(p.parent.parent)
    except Exception:
        return str(p.parent)


# ---------------------------------------------------------------------------
# AI / API sync functions
# ---------------------------------------------------------------------------

def validate_and_fix_books(
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    request_delay: float = 0.5,
) -> dict:
    """Audible + Google Books sync for every row in the books table."""
    import time
    from audible_validator  import fetch_audible_data
    from google_books_api   import fetch_google_books_data

    with _conn() as conn:
        rows = conn.execute(
            "SELECT parent_folder, best_author, best_title, target_abs_path, asin, "
            "total_duration, user_edited FROM books ORDER BY best_author, best_title"
        ).fetchall()

    total       = len(rows)
    updated     = 0
    skipped     = 0
    google_hits = 0

    for idx, row in enumerate(rows, 1):
        folder      = row["parent_folder"]
        best_title  = row["best_title"]  or ""
        best_author = row["best_author"] or ""
        folder_name = str(Path(folder).name)

        clean_title = _sanitize_title_for_search(best_title) or \
                      _sanitize_title_for_search(folder_name)
        search_term = f"{clean_title} {best_author}".strip()

        if on_progress:
            on_progress(idx, total, clean_title or folder_name)

        known_dur = row["total_duration"] or None
        result = fetch_audible_data(search_term, known_duration_sec=known_dur)
        time.sleep(request_delay)

        if stop_event and stop_event.is_set():
            log.info("Audible sync stopped by user.")
            break

        if not result:
            log.debug(f"Audible miss — trying Google Books for {clean_title!r}")
            result = fetch_google_books_data(clean_title, best_author)
            if result:
                google_hits += 1
            else:
                skipped += 1
                log.info(f"[Audible] No match: {clean_title!r}")
                if on_result:
                    on_result(folder, search_term, None, "skipped")
                continue

        source      = result.get("source", "audible") or "audible"
        cat_title   = result["title"]
        cat_author  = result["author"]
        audible_asin  = result.get("asin", "") or ""
        existing_asin = row["asin"] or ""
        runtime_min   = result.get("runtime_min")
        user_locked   = bool(row["user_edited"])

        existing_target = row["target_abs_path"] or ""
        output_root     = _infer_output_root(existing_target, best_author)

        # Shadow columns — always written
        cat_shadow = {
            "cat_title":    cat_title,
            "cat_author":   cat_author,
            "cat_series":   result.get("series_name", ""),
            "cat_sequence": result.get("series_sequence", ""),
            "cat_isbn":     result.get("isbn", ""),
            "cat_asin":     audible_asin,
            "cat_source":   source,
        }

        # Match/diverge — does the catalogue agree with what we already have?
        titles_match  = (cat_title.lower() == best_title.lower()) if cat_title else False
        best_is_empty = not best_title

        if user_locked:
            log.info(
                f"[{source}] Locked for {Path(folder).name!r} — "
                f"user edit in place, shadow-only write."
            )
            updates = {**cat_shadow, "cat_synced": 3}
            update_book(folder, updates)
            if on_update:
                on_update(folder)
            if on_result:
                on_result(folder, search_term, result, "locked")
            continue
        elif titles_match or best_is_empty:
            new_target  = build_target_path(
                output_root, cat_author, cat_title,
                result.get("series_name", ""), result.get("series_sequence", ""),
            )
            id_conflict = bool(
                existing_asin and audible_asin and existing_asin != audible_asin
            )
            if id_conflict:
                log.warning(
                    f"ASIN conflict for {Path(folder).name!r}: "
                    f"existing={existing_asin!r}  catalogue={audible_asin!r} — flagged for review"
                )
            updates = {
                **cat_shadow,
                "cat_synced":          1,
                "best_author":         cat_author,
                "best_title":          cat_title,
                "series_name":         result.get("series_name", ""),
                "series_sequence":     result.get("series_sequence", ""),
                "narrator":            result.get("narrator", ""),
                "asin":                audible_asin or existing_asin,
                "isbn":                result.get("isbn", ""),
                "manual_review":       1 if id_conflict else 0,
                "target_abs_path":     new_target,
                "audible_runtime_min": runtime_min if runtime_min else 0,
                "metadata_source":     source,
            }
            if source == "google":
                updates["google_title"]  = cat_title
                updates["google_author"] = cat_author
                updates["google_isbn"]   = result.get("isbn", "")
            outcome = "confirmed"
        else:
            log.info(
                f"[{source}] Diverges for {Path(folder).name!r}: "
                f"{source}={cat_title!r}  best={best_title!r}"
            )
            updates = {**cat_shadow, "cat_synced": 2}
            outcome = "diverged"

        update_book(folder, updates)
        if on_update:
            on_update(folder)
        updated += 1
        _series = result.get("series_name", "")
        _asin   = audible_asin
        log.info(
            f"[Audible] {outcome.title()}: {cat_title!r} by {cat_author}"
            + (f" | Series: {_series}" if _series else "")
            + (f" | ASIN: {_asin}" if _asin else "")
            + (f" | via Google Books" if source == "google" else "")
        )
        if on_result:
            on_result(folder, search_term, result, outcome)

    log.info(
        f"validate_and_fix_books: {total} checked, {updated} updated "
        f"({google_hits} via Google), {skipped} skipped."
    )
    return {"total": total, "updated": updated, "skipped": skipped,
            "google_hits": google_hits}


def google_validate_and_fix_books(
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    request_delay: float = 0.5,
) -> dict:
    """Standalone Google Books sync — match/diverge logic via _cat_sync_books."""
    from google_books_api import fetch_google_books_data
    with _conn() as conn:
        rows = conn.execute(
            "SELECT parent_folder, best_author, best_title, target_abs_path, asin, user_edited "
            "FROM books ORDER BY best_author, best_title"
        ).fetchall()
    return _cat_sync_books(
        "google",
        lambda title, author, row: fetch_google_books_data(title, author),
        rows, on_progress, on_update, on_result, stop_event, request_delay,
    )


def openlibrary_validate_and_fix_books(
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    request_delay: float = 1.0,
) -> dict:
    """Open Library sync — match/diverge logic via _cat_sync_books."""
    from openlibrary_validator import fetch_openlibrary_data
    with _conn() as conn:
        rows = conn.execute(
            "SELECT parent_folder, best_author, best_title, target_abs_path, asin, user_edited "
            "FROM books ORDER BY best_author, best_title"
        ).fetchall()
    return _cat_sync_books(
        "openlibrary",
        lambda title, author, row: fetch_openlibrary_data(title, author),
        rows, on_progress, on_update, on_result, stop_event, request_delay,
    )


def hardcover_validate_and_fix_books(
    api_key: str,
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    request_delay: float = 0.5,
) -> dict:
    """Hardcover GraphQL sync — match/diverge logic via _cat_sync_books."""
    from hardcover_validator import fetch_hardcover_data
    if not api_key:
        raise ValueError("Hardcover API key is required.")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT parent_folder, best_author, best_title, target_abs_path, asin, user_edited "
            "FROM books ORDER BY best_author, best_title"
        ).fetchall()
    return _cat_sync_books(
        "hardcover",
        lambda title, author, row: fetch_hardcover_data(title, author, api_key=api_key),
        rows, on_progress, on_update, on_result, stop_event, request_delay,
    )


def gemini_validate_and_fix_books(
    api_key: str,
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    request_delay: float = 4.0,
    batch_size: int = 25,
) -> dict:
    """Gemini 2.5 Flash sync — match/diverge logic via _llm_sync_books."""
    from gemini_validator import parse_book_metadata, parse_book_metadata_gemini_batch
    if not api_key:
        raise ValueError("Gemini API key is required.")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT parent_folder, best_author, best_title, target_abs_path, user_edited "
            "FROM books ORDER BY best_author, best_title"
        ).fetchall()
        samples = {r["parent_folder"]: _get_sample_filenames(conn, r["parent_folder"]) for r in rows}
    # Batch mode disabled — runs one book per request so behaviour matches
    # the single-book right-click sync (which never had reliability issues).
    return _llm_sync_books(
        "gemini",
        lambda ctx: parse_book_metadata(ctx, api_key),
        rows, samples, on_progress, on_update, on_result, stop_event, request_delay,
    )


def _cat_sync_books(
    source: str,
    fetch_fn,
    rows: list,
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    request_delay: float = 1.0,
) -> dict:
    """
    Shared match/diverge logic for all Catalogue lookup sources
    (Google Books, Open Library, Hardcover, and the standalone Audible path).

    fetch_fn(clean_title, best_author, row) → result dict or None

    - If the catalogue title agrees with best_title (or best_title is empty)
      → update best_* AND write cat_* shadow columns, cat_synced=1
    - If the catalogue disagrees
      → write only cat_* shadow columns, cat_synced=2, leave best_* untouched
    """
    import time

    total     = len(rows)
    confirmed = 0
    diverged  = 0
    skipped   = 0

    for idx, row in enumerate(rows, 1):
        folder      = row["parent_folder"]
        folder_name = Path(folder).name
        best_title  = (row["best_title"]  or "").strip()
        best_author = (row["best_author"] or "").strip()

        clean_title = _sanitize_title_for_search(best_title) or \
                      _sanitize_title_for_search(folder_name)
        sent = f"{clean_title} {best_author}".strip()

        if on_progress:
            on_progress(idx, total, clean_title or folder_name)

        result = fetch_fn(clean_title, best_author, row)
        time.sleep(request_delay)

        if stop_event and stop_event.is_set():
            log.info(f"[{source}] sync stopped by user.")
            break

        if not result:
            skipped += 1
            log.info(f"[{source}] No match: {clean_title!r}")
            if on_result:
                on_result(folder, sent, None, "skipped")
            continue

        cat_title  = result["title"]
        cat_author = result["author"]

        titles_match  = (cat_title.lower() == best_title.lower()) if cat_title else False
        best_is_empty = not best_title
        user_locked   = bool(row["user_edited"])

        existing_target = row["target_abs_path"] or ""
        output_root     = _infer_output_root(existing_target, best_author)
        existing_asin   = row["asin"] or ""

        # Shadow columns — always written regardless of match/diverge or lock
        cat_shadow = {
            "cat_title":    cat_title,
            "cat_author":   cat_author,
            "cat_series":   result.get("series_name", ""),
            "cat_sequence": result.get("series_sequence", ""),
            "cat_isbn":     result.get("isbn", ""),
            "cat_asin":     result.get("asin", ""),
            "cat_source":   source,
        }

        if user_locked:
            # Book was manually edited — write shadow columns only, never touch best_*
            log.info(
                f"[{source}] Locked for {folder_name!r} — "
                f"user edit in place, shadow-only write."
            )
            updates = {**cat_shadow, "cat_synced": 3}  # 3 = locked
            update_book(folder, updates)
            if on_update:
                on_update(folder)
            if on_result:
                on_result(folder, sent, result, "locked")
            continue
        elif titles_match or best_is_empty:
            confirmed += 1
            new_target = build_target_path(
                output_root, cat_author, cat_title,
                result.get("series_name", ""), result.get("series_sequence", ""),
            )
            updates = {
                **cat_shadow,
                "cat_synced":      1,
                "best_author":     cat_author,
                "best_title":      cat_title,
                "series_name":     result.get("series_name", ""),
                "series_sequence": result.get("series_sequence", ""),
                "narrator":        result.get("narrator", ""),
                "isbn":            result.get("isbn", ""),
                "asin":            result.get("asin", "") or existing_asin,
                "metadata_source": source,
                "target_abs_path": new_target,
            }
            if result.get("runtime_min"):
                updates["audible_runtime_min"] = result["runtime_min"]
            outcome = "confirmed"
        else:
            diverged += 1
            log.info(
                f"[{source}] Diverges for {folder_name!r}: "
                f"{source}={cat_title!r}  best={best_title!r}"
            )
            updates = {**cat_shadow, "cat_synced": 2}
            outcome = "diverged"

        update_book(folder, updates)
        if on_update:
            on_update(folder)
        log.info(
            f"[{source}] {outcome.title()}: {cat_title!r} by {cat_author}"
            + (f" | Series: {result.get('series_name','')}" if result.get("series_name") else "")
            + (f" | ISBN: {result.get('isbn','')}" if result.get("isbn") else "")
        )
        if on_result:
            on_result(folder, sent, result, outcome)

    log.info(
        f"{source}_validate_and_fix_books: {total} checked, "
        f"{confirmed} confirmed, {diverged} diverged, {skipped} skipped."
    )
    return {
        "total":     total,
        "confirmed": confirmed,
        "diverged":  diverged,
        "skipped":   skipped,
        "updated":   confirmed + diverged,   # alias used by poll done-handlers
    }


def _llm_sync_books(
    source: str,
    parse_fn,
    rows: list,
    samples: dict,
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    request_delay: float = 1.0,
    batch_parse_fn=None,
    batch_size: int = 25,
) -> dict:
    """
    Shared match/diverge logic for all LLM identify sources
    (Gemini, Groq, Claude, ChatGPT, Local).

    Batching (batch_parse_fn + batch_size):
      Up to batch_size books are sent in a single API call.  If the batch
      call fails to parse (returns None), every book in that batch is
      retried individually via parse_fn — so nothing is lost.

    Per-book outcomes:
      - LLM title agrees with best_title (or best_title is empty)
        → update best_* and llm_* shadow columns, llm_synced=1
      - LLM disagrees
        → write only llm_* shadow columns, llm_synced=2, leave best_* untouched
      - user_edited=1
        → shadow-only write, llm_synced=3 (locked)
    """
    import time

    # ------------------------------------------------------------------
    # Fuzzy title matcher
    # ------------------------------------------------------------------
    # LLM responses are usually a clean canonical title; best_title is
    # whatever the scanner extracted from messy filenames — typically with
    # parenthetical noise like "(Unabridged)", "(read by ...)", series
    # numbers in parens, or unbalanced parens.  Exact string equality
    # therefore misses almost every real match.
    #
    # Normaliser:
    #   - lowercase
    #   - strip closed parens / brackets:  "(...)" "[...]"
    #   - strip unclosed parens to end:    "(text without close"
    #   - drop leading articles:           "the" "a" "an"
    #   - reduce to alphanumerics only
    #
    # Match rule:  exact equality after normalisation, OR one normalised
    # form is contained in the other (lets "Halo: New Blood" match the
    # noisier "Halo New Blood (Unabridged)").
    def _norm_title(s: str) -> str:
        if not s:
            return ""
        s = s.lower()
        s = _re.sub(r"\([^)]*\)", "", s)
        s = _re.sub(r"\[[^\]]*\]", "", s)
        s = _re.sub(r"\(.*$", "", s)
        s = _re.sub(r"\[.*$", "", s)
        # Drop leading articles + common connectives so "Three Tales of Dunk and Egg"
        # matches "Three Tales of Dunk & Egg" (the & gets stripped as non-alpha,
        # so we have to drop the "and" too).
        s = _re.sub(r"\b(the|a|an|and|of)\b", "", s)
        s = _re.sub(r"[^a-z0-9]+", "", s)
        return s

    def _fuzzy_titles_match(llm_title: str, best_title: str) -> bool:
        a = _norm_title(llm_title)
        b = _norm_title(best_title)
        if not a or not b:
            return False
        if a == b:
            return True
        # Short titles (< 5 chars after norm) require exact match — substring
        # would create false positives like "war" matching "warhammer".
        if len(a) < 5 or len(b) < 5:
            return False
        return a in b or b in a

    # ------------------------------------------------------------------
    # Junk-title detector — patterns that look like CD-ripper / track-
    # number codes rather than real titles.  When best_title matches one
    # of these, we treat it as no-better-than-empty: the LLM's answer
    # auto-applies even without a fuzzy match (since the LLM's answer
    # is almost certainly better than the meaningless code).
    #
    # Examples that should match (auto-apply LLM result):
    #   D03, D03.01-18, CD01, CD-01, CD 1
    #   Disc 01, Track 001, Volume 2, Part 1
    #   01-02, 01.02.03, 001_002_003
    #
    # Examples that should NOT match (real titles — keep cautious):
    #   Dragon, 1984, A Game of Thrones,
    #   001 The NUMA Files 10 - The Storm,
    #   1996 - A Song of Ice and Fire
    # ------------------------------------------------------------------
    _JUNK_TITLE_PATTERNS = [
        # Letter-prefixed code: D03, CD01, CD-1, D03.01-18, D03_01_18
        _re.compile(r"^[A-Za-z]{1,3}\d+([.\-_ ]\d+)*$", _re.IGNORECASE),
        # Explicit disc / track / volume / part labels (with optional range)
        _re.compile(
            r"^(CD|Disc|Disk|Track|Part|Vol|Volume|Chapter|Ch|File|Side)"
            r"\s*[.\-_]?\s*\d+(\s*[-/]\s*\d+)?$",
            _re.IGNORECASE,
        ),
        # Pure multi-segment numbers: 01-02, 01.02.03, 001_002, 01 02 03
        _re.compile(r"^\d+[.\-_ ]+\d+([.\-_ ]+\d+)*$"),
    ]

    def _is_junk_title(s: str) -> bool:
        """True if *s* looks like a CD-ripper code rather than a real title."""
        if not s:
            return False  # empty handled separately as best_is_empty
        s = s.strip()
        if not s:
            return False
        return any(p.match(s) for p in _JUNK_TITLE_PATTERNS)

    # Filter out books in user-tagged skipped folders
    _skip_paths = get_skipped_folders()
    if _skip_paths:
        orig_count = len(rows)
        rows = [r for r in rows if r["parent_folder"] not in _skip_paths]
        skipped_folder_count = orig_count - len(rows)
        if skipped_folder_count:
            log.info(
                f"[{source}] Skipping {skipped_folder_count} book(s) "
                f"in user-tagged skip folders."
            )

    total     = len(rows)
    confirmed = 0
    diverged  = 0
    skipped   = 0

    # ------------------------------------------------------------------
    # Inner helper: apply match/diverge logic for one (row, result) pair
    # ------------------------------------------------------------------
    def _apply_result(row, context, result):
        nonlocal confirmed, diverged, skipped

        folder      = row["parent_folder"]
        folder_name = Path(folder).name
        best_title  = (row["best_title"]  or "").strip()
        best_author = (row["best_author"] or "").strip()

        if not result:
            skipped += 1
            log.info(f"[{source}] No match: {folder_name!r}")
            if on_result:
                on_result(folder, context, None, "skipped")
            return

        llm_title  = result["title"]
        llm_author = result["author"]
        llm_id     = result.get("asin", "") or result.get("isbn", "")

        titles_match  = _fuzzy_titles_match(llm_title, best_title)
        best_is_empty = not best_title
        best_is_junk  = _is_junk_title(best_title)
        user_locked   = bool(row["user_edited"])

        if best_is_junk and not titles_match:
            log.info(
                f"[{source}] best_title {best_title!r} looks like a CD/track "
                f"code — accepting LLM title {llm_title!r}"
            )

        if user_locked:
            log.info(
                f"[{source}] Locked for {folder_name!r} — "
                f"user edit in place, shadow-only write."
            )
            updates = {
                "llm_title":    llm_title,
                "llm_author":   llm_author,
                "llm_series":   result["series_name"],
                "llm_sequence": result["series_sequence"],
                "llm_id":       llm_id,
                "llm_synced":   3,
                "llm_source":   source,
            }
            update_book(folder, updates)
            if on_update:
                on_update(folder)
            if on_result:
                on_result(folder, context, result, "locked")
            return

        if titles_match or best_is_empty or best_is_junk:
            confirmed += 1
            existing_target = row["target_abs_path"] or ""
            output_root = _infer_output_root(existing_target, best_author)
            new_target  = build_target_path(
                output_root, llm_author, llm_title,
                result["series_name"], result["series_sequence"],
                asin=result.get("asin", ""), isbn=result.get("isbn", ""),
            )
            updates = {
                "best_author":     llm_author,
                "best_title":      llm_title,
                "series_name":     result["series_name"],
                "series_sequence": result["series_sequence"],
                "narrator":        result.get("narrator", ""),
                "isbn":            result.get("isbn", ""),
                "asin":            result.get("asin", ""),
                "llm_title":       llm_title,
                "llm_author":      llm_author,
                "llm_series":      result["series_name"],
                "llm_sequence":    result["series_sequence"],
                "llm_id":          llm_id,
                "llm_synced":      1,
                "llm_source":      source,
                "target_abs_path": new_target,
                "metadata_source": source,
            }
            outcome = "confirmed"
        else:
            diverged += 1
            log.info(
                f"[{source}] Diverges for {folder_name!r}: "
                f"{source}={llm_title!r}  best={best_title!r}"
            )
            updates = {
                "llm_title":    llm_title,
                "llm_author":   llm_author,
                "llm_series":   result["series_name"],
                "llm_sequence": result["series_sequence"],
                "llm_id":       llm_id,
                "llm_synced":   2,
                "llm_source":   source,
            }
            outcome = "diverged"

        update_book(folder, updates)
        if on_update:
            on_update(folder)
        _series = result.get("series_name", "")
        log.info(
            f"[{source}] {outcome.title()}: {llm_title!r} by {llm_author}"
            + (f" | Series: {_series}" if _series else "")
        )
        if on_result:
            on_result(folder, context, result, outcome)

    # ------------------------------------------------------------------
    # Main loop — process in batches when batch_parse_fn is available
    # ------------------------------------------------------------------
    effective_batch = batch_size if batch_parse_fn else 1
    book_idx        = 0   # running 1-based counter for progress / detail header

    for batch_start in range(0, total, effective_batch):
        batch_rows     = rows[batch_start : batch_start + effective_batch]
        batch_contexts = [
            _build_llm_context(r["parent_folder"], samples.get(r["parent_folder"], []))
            for r in batch_rows
        ]
        batch_end = batch_start + len(batch_rows)

        # --- Try batch call -------------------------------------------
        batch_results = None
        if batch_parse_fn:
            try:
                batch_results = batch_parse_fn(batch_contexts)
            except (TokenLimitError, RateLimitError):
                if stop_event:
                    stop_event.set()
                raise
            except Exception as e:
                log.warning(
                    f"[{source}] batch call failed for books "
                    f"{batch_start + 1}–{batch_end}, falling back to individual: {e}"
                )
                batch_results = None   # triggers fallback below

        # --- Fallback: individual calls if batch failed ---------------
        if batch_results is None:
            for row, context in zip(batch_rows, batch_contexts):
                book_idx += 1
                if on_progress:
                    on_progress(book_idx, total, Path(row["parent_folder"]).name)
                try:
                    result = parse_fn(context)
                except (TokenLimitError, RateLimitError):
                    if stop_event:
                        stop_event.set()
                    raise
                _apply_result(row, context, result)
                if stop_event and stop_event.is_set():
                    break
        else:
            # --- Process successful batch results ---------------------
            # Progress fires per-book so the detail header counter ticks
            # smoothly (1, 2, 3 …) rather than jumping in steps of batch_size.
            for row, context, result in zip(batch_rows, batch_contexts, batch_results):
                book_idx += 1
                if on_progress:
                    on_progress(book_idx, total, Path(row["parent_folder"]).name)
                _apply_result(row, context, result)

        time.sleep(request_delay)

        if stop_event and stop_event.is_set():
            log.info(f"[{source}] sync stopped by user.")
            break

    log.info(
        f"{source}_validate_and_fix_books: {total} checked, "
        f"{confirmed} confirmed, {diverged} diverged, {skipped} skipped."
    )
    return {
        "total":     total,
        "confirmed": confirmed,
        "diverged":  diverged,
        "skipped":   skipped,
        "updated":   confirmed + diverged,
    }


def groq_validate_and_fix_books(
    api_key: str,
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    request_delay: float = 2.0,
    batch_size: int = 25,
) -> dict:
    """Groq LLaMA sync — match/diverge logic via _llm_sync_books."""
    from groq_validator import parse_book_metadata_groq, parse_book_metadata_groq_batch
    if not api_key:
        raise ValueError("Groq API key is required.")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT parent_folder, best_author, best_title, target_abs_path, user_edited "
            "FROM books ORDER BY best_author, best_title"
        ).fetchall()
        samples = {r["parent_folder"]: _get_sample_filenames(conn, r["parent_folder"]) for r in rows}
    # Batch mode disabled — see comment in gemini_validate_and_fix_books.
    return _llm_sync_books(
        "groq",
        lambda ctx: parse_book_metadata_groq(ctx, api_key),
        rows, samples, on_progress, on_update, on_result, stop_event, request_delay,
    )


def claude_validate_and_fix_books(
    api_key: str,
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    request_delay: float = 0.5,
    batch_size: int = 25,
) -> dict:
    """Anthropic Claude sync — match/diverge logic via _llm_sync_books."""
    from claude_validator import parse_book_metadata_claude, parse_book_metadata_claude_batch
    if not api_key:
        raise ValueError("Anthropic API key is required.")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT parent_folder, best_author, best_title, target_abs_path, user_edited "
            "FROM books ORDER BY best_author, best_title"
        ).fetchall()
        samples = {r["parent_folder"]: _get_sample_filenames(conn, r["parent_folder"]) for r in rows}
    # Batch mode disabled — see comment in gemini_validate_and_fix_books.
    return _llm_sync_books(
        "claude",
        lambda ctx: parse_book_metadata_claude(ctx, api_key),
        rows, samples, on_progress, on_update, on_result, stop_event, request_delay,
    )


def chatgpt_validate_and_fix_books(
    api_key: str,
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    request_delay: float = 0.5,
    batch_size: int = 25,
) -> dict:
    """OpenAI ChatGPT sync — match/diverge logic via _llm_sync_books."""
    from chatgpt_validator import parse_book_metadata_chatgpt, parse_book_metadata_chatgpt_batch
    if not api_key:
        raise ValueError("OpenAI API key is required.")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT parent_folder, best_author, best_title, target_abs_path, user_edited "
            "FROM books ORDER BY best_author, best_title"
        ).fetchall()
        samples = {r["parent_folder"]: _get_sample_filenames(conn, r["parent_folder"]) for r in rows}
    # Batch mode disabled — see comment in gemini_validate_and_fix_books.
    return _llm_sync_books(
        "chatgpt",
        lambda ctx: parse_book_metadata_chatgpt(ctx, api_key),
        rows, samples, on_progress, on_update, on_result, stop_event, request_delay,
    )


def local_validate_and_fix_books(
    on_progress=None,
    on_update=None,
    on_result=None,
    stop_event=None,
    base_url: str = "http://127.0.0.1:1234",
    model: str = "local-model",
    batch_size: int = 25,
) -> dict:
    """Local LLM (LM Studio / Ollama) sync — match/diverge logic via _llm_sync_books."""
    from local_llm_validator import parse_book_metadata_local, parse_book_metadata_local_batch
    with _conn() as conn:
        rows = conn.execute(
            "SELECT parent_folder, best_author, best_title, target_abs_path, user_edited "
            "FROM books ORDER BY best_author, best_title"
        ).fetchall()
        samples = {r["parent_folder"]: _get_sample_filenames(conn, r["parent_folder"]) for r in rows}
    # Batch mode disabled — local models often have tiny context windows
    # (4096 tokens) that a 25-book batch blows past with HTTP 400.  One per
    # request matches the single-book sync path that works reliably.
    return _llm_sync_books(
        "local",
        lambda ctx: parse_book_metadata_local(ctx, base_url=base_url, model=model),
        rows, samples, on_progress, on_update, on_result, stop_event, request_delay=0,
    )


# ---------------------------------------------------------------------------
# Read / export
# ---------------------------------------------------------------------------

def get_stats() -> dict:
    with _conn() as c:
        row = c.execute("""
            SELECT
                COUNT(*)                    AS total,
                SUM(file_type = 'audio')    AS audio,
                SUM(file_type = 'ebook')    AS ebook,
                SUM(file_type = 'image')    AS image,
                SUM(file_type = 'other')    AS other,
                SUM(is_duplicate = 1)       AS duplicates,
                SUM(confidence_score = 100) AS conf_high,
                SUM(confidence_score = 80)  AS conf_mid,
                SUM(confidence_score <= 50) AS conf_low
            FROM files
        """).fetchone()
    return dict(row) if row else {}


def fetch_preview(
    limit: int = 2000,
    filter_type: Optional[str] = None,
    max_confidence: Optional[int] = None,
    dupes_only: bool = False,
) -> list:
    clauses = []
    params: list = []

    if filter_type:
        clauses.append("file_type = ?")
        params.append(filter_type)
    if max_confidence is not None:
        clauses.append("confidence_score <= ?")
        params.append(max_confidence)
    if dupes_only:
        clauses.append("is_duplicate = 1")

    # Always exclude output/library files — they are already organised
    clauses.insert(0, "is_library_file = 0")

    where = "WHERE " + " AND ".join(clauses)

    sql = f"""
        SELECT abs_path, file_type, extracted_title, extracted_author,
               extension, file_size, confidence_score, is_duplicate,
               duration_seconds, bitrate, parent_folder
        FROM files
        {where}
        ORDER BY extracted_author COLLATE NOCASE, extracted_title COLLATE NOCASE
        LIMIT ?
    """
    params.append(limit)
    with _conn() as c:
        return c.execute(sql, params).fetchall()


def export_csv(out_path: str) -> int:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM files ORDER BY extracted_author COLLATE NOCASE, "
            "extracted_title COLLATE NOCASE"
        ).fetchall()
        if not rows:
            return 0
        cols = list(rows[0].keys())
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows([tuple(r) for r in rows])
    log.info(f"Exported {len(rows)} rows to {out_path}")
    return len(rows)


def export_books_csv(out_path: str) -> int:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM books ORDER BY best_author, best_title"
        ).fetchall()
        if not rows:
            return 0
        cols = list(rows[0].keys())
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows([tuple(r) for r in rows])
    log.info(f"Books CSV exported: {out_path} ({len(rows)} rows)")
    return len(rows)
