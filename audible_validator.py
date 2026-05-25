"""
Audible Catalog API — unauthenticated metadata lookup.

Endpoint : https://api.audible.com/1.0/catalog/products
Params   : keywords, num_results, response_groups

Usage:
    from audible_validator import fetch_audible_data
    result = fetch_audible_data("Ender's Game Orson Scott Card")
    # {'title': "Ender's Game", 'author': 'Orson Scott Card',
    #  'series_name': 'Ender's Saga', 'series_sequence': '1'}
"""

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.audible.com/1.0/catalog/products"
_TIMEOUT  = 10   # seconds per request

# Lazily imported so the module loads even without requests installed
_requests = None


def _get_requests():
    global _requests
    if _requests is None:
        import requests as _r
        _requests = _r
    return _requests


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _parse_product(product: dict) -> Optional[dict]:
    """Extract all metadata fields from a single Audible product dict."""
    asin  = (product.get("asin")  or "").strip()
    title = (product.get("title") or "").strip()

    author = ""
    for contributor in product.get("authors") or []:
        name = (contributor.get("name") or "").strip()
        if name:
            author = name
            break
    if not author:
        for contributor in product.get("contributors") or []:
            if (contributor.get("role") or "").upper() == "AUTHOR":
                author = (contributor.get("name") or "").strip()
                if author:
                    break

    narrator = ""
    for n in product.get("narrators") or []:
        name = (n.get("name") or "").strip()
        if name:
            narrator = name
            break
    if not narrator:
        for contributor in product.get("contributors") or []:
            if (contributor.get("role") or "").upper() == "NARRATOR":
                narrator = (contributor.get("name") or "").strip()
                if narrator:
                    break

    series_name = series_sequence = ""
    for s in product.get("series") or []:
        series_name     = (s.get("title")    or "").strip()
        series_sequence = str(s.get("sequence") or "").strip()
        if series_name:
            break

    isbn = ""
    for ext in product.get("external_ids") or []:
        if (ext.get("type") or "").upper() in ("ISBN", "ISBN13"):
            isbn = str(ext.get("id") or "").strip()
            if isbn:
                break

    runtime_min = product.get("runtime_length_min")
    if runtime_min is not None:
        try:
            runtime_min = int(runtime_min)
        except (ValueError, TypeError):
            runtime_min = None

    if not (title or author):
        return None

    return {
        "title":           title,
        "author":          author,
        "series_name":     series_name,
        "series_sequence": series_sequence,
        "narrator":        narrator,
        "asin":            asin,
        "isbn":            isbn,
        "runtime_min":     runtime_min,
    }


