"""
OpenAI ChatGPT — audiobook metadata validator.

Uses the official openai Python library (pip install openai).
Defaults to gpt-4o-mini (fast, cheap, JSON-mode capable).

Usage:
    from chatgpt_validator import parse_book_metadata_chatgpt
    result = parse_book_metadata_chatgpt(
        "1996 - A Song of Ice and Fire 1 - A Game of Thrones (read by Roy Dotrice)",
        api_key="sk-..."
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

_MODEL = "gpt-4o-mini"


def parse_book_metadata_chatgpt(context_string: str, api_key: str) -> Optional[dict]:
    """
    Ask ChatGPT to extract structured metadata from a rich context string
    containing the folder path hierarchy and sample filenames.

    Returns a dict with keys: author, title, series_name, series_sequence,
    narrator, isbn, asin — or None if the API call fails or returns nothing useful.
    """
    if not context_string or not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai is not installed. Run: pip install openai")
        return None

    try:
        client = OpenAI(api_key=api_key)

        response = client.chat.completions.create(
            model=_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user",   "content": context_string.strip()},
            ],
            temperature=0,
            max_tokens=512,
        )

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise TokenLimitError(
                f"ChatGPT ({_MODEL}) hit the token limit while processing:\n"
                f"  {context_string[:80]!r}\n\n"
                "The response was cut off before a complete JSON object could be returned.\n"
                "Increase max_tokens in chatgpt_validator.py or reduce the input size."
            )

        raw = (choice.message.content or "").strip()

        # Strip accidental markdown fences just in case
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
            log.debug(f"ChatGPT returned empty author+title for: {context_string[:60]!r}")
            return None

        log.info(
            f"ChatGPT hit: {result['author']!r} — {result['title']!r}"
            + (f" [{result['series_name']} #{result['series_sequence']}]"
               if result["series_name"] else "")
        )
        return result

    except json.JSONDecodeError as e:
        log.warning(f"ChatGPT JSON parse error: {e}")
        return None
    except Exception as e:
        # Detect HTTP 429 rate-limit / quota errors from the OpenAI SDK
        err_str = str(e).lower()
        type_name = type(e).__name__
        if (type_name == "RateLimitError"
                or "rate_limit" in err_str
                or "rate limit" in err_str
                or "429" in err_str):
            friendly = (
                f"OpenAI rate limit reached — sync stopped.\n\n"
                f"{e}\n\n"
                "Wait for the reset window then run the sync again."
            )
            log.error(f"ChatGPT rate limit: {e}")
            raise RateLimitError(friendly) from e
        log.warning(f"ChatGPT API error: {e}")
        return None


# ---------------------------------------------------------------------------
# Batch API
# ---------------------------------------------------------------------------

def parse_book_metadata_chatgpt_batch(
    contexts: list,
    api_key: str,
) -> list:
    """
    Send up to len(contexts) book lookups in a single ChatGPT API call.

    Returns list[Optional[dict]] of the same length as contexts, or None
    on total failure (caller falls back to individual calls).
    """
    if not contexts or not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai is not installed. Run: pip install openai")
        return None

    n        = len(contexts)
    user_msg = build_batch_user_message(contexts)

    try:
        client = OpenAI(api_key=api_key)

        response = client.chat.completions.create(
            model=_MODEL,
            # No response_format — json_object requires an object, not an array
            messages=[
                {"role": "system", "content": LLM_BATCH_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0,
            max_tokens=batch_max_tokens(n),
        )

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise TokenLimitError(
                f"ChatGPT ({_MODEL}) hit the token limit on a batch of {n} books.\n\n"
                "Reduce the batch size or increase max_tokens in chatgpt_validator.py."
            )

        raw    = (choice.message.content or "").strip()
        result = parse_batch_array(raw, n)

        if result is None:
            log.warning(f"ChatGPT batch: could not parse array response for {n} books — will fallback")
        else:
            log.info(f"ChatGPT batch: {sum(1 for r in result if r)}/{n} hits")

        return result

    except (TokenLimitError, RateLimitError):
        raise
    except Exception as e:
        err_str   = str(e).lower()
        type_name = type(e).__name__
        if (type_name == "RateLimitError"
                or "rate_limit" in err_str
                or "rate limit" in err_str
                or "429" in err_str):
            friendly = (
                f"OpenAI rate limit reached — sync stopped.\n\n"
                f"{e}\n\n"
                "Wait for the reset window then run the sync again."
            )
            log.error(f"ChatGPT batch rate limit: {e}")
            raise RateLimitError(friendly) from e
        log.warning(f"ChatGPT batch API error ({type(e).__name__}): {e}")
        return None
