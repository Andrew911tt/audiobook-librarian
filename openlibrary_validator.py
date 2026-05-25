"""
Open Library API — unauthenticated metadata lookup.

Endpoint : https://openlibrary.org/search.json
Docs     : https://openlibrary.org/dev/docs/api

No API key required.  Rate-limit: be polite — callers should add a delay.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

_ENDPOINT = "https://openlibrary.org/search.json"
_TIMEOUT  = 12

_requests = None


def _get_requests():
    global _requests
    if _requests is None:
        import requests as _r
        _requests = _r
    return _requests


def fetch_openlibrary_data(title: str, author: str = "") -> Optional[dict]:
    """
    Search Open Library for *title* + optional *author*.

    Returns a dict with keys:
        title, author, series_name, series_sequence,
        narrator, asin, isbn, runtime_min, source
    or None if nothing usable was found.
    """
    if not title:
        return None

    requests = _get_requests()

    query = title.strip()
    if author.strip():
        query = f"{query} {author.strip()}"

    params = {
        "q":      query,
        "fields": "title,author_name,series,isbn,first_publish_year,edition_count,key",
        "limit":  5,
        "lang":   "eng",
    }

    try:
        resp = requests.get(_ENDPOINT, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Open Library request error for {title!r}: {e}")
        return None

    docs = data.get("docs") or []
    if not docs:
        log.debug(f"Open Library: no results for {title!r} / {author!r}")
        return None

    for doc in docs:
        parsed = _parse_doc(doc)
        if parsed:
            log.info(
                f"Open Library hit: {parsed['author']!r} — {parsed['title']!r}"
                + (f" | Series: {parsed['series_name']}" if parsed["series_name"] else "")
                + (f" | ISBN: {parsed['isbn']}" if parsed["isbn"] else "")
            )
            return parsed

    log.debug(f"Open Library: all results empty for {title!r} / {author!r}")
    return None


def _parse_doc(doc: dict) -> Optional[dict]:
    title = (doc.get("title") or "").strip()
    authors = doc.get("author_name") or []
    author  = authors[0].strip() if authors else ""

    if not (title or author):
        return None

    # Series — Open Library returns it as a list of strings
    series_list = doc.get("series") or []
    series_name     = ""
    series_sequence = ""
    if series_list:
        raw = series_list[0].strip()
        # Format is often "Series Name #N" or "Series Name, Book N"
        import re
        m = re.search(r'[#,]\s*(?:Book\s*)?(\d+(?:\.\d+)?)\s*$', raw, re.IGNORECASE)
        if m:
            series_sequence = m.group(1)
            series_name     = raw[:m.start()].strip(" ,#")
        else:
            series_name = raw

    # ISBN — prefer ISBN-13
    isbns = doc.get("isbn") or []
    isbn  = ""
    for candidate in isbns:
        candidate = candidate.strip()
        if len(candidate) == 13:
            isbn = candidate
            break
    if not isbn and isbns:
        isbn = isbns[0].strip()

    return {
        "title":           title,
        "author":          author,
        "series_name":     series_name,
        "series_sequence": series_sequence,
        "narrator":        "",
        "asin":            "",
        "isbn":            isbn,
        "runtime_min":     None,
        "source":          "openlibrary",
    }
