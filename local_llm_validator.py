"""
Local LLM validator — works with LM Studio, Ollama, or any
OpenAI-compatible local server.

Key design decisions
  • model="local-model" — LM Studio ignores this string and uses
    whatever model is currently loaded in the UI.  Ollama users can
    override it to e.g. "llama3".
  • No response_format constraint — different models support different
    formats; we rely on the prompt and robust JSON extraction instead.
  • Regex JSON extraction — finds the first '{' and last '}' in the
    raw response, so conversational "fluff" before/after the JSON is
    automatically discarded.
  • ConnectionError → friendly popup message (raised so the caller
    can show it; logged here for the log file).

Default endpoint : http://127.0.0.1:1234/v1
API key          : "lm-studio"  (ignored by both LM Studio and Ollama)
"""

import json
import logging
import re
from typing import Optional

from errors import TokenLimitError
from prompts import (
    LLM_BATCH_SYSTEM_PROMPT,
    LLM_SYSTEM_PROMPT,
    batch_max_tokens,
    build_batch_user_message,
    parse_batch_array,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL    = "local-model"


# ---------------------------------------------------------------------------
# JSON extraction — model-agnostic
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> Optional[dict]:
    """
    Robustly pull a JSON object out of a model response that may contain
    leading/trailing text, markdown fences, or <think> blocks.

    Strategy:
      1. Strip <think>…</think> blocks (reasoning models like Qwen3).
      2. Find the first '{' and the last '}' in the remaining text.
      3. Parse only that substring.
    """
    if not raw:
        return None

    # Remove <think>…</think> blocks (Qwen3, DeepSeek-R1, etc.)
    # LM Studio exposes thinking as reasoning_content; some builds may also
    # embed it inline as <think> tags — strip both.
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    # Find outermost JSON object
    start = raw.find("{")
    end   = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    candidate = raw[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Last resort: try stripping comments (some models add // …)
        cleaned = re.sub(r"//[^\n]*", "", candidate)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_book_metadata_local(
    context_string: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
) -> Optional[dict]:
    """
    Ask the locally-running model to extract structured audiobook metadata.

    base_url : full URL to the /v1 endpoint
               (e.g. "http://127.0.0.1:1234/v1" for LM Studio,
                     "http://localhost:11434/v1" for Ollama)
    model    : model identifier — use "local-model" for LM Studio
               (it ignores the string), or the actual model name for Ollama.

    Returns a 7-key dict or None.
    Raises ConnectionError with a user-friendly message if the server is down.
    """
    if not context_string:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai is not installed. Run: pip install openai")
        return None

    # Normalise URL — always ends with /v1
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url = url + "/v1"

    try:
        client = OpenAI(base_url=url, api_key="lm-studio")

        completion = client.chat.completions.create(
            model=model,
            # No response_format — model-agnostic; we parse with regex instead.
            # enable_thinking=False disables reasoning/thinking mode on Qwen3 and
            # similar models so content is returned directly rather than as think
            # tokens.  Non-thinking models silently ignore this parameter.
            extra_body={"enable_thinking": False},
            messages=[
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user",   "content": context_string.strip()},
            ],
            temperature=0,
            max_tokens=512,
        )

        choice = completion.choices[0]
        if choice.finish_reason == "length":
            raise TokenLimitError(
                f"Local LLM hit the token limit while processing:\n"
                f"  {context_string[:80]!r}\n\n"
                "The response was cut off before a complete JSON object could be returned.\n"
                "Increase max_tokens in local_llm_validator.py, or load a model with "
                "a larger context window in LM Studio / Ollama."
            )

        msg = choice.message
        raw = (msg.content or "").strip()

        # Fallback: some LM Studio builds / reasoning models put the actual
        # response in reasoning_content and leave content empty.  This is a
        # known quirk of certain Qwen3 / DeepSeek-R1 builds.
        if not raw:
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if reasoning.strip():
                log.debug("content empty — falling back to reasoning_content")
                raw = reasoning.strip()

        log.debug(f"Local LLM raw response: {raw[:200]!r}")

        if not raw:
            log.warning(
                "Local LLM returned empty content and reasoning_content. "
                "If using a reasoning/thinking model (Qwen3, DeepSeek-R1 etc.), "
                "try updating LM Studio to the latest version."
            )
            return None

        data = _extract_json(raw)
        if data is None:
            log.warning(
                f"Local LLM: could not extract JSON from response for "
                f"{context_string[:60]!r}\nRaw: {raw[:200]}"
            )
            return None

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
            log.debug(f"Local LLM: empty author+title for: {context_string[:60]!r}")
            return None

        log.info(
            f"Local LLM hit: {result['author']!r} — {result['title']!r}"
            + (f" [{result['series_name']} #{result['series_sequence']}]"
               if result["series_name"] else "")
        )
        return result

    except Exception as e:
        # Detect connection failures and re-raise with a friendly message
        err_str = str(e).lower()
        if any(k in err_str for k in ("connection", "refused", "connect", "timeout",
                                       "cannot connect", "remotely closed")):
            friendly = (
                f"Librarian couldn't find the local server at {url}.\n\n"
                "Please ensure LM Studio's Local Server is started, or check the URL "
                "in the LM Studio settings row."
            )
            log.warning(f"Local LLM connection error: {e}")
            raise ConnectionError(friendly) from e

        log.warning(f"Local LLM error ({type(e).__name__}): {e}")
        return None


# ---------------------------------------------------------------------------
# Batch API
# ---------------------------------------------------------------------------

def parse_book_metadata_local_batch(
    contexts: list,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
) -> list:
    """
    Send up to len(contexts) book lookups in a single local LLM API call.

    Returns list[Optional[dict]] of the same length as contexts, or None
    on total failure (caller falls back to individual calls).
    Raises ConnectionError if the local server is unreachable.
    """
    if not contexts:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai is not installed. Run: pip install openai")
        return None

    n        = len(contexts)
    user_msg = build_batch_user_message(contexts)

    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url = url + "/v1"

    try:
        client = OpenAI(base_url=url, api_key="lm-studio")

        completion = client.chat.completions.create(
            model=model,
            extra_body={"enable_thinking": False},
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
                f"Local LLM hit the token limit on a batch of {n} books.\n\n"
                "Reduce the batch size or load a model with a larger context window."
            )

        msg = choice.message
        raw = (msg.content or "").strip()

        # Fallback for reasoning models that put output in reasoning_content
        if not raw:
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if reasoning.strip():
                log.debug("batch: content empty — falling back to reasoning_content")
                raw = reasoning.strip()

        if not raw:
            log.warning("Local LLM batch returned empty response.")
            return None

        result = parse_batch_array(raw, n)

        if result is None:
            log.warning(f"Local LLM batch: could not parse array response for {n} books — will fallback")
        else:
            log.info(f"Local LLM batch: {sum(1 for r in result if r)}/{n} hits")

        return result

    except TokenLimitError:
        raise
    except Exception as e:
        err_str = str(e).lower()
        if any(k in err_str for k in ("connection", "refused", "connect", "timeout",
                                       "cannot connect", "remotely closed")):
            friendly = (
                f"Librarian couldn't find the local server at {url}.\n\n"
                "Please ensure LM Studio's Local Server is started, or check the URL "
                "in the LM Studio settings row."
            )
            log.warning(f"Local LLM batch connection error: {e}")
            raise ConnectionError(friendly) from e
        log.warning(f"Local LLM batch error ({type(e).__name__}): {e}")
        return None
