"""
Anthropic Claude — audiobook metadata validator.

Uses the official anthropic Python library (pip install anthropic).
Defaults to claude-3-5-haiku-20241022 (fast + affordable).
Falls back to stripping markdown fences since the Messages API does not
support response_format=json_object; the system prompt asks for bare JSON.

Usage:
    from claude_validator import parse_book_metadata_claude
    result = parse_book_metadata_claude(
        "1996 - A Song of Ice and Fire 1 - A Game of Thrones (read by Roy Dotrice)",
        api_key="sk-ant-..."
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

_MODEL = "claude-3-5-haiku-20241022"


def parse_book_metadata_claude(context_string: str, api_key: str) -> Optional[dict]:
    """
    Ask Claude to extract structured metadata from a rich context string
    containing the folder path hierarchy and sample filenames.

    Returns a dict with keys: author, title, series_name, series_sequence,
    narrator, isbn, asin — or None if the API call fails or returns nothing useful.
    """
    if not context_string or not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        log.error("anthropic is not installed. Run: pip install anthropic")
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)

        message = client.messages.create(
            model=_MODEL,
            max_tokens=512,
            system=LLM_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": context_string.strip()},
            ],
            temperature=0,
        )

        if message.stop_reason == "max_tokens":
            raise TokenLimitError(
                f"Claude ({_MODEL}) hit the token limit while processing:\n"
                f"  {context_string[:80]!r}\n\n"
                "The response was cut off before a complete JSON object could be returned.\n"
                "Increase max_tokens in claude_validator.py or reduce the input size."
            )

        raw = (message.content[0].text if message.content else "").strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

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
            log.debug(f"Claude returned empty author+title for: {context_string[:60]!r}")
            return None

        log.info(
            f"Claude hit: {result['author']!r} — {result['title']!r}"
            + (f" [{result['series_name']} #{result['series_sequence']}]"
               if result["series_name"] else "")
        )
        return result

    except json.JSONDecodeError as e:
        log.warning(f"Claude JSON parse error: {e}")
        return None
    except Exception as e:
        # Detect HTTP 429 rate-limit / quota errors from the Anthropic SDK
        err_str = str(e).lower()
        type_name = type(e).__name__
        if (type_name == "RateLimitError"
                or "rate_limit" in err_str
                or "rate limit" in err_str
                or "overloaded" in err_str
                or "429" in err_str):
            friendly = (
                f"Anthropic rate limit reached — sync stopped.\n\n"
                f"{e}\n\n"
                "Wait for the reset window then run the sync again."
            )
            log.error(f"Claude rate limit: {e}")
            raise RateLimitError(friendly) from e
        log.warning(f"Claude API error: {e}")
        return None


# ---------------------------------------------------------------------------
# Batch API
# ---------------------------------------------------------------------------

def parse_book_metadata_claude_batch(
    contexts: list,
    api_key: str,
) -> list:
    """
    Send up to len(contexts) book lookups in a single Claude API call.

    Returns list[Optional[dict]] of the same length as contexts, or None
    on total failure (caller falls back to individual calls).
    """
    if not contexts or not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        log.error("anthropic is not installed. Run: pip install anthropic")
        return None

    n        = len(contexts)
    user_msg = build_batch_user_message(contexts)

    try:
        client = anthropic.Anthropic(api_key=api_key)

        message = client.messages.create(
            model=_MODEL,
            max_tokens=batch_max_tokens(n),
            system=LLM_BATCH_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
        )

        if message.stop_reason == "max_tokens":
            raise TokenLimitError(
                f"Claude ({_MODEL}) hit the token limit on a batch of {n} books.\n\n"
                "Reduce the batch size or increase max_tokens in claude_validator.py."
            )

        raw    = (message.content[0].text if message.content else "").strip()
        result = parse_batch_array(raw, n)

        if result is None:
            log.warning(f"Claude batch: could not parse array response for {n} books — will fallback")
        else:
            log.info(f"Claude batch: {sum(1 for r in result if r)}/{n} hits")

        return result

    except (TokenLimitError, RateLimitError):
        raise
    except Exception as e:
        err_str   = str(e).lower()
        type_name = type(e).__name__
        if (type_name == "RateLimitError"
                or "rate_limit" in err_str
                or "rate limit" in err_str
                or "overloaded" in err_str
                or "429" in err_str):
            friendly = (
                f"Anthropic rate limit reached — sync stopped.\n\n"
                f"{e}\n\n"
                "Wait for the reset window then run the sync again."
            )
            log.error(f"Claude batch rate limit: {e}")
            raise RateLimitError(friendly) from e
        log.warning(f"Claude batch API error ({type(e).__name__}): {e}")
        return None
