"""
Shared LLM prompts and batch-parsing utilities for all Identifier validators.

A single source of truth so wording stays consistent across
Gemini, ChatGPT, Claude, Groq, and Local LLM.
"""

import json
import re
from typing import Optional

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

LLM_SYSTEM_PROMPT: str = (
    "Expert audiobook librarian. "
    "Given a folder path and filenames, identify the official author, title, and series — "
    "resolving hard to understand human naming schemes "
    "(e.g. 'ASOIAF 1' → 'A Game of Thrones', 'A Song of Ice and Fire', George R. R. Martin). "
    "Return ONLY a JSON object with keys: "
    "{author, title, series_name, series_sequence, narrator} "
    "Use null for uncertain fields. No markdown, no extra text."
)

LLM_BATCH_SYSTEM_PROMPT: str = (
    "Expert audiobook librarian. "
    "You will receive a numbered list of audiobook folder paths and filenames. "
    "Each entry starts with a marker like [id=3] — that number is the entry's ID. "
    "For each entry, identify the official author, title, and series — "
    "resolving human naming schemes "
    "(e.g. 'ASOIAF 1' → 'A Game of Thrones', 'A Song of Ice and Fire', George R. R. Martin). "
    "Return ONLY a JSON array. Each object MUST include the matching 'id' integer "
    "so results can be reliably aligned with inputs, plus the keys: "
    "{id, author, title, series_name, series_sequence, narrator}. "
    "Use null for uncertain fields. Return one object per input id, no duplicates, no extras. "
    "No markdown, no extra text."
)

# ---------------------------------------------------------------------------
# Batch helpers (used by all validators)
# ---------------------------------------------------------------------------

def build_batch_user_message(contexts: list) -> str:
    """
    Combine multiple single-book context strings into one user message.

    Every entry is prefixed with an [id=N] marker so the model can echo
    the id back in its JSON response.  We then map results back to inputs
    by id instead of by array position — Groq's LLaMA in particular tends
    to reorder its batch outputs, which would otherwise silently misalign
    every book in the batch.

    [id=1]
    Full path : A\\B\\Book1
    File 1    : chapter01.mp3

    [id=2]
    Full path : A\\B\\Book2
    File 1    : book.m4b
    """
    parts = []
    for i, ctx in enumerate(contexts, 1):
        parts.append(f"[id={i}]\n{ctx.strip()}")
    return "\n\n".join(parts)


def parse_batch_array(raw: str, n: int) -> Optional[list]:
    """
    Extract a JSON array from the raw model response and align each item
    back to its input position using the echoed 'id' field.

    Returns a list of exactly n items (each a dict or None) — index 0 is
    the result for input id=1, etc.

    If NO item carries an 'id' field (older / non-compliant model output),
    we fall back to positional alignment so we don't make the situation
    worse, but log a warning.  This used to be the only mode and caused
    silent misalignment (Groq in particular reorders its outputs).

    Returns None only when the array itself cannot be parsed at all — the
    caller should then fall back to individual single-book calls.

    Handles:
      - <think>…</think> reasoning blocks
      - Markdown code fences
      - Leading / trailing prose
    """
    if not raw:
        return None

    # Strip thinking blocks (Qwen3, DeepSeek-R1, etc.)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    # Strip markdown fences
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    # Find the outermost JSON array
    start = raw.find("[")
    end   = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        items = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        # Last-resort: strip // comments
        cleaned = re.sub(r"//[^\n]*", "", raw[start : end + 1])
        try:
            items = json.loads(cleaned)
        except json.JSONDecodeError:
            return None

    if not isinstance(items, list):
        return None

    def _normalise(item):
        if not isinstance(item, dict):
            return None
        r = {
            "author":          str(item.get("author")          or "").strip(),
            "title":           str(item.get("title")           or "").strip(),
            "series_name":     str(item.get("series_name")     or "").strip(),
            "series_sequence": str(item.get("series_sequence") or "").strip(),
            "narrator":        str(item.get("narrator")        or "").strip(),
            "isbn":            str(item.get("isbn")            or "").strip(),
            "asin":            str(item.get("asin")            or "").strip(),
        }
        return r if (r["author"] or r["title"]) else None

    # ── Id-based alignment (preferred) ────────────────────────────────
    has_any_id = any(
        isinstance(it, dict) and it.get("id") is not None for it in items
    )

    if has_any_id:
        results = [None] * n
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("id")) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n:
                results[idx] = _normalise(item)
        return results

    # ── Positional fallback (legacy / non-compliant models) ───────────
    import logging
    logging.getLogger(__name__).warning(
        "Batch response had no 'id' fields — falling back to positional "
        "alignment. Results may be misaligned if the model reordered them."
    )
    results = [_normalise(it) for it in items[:n]]
    while len(results) < n:
        results.append(None)
    return results


def batch_max_tokens(n: int) -> int:
    """
    Conservative output-token budget for a batch of n books.
    Each book response is roughly 80-100 tokens of JSON.
    """
    return min(8192, max(512, n * 150))