def fetch_audible_candidates(
    search_term: str,
    num_results: int = 10,
) -> list:
    """
    Return the full list of parsed Audible candidates for *search_term*,
    in the order Audible returned them.  Used by the single-book sync
    dialog so the user can manually pick which match to apply.

    Returns [] if the API errors out or no results.
    """
    if not search_term or not search_term.strip():
        return []

    requests = _get_requests()
    params = {
        "keywords":        search_term.strip(),
        "num_results":     max(1, int(num_results)),
        "response_groups": "contributors,series,product_desc,product_attrs",
    }
    try:
        resp = requests.get(_ENDPOINT, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Audible candidates error for {search_term!r}: {e}")
        return []

    products = data.get("products") or []
    candidates = [p for p in (_parse_product(r) for r in products) if p]
    log.debug(f"Audible candidates: {len(candidates)} for {search_term!r}")
    return candidates


def pick_best_by_duration(
    candidates: list,
    known_duration_sec: Optional[float],
) -> Optional[dict]:
    """
    Out of a list of candidates, return the one whose runtime is closest
    to known_duration_sec.  Falls back to the first item if no candidates
    have runtime data or known_duration_sec is missing.

    Used so the single-sync dialog can pre-select the same default that
    fetch_audible_data() would pick automatically.
    """
    if not candidates:
        return None
    if not known_duration_sec or known_duration_sec <= 0:
        return candidates[0]

    known_min = known_duration_sec / 60.0
    with_runtime = [c for c in candidates if c.get("runtime_min")]
    if not with_runtime:
        return candidates[0]
    best  = min(with_runtime, key=lambda c: abs(c["runtime_min"] - known_min))
    delta = abs(best["runtime_min"] - known_min)
    # Same guard as fetch_audible_data: >60 min off → top result.
    if delta > 60:
        return candidates[0]
    return best


def fetch_audible_data(
    search_term: str,
    known_duration_sec: Optional[float] = None,
) -> Optional[dict]:
    """
    Query the Audible catalog for *search_term*.

    When *known_duration_sec* is supplied (the actual scanned length of the
    audiobook in seconds), up to 10 candidates are fetched and the one whose
    Audible runtime is closest to the known duration is returned.  Without a
    known duration the top result is used.

    Returns a dict with keys:
        title, author, series_name, series_sequence, narrator, asin, isbn,
        runtime_min
    or None if nothing was found or an error occurred.
    """
    if not search_term or not search_term.strip():
        return None

    requests = _get_requests()
    num_results = 10 if known_duration_sec else 1
    params = {
        "keywords":        search_term.strip(),
        "num_results":     num_results,
        "response_groups": "contributors,series,product_desc,product_attrs",
    }

    try:
        resp = requests.get(_ENDPOINT, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        log.warning(f"Audible timeout for: {search_term!r}")
        return None
    except requests.exceptions.RequestException as e:
        log.warning(f"Audible request error for {search_term!r}: {e}")
        return None
    except ValueError as e:
        log.warning(f"Audible JSON parse error for {search_term!r}: {e}")
        return None

    products = data.get("products")
    if not products:
        log.debug(f"Audible: no results for {search_term!r}")
        return None

    # Parse all candidates
    candidates = [p for p in (_parse_product(r) for r in products) if p]
    if not candidates:
        return None

    # Pick best match by runtime proximity when we know the actual duration
    if known_duration_sec and known_duration_sec > 0:
        known_min = known_duration_sec / 60.0
        with_runtime    = [c for c in candidates if c["runtime_min"]]
        without_runtime = [c for c in candidates if not c["runtime_min"]]

        if with_runtime:
            best = min(with_runtime, key=lambda c: abs(c["runtime_min"] - known_min))
            delta = abs(best["runtime_min"] - known_min)
            log.debug(
                f"Audible duration match: known={known_min:.0f}m  "
                f"chosen={best['runtime_min']}m  delta={delta:.1f}m  "
                f"title={best['title']!r}"
            )
            # If the closest match is wildly off (>60 min) fall back to
            # the top result so a bad scan doesn't derail the metadata.
            if delta > 60 and without_runtime:
                log.debug("Delta >60 min — falling back to top result")
                product = products[0]
            else:
                result = best
                log.info(
                    f"Audible hit: {result['author']!r} — {result['title']!r}"
                    + (f" [{result['series_name']} #{result['series_sequence']}]"
                       if result["series_name"] else "")
                    + (f" | narrator: {result['narrator']}" if result["narrator"] else "")
                    + (f" | ASIN: {result['asin']}" if result["asin"] else "")
                    + (f" | runtime: {result['runtime_min']}m" if result["runtime_min"] else "")
                )
                return result
        # No candidates have runtime data — fall through to top result
        product = products[0]
    else:
        product = products[0]

    # Fall-through: use top result (no duration to compare, or delta too large)
    result = _parse_product(product)
    if not result:
        log.debug(f"Audible: empty title+author for top result of {search_term!r}")
        return None

    log.info(
        f"Audible hit: {result['author']!r} — {result['title']!r}"
        + (f" [{result['series_name']} #{result['series_sequence']}]"
           if result["series_name"] else "")
        + (f" | narrator: {result['narrator']}" if result["narrator"] else "")
        + (f" | ASIN: {result['asin']}" if result["asin"] else "")
        + (f" | runtime: {result['runtime_min']}m" if result["runtime_min"] else "")
    )
    return result
