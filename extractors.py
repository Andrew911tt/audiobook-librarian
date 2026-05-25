"""
Waterfall metadata extraction:
  1. Embedded tags (mutagen / ebooklib)  → confidence 100
  2. Filename regex patterns             → confidence 80
  3. Parent / grandparent folder names   → confidence 50

Also provides compute_hash() and clean_noise().
"""

import re
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Noise filter
# ---------------------------------------------------------------------------

_BRACKET_NOISE = re.compile(
    r'\[(?:unabridged|retail|mp3|flac|m4b|m4a|aac|ogg|audiobook|audible|\d{4}|\d+\s*kbps)[^\]]*\]',
    re.IGNORECASE,
)
_PAREN_NOISE = re.compile(
    r'\((?:unabridged|retail|mp3|flac|m4b|m4a|aac|ogg|audiobook|audible|\d{4}|\d+\s*kbps)[^\)]*\)',
    re.IGNORECASE,
)
_INLINE_NOISE = re.compile(
    r'\b(?:unabridged|retail|audiobook|audiobooks|v\s*\d+|cd\s*\d+|disc\s*\d+|'
    r'part\s*\d+|chapter\s*\d+|mp3|flac|m4b|m4a|aac|ogg|\d+\s*kbps)\b',
    re.IGNORECASE,
)


def clean_noise(text: str) -> str:
    if not text:
        return ""
    text = _BRACKET_NOISE.sub("", text)
    text = _PAREN_NOISE.sub("", text)
    text = _INLINE_NOISE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -_,.()")


# ---------------------------------------------------------------------------
# SHA-256 hash of first 10 MB
# ---------------------------------------------------------------------------

_HASH_LIMIT = 10 * 1024 * 1024  # 10 MB


def compute_hash(filepath: Path) -> Optional[str]:
    sha = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            remaining = _HASH_LIMIT
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                sha.update(chunk)
                remaining -= len(chunk)
        return sha.hexdigest()
    except OSError as e:
        log.warning(f"Hash failed [{filepath.name}]: {e}")
        return None


# ---------------------------------------------------------------------------
# Extraction result
# ---------------------------------------------------------------------------

@dataclass
class Extraction:
    title: str = ""
    author: str = ""
    duration_seconds: Optional[float] = None
    bitrate: Optional[int] = None
    confidence: int = 0


# ---------------------------------------------------------------------------
# Step 1 — Embedded tags
# ---------------------------------------------------------------------------

def _read_audio_stream_info(filepath: Path) -> tuple:
    """
    Open the audio file once and return (duration_seconds, bitrate).

    This is intentionally separate from tag reading — mutagen objects evaluate
    as False in a boolean context when no tags are embedded, but the stream
    info (length, bitrate) is always valid.  Using `is not None` rather than
    truthiness ensures tag-free files still report their duration correctly.

    Returns (None, None) if the file cannot be opened or has no stream info.
    """
    try:
        import mutagen
        track = mutagen.File(str(filepath))
        if track is not None and hasattr(track, "info"):
            return (
                getattr(track.info, "length",  None),
                getattr(track.info, "bitrate", None),
            )
    except Exception as ex:
        log.debug(f"Audio stream info failed [{filepath.name}]: {ex}")
    return None, None


def _audio_tags(filepath: Path) -> Optional[Extraction]:
    """
    Extract title/author from embedded tags only — no duration here.
    Duration is always read separately by _read_audio_stream_info so it is
    available even when tags are absent.

    Returns None if the file has no usable title + author tags, letting the
    waterfall continue to filename regex and folder fallback.
    """
    try:
        import mutagen
        track = mutagen.File(str(filepath), easy=True)
        if track is None:
            return None

        def g(key):
            val = track.get(key)
            return clean_noise(str(val[0])) if val else ""

        title  = g("title") or g("album")
        author = g("artist") or g("composer") or g("albumartist")

        if not (title and author):
            return None   # fall through to filename / folder waterfall

        return Extraction(title=title, author=author)
    except Exception as ex:
        log.debug(f"Audio tag fail [{filepath.name}]: {ex}")
        return None


def _epub_tags(filepath: Path) -> Optional[Extraction]:
    try:
        from ebooklib import epub
        book = epub.read_epub(str(filepath), options={"ignore_ncx": True})

        def dc(key):
            items = book.get_metadata("DC", key)
            if items:
                v = items[0]
                raw = v[0] if isinstance(v, tuple) else str(v)
                return clean_noise(raw)
            return ""

        title = dc("title")
        author = dc("creator")
        if not (title and author):
            return None
        return Extraction(title=title, author=author)
    except Exception as ex:
        log.debug(f"EPUB tag fail [{filepath.name}]: {ex}")
        return None


