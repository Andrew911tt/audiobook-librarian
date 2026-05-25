"""
Gemini 2.5 Flash — secondary audiobook metadata validator.

Uses the official google-genai SDK (pip install google-genai).
The model is prompted as a librarian to extract structured metadata
from a messy folder/file name and return clean JSON.

Usage:
    from gemini_validator import parse_book_metadata
    result = parse_book_metadata(
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

_MODEL = "gemini-2.5-flash-lite"


def parse_book_metadata(messy_string: str, api_key: str) -> Optional[dict]:
    """
    Ask Gemini to extract structured metadata from a messy folder/title string.

    Returns a dict with keys: author, title, series_name, series_sequence
    or None if the API call fails or the response can't be parsed as JSON.
    """
    if not messy_string or not api_key:
        return None

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        log.error(
            "google-genai is not installed. Run: pip install google-genai"
        )
        return None

    try:
        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model=_MODEL,
            contents=messy_string.strip(),
            config=types.GenerateContentConfig(
                system_instruction=LLM_SYSTEM_PROMPT,
                temperature=0,          # deterministic output
            ),
        )

        # Check finish reason before accessing response.text — MAX_TOKENS = 2
        if response.candidates:
            fr = response.candidates[0].finish_reason
            # Defensive: handle enum, int, or string representations
            if getattr(fr, "value", fr) == 2 or "MAX_TOKEN" in str(fr).upper():
                raise TokenLimitError(
                    f"Gemini ({_MODEL}) hit the token limit while processing:\n"
                    f"  {messy_string[:80]!r}\n\n"
                    "The response was cut off before a complete JSON object could be returned.\n"
                    "Increase the token budget in gemini_validator.py "
                    "(add max_output_tokens to GenerateContentConfig)."
                )

        raw = (response.text or "").strip()

        # Strip accidental markdown fences the model sometimes adds anyway
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        data = json.loads(raw)

        # Normalise: ensure all expected keys exist
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
            log.debug(f"Gemini returned empty author+title for: {messy_string!r}")
            return None

        log.info(
            f"Gemini hit: {result['author']!r} — {result['title']!r}"
            + (f" [{result['series_name']} #{result['series_sequence']}]"
               if result["series_name"] else "")
        )
        return result

    except json.JSONDecodeError as e:
        log.warning(f"Gemini JSON parse error for {messy_string!r}: {e}")
        return None
    except Exception as e:
        # Detect HTTP 429 / RESOURCE_EXHAUSTED rate-limit errors from the Google SDK
        err_str = str(e).lower()
        type_name = type(e).__name__
        if (type_name in ("ResourceExhausted", "TooManyRequests")
                or "resource_exhausted" in err_str
                or "rate_limit" in err_str
                or "rate limit" in err_str
                or "quota" in err_str
                or "429" in err_str):
            friendly = (
                f"Google Gemini rate limit reached — sync stopped.\n\n"
                f"{e}\n\n"
                "Wait for the reset window then run the sync again."
            )
            log.error(f"Gemini rate limit: {e}")
            raise RateLimitError(friendly) from e
        log.warning(f"Gemini API error for {messy_string!r}: {e}")
        return None


# ---------------------------------------------------------------------------
# Batch API
# ---------------------------------------------------------------------------

def parse_book_metadata_gemini_batch(
    contexts: list,
    api_key: str,
) -> list:
    """
    Send up to len(contexts) book lookups in a single Gemini API call.

    Returns list[Optional[dict]] of the same length as contexts, or None
    on total failure (caller falls back to individual calls).
    """
    if not contexts or not api_key:
        return None

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        log.error("google-genai is not installed. Run: pip install google-genai")
        return None

    n        = len(contexts)
    user_msg = build_batch_user_message(contexts)

    try:
        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model=_MODEL,
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=LLM_BATCH_SYSTEM_PROMPT,
                temperature=0,
                max_output_tokens=batch_max_tokens(n),
            ),
        )

        if response.candidates:
            fr = response.candidates[0].finish_reason
            if getattr(fr, "value", fr) == 2 or "MAX_TOKEN" in str(fr).upper():
                raise TokenLimitError(
                    f"Gemini ({_MODEL}) hit the token limit on a batch of {n} books.\n\n"
                    "Reduce the batch size or increase max_output_tokens in gemini_validator.py."
                )

        raw    = (response.text or "").strip()
        result = parse_batch_array(raw, n)

        if result is None:
            log.warning(f"Gemini batch: could not parse array response for {n} books — will fallback")
        else:
            log.info(f"Gemini batch: {sum(1 for r in result if r)}/{n} hits")

        return result

    except (TokenLimitError, RateLimitError):
        raise
    except Exception as e:
        err_str   = str(e).lower()
        type_name = type(e).__name__
        if (type_name in ("ResourceExhausted", "TooManyRequests")
                or "resource_exhausted" in err_str
                or "rate_limit" in err_str
                or "rate limit" in err_str
                or "quota" in err_str
                or "429" in err_str):
            friendly = (
                f"Google Gemini rate limit reached — sync stopped.\n\n"
                f"{e}\n\n"
                "Wait for the reset window then run the sync again."
            )
            log.error(f"Gemini batch rate limit: {e}")
            raise RateLimitError(friendly) from e
        log.warning(f"Gemini batch API error ({type(e).__name__}): {e}")
        return None
