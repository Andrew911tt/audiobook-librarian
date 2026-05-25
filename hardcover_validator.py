"""
Hardcover API — authenticated GraphQL metadata lookup.

Endpoint : https://api.hardcover.app/v1/graphql
Auth     : Authorization: Bearer <token>
Docs     : https://hardcover.app/account/api

Free API key available at hardcover.app/account/api.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.hardcover.app/v1/graphql"
_TIMEOUT  = 12

_requests = None


def _get_requests():
    global _requests
    if _requests is None:
        import requests as _r
        _requests = _r
    return _requests


_SEARCH_QUERY = """
query SearchBooks($query: String!) {
  search(query: $query, query_type: "Book", per_page: 5) {
    results {
      ... on Book {
        title
        contributions {
          author { name }
        }
        series_books {
          position
          series { name }
        }
      }
    }
  }
}
"""


def fetch_hardcover_data(title: str, author: str = "", api_key: str = "") -> Optional[dict]:
    """
    Search Hardcover for *title* + optional *author*.

    Returns a dict with keys:
        title, author, series_name, series_sequence,
        narrator, asin, isbn, runtime_min, source
    or None if nothing usable was found.
    """
    if not title or not api_key:
        return None

    requests = _get_requests()

    query = title.strip()
    if author.strip():
        query = f"{query} {author.strip()}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = {
        "query":     _SEARCH_QUERY,
        "variables": {"query": query},
    }

    try:
        resp = requests.post(_ENDPOINT, json=payload, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Hardcover request error for {title!r}: {e}")
        return None

    if "errors" in data:
        log.warning(f"Hardcover GraphQL error for {title!r}: {data['errors']}")
        return None

    try:
        results = data["data"]["search"]["results"] or []
    except (KeyError, TypeError):
        log.debug(f"Hardcover: unexpected response shape for {title!r}")
        return None

    if not results:
        log.debug(f"Hardcover: no results for {title!r} / {author!r}")
        return None

    for item in results:
        parsed = _parse_result(item)
        if parsed:
            log.info(
                f"Hardcover hit: {parsed['author']!r} — {parsed['title']!r}"
                + (f" | Series: {parsed['series_name']} #{parsed['series_sequence']}"
                   if parsed["series_name"] else "")
            )
            return parsed

    log.debug(f"Hardcover: all results empty for {title!r} / {author!r}")
    return None


def _parse_result(item: dict) -> Optional[dict]:
    if not isinstance(item, dict):
        return None

    title = (item.get("title") or "").strip()

    # Author — first contributing author
    author = ""
    for contrib in item.get("contributions") or []:
        name = (contrib.get("author") or {}).get("name", "").strip()
        if name:
            author = name
            break

    if not (title or author):
        return None

    # Series
    series_name     = ""
    series_sequence = ""
    series_books = item.get("series_books") or []
    if series_books:
        sb = series_books[0]
        series_name = ((sb.get("series") or {}).get("name") or "").strip()
        pos = sb.get("position")
        if pos is not None:
            series_sequence = str(pos)

    return {
        "title":           title,
        "author":          author,
        "series_name":     series_name,
        "series_sequence": series_sequence,
        "narrator":        "",
        "asin":            "",
        "isbn":            "",
        "runtime_min":     None,
        "source":          "hardcover",
    }