# ---------------------------------------------------------------------------
# Step 1b — PDF and Image tags
# ---------------------------------------------------------------------------

def _pdf_tags(filepath: Path) -> Optional[Extraction]:
    try:
        import fitz  # PyMuPDF
        with fitz.open(filepath) as doc:
            meta = doc.metadata
            title = clean_noise(meta.get("title", ""))
            author = clean_noise(meta.get("author", ""))
            if not (title and author):
                return None
            return Extraction(title=title, author=author)
    except Exception as ex:
        log.debug(f"PDF tag fail [{filepath.name}]: {ex}")
        return None


def _image_tags(filepath: Path) -> Optional[Extraction]:
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            img.size  # validate it opens cleanly
        return Extraction(title=filepath.stem, author="Cover Art", confidence=100)
    except Exception as ex:
        log.debug(f"Image read fail [{filepath.name}]: {ex}")
        return None


# ---------------------------------------------------------------------------
# Step 2 — Filename regex
# ---------------------------------------------------------------------------

# Each entry: (compiled_pattern, author_group_index, title_group_index)
_FILENAME_PATTERNS = [
    (re.compile(r"^(.+?)\s*-\s*(.+)$"),   1, 2),   # Author - Title
    (re.compile(r"^\[(.+?)\]\s*(.+)$"),   1, 2),   # [Author] Title
    (re.compile(r"^(.+?)\s*\(([^)]+)\)$"), 2, 1),  # Title (Author)
]


def _filename_regex(stem: str) -> Optional[Extraction]:
    cleaned = clean_noise(stem)
    for pattern, ag, tg in _FILENAME_PATTERNS:
        m = pattern.match(cleaned)
        if m:
            author = clean_noise(m.group(ag))
            title  = clean_noise(m.group(tg))
            if author and title:
                # Reject purely numeric authors (e.g. "00.04", "01", "12.5")
                # — these are track/series numbers, not real author names.
                # Fall through to folder fallback instead.
                if re.fullmatch(r"[\d\.\-\s]+", author):
                    continue
                # Truncate rather than completely discarding long matches
                if len(author) > 100: author = author[:97] + "..."
                if len(title) > 250:  title  = title[:247] + "..."
                return Extraction(title=title, author=author, confidence=80)
    return None


# ---------------------------------------------------------------------------
# Step 3 — Folder fallback
# ---------------------------------------------------------------------------

def _folder_fallback(filepath: Path) -> Extraction:
    parent      = filepath.parent
    grandparent = parent.parent

    title  = clean_noise(parent.name)
    author = clean_noise(grandparent.name)

    # Reject drive roots and single chars
    if len(author) < 2 or author in {"\\", "/", "."}:
        author = ""

    return Extraction(
        title=title or clean_noise(filepath.stem),
        author=author,
        confidence=50,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract(filepath: Path, file_type: str) -> Extraction:
    """Run waterfall extraction and return an Extraction with confidence score.

    For audio files the stream info (duration / bitrate) is always read once
    up-front — independently of whether embedded tags exist — so that even
    completely untagged files still report a valid duration.  Whichever
    text-metadata source wins (tags → filename regex → folder fallback) then
    receives the pre-read timing values before returning.
    """

    # ── Audio stream info (duration / bitrate) — read once, always ──────────
    # Kept separate from tag extraction because mutagen containers evaluate as
    # False when no tags are embedded, even though track.info.length is valid.
    # Reading it here (before any waterfall step) guarantees timing is captured
    # regardless of which text-metadata source ultimately wins.
    duration_seconds: Optional[float] = None
    bitrate:          Optional[int]   = None
    if file_type == "audio":
        duration_seconds, bitrate = _read_audio_stream_info(filepath)

    # ── Text-metadata waterfall ──────────────────────────────────────────────

    # 1. Embedded tags (format-aware routing)
    result = None
    if file_type == "audio":
        result = _audio_tags(filepath)
    elif file_type == "ebook":
        if filepath.suffix.lower() == ".epub":
            result = _epub_tags(filepath)
        elif filepath.suffix.lower() == ".pdf":
            result = _pdf_tags(filepath)
    elif file_type == "image":
        result = _image_tags(filepath)

    if result and result.title and result.author:
        result.confidence        = 100
        result.duration_seconds  = duration_seconds
        result.bitrate           = bitrate
        return result

    # 2. Filename regex
    result = _filename_regex(filepath.stem)
    if result and result.title and result.author:
        result.confidence        = 80
        result.duration_seconds  = duration_seconds
        result.bitrate           = bitrate
        return result

    # 3. Folder fallback
    result = _folder_fallback(filepath)
    result.confidence        = 50
    result.duration_seconds  = duration_seconds
    result.bitrate           = bitrate
    return result
