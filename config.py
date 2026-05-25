"""
Persistent settings manager.
Stores user preferences in ~/.bookorganizer/settings.json.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_SETTINGS_PATH = Path.home() / ".bookorganizer" / "settings.json"

_DEFAULTS: dict = {
    # ── Paths & keys ──────────────────────────────────────────────────────────
    "source_dir":               "",
    "output_root":              "",
    "gemini_api_key":           "",
    "groq_api_key":             "",
    "claude_api_key":           "",
    "lm_studio_url":            "http://127.0.0.1:1234",
    "lm_studio_model":          "local-model",
    # ── Behaviour flags ───────────────────────────────────────────────────────
    "skip_delete_file_confirm": False,
    "skip_delete_book_confirm": False,
    # After Re-Compare with Output: what to do with newly-flagged duplicates.
    # "prompt" = always ask; "hide" / "move" / "delete" = remembered choice.
    "recompare_dup_action":     "prompt",
    # Bytes threshold used by the "review small folders" sweep that runs
    # after Delete Empty Folders.  Remembered between runs.
    "small_folder_threshold":   1024,
    # ── GUI state — persisted on close, restored on startup ───────────────────
    "window_geometry":          "1250x820",
    "active_tab":               0,
    "show_review_only":         False,
    "log_level":                "INFO",
    "col_widths_books":         {},
    "col_widths_files":         {},
    "book_sort_col":            "",
    "book_sort_rev":            False,
}


def load_settings() -> dict:
    """
    Return the current settings dict.
    Missing keys are filled from _DEFAULTS so callers always get a complete dict.
    Returns _DEFAULTS if the file doesn't exist yet.
    """
    settings = dict(_DEFAULTS)
    if _SETTINGS_PATH.exists():
        try:
            on_disk = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            settings.update({k: v for k, v in on_disk.items() if k in _DEFAULTS})
        except Exception as e:
            log.warning(f"Could not read settings file: {e}")

    # Write back any keys that are in _DEFAULTS but missing from the file
    # (handles new keys added after initial setup)
    missing = [k for k in _DEFAULTS if k not in (json.loads(
        _SETTINGS_PATH.read_text(encoding="utf-8")) if _SETTINGS_PATH.exists() else {}
    )]
    if missing:
        try:
            _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SETTINGS_PATH.write_text(
                json.dumps(settings, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.debug(f"Settings: added missing keys {missing}")
        except Exception as e:
            log.warning(f"Could not update settings file: {e}")

    return settings


def save_settings_batch(updates: dict) -> None:
    """
    Update multiple keys in one atomic file write.
    Unknown keys are silently ignored (same policy as save_settings).
    """
    valid = {k: v for k, v in updates.items() if k in _DEFAULTS}
    if not valid:
        return
    settings = load_settings()
    settings.update(valid)
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_PATH.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.debug(f"Settings batch-saved: {list(valid.keys())}")
    except Exception as e:
        log.error(f"Could not save settings: {e}")


def save_settings(key: str, value) -> None:
    """
    Update a single key in the settings file.
    Value may be any JSON-serialisable type (str, bool, int, …).
    Unknown keys are silently ignored to avoid polluting the file.
    """
    if key not in _DEFAULTS:
        log.warning(f"save_settings: unknown key {key!r} — ignored")
        return

    settings = load_settings()
    settings[key] = value

    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_PATH.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.debug(f"Settings saved: {key}={value!r}")
    except Exception as e:
        log.error(f"Could not save settings: {e}")
