"""
Groq LLaMA 3.3 70B — fast audiobook metadata validator.

Uses the official groq Python library (pip install groq).
Structured JSON output is enforced via response_format so no
markdown-stripping is needed.

Usage:
    from groq_validator import parse_book_metadata_groq
    result = parse_book_metadata_groq(
        "1996 - A Song of Ice and Fire 1 - A Game of Thrones (read by Roy Dotrice)",
        api_key="YOUR_KEY"
    )
    # {'author': 'George R. R. Martin', 'title': 'A Game of Thrones',
    #  'series_name': 'A Song of Ice and Fire', 'series_sequence': '1'}
"""

import json
import logging
from typing import Optional

from errors import RateLimitError, TokenLimitError
from prompts import (
    LLM_BATCH_SYSTEM_PROMPT,
    LLM_SYSTEM_PROMPT,
    batch_max_tokens,
    build_batch_user_message,
    parse_batch_array,
)

log = logging.getLogger(__name__)

_MODEL = "llama-3.3-70b-versatile"


def parse_book_metadata_groq(context_string: str, api_key: str) -> Optional[dict]:
    """
    Ask Groq LLaMA 3.3 70B to extract structured metadata from a rich
    context string containing the folder name and sample filenames.

    response_format={"type": "json_object"} is used so the model always
    returns clean JSON — no markdown fences to strip.

    Returns a dict with keys: author, title, series_name, series_sequence,
    narrator, isbn, asin — or None if the API call fails.
    """
    if not context_string or not api_key:
        return None

    try:
        from groq import Groq
    except ImportError:
        log.error("groq is not installed. Run: pip install groq")
        return None

    try:
        client = Groq(api_key=api_key)

        completion = client.chat.completions.create(
            model=_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user",   "content": context_string.strip()},
            ],
            temperature=0,
        )

        choice = completion.choices[0]
        if choice.finish_reason == "length":
            raise TokenLimitError(
                f"Groq ({_MODEL}) hit the token limit while processing:\n"
                f"  {context_string[:80]!r}\n\n"
                "The response was cut off before a complete JSON object could be returned.\n"
                "Groq does not support increasing max_tokens for this model — "
                "try a model with a larger output window."
            )

        raw = (choice.message.content or "").strip()
        data = json.loads(raw)

        result = {
            "author":          str(data.get("author")          or "").strip(),
            "title":           str(data.get("title")           or "").strip(),
            "series_name":     str(data.get("series_name")     or "").strip(),
            "series_sequence": str(data.get("series_sequence") or "").strip(),
            "narrator":        str(data.get("narrator")        or "").strip(),
            "isbn":            str(data.get("isbn")            or "").strip(),
            "asin":            str(data.get("asin")            or "").strip(),
        }

        if not (result["author"] or result["title"]):
            log.debug(f"Groq returned empty author+title for: {context_string[:60]!r}")
            return None

        log.info(
            f"Groq hit: {result['author']!r} — {result['title']!r}"
            + (f" [{result['series_name']} #{result['series_sequence']}]"
               if result["series_name"] else "")
        )
        return result

    except json.JSONDecodeError as e:
        log.warning(f"Groq JSON parse error: {e}")
        return None
    except Exception as e:
        # Detect HTTP 429 rate-limit / quota-exhausted errors from the Groq SDK
        # (groq.RateLimitError) and re-raise as our shared RateLimitError so the
        # sync loop can stop immediately instead of silently skipping every book.
        err_str = str(e).lower()
        type_name = type(e).__name__
        if (type_name == "RateLimitError"
                or "rate_limit" in err_str
                or "rate limit" in err_str
                or "429" in err_str):
            friendly = (
                f"Groq rate limit reached — sync stopped.\n\n"
                f"{e}\n\n"
                "Wait for the reset window shown above, then run the sync again.\n"
                "Consider upgrading to Groq Dev Tier for higher daily token limits."
            )
            log.error(f"Groq rate limit: {e}")
            raise RateLimitError(friendly) from e
        log.warning(f"Groq API error: {e}")
        return None


# ---------------------------------------------------------------------------
# Batch API
# ---------------------------------------------------------------------------

def parse_book_metadata_groq_batch(
    contexts: list,
    api_key: str,
) -> list:
    """
    Send up to len(contexts) book lookups in a single Groq API call.

    Returns list[Optional[dict]] of the same length as contexts.
    Returns None on total failure — the caller should fall back to
    individual parse_book_metadata_groq() calls.

    Note: response_format is omitted here because json_object mode requires
    a JSON object, not an array.  The batch prompt + parse_batch_array()
    handle extraction robustly.
    """
    if not contexts or not api_key:
        return None

    try:
        from groq import Groq
    except ImportError:
        log.error("groq is not installed. Run: pip install groq")
        return None

    n        = len(contexts)
    user_msg = build_batch_user_message(contexts)

    try:
        client = Groq(api_key=api_key)

        completion = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": LLM_BATCH_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0,
            max_tokens=batch_max_tokens(n),
        )

        choice = completion.choices[0]
        if choice.finish_reason == "length":
            raise TokenLimitError(
                f"Groq ({_MODEL}) hit the token limit on a batch of {n} books.\n\n"
                "Reduce the batch size or load a model with a larger output window."
            )

        raw = (choice.message.content or "").strip()
        result = parse_batch_array(raw, n)

        if result is None:
            log.warning(f"Groq batch: could not parse array response for {n} books — will fallback")
        else:
            hits = sum(1 for r in result if r)
            log.info(f"Groq batch: {hits}/{n} hits")

        return result

    except (TokenLimitError, RateLimitError):
        raise
    except Exception as e:
        err_str = str(e).lower()
        type_name = type(e).__name__
        if (type_name == "RateLimitError"
                or "rate_limit" in err_str
                or "rate limit" in err_str
                or "429" in err_str):
            friendly = (
                f"Groq rate limit reached — sync stopped.\n\n"
                f"{e}\n\n"
                "Wait for the reset window shown above, then run the sync again.\n"
                "Consider upgrading to Groq Dev Tier for higher daily token limits."
            )
            log.error(f"Groq batch rate limit: {e}")
            raise RateLimitError(friendly) from e
        log.warning(f"Groq batch API error ({type(e).__name__}): {e}")
        return None
