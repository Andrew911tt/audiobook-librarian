"""
Google Books API — unauthenticated metadata lookup.

Endpoint : https://www.googleapis.com/books/v1/volumes
Query    : intitle:[TITLE]+inauthor:[AUTHOR]

Used as a fallback when Audible returns no match.  Google Books covers
print editions, so results will not include narrator or ASIN — those fields
are returned as empty strings.  ISBN-13 is extracted from industryIdentifiers
when available.

No API key is required for basic read-only queries (up to ~1,000 req/day
per IP under the unauthenticated quota).
"""

import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

_ENDPOINT = "https://www.googleapis.com/books/v1/volumes"
_TIMEOUT  = 10   # seconds per request

_requests = None


def _get_requests():
    global _requests
    if _requests is None:
        import requests as _r
        _requests = _r
    return _requests


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _best_isbn(industry_identifiers: list) -> str:
    """Return ISBN-13 if present, otherwise ISBN-10, otherwise ''."""
    isbn13 = isbn10 = ""
    for entry in industry_identifiers or []:
        id_type = (entry.get("type") or "").upper()
        val     = (entry.get("identifier") or "").strip()
        if id_type == "ISBN_13" and not isbn13:
            isbn13 = val
        elif id_type == "ISBN_10" and not isbn10:
            isbn10 = val
    return isbn13 or isbn10


def _parse_volume(vol: dict) -> Optional[dict]:
    """Extract metadata from a single Google Books volume dict."""
    info = vol.get("volumeInfo") or {}

    title = (info.get("title") or "").strip()
    # Append subtitle if present and not already in title
    subtitle = (info.get("subtitle") or "").strip()
    if subtitle and subtitle.lower() not in title.lower():
        title = f"{title}: {subtitle}" if title else subtitle

    authors = info.get("authors") or []
    author  = authors[0].strip() if authors else ""

    isbn        = _best_isbn(info.get("industryIdentifiers") or [])
    categories  = ", ".join(info.get("categories") or [])
    description = (info.get("description") or "").strip()[:500]  # cap for logging

    if not (title or author):
        return None

    return {
        "title":       title,
        "author":      author,
        "isbn":        isbn,
        "categories":  categories,
        "description": description,
        # Google Books never has narrator or ASIN
        "narrator":        "",
        "asin":            "",
        "series_name":     "",
        "series_sequence": "",
        "runtime_min":     None,
        "source":          "google",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_google_books_data(
    title: str,
    author: str = "",
) -> Optional[dict]:
    """
    Query the Google Books API for *title* (+ optional *author*).

    Returns a dict with keys:
        title, author, isbn, categories, description,
        narrator, asin, series_name, series_sequence, runtime_min, source
    or None if nothing usable was found.

    The 'source' key is always 'google' so callers can distinguish results
    from Audible matches.
    """
    if not title:
        return None

    requests = _get_requests()

    # Build the query string
    # intitle: and inauthor: are Google Books structured-query operators
    query_parts = [f"intitle:{title.strip()}"]
    if author.strip():
        query_parts.append(f"inauthor:{author.strip()}")
    query = "+".join(query_parts)

    params = {
        "q":          query,
        "maxResults": 5,
        "printType":  "books",
        "langRestrict": "en",
    }

    try:
        resp = requests.get(_ENDPOINT, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Google Books request error for {title!r}: {e}")
        return None

    items = data.get("items") or []
    if not items:
        log.debug(f"Google Books: no results for {title!r} / {author!r}")
        return None

    # Pick the first parseable result — Google Books already ranks by relevance
    for item in items:
        parsed = _parse_volume(item)
        if parsed:
            log.info(
                f"Google Books hit: {parsed['author']!r} — {parsed['title']!r}"
                + (f" | ISBN: {parsed['isbn']}" if parsed["isbn"] else "")
                + (f" | categories: {parsed['categories']}" if parsed["categories"] else "")
            )
            return parsed

    log.debug(f"Google Books: all results empty for {title!r} / {author!r}")
    return None
