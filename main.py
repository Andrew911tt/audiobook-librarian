"""
Audiobook Librarian — Scan, Catalog & Review
Two-tab tkinter GUI — dry run only, no files are moved.
"""

import logging
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import config
import database
import file_mover
import scanner
from errors import RateLimitError, TokenLimitError

# ---------------------------------------------------------------------------
# Logging — file + console
# ---------------------------------------------------------------------------

LOG_PATH = Path.home() / ".bookorganizer" / "organizer.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spine design-system — color palette (light theme) + typography
# ---------------------------------------------------------------------------
# Colors are the exact Spine tokens from tokens.css, with oklch values
# pre-converted to sRGB hex for tkinter compatibility.

SPINE = {
    # Backgrounds — warm cream
    "bg":            "#f6f3ec",
    "surface":       "#ffffff",
    "surface2":      "#faf8f3",
    "surface3":      "#f0ede5",
    # Borders (rgba blended onto warm cream bg)
    "border":        "#e5e2db",   # rgba(34,26,18, 0.08)
    "border_strong": "#d8d5cd",   # rgba(34,26,18, 0.14)
    # Text
    "fg":            "#1c1814",
    "fg2":           "#4a4339",
    "fg3":           "#7a7163",
    "fg4":           "#aaa395",
    # Interactive fills
    "hover":         "#edeae3",   # rgba(34,26,18, 0.04)
    "selected":      "#f2eade",   # accent at 8% alpha on bg
    # Accent — warm amber  (oklch 0.66 0.13 55)
    "accent":        "#c4803a",
    "accent_soft":   "#f2e4d0",
    "accent_strong": "#a86828",   # oklch 0.58 0.15 50
    # Semantic
    "user":          "#8060c0",   # muted purple — manually locked books
    "ok":            "#4a9060",   # oklch 0.62 0.11 150  green
    "warn":          "#c09030",   # oklch 0.72 0.13  75  amber
    "bad":           "#c04828",   # oklch 0.60 0.16  28  red-orange
    "info":          "#4080b0",   # oklch 0.62 0.10 240  blue
}

# Typography — best font available on the system, with Windows defaults
def _pick_font(candidates: list[str], fallback: str) -> str:
    """Return the first candidate font family present on this system."""
    try:
        import tkinter.font as tkfont
        available = set(tkfont.families())
        for f in candidates:
            if f in available:
                return f
    except Exception:
        pass
    return fallback

# These are resolved lazily on first call (after Tk root exists)
_FONT_SANS:  str = ""
_FONT_MONO:  str = ""
_FONT_SERIF: str = ""

def _init_fonts() -> None:
    global _FONT_SANS, _FONT_MONO, _FONT_SERIF
    if _FONT_SANS:
        return  # already resolved
    _FONT_SANS  = _pick_font(["Geist", "Segoe UI", "Helvetica Neue", "Arial"], "TkDefaultFont")
    _FONT_MONO  = _pick_font(["JetBrains Mono", "Cascadia Mono", "Cascadia Code", "Consolas", "Courier New"], "TkFixedFont")
    _FONT_SERIF = _pick_font(["Source Serif 4", "Georgia", "Book Antiqua"], "TkDefaultFont")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_size(b) -> str:
    if b is None:
        return ""
    b = int(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _fmt_dur(seconds) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_mins(minutes) -> str:
    """Format an integer number of minutes as H:MM for Audible runtime display."""
    if not minutes:
        return ""
    m = int(minutes)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}" if h else f"{m}m"


# ---------------------------------------------------------------------------
# Reusable confirm-delete dialog with "Don't show again" checkbox
# ---------------------------------------------------------------------------

class _ConfirmDeleteDialog(tk.Toplevel):
    """
    Yes/No confirmation dialog with an optional 'Don't show this again' checkbox.

    Attributes after wait_window():
        confirmed  — True if the user clicked Yes
        dont_show  — True if the checkbox was ticked when Yes was clicked
    """

    def __init__(self, parent, message: str, title: str = "Confirm"):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        self.confirmed = False
        self.dont_show = False
        self._dont_var = tk.BooleanVar(value=False)

        frm = ttk.Frame(self, padding=18)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=message, justify="left",
                  wraplength=380).pack(anchor="w", pady=(0, 14))

        ttk.Checkbutton(frm, text="Don't show this warning again",
                        variable=self._dont_var).pack(anchor="w", pady=(0, 14))

        btn_frm = ttk.Frame(frm)
        btn_frm.pack()
        ttk.Button(btn_frm, text="Yes", width=10,
                   command=self._yes).pack(side="left", padx=6)
        ttk.Button(btn_frm, text="No",  width=10,
                   command=self.destroy).pack(side="left", padx=6)

        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - self.winfo_width() // 2}+{ph - self.winfo_height() // 2}")

        # Allow Enter = Yes, Escape = No
        self.bind("<Return>", lambda _: self._yes())
        self.bind("<Escape>", lambda _: self.destroy())

    def _yes(self):
        self.confirmed = True
        self.dont_show = self._dont_var.get()
        self.destroy()


# ---------------------------------------------------------------------------
# GUI logging handler — routes Python log records to the Activity Log tab
# ---------------------------------------------------------------------------

class _GuiLogHandler(logging.Handler):
    """
    Thread-safe logging.Handler that appends records to the Activity Log
    Text widget using Tk's after() so it is always called on the main thread.

    *append_fn* is a callable(level_name: str, text: str) provided by the App.
    The handler is attached to the root logger so it captures everything.
    """

    _FMT = logging.Formatter("[%(asctime)s][%(levelname)s][%(name)s] %(message)s",
                              datefmt="%H:%M:%S")

    def __init__(self, append_fn):
        super().__init__()
        self._append = append_fn
        self._widget = None   # set lazily once Tk mainloop is running

    def emit(self, record: logging.LogRecord):
        try:
            text = self._FMT.format(record)
            level = record.levelname  # "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
            # Schedule the UI update on the main thread
            self._append.__self__.after(0, self._append, level, text)
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# Tooltip helper — hover-delayed pop-up, attachable to any widget
# ---------------------------------------------------------------------------

class _Tooltip:
    """
    Lightweight cross-platform hover tooltip for tkinter / ttk widgets.
    Shows a small bordered popup after the cursor sits on the widget for
    *delay_ms* milliseconds; hides on leave, click, or focus-out.

    Usage:
        _Tooltip(my_button, "Scan the source directory")
        _tip(my_button, "...")      # shorthand

    For Treeview column-header tooltips use _tree_heading_tip().
    """

    _DELAY_MS = 550
    _MAX_WRAP_PX = 360

    def __init__(self, widget, text: str, *, delay_ms: int = None):
        self.widget = widget
        self.text   = text
        self.delay  = delay_ms or self._DELAY_MS
        self._after_id = None
        self._tip = None

        widget.bind("<Enter>",    self._schedule, add=True)
        widget.bind("<Leave>",    self._cancel,   add=True)
        widget.bind("<ButtonPress>", self._cancel, add=True)

    def _schedule(self, _evt=None):
        self._cancel()
        try:
            self._after_id = self.widget.after(self.delay, self._show)
        except tk.TclError:
            pass

    def _cancel(self, _evt=None):
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    def _show(self):
        if self._tip or not self.text:
            return
        try:
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 18
        except tk.TclError:
            return

        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)        # no window decorations
        tw.wm_geometry(f"+{x}+{y}")
        try:
            tw.attributes("-topmost", True)
        except Exception:
            pass

        lbl = tk.Label(
            tw, text=self.text, justify="left",
            background="#1c1814", foreground="#f6f3ec",
            relief="solid", borderwidth=1,
            font=("Segoe UI", 8),
            wraplength=self._MAX_WRAP_PX,
            padx=8, pady=4,
        )
        lbl.pack()


def _tip(widget, text: str) -> None:
    """Convenience wrapper — attach a hover tooltip to *widget*."""
    if widget is not None and text:
        _Tooltip(widget, text)


def _tree_heading_tip(tree: "ttk.Treeview", col_to_text: dict) -> None:
    """
    Attach hover tooltips to treeview column headers.  Tkinter's headings
    are drawn by Tcl rather than as real widgets, so we listen for motion
    over the heading region and show a tooltip describing the column the
    cursor is currently over.
    """
    state = {"col": None, "after_id": None, "tip": None}

    def _hide():
        if state["after_id"]:
            try: tree.after_cancel(state["after_id"])
            except Exception: pass
            state["after_id"] = None
        if state["tip"]:
            try: state["tip"].destroy()
            except Exception: pass
            state["tip"] = None

    def _show(col):
        text = col_to_text.get(col)
        if not text:
            return
        try:
            x = tree.winfo_pointerx() + 14
            y = tree.winfo_pointery() + 18
        except tk.TclError:
            return
        tw = tk.Toplevel(tree)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        try: tw.attributes("-topmost", True)
        except Exception: pass
        tk.Label(
            tw, text=text, justify="left",
            background="#1c1814", foreground="#f6f3ec",
            relief="solid", borderwidth=1,
            font=("Segoe UI", 8), wraplength=360,
            padx=8, pady=4,
        ).pack()
        state["tip"] = tw

    def on_motion(evt):
        if tree.identify_region(evt.x, evt.y) != "heading":
            if state["col"] is not None:
                _hide()
                state["col"] = None
            return
        col_id = tree.identify_column(evt.x)
        try:
            idx  = int(col_id.lstrip("#")) - 1
            cols = tree["columns"]
            col  = cols[idx] if 0 <= idx < len(cols) else None
        except (ValueError, IndexError):
            col = None
        if col != state["col"]:
            _hide()
            state["col"] = col
            if col:
                state["after_id"] = tree.after(550, lambda c=col: _show(c))

    def on_leave(_):
        _hide()
        state["col"] = None

    tree.bind("<Motion>", on_motion, add=True)
    tree.bind("<Leave>",  on_leave,  add=True)


# ---------------------------------------------------------------------------
# User-guide viewer dialog (renders USAGE.md with light markdown styling)
# ---------------------------------------------------------------------------

class _HelpDialog(tk.Toplevel):
    """Scrollable read-only window that renders USAGE.md with simple
    markdown-aware styling (headings, code, lists, tables shown verbatim).
    Modal-ish (transient + topmost) but doesn't grab so the user can refer
    back to the main app while reading."""

    def __init__(self, parent, markdown_text: str):
        super().__init__(parent)
        self.title("Audiobook Librarian — User Guide")
        self.geometry("960x760")
        self.resizable(True, True)
        self.transient(parent)

        frm = ttk.Frame(self, padding=8)
        frm.pack(fill="both", expand=True)

        # Scrollable text area
        wrap = ttk.Frame(frm)
        wrap.pack(fill="both", expand=True)
        vsb = ttk.Scrollbar(wrap, orient="vertical")
        vsb.pack(side="right", fill="y")
        txt = tk.Text(
            wrap, wrap="word", font=("Segoe UI", 10),
            bg=SPINE["surface"], fg=SPINE["fg"],
            relief="flat", padx=14, pady=10,
            yscrollcommand=vsb.set, state="normal",
        )
        txt.pack(side="left", fill="both", expand=True)
        vsb.config(command=txt.yview)

        # Tags for basic markdown styling
        txt.tag_configure("h1", font=("Segoe UI", 16, "bold"),
                          foreground=SPINE["accent_strong"], spacing1=12, spacing3=6)
        txt.tag_configure("h2", font=("Segoe UI", 13, "bold"),
                          foreground=SPINE["accent"], spacing1=10, spacing3=4)
        txt.tag_configure("h3", font=("Segoe UI", 11, "bold"),
                          foreground=SPINE["fg"], spacing1=8, spacing3=3)
        txt.tag_configure("hr", foreground=SPINE["fg4"])
        txt.tag_configure("code", font=("Consolas", 9),
                          background=SPINE["surface2"], foreground=SPINE["fg2"])
        txt.tag_configure("li", lmargin1=18, lmargin2=32)
        txt.tag_configure("table", font=("Consolas", 9),
                          background=SPINE["surface2"], foreground=SPINE["fg"])
        txt.tag_configure("bold", font=("Segoe UI", 10, "bold"))

        # Very small markdown renderer
        in_code_block = False
        for raw in markdown_text.splitlines():
            line = raw.rstrip("\n")

            if line.strip().startswith("```"):
                in_code_block = not in_code_block
                txt.insert("end", "\n")
                continue
            if in_code_block:
                txt.insert("end", line + "\n", "code")
                continue

            stripped = line.lstrip()
            if stripped.startswith("# "):
                txt.insert("end", stripped[2:] + "\n", "h1")
            elif stripped.startswith("## "):
                txt.insert("end", stripped[3:] + "\n", "h2")
            elif stripped.startswith("### "):
                txt.insert("end", stripped[4:] + "\n", "h3")
            elif stripped.startswith("---"):
                txt.insert("end", "─" * 80 + "\n", "hr")
            elif stripped.startswith(("- ", "* ")):
                txt.insert("end", "  • " + stripped[2:] + "\n", "li")
            elif stripped[:2].isdigit() and stripped[:3].endswith("."):
                txt.insert("end", "  " + stripped + "\n", "li")
            elif "|" in line and line.strip().startswith("|"):
                # Markdown table row — render verbatim in monospace
                txt.insert("end", line + "\n", "table")
            else:
                # Inline bold **…** — simple pass
                if "**" in line:
                    parts = line.split("**")
                    for i, part in enumerate(parts):
                        if not part:
                            continue
                        txt.insert("end", part, "bold" if i % 2 == 1 else "")
                    txt.insert("end", "\n")
                else:
                    txt.insert("end", line + "\n")

        txt.config(state="disabled")

        # Buttons
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Open Raw File",
                   command=self._open_raw, width=14).pack(side="left")
        ttk.Button(btns, text="Close", width=10,
                   command=self.destroy).pack(side="right")

        # Position centred on parent
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - 480}+{ph - 380}")

    def _open_raw(self):
        """Open USAGE.md in the OS default editor."""
        import subprocess
        usage_path = Path(__file__).parent / "USAGE.md"
        try:
            os.startfile(str(usage_path))  # Windows
        except AttributeError:
            subprocess.Popen(["xdg-open", str(usage_path)])
        except Exception as e:
            messagebox.showwarning("Could Not Open",
                                   str(e), parent=self)


# ---------------------------------------------------------------------------
# Database record viewer dialog
# ---------------------------------------------------------------------------

class _BookRecordDialog(tk.Toplevel):
    """
    Scrollable read-only popup that shows every column from the books table
    for the selected book, plus the full list of associated files.
    """

    def __init__(self, parent, parent_folder: str):
        super().__init__(parent)
        self.title("Database Record")
        self.resizable(True, True)
        self.grab_set()
        self.transient(parent)

        import database as _db, sqlite3

        # ── Fetch book row ────────────────────────────────────────────
        book = _db.fetch_book(parent_folder)
        with sqlite3.connect(str(_db.DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            files = conn.execute(
                "SELECT abs_path, file_name, file_type, extension, "
                "file_size, duration_seconds, bitrate, confidence_score, "
                "is_duplicate, extracted_title, extracted_author "
                "FROM files WHERE parent_folder = ? "
                "ORDER BY file_type, file_name",
                (parent_folder,),
            ).fetchall()

        # ── Layout ───────────────────────────────────────────────────
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        # Top label
        ttk.Label(frm, text=parent_folder, font=("Segoe UI", 9, "bold"),
                  foreground=SPINE["info"], wraplength=700, justify="left").pack(
            anchor="w", pady=(0, 8))

        # Scrollable text area
        txt_frame = ttk.Frame(frm)
        txt_frame.pack(fill="both", expand=True)

        vsb = ttk.Scrollbar(txt_frame, orient="vertical")
        vsb.pack(side="right", fill="y")
        hsb = ttk.Scrollbar(txt_frame, orient="horizontal")
        hsb.pack(side="bottom", fill="x")

        txt = tk.Text(txt_frame, font=("Consolas", 9), wrap="none",
                      state="disabled", relief="flat", bg=SPINE["surface2"],
                      fg=SPINE["fg"],
                      yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        txt.pack(fill="both", expand=True)
        vsb.config(command=txt.yview)
        hsb.config(command=txt.xview)

        # Tags for formatting
        txt.tag_config("header",  font=("Consolas", 9, "bold"), foreground=SPINE["accent"])
        txt.tag_config("key",     foreground=SPINE["warn"])
        txt.tag_config("val",     foreground=SPINE["fg"])
        txt.tag_config("empty",   foreground=SPINE["fg4"])
        txt.tag_config("divider", foreground="#cccccc")

        def append(text, tag="val"):
            txt.config(state="normal")
            txt.insert("end", text, tag)
            txt.config(state="disabled")

        def section(title):
            append(f"\n{'─'*60}\n  {title}\n{'─'*60}\n", "header")

        def field(key, value):
            append(f"  {key:<28}", "key")
            if value is None or value == "" or value == 0:
                append("—\n", "empty")
            else:
                append(f"{value}\n", "val")

        # ── Book fields ───────────────────────────────────────────────
        section("BOOK GROUP")
        if book:
            for col in book.keys():
                field(col, book[col])
        else:
            append("  (no book record found)\n", "empty")

        # ── Files ─────────────────────────────────────────────────────
        section(f"FILES  ({len(files)} total)")
        for f in files:
            append(f"\n  {f['abs_path']}\n", "key")
            field("  type / ext",
                  f"{f['file_type']} / {f['extension']}")
            field("  size",
                  f"{f['file_size']:,} bytes" if f['file_size'] else None)
            field("  duration",
                  f"{f['duration_seconds']:.1f}s" if f['duration_seconds'] else None)
            field("  bitrate",
                  f"{f['bitrate']} kbps" if f['bitrate'] else None)
            field("  confidence",  f['confidence_score'])
            field("  extracted title",  f['extracted_title'])
            field("  extracted author", f['extracted_author'])
            field("  is_duplicate", "YES" if f['is_duplicate'] else "no")

        # ── Buttons ───────────────────────────────────────────────────
        btn_frm = ttk.Frame(frm)
        btn_frm.pack(pady=(8, 0))
        ttk.Button(btn_frm, text="Copy All", width=10,
                   command=lambda: self._copy(txt)).pack(side="left", padx=4)
        ttk.Button(btn_frm, text="Close",    width=10,
                   command=self.destroy).pack(side="left", padx=4)

        # Size and centre
        self.update_idletasks()
        self.geometry("780x560")
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - 390}+{ph - 280}")

    def _copy(self, txt):
        txt.config(state="normal")
        content = txt.get("1.0", "end")
        txt.config(state="disabled")
        self.clipboard_clear()
        self.clipboard_append(content)


# ---------------------------------------------------------------------------
# Transfer mode dialog
# ---------------------------------------------------------------------------

class _TransferModeDialog(tk.Toplevel):
    """
    Asks the user which file-transfer method to use before organising.

    Attributes after wait_window():
        mode  — 'copy' (default) or 'move', or None if cancelled.
    """

    _MODE_INFO = {
        "copy": (
            "Copy  (recommended)",
            "Duplicates every file into the output folder.\n"
            "Uses extra disk space equal to your library size.\n"
            "Original files are kept exactly where they are.",
        ),
        "move": (
            "Move  ⚠ destructive",
            "Moves files out of their current location into the output folder.\n"
            "Original folder structure is removed after transfer.\n"
            "This cannot be undone — make sure your output root is correct.",
        ),
    }

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Choose Transfer Method")
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        self.mode = None
        self._var = tk.StringVar(value="copy")

        frm = ttk.Frame(self, padding=18)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="How should files be transferred to the output folder?",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 12))

        self._desc_var = tk.StringVar()
        for mode_key, (label, desc) in self._MODE_INFO.items():
            rb = ttk.Radiobutton(
                frm, text=label, variable=self._var, value=mode_key,
                command=self._update_desc,
            )
            rb.pack(anchor="w", pady=2)

        # Description box
        desc_frm = ttk.LabelFrame(frm, text="Details", padding=8)
        desc_frm.pack(fill="x", pady=(10, 14))
        self._desc_lbl = ttk.Label(desc_frm, textvariable=self._desc_var,
                                   justify="left", wraplength=360)
        self._desc_lbl.pack(anchor="w")
        self._update_desc()

        btn_frm = ttk.Frame(frm)
        btn_frm.pack()
        ttk.Button(btn_frm, text="Proceed", width=12,
                   command=self._proceed).pack(side="left", padx=6)
        ttk.Button(btn_frm, text="Cancel",  width=12,
                   command=self.destroy).pack(side="left", padx=6)

        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - self.winfo_width() // 2}+{ph - self.winfo_height() // 2}")

        self.bind("<Return>", lambda _: self._proceed())
        self.bind("<Escape>", lambda _: self.destroy())

    def _update_desc(self):
        _, desc = self._MODE_INFO[self._var.get()]
        self._desc_var.set(desc)

    def _proceed(self):
        self.mode = self._var.get()
        self.destroy()


# ---------------------------------------------------------------------------
# Threshold picker for the "review small folders" sweep
# ---------------------------------------------------------------------------

class _ThresholdDialog(tk.Toplevel):
    """
    Modal radio-button picker for a byte threshold.  Used after Delete Empty
    Folders finishes, so the user can choose what counts as a "small" folder
    worth manually reviewing (e.g. 1 KB to catch folders with just a stray
    desktop.ini, or 100 KB to catch larger junk).

    Attributes after wait_window():
        result  — int byte threshold, or None if cancelled
    """

    _PRESETS = [
        (1024,       "1 KB",     "Catches folders with only tiny stub files (desktop.ini, .DS_Store)."),
        (10 * 1024,  "10 KB",    "Catches single small text files / cue sheets / metadata leftovers."),
        (100 * 1024, "100 KB",   "Catches small images or short audio fragments."),
        (1024 * 1024,"1 MB",     "Catches partial / corrupted audiobook leftovers."),
    ]

    def __init__(self, parent, default_bytes: int = 1024):
        super().__init__(parent)
        self.title("Review Small Folders — Pick Threshold")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result = None
        self._choice = tk.StringVar(value=str(default_bytes))
        self._custom_var = tk.StringVar(value=str(default_bytes))

        frm = ttk.Frame(self, padding=18)
        frm.pack(fill="both", expand=True)

        ttk.Label(
            frm,
            text="Review folders smaller than:",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        for value, label, desc in self._PRESETS:
            rb = ttk.Radiobutton(
                frm, text=label, variable=self._choice, value=str(value),
            )
            rb.pack(anchor="w", pady=(2, 0))
            ttk.Label(frm, text="   " + desc,
                      foreground=SPINE["fg3"],
                      font=("Segoe UI", 8)).pack(anchor="w")

        ttk.Separator(frm, orient="horizontal").pack(fill="x", pady=(10, 6))

        # Custom row
        custom_frm = ttk.Frame(frm)
        custom_frm.pack(fill="x")
        ttk.Radiobutton(custom_frm, text="Custom (bytes):",
                        variable=self._choice, value="custom").pack(side="left")
        ttk.Entry(custom_frm, textvariable=self._custom_var,
                  width=12).pack(side="left", padx=(6, 0))

        # Buttons
        btn_frm = ttk.Frame(frm)
        btn_frm.pack(fill="x", pady=(14, 0))
        ttk.Button(btn_frm, text="Cancel", width=10,
                   command=self._cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frm, text="Continue", width=12,
                   command=self._ok).pack(side="right")

        self.bind("<Return>", lambda _: self._ok())
        self.bind("<Escape>", lambda _: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - self.winfo_width() // 2}+{ph - self.winfo_height() // 2}")

    def _ok(self):
        choice = self._choice.get()
        if choice == "custom":
            try:
                self.result = max(1, int(self._custom_var.get().strip()))
            except ValueError:
                messagebox.showwarning(
                    "Invalid Number",
                    "Enter a positive integer number of bytes.",
                    parent=self,
                )
                return
        else:
            try:
                self.result = int(choice)
            except ValueError:
                self.result = 1024
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ---------------------------------------------------------------------------
# Per-folder Keep / Delete prompt for the small-folder sweep
# ---------------------------------------------------------------------------

class _SmallFolderDialog(tk.Toplevel):
    """
    Modal popup showing one small folder's contents so the user can decide
    keep/delete on the spot.  Cancel aborts the entire sweep.

    Attributes after wait_window():
        result  — "keep" | "delete" | "cancel"
    """

    def __init__(self, parent, folder: str, total_bytes: int,
                 files: list, index: int, total: int):
        super().__init__(parent)
        self.title(f"Small Folder Review  ({index}/{total})")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self.result = "keep"   # default if window is closed without picking

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(
            frm,
            text=f"Folder {index} of {total}",
            font=("Segoe UI", 9),
            foreground=SPINE["fg3"],
        ).pack(anchor="w")

        ttk.Label(
            frm,
            text=folder,
            font=("Segoe UI", 10, "bold"),
            foreground=SPINE["info"],
            wraplength=620, justify="left",
        ).pack(anchor="w", pady=(2, 8))

        ttk.Label(
            frm,
            text=(
                f"Total size: {_fmt_size(total_bytes)}  "
                f"({total_bytes:,} bytes)"
                f"   •   {len(files)} file(s)"
            ),
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(frm, text="Contents:",
                  font=("Segoe UI", 9, "bold")).pack(anchor="w")

        # Scrollable file list
        list_frm = ttk.Frame(frm)
        list_frm.pack(fill="both", expand=True, pady=(2, 10))
        vsb = ttk.Scrollbar(list_frm, orient="vertical")
        vsb.pack(side="right", fill="y")
        txt = tk.Text(list_frm, height=10, wrap="none",
                      font=("Consolas", 9),
                      bg=SPINE["surface2"], fg=SPINE["fg"],
                      yscrollcommand=vsb.set, state="normal")
        txt.pack(side="left", fill="both", expand=True)
        vsb.config(command=txt.yview)

        for rel_path, size in files:
            txt.insert("end", f"  {_fmt_size(size):>10}   {rel_path}\n")
        txt.config(state="disabled")

        # Buttons — Keep | Delete | Cancel All
        btn_frm = ttk.Frame(frm)
        btn_frm.pack(fill="x")
        ttk.Button(btn_frm, text="Cancel All", width=12,
                   command=lambda: self._set("cancel")).pack(side="right")
        ttk.Button(btn_frm, text="Delete", width=10,
                   command=lambda: self._set("delete")).pack(
            side="right", padx=(0, 6))
        ttk.Button(btn_frm, text="Keep", width=10,
                   command=lambda: self._set("keep")).pack(
            side="right", padx=(0, 6))

        self.bind("<Escape>", lambda _: self._set("cancel"))
        self.protocol("WM_DELETE_WINDOW", lambda: self._set("cancel"))

        self.update_idletasks()
        self.geometry("700x440")
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - 350}+{ph - 220}")

    def _set(self, choice: str):
        self.result = choice
        self.destroy()


# ---------------------------------------------------------------------------
# Re-Compare → action chooser dialog
# ---------------------------------------------------------------------------

class _RecompareDupDialog(tk.Toplevel):
    """
    Modal popup shown after Re-Compare with Output finishes with newly-flagged
    duplicate books. The user picks one of:

      - hide   : leave is_duplicate=1, rows stay hidden (no file changes)
      - move   : move folders to <source>/ABL_duplicates + add to skip list
      - delete : permanently delete the duplicate folders from disk

    Cancel un-flags the books (they reappear in the list).

    Attributes after wait_window():
        result    — "hide" | "move" | "delete" | "cancel" | None
        remember  — True if the "Remember my choice" box was ticked
    """

    def __init__(self, parent, n_books: int, total_bytes: int):
        super().__init__(parent)
        self.title("Re-Compare — Duplicates Found")
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        self.result   = None
        self.remember = False

        self._choice  = tk.StringVar(value="move")  # recommended default
        self._remem   = tk.BooleanVar(value=False)

        frm = ttk.Frame(self, padding=18)
        frm.pack(fill="both", expand=True)

        ttk.Label(
            frm,
            text=f"Re-Compare flagged {n_books:,} new duplicate book(s) "
                 f"({_fmt_size(total_bytes)}).",
            font=("Segoe UI", 10, "bold"),
            wraplength=420, justify="left",
        ).pack(anchor="w", pady=(0, 6))

        ttk.Label(
            frm,
            text="These already exist in your output library. "
                 "What should happen to the source copies?",
            wraplength=420, justify="left",
            foreground=SPINE["fg2"],
        ).pack(anchor="w", pady=(0, 12))

        opts = [
            ("hide",
             "Hide only",
             "Keep files on disk; rows stay hidden from the Books list. "
             "Will reappear after the next Scan."),
            ("move",
             "Move to ABL_duplicates  (recommended)",
             "Move each duplicate folder into <source>/ABL_duplicates and add "
             "that path to the Ignored Folders list so future scans skip it. "
             "Reversible — move folders back out if you change your mind."),
            ("delete",
             "Delete from disk",
             "Permanently remove the duplicate folders and everything inside "
             "them. Confirms again before deleting. Cannot be undone."),
        ]
        for value, label, desc in opts:
            rb = ttk.Radiobutton(
                frm, text=label, variable=self._choice, value=value,
            )
            rb.pack(anchor="w", pady=(4, 0))
            ttk.Label(frm, text="   " + desc,
                      foreground=SPINE["fg3"],
                      font=("Segoe UI", 8),
                      wraplength=420, justify="left").pack(anchor="w")

        ttk.Separator(frm, orient="horizontal").pack(fill="x", pady=(12, 8))

        ttk.Checkbutton(
            frm,
            text="Remember my choice (skip this dialog next time)",
            variable=self._remem,
        ).pack(anchor="w", pady=(0, 12))

        btn_frm = ttk.Frame(frm)
        btn_frm.pack(fill="x")
        ttk.Button(btn_frm, text="Cancel", width=12,
                   command=self._cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frm, text="Apply", width=12,
                   command=self._apply).pack(side="right")

        self.bind("<Return>", lambda _: self._apply())
        self.bind("<Escape>", lambda _: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - self.winfo_width() // 2}+{ph - self.winfo_height() // 2}")

    def _apply(self):
        self.result   = self._choice.get()
        self.remember = bool(self._remem.get())
        self.destroy()

    def _cancel(self):
        self.result   = "cancel"
        self.remember = False
        self.destroy()


# ---------------------------------------------------------------------------
# Single-book sync detail dialog
# ---------------------------------------------------------------------------

class SingleSyncDialog(tk.Toplevel):
    """
    Modal popup that runs a sync for one book and shows everything:
      - What was sent (search term / context string / URL)
      - Raw response from the API (full JSON or LLM reply)
      - Parsed fields extracted by our validator
    User can Apply (write to DB) or Cancel.
    """

    def __init__(self, parent, book_row: dict, source: str,
                 api_key: str = "", output_root: str = "", model: str = ""):
        super().__init__(parent)
        self.title(f"Single Sync — {source.title()}")
        # Audible dialog needs extra room for the candidates table
        self.geometry("1000x780" if source == "audible" else "900x620")
        self.resizable(True, True)
        self.grab_set()
        self.transient(parent)

        self._book     = book_row
        self._source   = source          # "audible" | "gemini" | "groq" | "local"
        self._api_key  = api_key         # for local: carries base_url
        self._model    = model           # for local LM Studio model name
        self._out_root = output_root
        self._result   = None            # parsed result dict if hit
        self._applied  = False
        self._known_dur_sec = None       # set by Audible path in _do_sync

        self._build_ui()
        self.after(100, self._run_sync)  # start after window is visible

        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - 450}+{ph - 310}")

    # ------------------------------------------------------------------
    def _build_ui(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        # Book info header
        folder_name = str(Path(self._book.get("parent_folder", "")).name)
        ttk.Label(frm, text=f"Book: {folder_name}",
                  font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        # ── Left panel: Sent ───────────────────────────────────────────
        ttk.Label(frm, text=f"Sent to {self._source.title()}:",
                  font=("Segoe UI", 9, "bold")).grid(
            row=1, column=0, sticky="w")

        self._sent_box = tk.Text(frm, width=42, height=18, wrap="word",
                                  font=("Consolas", 8), state="disabled",
                                  bg=SPINE["surface2"], fg=SPINE["fg"])
        self._sent_box.grid(row=2, column=0, sticky="nsew", padx=(0, 6))

        # ── Right panel: Received ──────────────────────────────────────
        ttk.Label(frm, text="Raw Response:",
                  font=("Segoe UI", 9, "bold")).grid(
            row=1, column=1, sticky="w")

        self._recv_box = tk.Text(frm, width=42, height=18, wrap="word",
                                  font=("Consolas", 8), state="disabled",
                                  bg=SPINE["surface2"], fg=SPINE["fg"])
        self._recv_box.grid(row=2, column=1, sticky="nsew")

        # ── Parsed result ──────────────────────────────────────────────
        ttk.Separator(frm, orient="horizontal").grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=8)

        self._parsed_var = tk.StringVar(value="Running sync…")
        ttk.Label(frm, textvariable=self._parsed_var, justify="left",
                  font=("Segoe UI", 9)).grid(
            row=4, column=0, columnspan=2, sticky="w")

        # ── Candidates chooser (Audible only) ──────────────────────────
        # Hidden by default; populated by _show_audible_candidates() once
        # results come back.  Selecting a row updates self._result so the
        # Apply button writes the user-chosen match.
        self._candidates: list = []
        self._cand_frame = ttk.LabelFrame(
            frm, text=" Pick the correct match (click to select) ", padding=4)
        # Stored — not grid'd until needed.  See _show_audible_candidates().
        cand_cols = ("title", "author", "narrator", "runtime", "series", "asin")
        self._cand_tree = ttk.Treeview(
            self._cand_frame, columns=cand_cols, show="headings",
            selectmode="browse", height=6,
        )
        for col, head, w in (
            ("title",    "Title",    260),
            ("author",   "Author",   150),
            ("narrator", "Narrator", 130),
            ("runtime",  "Runtime",   70),
            ("series",   "Series",   140),
            ("asin",     "ASIN",     100),
        ):
            self._cand_tree.heading(col, text=head)
            self._cand_tree.column(col, width=w, anchor="w")
        cvsb = ttk.Scrollbar(self._cand_frame, orient="vertical",
                             command=self._cand_tree.yview)
        self._cand_tree.configure(yscrollcommand=cvsb.set)
        self._cand_tree.pack(side="left", fill="both", expand=True)
        cvsb.pack(side="right", fill="y")
        self._cand_tree.bind("<<TreeviewSelect>>", self._on_candidate_pick)

        # ── Buttons ────────────────────────────────────────────────────
        btn_frm = ttk.Frame(frm)
        btn_frm.grid(row=6, column=0, columnspan=2, pady=(8, 0))

        self._apply_btn = ttk.Button(btn_frm, text="Apply Changes",
                                      width=16, command=self._apply,
                                      state="disabled")
        self._apply_btn.pack(side="left", padx=6)
        ttk.Button(btn_frm, text="Cancel", width=10,
                   command=self.destroy).pack(side="left")

        frm.rowconfigure(2, weight=1)
        frm.columnconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

    # ------------------------------------------------------------------
    def _write(self, widget: tk.Text, text: str):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.config(state="disabled")

    def _run_sync(self):
        import json, threading
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _do_sync(self):
        import json, requests as _req
        folder      = self._book.get("parent_folder", "")
        folder_name = Path(folder).name
        best_title  = self._book.get("best_title", "")
        best_author = self._book.get("best_author", "")

        sent_text = ""
        raw_text  = ""
        result    = None

        try:
            if self._source == "audible":
                from audible_validator import (
                    _ENDPOINT,
                    fetch_audible_candidates,
                    pick_best_by_duration,
                )
                from database import _sanitize_title_for_search
                clean = _sanitize_title_for_search(best_title) or \
                        _sanitize_title_for_search(folder_name)
                search_term = f"{clean} {best_author}".strip()
                known_dur   = self._book.get("total_duration") or None
                # Always fetch up to 10 so the user has a real choice.
                params = {
                    "keywords":        search_term,
                    "num_results":     10,
                    "response_groups": "contributors,series,product_desc,product_attrs",
                }
                url = _req.Request("GET", _ENDPOINT, params=params).prepare().url
                sent_text = (
                    f"GET {url}\n\nSearch term:\n  {search_term}"
                    + (f"\nKnown duration: {known_dur/60:.1f} min" if known_dur else "")
                    + "\n\nShowing all candidates — click one to choose, then Apply."
                )
                resp = _req.get(_ENDPOINT, params=params, timeout=10)
                raw_json = resp.json()
                raw_text = json.dumps(raw_json, indent=2)
                # Build candidate list + pre-selected best
                candidates = fetch_audible_candidates(search_term, num_results=10)
                self._candidates = candidates
                result = pick_best_by_duration(candidates, known_dur)
                # known_dur stored so the UI can compute Δ to known length per row
                self._known_dur_sec = known_dur

            elif self._source == "google":
                from google_books_api import fetch_google_books_data, _ENDPOINT as _GB_ENDPOINT
                clean = __import__("database")._sanitize_title_for_search(best_title) or \
                        __import__("database")._sanitize_title_for_search(folder_name)
                query = f"intitle:{clean}"
                if best_author:
                    query += f"+inauthor:{best_author}"
                params = {"q": query, "maxResults": 5, "printType": "books", "langRestrict": "en"}
                import requests as _req2
                url2 = _req2.Request("GET", _GB_ENDPOINT, params=params).prepare().url
                sent_text = f"GET {url2}\n\nTitle: {clean}\nAuthor: {best_author}"
                resp2 = _req2.get(_GB_ENDPOINT, params=params, timeout=10)
                raw_json = resp2.json()
                raw_text = json.dumps(raw_json, indent=2)
                result   = fetch_google_books_data(clean, best_author)

            elif self._source == "openlibrary":
                from openlibrary_validator import fetch_openlibrary_data, _ENDPOINT as _OL_ENDPOINT
                clean = __import__("database")._sanitize_title_for_search(best_title) or \
                        __import__("database")._sanitize_title_for_search(folder_name)
                sent = f"{clean} {best_author}".strip()
                sent_text = f"GET {_OL_ENDPOINT}\n\nTitle: {clean}\nAuthor: {best_author}"
                result   = fetch_openlibrary_data(clean, best_author)
                raw_text = json.dumps(result, indent=2) if result else "(no result)"

            elif self._source == "hardcover":
                from hardcover_validator import fetch_hardcover_data, _ENDPOINT as _HC_ENDPOINT
                clean = __import__("database")._sanitize_title_for_search(best_title) or \
                        __import__("database")._sanitize_title_for_search(folder_name)
                sent_text = f"POST {_HC_ENDPOINT}\n\nTitle: {clean}\nAuthor: {best_author}"
                result   = fetch_hardcover_data(clean, best_author, api_key=self._api_key)
                raw_text = json.dumps(result, indent=2) if result else "(no result)"

            elif self._source in ("gemini", "groq", "claude", "chatgpt", "local"):
                with __import__("sqlite3").connect(
                        str(__import__("database").DB_PATH)) as conn:
                    conn.row_factory = __import__("sqlite3").Row
                    samples = __import__("database")._get_sample_filenames(
                        conn, folder)
                context = __import__("database")._build_llm_context(
                    folder, samples)
                sent_text = context

                if self._source == "chatgpt":
                    from chatgpt_validator import parse_book_metadata_chatgpt, _MODEL as _CHATGPT_MODEL
                    sent_text = f"Model: {_CHATGPT_MODEL}\n\n{context}"
                    result    = parse_book_metadata_chatgpt(context, api_key=self._api_key)
                    raw_text  = json.dumps(result, indent=2) if result else "(no result)"

                elif self._source == "claude":
                    from claude_validator import parse_book_metadata_claude, _MODEL as _CLAUDE_MODEL
                    sent_text = (
                        f"Model: {_CLAUDE_MODEL}\n\n{context}"
                    )
                    result   = parse_book_metadata_claude(context, api_key=self._api_key)
                    raw_text = json.dumps(result, indent=2) if result else "(no result)"

                elif self._source == "local":
                    from local_llm_validator import parse_book_metadata_local
                    base_url = self._api_key or "http://127.0.0.1:1234"  # api_key field carries URL
                    client_label = f"LM Studio @ {base_url}"
                    sent_text = f"URL: {base_url}\nModel: {self._model or 'local-model'}\n\n{context}"
                    result = parse_book_metadata_local(
                        context, base_url=base_url,
                        model=self._model or "local-model",
                    )
                    raw_text = json.dumps(result, indent=2) if result else "(no result)"

                elif self._source == "gemini":
                    from google import genai
                    from google.genai import types
                    from gemini_validator import _MODEL as _GEMINI_MODEL, parse_book_metadata
                    from prompts import LLM_SYSTEM_PROMPT
                    client = genai.Client(api_key=self._api_key)
                    response = client.models.generate_content(
                        model=_GEMINI_MODEL,
                        contents=context,
                        config=types.GenerateContentConfig(
                            system_instruction=LLM_SYSTEM_PROMPT,
                            temperature=0,
                        ),
                    )
                    raw_text = response.text or ""
                    result = parse_book_metadata(context, self._api_key)

                else:  # groq
                    from groq import Groq
                    from groq_validator import _MODEL as _GROQ_MODEL, parse_book_metadata_groq
                    from prompts import LLM_SYSTEM_PROMPT
                    client = Groq(api_key=self._api_key)
                    completion = client.chat.completions.create(
                        model=_GROQ_MODEL,
                        response_format={"type": "json_object"},
                        messages=[
                            {"role": "system", "content": LLM_SYSTEM_PROMPT},
                            {"role": "user",   "content": context},
                        ],
                        temperature=0,
                    )
                    raw_text = completion.choices[0].message.content or ""
                    result = parse_book_metadata_groq(context, self._api_key)

        except Exception as e:
            raw_text = f"ERROR: {e}"

        self._result = result
        self.after(0, lambda: self._show_results(sent_text, raw_text, result))

    def _show_results(self, sent: str, raw: str, result):
        self._write(self._sent_box, sent)
        self._write(self._recv_box, raw)

        # ── Audible: populate the candidates chooser ──────────────────
        if self._source == "audible" and self._candidates:
            self._populate_audible_candidates(result)
            return

        # ── Everything else: single parsed result ─────────────────────
        if result:
            runtime = result.get('runtime_min')
            lines = [
                f"Author   : {result.get('author','')}",
                f"Title    : {result.get('title','')}",
                f"Series   : {result.get('series_name','')}",
                f"Sequence : {result.get('series_sequence','')}",
                f"Narrator : {result.get('narrator','')}",
                f"ISBN     : {result.get('isbn','')}",
                f"ASIN     : {result.get('asin','')}",
                f"Runtime  : {runtime} min" if runtime else "Runtime  : —",
            ]
            self._parsed_var.set("Parsed Result:\n" + "\n".join(lines))
            self._apply_btn.config(state="normal")
        else:
            self._parsed_var.set("No result returned — nothing to apply.")

    def _populate_audible_candidates(self, preselected: dict | None):
        """
        Show the candidates treeview, fill it with self._candidates, and
        pre-select the row matching *preselected* (the duration-best match).
        """
        # Reveal the candidates frame below the parsed-result label
        self._cand_frame.grid(row=5, column=0, columnspan=2,
                              sticky="nsew", pady=(8, 0))
        # Give it row weight so it stretches
        try:
            self._cand_frame.master.rowconfigure(5, weight=1)
        except Exception:
            pass

        self._parsed_var.set(
            f"Audible returned {len(self._candidates)} candidate(s). "
            f"The duration-best match is pre-selected — pick a different row "
            f"if Audible got it wrong, then click Apply Changes."
        )

        # Build the rows
        self._cand_tree.delete(*self._cand_tree.get_children())
        known_dur_min = (self._known_dur_sec or 0) / 60.0 if self._known_dur_sec else 0
        pre_iid = None
        for i, c in enumerate(self._candidates):
            rt = c.get("runtime_min")
            if rt and known_dur_min:
                delta = abs(rt - known_dur_min)
                rt_str = f"{rt}m  (Δ{delta:.0f}m)"
            elif rt:
                rt_str = f"{rt}m"
            else:
                rt_str = "—"
            series_str = c.get("series_name", "") or ""
            if series_str and c.get("series_sequence"):
                series_str = f"{series_str} #{c['series_sequence']}"
            iid = f"cand-{i}"
            self._cand_tree.insert(
                "", "end", iid=iid,
                values=(
                    c.get("title", ""),
                    c.get("author", ""),
                    c.get("narrator", ""),
                    rt_str,
                    series_str,
                    c.get("asin", ""),
                ),
            )
            if preselected and c.get("asin") and c.get("asin") == preselected.get("asin"):
                pre_iid = iid

        if pre_iid:
            self._cand_tree.selection_set(pre_iid)
            self._cand_tree.focus(pre_iid)
            self._cand_tree.see(pre_iid)

        # Selection alone is enough to enable Apply (we already set _result)
        self._apply_btn.config(state="normal" if self._result else "disabled")

    def _on_candidate_pick(self, _event=None):
        """User clicked a row in the candidates list — set self._result."""
        sel = self._cand_tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0].split("-", 1)[1])
        except (ValueError, IndexError):
            return
        if 0 <= idx < len(self._candidates):
            self._result = self._candidates[idx]
            self._apply_btn.config(state="normal")
            # Show the picked record summary in the status label
            r = self._result
            self._parsed_var.set(
                f"Selected: {r.get('title','')} — {r.get('author','')}"
                + (f"  [{r.get('series_name')} #{r.get('series_sequence','')}]"
                   if r.get('series_name') else "")
                + (f"  | {r.get('runtime_min')}m" if r.get('runtime_min') else "")
                + (f"  | ASIN {r.get('asin')}" if r.get('asin') else "")
            )

    def _apply(self):
        if not self._result:
            return
        folder = self._book.get("parent_folder", "")
        existing_target = self._book.get("target_abs_path", "")
        output_root = database._infer_output_root(
            existing_target, self._book.get("best_author", ""))
        if not output_root and self._out_root:
            output_root = self._out_root

        new_target = database.build_target_path(
            output_root,
            self._result["author"],
            self._result["title"],
            self._result.get("series_name", ""),
            self._result.get("series_sequence", ""),
        )
        source       = self._result.get("source", self._source) or self._source
        runtime_min  = self._result.get("runtime_min")
        new_asin     = self._result.get("asin", "") or ""
        existing_asin = self._book.get("asin", "") or ""
        id_conflict  = bool(existing_asin and new_asin and existing_asin != new_asin)
        if id_conflict:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                f"ASIN conflict on single sync for "
                f"{self._book.get('parent_folder','')!r}: "
                f"existing={existing_asin!r}  new={new_asin!r} — flagged for review"
            )
        updates = {
            "best_author":     self._result["author"],
            "best_title":      self._result["title"],
            "series_name":     self._result.get("series_name", ""),
            "series_sequence": self._result.get("series_sequence", ""),
            "narrator":        self._result.get("narrator", ""),
            "isbn":            self._result.get("isbn", ""),
            "asin":            new_asin or existing_asin,
            "target_abs_path": new_target,
            "metadata_source": source,
            "manual_review":   1 if id_conflict else 0,
        }
        # Only overwrite Audible runtime when the result actually came from Audible
        if source == "audible":
            updates["audible_runtime_min"] = runtime_min if runtime_min else 0
        if source == "google":
            updates["google_title"]  = self._result["title"]
            updates["google_author"] = self._result["author"]
            updates["google_isbn"]   = self._result.get("isbn", "")
        database.update_book(folder, updates)
        self._applied = True
        self.destroy()


# ---------------------------------------------------------------------------
# Edit Book Dialog
# ---------------------------------------------------------------------------

class EditBookDialog(tk.Toplevel):
    """
    Modal popup to manually override metadata for a book group.
    Auto-recalculates the target path as fields change.
    """

    def __init__(self, parent, book_row: dict, output_root: str):
        super().__init__(parent)
        self.title("Edit Book")
        self.resizable(True, False)
        self.grab_set()           # modal
        self.transient(parent)

        self._output_root = output_root
        self._parent_folder = book_row["parent_folder"]
        self._saved = False

        # ── Field variables ────────────────────────────────────────────
        self._author   = tk.StringVar(value=book_row.get("best_author", ""))
        self._title    = tk.StringVar(value=book_row.get("best_title", ""))
        self._series   = tk.StringVar(value=book_row.get("series_name", ""))
        self._seq      = tk.StringVar(value=book_row.get("series_sequence", ""))
        self._narrator = tk.StringVar(value=book_row.get("narrator", ""))
        self._isbn     = tk.StringVar(value=book_row.get("isbn", ""))
        self._asin     = tk.StringVar(value=book_row.get("asin", ""))
        self._target   = tk.StringVar(value=book_row.get("target_abs_path", ""))

        # Auto-recalculate target when any field changes
        for var in (self._author, self._title, self._series, self._seq,
                    self._asin, self._isbn):
            var.trace_add("write", self._recalc_target)

        # ── Layout ─────────────────────────────────────────────────────
        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        # Source folder (read-only info)
        ttk.Label(frm, text="Source Folder:", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="e", **pad
        )
        ttk.Label(frm, text=str(Path(self._parent_folder).name),
                  foreground=SPINE["fg3"], font=("Segoe UI", 9)).grid(
            row=0, column=1, columnspan=2, sticky="w", **pad
        )

        # Author
        ttk.Label(frm, text="Author:").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self._author, width=52).grid(
            row=1, column=1, columnspan=2, sticky="ew", **pad
        )

        # Title
        ttk.Label(frm, text="Title:").grid(row=2, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self._title, width=52).grid(
            row=2, column=1, columnspan=2, sticky="ew", **pad
        )

        # Series name
        ttk.Label(frm, text="Series Name:").grid(row=3, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self._series, width=36).grid(
            row=3, column=1, sticky="ew", **pad
        )
        ttk.Label(frm, text="(leave blank if standalone)", foreground=SPINE["fg3"],
                  font=("Segoe UI", 8)).grid(row=3, column=2, sticky="w")

        # Series sequence
        ttk.Label(frm, text="Series Sequence:").grid(row=4, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self._seq, width=12).grid(
            row=4, column=1, sticky="w", **pad
        )

        # Narrator
        ttk.Label(frm, text="Narrator:").grid(row=5, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self._narrator, width=52).grid(
            row=5, column=1, columnspan=2, sticky="ew", **pad
        )

        # ISBN
        ttk.Label(frm, text="ISBN:").grid(row=6, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self._isbn, width=20).grid(
            row=6, column=1, sticky="w", **pad
        )

        # ASIN
        ttk.Label(frm, text="ASIN:").grid(row=7, column=0, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self._asin, width=20).grid(
            row=7, column=1, sticky="w", **pad
        )

        # Target path (calculated, editable)
        ttk.Label(frm, text="Target Path:").grid(row=8, column=0, sticky="e", **pad)
        target_entry = ttk.Entry(frm, textvariable=self._target, width=52,
                                  foreground=SPINE["info"])
        target_entry.grid(row=8, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Label(frm, text="(auto-generated — edit to override)",
                  foreground=SPINE["fg3"], font=("Segoe UI", 8)).grid(
            row=9, column=1, columnspan=2, sticky="w", padx=10
        )

        frm.columnconfigure(1, weight=1)

        # ── Buttons ────────────────────────────────────────────────────
        btn_frm = ttk.Frame(frm)
        btn_frm.grid(row=10, column=0, columnspan=3, pady=(8, 0))

        ttk.Button(btn_frm, text="Save", width=12, command=self._save).pack(
            side="left", padx=6
        )
        ttk.Button(btn_frm, text="Cancel", width=10, command=self.destroy).pack(
            side="left"
        )

        # Centre on parent
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - self.winfo_width() // 2}+{ph - self.winfo_height() // 2}")

    # ------------------------------------------------------------------

    def _recalc_target(self, *_):
        """Recalculate target path from current field values."""
        if not self._output_root:
            return
        try:
            new_path = database.build_target_path(
                self._output_root,
                self._author.get(),
                self._title.get(),
                self._series.get(),
                self._seq.get(),
                asin=self._asin.get(),
                isbn=self._isbn.get(),
            )
            self._target.set(new_path)
        except Exception:
            pass

    def _save(self):
        from datetime import datetime as _dt
        author   = self._author.get().strip()
        title    = self._title.get().strip()
        series   = self._series.get().strip()
        seq      = self._seq.get().strip()
        narrator = self._narrator.get().strip()
        isbn     = self._isbn.get().strip()
        asin     = self._asin.get().strip()
        target   = self._target.get().strip()
        updates = {
            # best_* — the live values used everywhere
            "best_author":      author,
            "best_title":       title,
            "series_name":      series,
            "series_sequence":  seq,
            "narrator":         narrator,
            "isbn":             isbn,
            "asin":             asin,
            "target_abs_path":  target,
            "metadata_source":  "user",
            # user_* — audit trail + lock flag
            "user_title":       title,
            "user_author":      author,
            "user_series":      series,
            "user_sequence":    seq,
            "user_narrator":    narrator,
            "user_isbn":        isbn,
            "user_asin":        asin,
            "user_edited":      1,
            "user_edited_at":   _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            database.update_book(self._parent_folder, updates)
            self._saved = True
            self.destroy()
        except Exception as e:
            messagebox.showerror("Save Failed", str(e), parent=self)


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Audiobook Librarian")
        self.geometry("1250x820")
        self.minsize(900, 600)
        self._queue: queue.Queue = queue.Queue()
        self._scanning = False
        self._output_root = ""   # set via the Book Review tab
        self._sync_stop = threading.Event()
        self._sync_current: int = 0
        self._sync_total:   int = 0
        self._sync_source:  str = ""
        # Sort state for each treeview
        self._tree_sort_col: str = ""
        self._tree_sort_rev: bool = False
        self._btree_sort_col: str = ""
        self._btree_sort_rev: bool = False
        # Chip filter state for the book treeview
        self._book_chip: str = "all"
        self._book_row_cache: list = []   # [(iid, values, tag), ...]
        self._chip_btns: dict = {}
        # Cached set of skipped folder paths (refreshed on _load_books)
        self._skipped_folders: set = set()

        database.init()
        self._settings = config.load_settings()
        self._build_ui()
        self._apply_spine_theme()
        self._apply_settings()
        self._refresh_db_label()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ======================================================================
    # Spine visual theme
    # ======================================================================

    def _apply_spine_theme(self) -> None:
        """
        Apply the Spine design-system palette and typography to every ttk
        widget type.  Called once after _build_ui() so the Tk root already
        exists (needed for font detection).
        """
        _init_fonts()
        sp   = SPINE
        sans = _FONT_SANS
        mono = _FONT_MONO

        style = ttk.Style(self)
        # "clam" gives us fine-grained control over all widget elements.
        style.theme_use("clam")

        # ── Base defaults ─────────────────────────────────────────────
        style.configure(".",
            background=sp["bg"],
            foreground=sp["fg"],
            font=(sans, 9),
            troughcolor=sp["surface3"],
            selectforeground=sp["fg"],
            selectbackground=sp["selected"],
            borderwidth=1,
            relief="flat",
        )

        # ── Frames ────────────────────────────────────────────────────
        style.configure("TFrame", background=sp["bg"])

        # ── LabelFrame ───────────────────────────────────────────────
        style.configure("TLabelframe",
            background=sp["bg"],
            bordercolor=sp["border_strong"],
            relief="groove",
        )
        style.configure("TLabelframe.Label",
            background=sp["bg"],
            foreground=sp["fg3"],
            font=(sans, 8, "bold"),
        )

        # ── Labels ────────────────────────────────────────────────────
        style.configure("TLabel",
            background=sp["bg"],
            foreground=sp["fg"],
        )

        # ── Buttons ───────────────────────────────────────────────────
        style.configure("TButton",
            background=sp["surface"],
            foreground=sp["fg2"],
            bordercolor=sp["border"],
            darkcolor=sp["border"],
            lightcolor=sp["border"],
            relief="flat",
            padding=(8, 3),
            font=(sans, 9),
            anchor="center",
        )
        style.map("TButton",
            background=[
                ("active",   sp["hover"]),
                ("pressed",  sp["surface3"]),
                ("disabled", sp["surface3"]),
            ],
            foreground=[
                ("active",   sp["fg"]),
                ("disabled", sp["fg4"]),
            ],
            bordercolor=[
                ("active", sp["border_strong"]),
                ("focus",  sp["accent"]),
            ],
        )

        # ── Entry ─────────────────────────────────────────────────────
        style.configure("TEntry",
            fieldbackground=sp["surface"],
            foreground=sp["fg"],
            insertcolor=sp["fg"],
            bordercolor=sp["border"],
            lightcolor=sp["border"],
            darkcolor=sp["border"],
            selectbackground=sp["selected"],
            selectforeground=sp["fg"],
        )
        style.map("TEntry",
            bordercolor=[("focus", sp["accent"])],
            lightcolor=[("focus", sp["accent_soft"])],
            fieldbackground=[("readonly", sp["surface2"])],
        )

        # ── Combobox ──────────────────────────────────────────────────
        style.configure("TCombobox",
            fieldbackground=sp["surface"],
            foreground=sp["fg"],
            selectbackground=sp["selected"],
            selectforeground=sp["fg"],
            arrowcolor=sp["fg3"],
            bordercolor=sp["border"],
        )

        # ── Notebook (tabs) ───────────────────────────────────────────
        style.configure("TNotebook",
            background=sp["bg"],
            bordercolor=sp["border"],
            tabmargins=(2, 4, 2, 0),
        )
        style.configure("TNotebook.Tab",
            background=sp["surface3"],
            foreground=sp["fg3"],
            padding=(10, 5),
            font=(sans, 9),
            bordercolor=sp["border"],
        )
        style.map("TNotebook.Tab",
            background=[
                ("selected", sp["surface"]),
                ("active",   sp["surface2"]),
            ],
            foreground=[
                ("selected", sp["fg"]),
                ("active",   sp["fg2"]),
            ],
        )

        # ── Scrollbar ─────────────────────────────────────────────────
        style.configure("TScrollbar",
            background=sp["surface3"],
            troughcolor=sp["surface2"],
            bordercolor=sp["border"],
            arrowcolor=sp["fg4"],
            arrowsize=12,
            relief="flat",
        )
        style.map("TScrollbar",
            background=[("active", sp["fg4"]), ("pressed", sp["fg3"])],
            arrowcolor=[("active", sp["fg3"])],
        )

        # ── Progressbar ───────────────────────────────────────────────
        style.configure("TProgressbar",
            background=sp["accent"],
            troughcolor=sp["surface3"],
            bordercolor=sp["border"],
            darkcolor=sp["accent"],
            lightcolor=sp["accent"],
            thickness=6,
        )

        # ── PanedWindow sash ──────────────────────────────────────────
        style.configure("TPanedwindow", background=sp["border"])
        style.configure("Sash",
            sashthickness=5,
            sashpad=1,
            relief="flat",
            background=sp["border"],
        )

        # ── Checkbutton ───────────────────────────────────────────────
        style.configure("TCheckbutton",
            background=sp["bg"],
            foreground=sp["fg2"],
            indicatorcolor=sp["surface"],
            indicatorrelief="flat",
            focuscolor=sp["accent_soft"],
        )
        style.map("TCheckbutton",
            background=[("active", sp["bg"])],
            foreground=[("active", sp["fg"])],
            indicatorcolor=[
                ("selected",         sp["accent"]),
                ("selected active",  sp["accent_strong"]),
                ("active",           sp["surface2"]),
            ],
        )

        # ── Treeview ──────────────────────────────────────────────────
        style.configure("Treeview",
            background=sp["surface"],
            foreground=sp["fg"],
            fieldbackground=sp["surface"],
            rowheight=24,
            borderwidth=0,
            font=(sans, 9),
        )
        style.configure("Treeview.Heading",
            background=sp["bg"],
            foreground=sp["fg3"],
            font=(sans, 8),
            borderwidth=0,
            relief="flat",
            padding=(4, 4),
        )
        style.map("Treeview",
            background=[("selected", sp["selected"])],
            foreground=[("selected", sp["fg"])],
        )
        style.map("Treeview.Heading",
            background=[("active", sp["surface3"])],
            relief=[("active", "flat")],
        )

        # ── Main window background ────────────────────────────────────
        self.configure(bg=sp["bg"])

    # ======================================================================
    # Top-level layout
    # ======================================================================

    def _build_ui(self):
        self.configure(bg=SPINE["bg"])

        # ── Menu bar ──────────────────────────────────────────────────
        menubar = tk.Menu(self)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="User Guide…",  command=self._open_help)
        help_menu.add_command(label="About…",       command=self._open_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        try:
            self.config(menu=menubar)
        except Exception:
            pass

        # ── Shared toolbar (db label lives here) ──────────────────────
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=12, pady=(6, 0))
        self.db_label = ttk.Label(toolbar, text="", foreground=SPINE["fg3"],
                                  font=("Segoe UI", 8))
        self.db_label.pack(side="right")

        # ── Notebook ──────────────────────────────────────────────────
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=6)

        # Tab 1 — File Scanner
        scanner_tab = ttk.Frame(nb)
        nb.add(scanner_tab, text="  File Scanner  ")
        self._build_scanner_tab(scanner_tab)

        # Tab 2 — Book Review
        review_tab = ttk.Frame(nb)
        nb.add(review_tab, text="  Book Review  ")
        self._build_review_tab(review_tab)

        # Hover tooltips on the tab labels themselves
        self._setup_tab_tooltips(nb, {
            0: "Set source/output paths, run scans, manage Ignored Folders, "
               "and view all individual files the scanner found.",
            1: "Work with books — one row per audiobook. Sync metadata, "
               "edit fields, then click Organize Library to physically "
               "transfer files to their target paths.",
            2: "Real-time log of every action the app takes — useful "
               "when something doesn't look right.",
            3: "Manage API keys and endpoints for Gemini, Groq, Claude, "
               "ChatGPT, Hardcover, and the Local LLM (LM Studio / Ollama).",
        })

        # Tab 3 — Activity Log
        log_tab = ttk.Frame(nb)
        nb.add(log_tab, text="  Activity Log  ")
        self._build_log_tab(log_tab)

        # Tab 4 — API Keys
        apikeys_tab = ttk.Frame(nb)
        nb.add(apikeys_tab, text="  API Keys  ")
        self._build_api_keys_tab(apikeys_tab)

        self.nb = nb
        self.nb.bind("<<NotebookTabChanged>>",
                     lambda _: config.save_settings("active_tab", self.nb.index("current")))

    # ======================================================================
    # Tab 3 — Activity Log
    # ======================================================================

    def _build_log_tab(self, parent):
        """
        A live-scrolling terminal that mirrors the Python logger output.
        Keeps up to 20,000 lines.  Auto-scrolls unless the user has
        manually scrolled up.
        """
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", padx=8, pady=(6, 0))

        ttk.Label(toolbar, text="Activity Log",
                  font=("Segoe UI", 10, "bold")).pack(side="left")

        self._log_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="Auto-scroll",
                        variable=self._log_autoscroll).pack(side="left", padx=(16, 0))

        # Level filter
        ttk.Label(toolbar, text="  Level:").pack(side="left")
        self._log_level_var = tk.StringVar(value="INFO")
        lvl_combo = ttk.Combobox(toolbar, textvariable=self._log_level_var,
                                  values=["DEBUG", "INFO", "WARNING", "ERROR"],
                                  width=9, state="readonly")
        lvl_combo.pack(side="left", padx=(4, 0))
        lvl_combo.bind("<<ComboboxSelected>>", self._apply_log_level)
        lvl_combo.bind("<<ComboboxSelected>>",
                       lambda _: config.save_settings("log_level", self._log_level_var.get()),
                       add="+")

        ttk.Button(toolbar, text="Clear", width=8,
                   command=self._clear_log).pack(side="right")
        ttk.Button(toolbar, text="Copy All", width=9,
                   command=self._copy_log).pack(side="right", padx=(0, 4))

        self._log_line_var = tk.StringVar(value="0 lines")
        ttk.Label(toolbar, textvariable=self._log_line_var,
                  foreground=SPINE["fg3"], font=("Segoe UI", 8)).pack(side="right", padx=(0, 8))

        # ── Text widget + scrollbars ───────────────────────────────────
        txt_frame = ttk.Frame(parent)
        txt_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        vsb = ttk.Scrollbar(txt_frame, orient="vertical")
        vsb.pack(side="right", fill="y")
        hsb = ttk.Scrollbar(txt_frame, orient="horizontal")
        hsb.pack(side="bottom", fill="x")

        self.log_text = tk.Text(
            txt_frame,
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="#d4d4d4",
            wrap="none",
            state="disabled",
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
            relief="flat",
        )
        self.log_text.pack(fill="both", expand=True)
        vsb.config(command=self.log_text.yview)
        hsb.config(command=self.log_text.xview)

        # Colour tags
        self.log_text.tag_config("DEBUG",    foreground="#6a9955")
        self.log_text.tag_config("INFO",     foreground="#d4d4d4")
        self.log_text.tag_config("WARNING",  foreground="#dcdcaa")
        self.log_text.tag_config("ERROR",    foreground="#f44747")
        self.log_text.tag_config("CRITICAL", foreground="#ff0000",
                                             font=("Consolas", 9, "bold"))

        # Attach the GUI log handler to the root logger
        self._gui_log_handler = _GuiLogHandler(self._append_log_line)
        self._gui_log_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self._gui_log_handler)

    def _apply_log_level(self, _event=None):
        level = getattr(logging, self._log_level_var.get(), logging.INFO)
        if hasattr(self, "_gui_log_handler"):
            self._gui_log_handler.setLevel(level)

    def _append_log_line(self, level_name: str, text: str):
        """Called from any thread via after() — appends one log line."""
        widget = self.log_text
        widget.config(state="normal")

        # Trim to 20,000 lines max
        line_count = int(widget.index("end-1c").split(".")[0])
        if line_count > 20_000:
            widget.delete("1.0", f"{line_count - 20_000}.0")

        # Check if we were at the bottom *before* inserting
        at_bottom = widget.yview()[1] >= 0.98

        widget.insert("end", text + "\n", level_name)
        widget.config(state="disabled")

        # Update line counter
        line_count = int(widget.index("end-1c").split(".")[0])
        self._log_line_var.set(f"{line_count:,} lines")

        # Auto-scroll only if we were already at the bottom or the option is on
        if self._log_autoscroll.get() and at_bottom:
            widget.see("end")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self._log_line_var.set("0 lines")

    def _copy_log(self):
        self.log_text.config(state="normal")
        content = self.log_text.get("1.0", "end")
        self.log_text.config(state="disabled")
        self.clipboard_clear()
        self.clipboard_append(content)

    # ======================================================================
    # Tab 4 — API Keys
    # ======================================================================

    def _build_api_keys_tab(self, parent):
        canvas = tk.Canvas(parent, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        # ── Gemini API key row ─────────────────────────────────────────
        gemini_outer = ttk.LabelFrame(inner, text=" Gemini API Key ", padding=8)
        gemini_outer.pack(fill="x", padx=8, pady=(10, 0))
        _tip(gemini_outer,
             "API key for Google Gemini 2.5 Flash. Free tier: 15 req/min, "
             "1500 req/day. Get yours at aistudio.google.com.")

        self.gemini_key_var = tk.StringVar()
        self._gemini_input_frame = ttk.Frame(gemini_outer)
        ttk.Entry(
            self._gemini_input_frame,
            textvariable=self.gemini_key_var,
            show="*",
            font=("Segoe UI", 10),
            width=52,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(
            self._gemini_input_frame,
            text="Save Key",
            width=10,
            command=self._save_gemini_key,
        ).pack(side="left")
        ttk.Label(
            self._gemini_input_frame,
            text="Free tier: 15 req/min — aistudio.google.com",
            foreground=SPINE["fg3"],
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(10, 0))

        self._gemini_saved_frame = ttk.Frame(gemini_outer)
        ttk.Label(
            self._gemini_saved_frame,
            text="Key saved",
            foreground=SPINE["ok"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        ttk.Button(
            self._gemini_saved_frame,
            text="Change",
            width=8,
            command=lambda: self._show_key_input("gemini"),
        ).pack(side="left", padx=(10, 0))

        # ── Groq API key row ───────────────────────────────────────────
        groq_outer = ttk.LabelFrame(inner, text=" Groq API Key ", padding=8)
        groq_outer.pack(fill="x", padx=8, pady=(6, 0))
        _tip(groq_outer,
             "API key for Groq's LLaMA-3.3-70B endpoint. Free tier. "
             "Get yours at console.groq.com.")

        self.groq_key_var = tk.StringVar()
        self._groq_input_frame = ttk.Frame(groq_outer)
        ttk.Entry(
            self._groq_input_frame,
            textvariable=self.groq_key_var,
            show="*",
            font=("Segoe UI", 10),
            width=52,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(
            self._groq_input_frame,
            text="Save Key",
            width=10,
            command=self._save_groq_key,
        ).pack(side="left")
        ttk.Label(
            self._groq_input_frame,
            text="Free tier — console.groq.com",
            foreground=SPINE["fg3"],
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(10, 0))

        self._groq_saved_frame = ttk.Frame(groq_outer)
        ttk.Label(
            self._groq_saved_frame,
            text="Key saved",
            foreground=SPINE["ok"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        ttk.Button(
            self._groq_saved_frame,
            text="Change",
            width=8,
            command=lambda: self._show_key_input("groq"),
        ).pack(side="left", padx=(10, 0))

        # ── Claude API key row ─────────────────────────────────────────
        claude_outer = ttk.LabelFrame(inner, text=" Anthropic Claude API Key ", padding=8)
        _tip(claude_outer,
             "API key for Anthropic Claude (claude-3-5-haiku). Paid usage. "
             "Get yours at console.anthropic.com.")
        claude_outer.pack(fill="x", padx=8, pady=(6, 0))

        self.claude_key_var = tk.StringVar()
        self._claude_input_frame = ttk.Frame(claude_outer)
        ttk.Entry(
            self._claude_input_frame,
            textvariable=self.claude_key_var,
            show="*",
            font=("Segoe UI", 10),
            width=52,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(
            self._claude_input_frame,
            text="Save Key",
            width=10,
            command=self._save_claude_key,
        ).pack(side="left")
        ttk.Label(
            self._claude_input_frame,
            text="console.anthropic.com  |  model: claude-3-5-haiku",
            foreground=SPINE["fg3"],
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(10, 0))

        self._claude_saved_frame = ttk.Frame(claude_outer)
        ttk.Label(
            self._claude_saved_frame,
            text="Key saved",
            foreground=SPINE["ok"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        ttk.Button(
            self._claude_saved_frame,
            text="Change",
            width=8,
            command=lambda: self._show_key_input("claude"),
        ).pack(side="left", padx=(10, 0))

        # ── ChatGPT API key row ────────────────────────────────────────
        chatgpt_outer = ttk.LabelFrame(inner, text=" OpenAI ChatGPT API Key ", padding=8)
        _tip(chatgpt_outer,
             "API key for OpenAI GPT-4o-mini. Paid usage (very cheap "
             "per book). Get yours at platform.openai.com.")
        chatgpt_outer.pack(fill="x", padx=8, pady=(6, 0))

        self.chatgpt_key_var = tk.StringVar()
        self._chatgpt_input_frame = ttk.Frame(chatgpt_outer)
        ttk.Entry(
            self._chatgpt_input_frame,
            textvariable=self.chatgpt_key_var,
            show="*",
            font=("Segoe UI", 10),
            width=52,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(
            self._chatgpt_input_frame,
            text="Save Key",
            width=10,
            command=self._save_chatgpt_key,
        ).pack(side="left")
        ttk.Label(
            self._chatgpt_input_frame,
            text="platform.openai.com/api-keys  |  model: gpt-4o-mini",
            foreground=SPINE["fg3"],
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(10, 0))

        self._chatgpt_saved_frame = ttk.Frame(chatgpt_outer)
        ttk.Label(
            self._chatgpt_saved_frame,
            text="Key saved",
            foreground=SPINE["ok"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        ttk.Button(
            self._chatgpt_saved_frame,
            text="Change",
            width=8,
            command=lambda: self._show_key_input("chatgpt"),
        ).pack(side="left", padx=(10, 0))

        # ── Hardcover API key row ──────────────────────────────────────
        hardcover_outer = ttk.LabelFrame(inner, text=" Hardcover API Key ", padding=8)
        _tip(hardcover_outer,
             "API key for the Hardcover.app GraphQL API. Get yours by "
             "logging in at hardcover.app → Settings → API Access.")
        hardcover_outer.pack(fill="x", padx=8, pady=(6, 0))

        self.hardcover_key_var = tk.StringVar()
        self._hardcover_input_frame = ttk.Frame(hardcover_outer)
        ttk.Entry(
            self._hardcover_input_frame,
            textvariable=self.hardcover_key_var,
            show="*",
            font=("Segoe UI", 10),
            width=52,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(
            self._hardcover_input_frame,
            text="Save Key",
            width=10,
            command=self._save_hardcover_key,
        ).pack(side="left")
        ttk.Label(
            self._hardcover_input_frame,
            text="Free key at hardcover.app/account/api",
            foreground=SPINE["fg3"],
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(10, 0))

        self._hardcover_saved_frame = ttk.Frame(hardcover_outer)
        ttk.Label(
            self._hardcover_saved_frame,
            text="Key saved",
            foreground=SPINE["ok"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        ttk.Button(
            self._hardcover_saved_frame,
            text="Change",
            width=8,
            command=lambda: self._show_key_input("hardcover"),
        ).pack(side="left", padx=(10, 0))

        # ── Local LLM settings row ────────────────────────────────────
        lm_outer = ttk.LabelFrame(inner, text=" Local LLM (LM Studio / Ollama) ", padding=8)
        _tip(lm_outer,
             "Endpoint URL and model name for a locally-running LLM "
             "server. LM Studio: usually http://127.0.0.1:1234. Ollama: "
             "http://127.0.0.1:11434. Model name is the loaded model "
             "(LM Studio ignores it; Ollama uses it).")
        lm_outer.pack(fill="x", padx=8, pady=(6, 0))

        ttk.Label(lm_outer, text="Local Server URL:").pack(side="left")
        self.lm_url_var = tk.StringVar()
        lm_url_entry = ttk.Entry(lm_outer, textvariable=self.lm_url_var,
                                  font=("Segoe UI", 10), width=38)
        lm_url_entry.pack(side="left", padx=(4, 12))
        lm_url_entry.bind("<FocusOut>", lambda e: config.save_settings(
            "lm_studio_url", self.lm_url_var.get().strip()
        ))

        ttk.Label(
            lm_outer,
            text="No API key needed — runs locally at hardware speed  |  "
                 "LM Studio: use model 'local-model'  |  Ollama: use /v1 endpoint",
            foreground=SPINE["fg3"], font=("Segoe UI", 8),
        ).pack(side="left")

    # ======================================================================
    # Tab 1 — File Scanner
    # ======================================================================

    def _build_scanner_tab(self, parent):
        # ── Directory row ──────────────────────────────────────────────
        dir_frame = ttk.LabelFrame(parent, text=" Source Directory ", padding=8)
        dir_frame.pack(fill="x", padx=8, pady=(10, 0))

        self.dir_var = tk.StringVar()
        dir_entry = ttk.Entry(dir_frame, textvariable=self.dir_var, font=("Segoe UI", 10))
        dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        dir_entry.bind("<FocusOut>", lambda e: config.save_settings(
            "source_dir", self.dir_var.get().strip()
        ))
        _tip(dir_entry,
             "The messy audiobook folder you want to clean up. "
             "Network paths supported (\\\\server\\share). Saved automatically.")
        _src_browse_btn = ttk.Button(dir_frame, text="Browse…", command=self._browse)
        _src_browse_btn.pack(side="left")
        _tip(_src_browse_btn, "Pick the Source Directory with a folder browser.")
        ttk.Label(
            dir_frame,
            text="Network paths: \\\\server\\share",
            foreground=SPINE["fg3"],
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(10, 0))

        # ── Output root row ────────────────────────────────────────────
        out_frame = ttk.LabelFrame(parent, text=" Output Root (Audiobookshelf library path) ",
                                   padding=8)
        out_frame.pack(fill="x", padx=8, pady=(6, 0))

        self.out_var = tk.StringVar(value=r"C:\AudiobookshelfOutput")
        out_entry = ttk.Entry(out_frame, textvariable=self.out_var, font=("Segoe UI", 10))
        out_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        out_entry.bind("<FocusOut>", lambda e: config.save_settings(
            "output_root", self.out_var.get().strip()
        ))
        _tip(out_entry,
             "Where the cleaned library will go. Typically your "
             "Audiobookshelf library root. Saved automatically.")
        _out_browse_btn = ttk.Button(out_frame, text="Browse…", command=self._browse_output)
        _out_browse_btn.pack(side="left")
        _tip(_out_browse_btn, "Pick the Output Root with a folder browser.")

        # ── Ignored Folders row ────────────────────────────────────────
        ign_frame = ttk.LabelFrame(
            parent,
            text=" Ignored Folders  (any path listed below — and anything inside it — "
                 "is skipped by Scan, Books, and Sync) ",
            padding=8,
        )
        ign_frame.pack(fill="x", padx=8, pady=(6, 0))

        # Top row: entry + browse + add
        ign_top = ttk.Frame(ign_frame)
        ign_top.pack(fill="x")

        self.ign_entry_var = tk.StringVar()
        ign_entry = ttk.Entry(ign_top, textvariable=self.ign_entry_var,
                              font=("Segoe UI", 10))
        ign_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ign_entry.bind("<Return>", lambda e: self._ignored_add())
        _tip(ign_entry,
             "Type or browse a folder to ignore. Anything under that path "
             "is skipped by Scan and LLM sync, and existing rows are purged.")
        _ign_browse_btn = ttk.Button(ign_top, text="Browse…",
                                     command=self._ignored_browse)
        _ign_browse_btn.pack(side="left", padx=(0, 6))
        _tip(_ign_browse_btn, "Pick a folder to ignore.")
        _ign_add_btn = ttk.Button(ign_top, text="+ Add",
                                  command=self._ignored_add)
        _ign_add_btn.pack(side="left")
        _tip(_ign_add_btn,
             "Add the path to the skip list. Existing rows under this "
             "folder are purged from the database immediately so the "
             "Books tab updates on the spot.")

        # Listbox + remove button
        ign_body = ttk.Frame(ign_frame)
        ign_body.pack(fill="x", pady=(6, 0))

        lst_frame = ttk.Frame(ign_body)
        lst_frame.pack(side="left", fill="x", expand=True)
        vsb = ttk.Scrollbar(lst_frame, orient="vertical")
        vsb.pack(side="right", fill="y")
        self.ign_listbox = tk.Listbox(
            lst_frame, height=4, font=("Consolas", 9),
            selectmode="extended",
            bg=SPINE["surface2"], fg=SPINE["fg"],
            selectbackground=SPINE["selected"], selectforeground=SPINE["fg"],
            borderwidth=1, relief="solid",
            yscrollcommand=vsb.set,
        )
        self.ign_listbox.pack(side="left", fill="x", expand=True)
        vsb.config(command=self.ign_listbox.yview)

        ign_btns = ttk.Frame(ign_body)
        ign_btns.pack(side="left", padx=(6, 0), fill="y")
        _tip(self.ign_listbox,
             "Currently ignored folders. Select rows and click Remove "
             "Selected to un-ignore. Folders will reappear on next Scan.")
        _ign_rm_btn = ttk.Button(ign_btns, text="Remove Selected", width=18,
                                 command=self._ignored_remove)
        _ign_rm_btn.pack(side="top")
        _tip(_ign_rm_btn,
             "Un-ignore the selected folder(s). Disk is untouched; "
             "files will be re-scanned next time you run Scan.")
        self.ign_count_var = tk.StringVar(value="0 ignored")
        ttk.Label(ign_btns, textvariable=self.ign_count_var,
                  foreground=SPINE["fg3"], font=("Segoe UI", 8)).pack(
            side="top", pady=(6, 0))

        # Initial load from DB
        self._ignored_refresh()

        # ── Action buttons ─────────────────────────────────────────────
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill="x", padx=8, pady=6)

        self.scan_btn = ttk.Button(
            btn_frame, text="▶  Scan Directory", width=22, command=self._toggle_scan
        )
        self.scan_btn.pack(side="left")
        _tip(self.scan_btn,
             "Walk the Source Directory (and Output Root if set) recursively. "
             "Reads every audio / ebook / image file, extracts metadata tags, "
             "and writes the result to the database. Click again while running "
             "to stop. Safe — no files are moved or modified.")

        self.dup_btn = ttk.Button(
            btn_frame,
            text="Flag Duplicates",
            width=18,
            command=self._flag_duplicates,
            state="disabled",
        )
        self.dup_btn.pack(side="left", padx=6)
        _tip(self.dup_btn,
             "Re-evaluate duplicate flags using a quality score "
             "(FLAC > M4B > MP3, weighted by bitrate). Cross-folder aware. "
             "Higher-quality copies are kept; lower-quality ones are flagged.")

        self.export_files_btn = ttk.Button(
            btn_frame,
            text="Export Files CSV",
            width=16,
            command=self._export_files_csv,
            state="disabled",
        )
        self.export_files_btn.pack(side="left")
        _tip(self.export_files_btn,
             "Dump every row of the files table to a CSV file. "
             "Useful for spreadsheet review or external scripting.")

        ttk.Separator(btn_frame, orient="vertical").pack(side="left", fill="y", padx=10)

        self.del_empty_btn = ttk.Button(
            btn_frame,
            text="Delete Empty Folders",
            width=22,
            command=self._delete_empty_folders,
        )
        self.del_empty_btn.pack(side="left")
        _tip(self.del_empty_btn,
             "Permanently remove empty folders from the Source Directory "
             "(up to 5 passes — catches parents emptied by children). "
             "Then prompts you to review small folders (under a size "
             "threshold you pick), letting you Keep or Delete each one.")

        self.del_hash_dupes_btn = ttk.Button(
            btn_frame,
            text="Delete Hash Duplicates",
            width=22,
            command=self._delete_hash_dupes,
            state="disabled",
        )
        self.del_hash_dupes_btn.pack(side="left", padx=(6, 0))
        _tip(self.del_hash_dupes_btn,
             "Permanently delete every audio file flagged as a byte-"
             "identical lower-quality duplicate of another file in the "
             "library. The higher-quality keeper is preserved. "
             "Cannot be undone — confirms before deletion.")

        # ── Progress ───────────────────────────────────────────────────
        prog_frame = ttk.Frame(parent)
        prog_frame.pack(fill="x", padx=8, pady=(0, 4))

        self.progress = ttk.Progressbar(prog_frame, mode="indeterminate", length=200)
        self.progress.pack(fill="x")

        self.status_var = tk.StringVar(value="Select a directory and click Scan.")
        ttk.Label(prog_frame, textvariable=self.status_var, anchor="w",
                  font=("Segoe UI", 9)).pack(fill="x", pady=(2, 0))

        # ── Stats boxes ────────────────────────────────────────────────
        stats_outer = ttk.Frame(parent)
        stats_outer.pack(fill="x", padx=8, pady=(0, 6))

        self._stat_vars: dict[str, tk.StringVar] = {}
        stat_defs = [
            ("Total",       "total",      SPINE["fg"]),
            ("Audio",       "audio",      SPINE["ok"]),
            ("Ebook",       "ebook",      SPINE["info"]),
            ("Image",       "image",      SPINE["warn"]),
            ("Other",       "other",      SPINE["fg3"]),
            ("Duplicates",  "duplicates", SPINE["bad"]),
            ("High Conf",   "conf_high",  SPINE["ok"]),
            ("Mid Conf",    "conf_mid",   SPINE["warn"]),
            ("Low Conf",    "conf_low",   SPINE["bad"]),
        ]

        for label, key, color in stat_defs:
            box = tk.Frame(stats_outer, relief="flat", bd=1, bg=SPINE["surface"], padx=8, pady=4)
            box.pack(side="left", padx=(0, 4))
            tk.Label(box, text=label, font=("Segoe UI", 7), fg=SPINE["fg3"], bg=SPINE["surface"]).pack()
            var = tk.StringVar(value="—")
            tk.Label(box, textvariable=var, font=("Segoe UI", 14, "bold"),
                     fg=color, bg=SPINE["surface"]).pack()
            self._stat_vars[key] = var

        # ── Filter row ─────────────────────────────────────────────────
        filt_frame = ttk.Frame(parent)
        filt_frame.pack(fill="x", padx=8, pady=(0, 4))

        ttk.Label(filt_frame, text="Filter:").pack(side="left")

        self.filt_type = tk.StringVar(value="All")
        ttk.Combobox(
            filt_frame,
            textvariable=self.filt_type,
            values=["All", "audio", "ebook", "image", "other"],
            state="readonly",
            width=10,
        ).pack(side="left", padx=(4, 12))

        self.filt_conf = tk.StringVar(value="All confidence")
        ttk.Combobox(
            filt_frame,
            textvariable=self.filt_conf,
            values=["All confidence", "Low (≤ 50)", "Mid (≤ 80)", "High (100)"],
            state="readonly",
            width=16,
        ).pack(side="left", padx=(0, 12))

        self.filt_dupes = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filt_frame, text="Duplicates only", variable=self.filt_dupes
        ).pack(side="left")

        ttk.Button(filt_frame, text="Apply", command=self._apply_filter).pack(
            side="left", padx=8
        )

        self.row_count_var = tk.StringVar(value="")
        ttk.Label(filt_frame, textvariable=self.row_count_var, foreground=SPINE["fg3"],
                  font=("Segoe UI", 8)).pack(side="right")

        # ── Results treeview ───────────────────────────────────────────
        tree_frame = ttk.LabelFrame(
            parent,
            text=" Results  (showing up to 2,000 rows — full data in DB / CSV) ",
            padding=2,
        )
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        cols = ("author", "title", "type", "conf", "dup", "ext", "duration", "size", "path")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                  selectmode="extended")

        self._file_col_heads = {
            "author":   "Author",
            "title":    "Title",
            "type":     "Type",
            "conf":     "Conf",
            "dup":      "Dup",
            "ext":      "Ext",
            "duration": "Duration",
            "size":     "Size",
            "path":     "Full Path",
        }
        widths = {
            "author": 180, "title": 240, "type": 65, "conf": 50,
            "dup": 40, "ext": 55, "duration": 75, "size": 75, "path": 400,
        }
        minwidths = {
            "author": 60, "title": 80, "type": 40, "conf": 40,
            "dup": 30, "ext": 30, "duration": 55, "size": 55, "path": 100,
        }

        for col in cols:
            self.tree.heading(col, text=self._file_col_heads[col],
                              command=lambda c=col: self._sort_tree(c))
            self.tree.column(col, width=widths[col], minwidth=minwidths[col],
                             stretch=(col == "path"))

        _tree_heading_tip(self.tree, {
            "author":   "Extracted author from file metadata tags (ID3 etc.).",
            "title":    "Extracted title from file metadata tags.",
            "type":     "File classification: audio / ebook / image / other.",
            "conf":     "Confidence score (0-100) for the extracted metadata.",
            "dup":      "'DUP' if this file is flagged as a duplicate "
                        "(lower-quality copy of a keeper).",
            "ext":      "File extension.",
            "duration": "Audio duration (H:MM:SS) if applicable.",
            "size":     "File size on disk.",
            "path":     "Full absolute path to the file on disk.",
        })

        self.tree.tag_configure("dup", foreground=SPINE["info"])
        self.tree.tag_configure("low", foreground=SPINE["warn"])

        self.tree.bind("<Delete>", self._delete_selected_files)
        self.tree.bind("<Button-3>", self._show_context_menu_files)
        self.tree.bind("<Button-1>", self._toggle_deselect_files)
        self._setup_column_resize(self.tree)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

    # ======================================================================
    # Tab 2 — Book Review
    # ======================================================================

    def _build_sync_detail_panel(self, parent) -> None:
        """
        Persistent scrollable log panel that accumulates one entry per book
        during any bulk sync.  Each entry shows the context sent and the
        parsed result, colour-coded by outcome.
        """
        self._sync_detail_frame = ttk.LabelFrame(
            parent, text=" Sync Log ", padding=4)
        # Layout is handled by the caller (PanedWindow.add) — do not pack here.

        # ── toolbar row ──────────────────────────────────────────────────
        toolbar = ttk.Frame(self._sync_detail_frame)
        toolbar.pack(fill="x", pady=(0, 4))

        self._sync_detail_hdr = tk.StringVar(value="No sync running.")
        ttk.Label(toolbar, textvariable=self._sync_detail_hdr,
                  font=("Segoe UI", 9, "bold")).pack(side="left")

        ttk.Button(
            toolbar, text="Clear log",
            command=self._clear_sync_log, width=9,
        ).pack(side="right")

        # ── scrollable log ───────────────────────────────────────────────
        log_frm = ttk.Frame(self._sync_detail_frame)
        log_frm.pack(fill="both", expand=True)

        vsb = ttk.Scrollbar(log_frm, orient="vertical")
        vsb.pack(side="right", fill="y")
        hsb = ttk.Scrollbar(log_frm, orient="horizontal")
        hsb.pack(side="bottom", fill="x")

        self._sync_log = tk.Text(
            log_frm,
            wrap="none",
            font=("Consolas", 8),
            state="disabled",
            relief="flat",
            bg=SPINE["surface2"],
            fg=SPINE["fg"],
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
        )
        self._sync_log.pack(fill="both", expand=True)
        vsb.config(command=self._sync_log.yview)
        hsb.config(command=self._sync_log.xview)

        # ── colour tags ──────────────────────────────────────────────────
        self._sync_log.tag_configure(
            "hdr_confirmed", font=("Consolas", 8, "bold"), foreground="#4caf50")
        self._sync_log.tag_configure(
            "hdr_diverged",  font=("Consolas", 8, "bold"), foreground="#ff9800")
        self._sync_log.tag_configure(
            "hdr_skipped",   font=("Consolas", 8, "bold"), foreground="#9e9e9e")
        self._sync_log.tag_configure(
            "hdr_locked",    font=("Consolas", 8, "bold"), foreground="#ce93d8")
        self._sync_log.tag_configure(
            "sent",          foreground="#78909c")
        self._sync_log.tag_configure(
            "result",        foreground="#90caf9")
        self._sync_log.tag_configure(
            "divider",       foreground="#424242")

    def _clear_sync_log(self) -> None:
        """Erase all entries from the sync log."""
        self._sync_log.config(state="normal")
        self._sync_log.delete("1.0", "end")
        self._sync_log.config(state="disabled")
        self._sync_detail_hdr.set("Log cleared.")

    def _update_sync_detail(self, sent: str, result,
                             folder_name: str, outcome: str) -> None:
        """Append one book's entry to the scrollable sync log."""
        n      = self._sync_current
        total  = self._sync_total
        source = self._sync_source.title()

        # ── outcome label ────────────────────────────────────────────────
        outcome_labels = {
            "confirmed": "✓ CONFIRMED",
            "diverged":  "≠ DIVERGED ",
            "skipped":   "– SKIPPED  ",
            "locked":    "# LOCKED   ",
        }
        hdr_tag     = f"hdr_{outcome}"
        outcome_lbl = outcome_labels.get(outcome, outcome.upper())
        hdr_line    = f"[{n:>4}/{total}]  {outcome_lbl}  {source}  {folder_name}\n"

        # ── sent: collapse multi-line context to one readable line ───────
        sent_parts = [ln.strip() for ln in (sent or "").splitlines() if ln.strip()]
        sent_line  = "         " + "  ·  ".join(sent_parts) + "\n"

        # ── result line ──────────────────────────────────────────────────
        if result:
            parts = []
            for k in ("title", "author", "series_name", "series_sequence", "narrator"):
                v = (result.get(k) or "").strip()
                if v:
                    parts.append(v)
            result_line = "      →  " + "  ·  ".join(parts) + "\n"
        else:
            result_line = "      →  no match\n"

        divider = "─" * 76 + "\n"

        # ── append to log ────────────────────────────────────────────────
        log = self._sync_log
        log.config(state="normal")
        log.insert("end", hdr_line,    hdr_tag)
        log.insert("end", sent_line,   "sent")
        log.insert("end", result_line, "result")
        log.insert("end", divider,     "divider")
        log.see("end")
        log.config(state="disabled")

        # ── update toolbar status ────────────────────────────────────────
        self._sync_detail_hdr.set(
            f"{source} sync  ·  {n} / {total} processed"
        )

    def _build_review_tab(self, parent):
        # ── Row 1: utility actions + right-side re-compare / count ────
        row1 = ttk.Frame(parent)
        row1.pack(fill="x", padx=8, pady=(6, 2))

        self.gen_btn = ttk.Button(
            row1,
            text="Generate Target Paths",
            width=24,
            command=self._generate_target_paths,
        )
        self.gen_btn.pack(side="left")
        _tip(self.gen_btn,
             "Group scanned audio files into book groups (one per source "
             "folder of audio), and compute each book's planned destination "
             "path under Output Root. Must be run before Organize Library, "
             "and after any source edits.")

        self.edit_btn = ttk.Button(
            row1,
            text="Edit Selected",
            width=16,
            command=self._edit_book,
            state="disabled",
        )
        self.edit_btn.pack(side="left", padx=(6, 0))
        _tip(self.edit_btn,
             "Open a modal to manually override the selected book's "
             "metadata (author, title, series, narrator, etc.). "
             "Saving locks the book from automatic sync overwrites.")

        self.export_books_btn = ttk.Button(
            row1,
            text="Export CSV",
            width=12,
            command=self._export_books_csv,
            state="disabled",
        )
        self.export_books_btn.pack(side="left", padx=(4, 0))
        _tip(self.export_books_btn,
             "Dump every row of the books table to a CSV file.")

        self.show_review_var = tk.BooleanVar(value=False)
        _review_cb = ttk.Checkbutton(
            row1, text="Needs review only",
            variable=self.show_review_var,
            command=self._on_review_filter_change,
        )
        _review_cb.pack(side="left", padx=(10, 0))
        _tip(_review_cb,
             "When ticked, the books table only shows rows flagged for "
             "manual review (e.g. ASIN conflicts, LLM divergences).")

        # right side of row 1
        self.book_count_var = tk.StringVar(value="")
        ttk.Label(row1, textvariable=self.book_count_var, foreground=SPINE["fg3"],
                  font=("Segoe UI", 8)).pack(side="right")

        self.btn_recompare = ttk.Button(
            row1,
            text="↻ Re-Compare with Output",
            command=self._start_recompare,
        )
        self.btn_recompare.pack(side="right", padx=(0, 8))
        _tip(self.btn_recompare,
             "Re-walks the Output Root and re-runs duplicate detection. "
             "Use this when you've added books to the output library "
             "manually. When new duplicates are found, a chooser asks "
             "whether to Hide, Move to ABL_duplicates, or Delete them.")

        # ── Row 2: sync sources grouped by Identifiers / Catalogues + Stop + status
        row2 = ttk.Frame(parent)
        row2.pack(fill="x", padx=8, pady=(2, 4))

        # ── Identifiers group (LLMs — run first, figure out what the book is)
        id_frame = ttk.LabelFrame(row2, text=" Identifiers ", padding=(4, 2))
        id_frame.pack(side="left", padx=(0, 6))

        self.gemini_btn = ttk.Button(
            id_frame,
            text="Gemini",
            width=10,
            command=self._sync_gemini,
            state="disabled",
        )
        self.gemini_btn.pack(side="left", padx=(0, 3))
        _tip(self.gemini_btn,
             "Bulk-identify every book via Google Gemini 2.5 Flash. "
             "Excellent at parsing messy folder names ('ASOIAF 1' → "
             "'A Game of Thrones'). Requires a Google API key in settings.")

        self.groq_btn = ttk.Button(
            id_frame,
            text="Groq",
            width=8,
            command=self._sync_groq,
            state="disabled",
        )
        self.groq_btn.pack(side="left", padx=(0, 3))
        _tip(self.groq_btn,
             "Bulk-identify via Groq LLaMA 3.3 70B. Fast and free with "
             "a Groq API key. Watch for daily-quota rate limits.")

        self.claude_btn = ttk.Button(
            id_frame,
            text="Claude",
            width=8,
            command=self._sync_claude,
            state="disabled",
        )
        self.claude_btn.pack(side="left", padx=(0, 3))
        _tip(self.claude_btn,
             "Bulk-identify via Anthropic Claude (claude-3-5-haiku). "
             "Requires an Anthropic API key.")

        self.chatgpt_btn = ttk.Button(
            id_frame,
            text="ChatGPT",
            width=9,
            command=self._sync_chatgpt,
            state="disabled",
        )
        self.chatgpt_btn.pack(side="left", padx=(0, 3))
        _tip(self.chatgpt_btn,
             "Bulk-identify via OpenAI GPT-4o-mini. "
             "Requires an OpenAI API key.")

        self.local_btn = ttk.Button(
            id_frame,
            text="Local LLM",
            width=10,
            command=self._sync_local,
            state="disabled",
        )
        self.local_btn.pack(side="left")
        _tip(self.local_btn,
             "Bulk-identify via a locally-running LLM server (LM Studio "
             "or Ollama). Free and private. Set the URL and model in "
             "the settings row below.")

        # ── Catalogues group (databases — run after Identifiers to enrich data)
        lu_frame = ttk.LabelFrame(row2, text=" Catalogues ", padding=(4, 2))
        lu_frame.pack(side="left", padx=(0, 6))

        self.audible_btn = ttk.Button(
            lu_frame,
            text="Audible",
            width=9,
            command=self._sync_audible,
            state="disabled",
        )
        self.audible_btn.pack(side="left", padx=(0, 3))
        _tip(self.audible_btn,
             "Bulk-match every book against the Audible catalogue. "
             "No API key needed. Uses duration matching to pick the "
             "right edition. Fills in ASIN, narrator, official series.")

        self.google_btn = ttk.Button(
            lu_frame,
            text="Google",
            width=8,
            command=self._sync_google,
            state="disabled",
        )
        self.google_btn.pack(side="left", padx=(0, 3))
        _tip(self.google_btn,
             "Bulk-match every book against Google Books. "
             "Good for ISBN lookups and ebook metadata.")

        self.openlibrary_btn = ttk.Button(
            lu_frame,
            text="Open Library",
            width=12,
            command=self._sync_openlibrary,
            state="disabled",
        )
        self.openlibrary_btn.pack(side="left", padx=(0, 3))
        _tip(self.openlibrary_btn,
             "Bulk-match against the free Open Library catalogue.")

        self.hardcover_btn = ttk.Button(
            lu_frame,
            text="Hardcover",
            width=10,
            command=self._sync_hardcover,
            state="disabled",
        )
        self.hardcover_btn.pack(side="left")
        _tip(self.hardcover_btn,
             "Bulk-match via the Hardcover.app GraphQL API. "
             "Requires a Hardcover API key in settings.")

        # ── Stop + status (fill remainder of row 2)
        self.stop_sync_btn = ttk.Button(
            row2,
            text="Stop",
            width=6,
            command=self._stop_sync,
            state="disabled",
        )
        self.stop_sync_btn.pack(side="left", padx=(0, 8))
        _tip(self.stop_sync_btn,
             "Abort the currently-running bulk sync. Books already "
             "processed keep their updates; remaining books are skipped.")

        self.books_status_var = tk.StringVar(value="Click 'Generate Target Paths' to build book groups.")
        ttk.Label(row2, textvariable=self.books_status_var, foreground=SPINE["fg3"],
                  font=("Segoe UI", 9), anchor="w").pack(side="left", fill="x", expand=True)

        # ── Organize Library row ───────────────────────────────────────
        org_frame = ttk.Frame(parent)
        org_frame.pack(fill="x", padx=8, pady=(0, 4))

        self.organize_btn = ttk.Button(
            org_frame,
            text="Organize Library",
            width=26,
            command=self._organize_library,
            state="disabled",
        )
        self.organize_btn.pack(side="left")
        _tip(self.organize_btn,
             "Transfer every book to its planned Target Path under "
             "Output Root. A dialog asks Copy (safe, default — keeps "
             "originals) or Move (destructive, removes source folders). "
             "Run 'Generate Target Paths' first.")

        self.org_progress = ttk.Progressbar(org_frame, mode="determinate", length=300)
        self.org_progress.pack(side="left", padx=(12, 0), fill="x", expand=True)

        self.org_status_var = tk.StringVar(value="")
        ttk.Label(org_frame, textvariable=self.org_status_var, foreground=SPINE["fg3"],
                  font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))

        # ── Resizable split: book tree (top) ↕ sync detail (bottom) ──────
        paned = ttk.PanedWindow(parent, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Top pane — book treeview
        tree_frame = ttk.LabelFrame(paned, text=" Book Groups ", padding=2)
        paned.add(tree_frame, weight=3)

        # Bottom pane — sync detail (draggable sash to resize)
        self._build_sync_detail_panel(paned)
        paned.add(self._sync_detail_frame, weight=1)

        # ── Chip filter bar (sits at top of tree_frame, row 0) ────────
        chip_bar = ttk.Frame(tree_frame)
        chip_bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=(3, 3))

        _chip_defs = [
            ("all",       "All",          SPINE["fg2"]),
            ("unmatched", "Unmatched",    SPINE["bad"]),
            ("low",       "Low Conf",     SPINE["warn"]),
            ("dup",       "Duplicates",   SPINE["info"]),
            ("review",    "Needs Review", SPINE["bad"]),
        ]
        _chip_tooltips = {
            "all":       "Show every book in the database.",
            "unmatched": "Books with no metadata source and zero confidence — "
                         "the LLMs and catalogues couldn't identify them. "
                         "Probably need manual editing.",
            "low":       "Books with confidence under 80, OR where an LLM / "
                         "catalogue suggested a different title than the "
                         "current one. Review and accept manually if needed.",
            "dup":       "Books flagged as duplicates (either by Flag "
                         "Duplicates or by Re-Compare with Output finding "
                         "them in the library already).",
            "review":    "Books explicitly flagged for manual review — "
                         "typically ASIN conflicts during sync.",
        }
        for chip_id, chip_label, chip_color in _chip_defs:
            btn = tk.Button(
                chip_bar,
                text=chip_label,
                relief="flat",
                bd=0,
                padx=8, pady=2,
                font=("Segoe UI", 9),
                cursor="hand2",
                fg=chip_color,
                bg=SPINE["surface3"],
                activebackground=SPINE["surface2"],
                command=lambda cid=chip_id: self._set_book_chip(cid),
            )
            btn.pack(side="left", padx=(0, 4))
            self._chip_btns[chip_id] = btn
            _tip(btn, _chip_tooltips.get(chip_id, ""))
        # Mark "All" as initially active
        self._chip_btns["all"].config(relief="solid", bd=1, bg=SPINE["surface"],
                                       font=("Segoe UI", 9, "bold"))

        bcols = ("author", "series", "title", "seq", "narrator", "id",
                 "files", "duration", "adur", "conf", "src", "rev", "lm", "cat", "usr",
                 "target", "current")
        self.book_tree = ttk.Treeview(tree_frame, columns=bcols, show="headings",
                                       selectmode="extended")

        self._book_col_heads = {
            "author":   "Author",
            "series":   "Series",
            "title":    "Title",
            "seq":      "Seq",
            "narrator": "Narrator",
            "id":       "ISBN / ASIN",
            "files":    "Files",
            "duration": "Duration",
            "adur":     "Audible",
            "conf":     "Conf",
            "src":      "Src",
            "rev":      "Rev",
            "lm":       "LM",
            "cat":      "Cat",
            "usr":      "Usr",
            "target":   "Target Path",
            "current":  "Current Path",
        }
        bwidths = {
            "author": 160, "series": 120, "title": 200, "seq": 45,
            "narrator": 140, "id": 130,
            "files": 45, "duration": 70, "adur": 70, "conf": 45,
            "src": 45, "rev": 35, "lm": 35, "cat": 35, "usr": 35,
            "target": 300, "current": 150,
        }
        bminwidths = {
            "author": 60, "series": 50, "title": 80, "seq": 30,
            "narrator": 60, "id": 60,
            "files": 30, "duration": 55, "adur": 55, "conf": 35,
            "src": 35, "rev": 30, "lm": 30, "cat": 30, "usr": 30,
            "target": 100, "current": 60,
        }

        for col in bcols:
            self.book_tree.heading(col, text=self._book_col_heads[col],
                                   command=lambda c=col: self._sort_book_tree(c))
            # 'current' is the rightmost column — make IT the stretch column so
            # spare horizontal space goes to Current Path rather than pushing it
            # off-screen behind Target Path.
            self.book_tree.column(col, width=bwidths[col], minwidth=bminwidths[col],
                                  stretch=(col == "current"))

        # Hover tooltips on each column header explaining what it means
        _tree_heading_tip(self.book_tree, {
            "author":   "Best-known author — auto-updated by syncs.",
            "series":   "Series name, if the book belongs to one.",
            "title":    "Best-known title — auto-updated by syncs.",
            "seq":      "Series sequence (e.g. '1', '0.5').",
            "narrator": "Narrator name — usually filled by Audible sync.",
            "id":       "Catalogue identifier — ASIN preferred, ISBN fallback.",
            "files":    "Number of audio files in this book group.",
            "duration": "Total scanned audio duration (H:MM:SS).",
            "adur":     "Audible's published runtime in minutes.",
            "conf":     "Confidence score (0-100) from initial file-tag extraction.",
            "src":      "Which source provided the current metadata "
                        "(Aud=Audible, Ggl=Google, Gem=Gemini, Grq=Groq, "
                        "Cld=Claude, GPT=ChatGPT, LLM=Local, OLib=Open Library, "
                        "HC=Hardcover, Usr=user-edited).",
            "rev":      "'!' if this book is flagged for manual review.",
            "lm":       "LLM sync flag: ✓ confirmed, ? diverged, blank = no LLM sync yet.",
            "cat":      "Catalogue sync flag (Audible / Google / OL / HC).",
            "usr":      "'✎' if you've manually edited this book — locks it "
                        "from automatic sync overwrites.",
            "target":   "Planned destination path under Output Root after Organize.",
            "current":  "Folder name where the source audio files currently live.",
        })

        # Row colour tags  (review > lm_diff > cat_diff > lm_ok > cat_ok > google > low)
        self.book_tree.tag_configure("dup",      foreground=SPINE["info"])     # blue   — duplicate
        self.book_tree.tag_configure("review",   foreground=SPINE["bad"])      # red    — needs review
        self.book_tree.tag_configure("lm_diff",  foreground=SPINE["warn"])     # amber  — LLM diverged
        self.book_tree.tag_configure("cat_diff", foreground=SPINE["warn"])     # amber  — catalogue diverged
        self.book_tree.tag_configure("lm_ok",    foreground=SPINE["ok"])       # green  — LLM confirmed
        self.book_tree.tag_configure("cat_ok",   foreground=SPINE["ok"])       # green  — catalogue confirmed
        self.book_tree.tag_configure("google",   foreground=SPINE["accent"])   # accent — legacy Google match
        self.book_tree.tag_configure("low",      foreground=SPINE["warn"])     # amber  — low confidence
        self.book_tree.tag_configure("user_locked", foreground=SPINE["user"]) # purple — manually edited & locked
        self.book_tree.tag_configure("noseries", foreground=SPINE["fg3"])      # muted  — no series info

        bvsb = ttk.Scrollbar(tree_frame, orient="vertical",   command=self.book_tree.yview)
        bhsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.book_tree.xview)
        self.book_tree.configure(yscrollcommand=bvsb.set, xscrollcommand=bhsb.set)

        self.book_tree.grid(row=1, column=0, sticky="nsew")
        bvsb.grid(row=1, column=1, sticky="ns")
        bhsb.grid(row=2, column=0, sticky="ew")
        tree_frame.rowconfigure(1, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        # Enable Edit button when a row is selected
        self.book_tree.bind("<<TreeviewSelect>>", self._on_book_select)
        self.book_tree.bind("<Double-1>", lambda e: self._edit_book())
        self.book_tree.bind("<Delete>", self._delete_selected_books)
        self.book_tree.bind("<Button-3>", self._show_context_menu_books)
        self.book_tree.bind("<Button-1>", self._toggle_deselect_books)
        self._setup_column_resize(self.book_tree)

    # ======================================================================
    # Column resize helper — applied to both treeviews
    # ======================================================================

    def _setup_column_resize(self, tree: ttk.Treeview):
        """
        Enable column resizing by dragging header separators or header edges.

        Key insight: hovering directly over the thin bar *between* two headers
        returns identify_region() == "separator", not "heading".  The previous
        code only checked for "heading", so the cursor never changed and drags
        never started when the user was in exactly the right spot.  Both regions
        must be accepted.

        _col_at_edge also accounts for horizontal scroll offset so that column
        edge positions remain accurate when the table is scrolled sideways.
        """
        _EDGE = 8   # pixel tolerance — wider is easier to grab
        state: dict = {"col": None, "start_x": 0, "start_w": 0}

        def _col_at_edge(x: int):
            """
            Return the column whose right edge is within _EDGE pixels of x.
            Converts screen x → content x using the current xview offset so
            that results are correct when the treeview is scrolled horizontally.
            """
            cols = tree["columns"]
            total = sum(tree.column(c)["width"] for c in cols)
            # xview()[0] is the fraction of content scrolled off to the left;
            # multiplying by total gives the pixel offset.
            scroll_x = tree.xview()[0] * total
            cum = 0
            for col in cols:
                cum += tree.column(col)["width"]
                # cum is a content coordinate; subtract scroll_x to get screen x
                if abs(x - (cum - scroll_x)) <= _EDGE:
                    return col
            return None

        def on_motion(event):
            region = tree.identify_region(event.x, event.y)
            if region in ("heading", "separator"):
                # Always show resize cursor over a separator; only show it near
                # an edge when hovering over the header body itself.
                if region == "separator" or _col_at_edge(event.x):
                    tree.config(cursor="size_we")
                else:
                    tree.config(cursor="")
            elif not state["col"]:   # don't reset cursor while a drag is live
                tree.config(cursor="")

        def on_press(event):
            region = tree.identify_region(event.x, event.y)
            if region not in ("heading", "separator"):
                return
            # For a separator click, identify which column it belongs to; for a
            # heading-body click check whether we are close enough to an edge.
            if region == "separator":
                col = tree.identify_column(event.x)
                # identify_column returns "#N" (1-based); map to actual column id
                try:
                    idx = int(col.lstrip("#")) - 1
                    cols = tree["columns"]
                    col = cols[idx] if 0 <= idx < len(cols) else None
                except (ValueError, IndexError):
                    col = None
            else:
                col = _col_at_edge(event.x)

            if col:
                state["col"]     = col
                state["start_x"] = event.x
                state["start_w"] = tree.column(col)["width"]
                return "break"   # suppress sort command when starting a resize

        def on_drag(event):
            if state["col"]:
                dx    = event.x - state["start_x"]
                minw  = tree.column(state["col"])["minwidth"]
                new_w = max(minw, state["start_w"] + dx)
                tree.column(state["col"], width=new_w)
                return "break"

        def on_release(event):
            state["col"] = None
            tree.config(cursor="")

        tree.bind("<Motion>",          on_motion,   add=True)
        tree.bind("<ButtonPress-1>",   on_press,    add=True)
        tree.bind("<B1-Motion>",       on_drag,     add=True)
        tree.bind("<ButtonRelease-1>", on_release,  add=True)

        # Double-click on a separator (or column edge) = autosize that one column.
        # Industry-standard "fit to content" gesture, mirrors Excel / file explorers.
        def on_double(event):
            region = tree.identify_region(event.x, event.y)
            if region == "separator":
                col = tree.identify_column(event.x)
                try:
                    idx  = int(col.lstrip("#")) - 1
                    cols = tree["columns"]
                    col  = cols[idx] if 0 <= idx < len(cols) else None
                except (ValueError, IndexError):
                    col = None
            elif region == "heading":
                col = _col_at_edge(event.x)
            else:
                return
            if col:
                self._autosize_columns(tree, only=(col,))
                return "break"
        tree.bind("<Double-Button-1>", on_double, add=True)

    # ======================================================================
    # Autosize columns — fit width to header + visible content
    # ======================================================================

    def _autosize_columns(self, tree: ttk.Treeview, *,
                          only: tuple = (),
                          padding: int = 18,
                          max_w: int = 480,
                          sample_limit: int = 500):
        """
        Resize columns of *tree* so each one fits its widest visible value
        (or header text), capped between the column's existing minwidth and
        max_w pixels.

        only:          if non-empty, autosize only these columns
        padding:       pixels added on top of the measured text width
        max_w:         hard upper cap so one huge cell doesn't blow out the row
        sample_limit:  scan at most this many rows for measurement (perf guard)
        """
        try:
            import tkinter.font as tkfont
        except Exception:
            return

        # Pick the font Treeview actually uses (falls back to TkDefaultFont)
        try:
            font_name = self.style.lookup("Treeview", "font") or "TkDefaultFont"
        except Exception:
            font_name = "TkDefaultFont"
        try:
            font = tkfont.nametofont(font_name)
        except Exception:
            font = tkfont.Font(font=font_name)

        cols = tree["columns"]
        targets = [c for c in cols if (not only) or c in only]

        # Grab visible rows once
        rows = tree.get_children()
        if sample_limit and len(rows) > sample_limit:
            # Even sampling across the dataset
            step = max(1, len(rows) // sample_limit)
            rows = rows[::step][:sample_limit]

        for col in targets:
            # Header text (allow extra for sort arrow space)
            try:
                heading = tree.heading(col)["text"]
            except Exception:
                heading = col
            widest = font.measure(heading) + 18  # header padding + arrow allowance

            col_idx = cols.index(col)
            for iid in rows:
                try:
                    vals = tree.item(iid, "values")
                    if col_idx < len(vals):
                        v = vals[col_idx]
                        if v is None:
                            continue
                        w = font.measure(str(v))
                        if w > widest:
                            widest = w
                except Exception:
                    continue

            try:
                minw = tree.column(col)["minwidth"]
            except Exception:
                minw = 30
            new_w = min(max(widest + padding, minw), max_w)
            try:
                tree.column(col, width=new_w)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Notebook-tab hover tooltips
    # ------------------------------------------------------------------
    def _setup_tab_tooltips(self, nb: ttk.Notebook, tab_texts: dict):
        """
        Attach hover tooltips to ttk.Notebook tab labels.  Tk doesn't expose
        per-tab widgets, so we listen for mouse motion and show a tooltip
        when the cursor sits on a tab.
        """
        state = {"idx": None, "after_id": None, "tip": None}

        def _hide():
            if state["after_id"]:
                try: nb.after_cancel(state["after_id"])
                except Exception: pass
                state["after_id"] = None
            if state["tip"]:
                try: state["tip"].destroy()
                except Exception: pass
                state["tip"] = None

        def _show(idx):
            text = tab_texts.get(idx)
            if not text:
                return
            try:
                x = nb.winfo_pointerx() + 14
                y = nb.winfo_pointery() + 18
            except tk.TclError:
                return
            tw = tk.Toplevel(nb)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            try: tw.attributes("-topmost", True)
            except Exception: pass
            tk.Label(
                tw, text=text, justify="left",
                background="#1c1814", foreground="#f6f3ec",
                relief="solid", borderwidth=1,
                font=("Segoe UI", 8), wraplength=360,
                padx=8, pady=4,
            ).pack()
            state["tip"] = tw

        def on_motion(evt):
            try:
                idx = nb.index(f"@{evt.x},{evt.y}")
            except tk.TclError:
                idx = None
            if idx != state["idx"]:
                _hide()
                state["idx"] = idx
                if idx is not None:
                    state["after_id"] = nb.after(550, lambda i=idx: _show(i))

        def on_leave(_):
            _hide()
            state["idx"] = None

        nb.bind("<Motion>", on_motion, add=True)
        nb.bind("<Leave>",  on_leave,  add=True)

    # ======================================================================
    # Tab 1 — Scan logic
    # ======================================================================

    def _browse(self):
        path = filedialog.askdirectory(title="Select Library Root Directory")
        if path:
            self.dir_var.set(path)
            config.save_settings("source_dir", path)

    def _toggle_scan(self):
        if self._scanning:
            scanner.stop_scan()
            self.status_var.set("Stopping…")
        else:
            self._start_scan()

    def _start_scan(self):
        path = self.dir_var.get().strip()
        if not path:
            messagebox.showwarning("No Directory", "Enter or browse to a directory first.")
            return
        if not os.path.isdir(path):
            messagebox.showerror(
                "Directory Not Found",
                f"Cannot access:\n{path}\n\n"
                "For network drives use UNC format:  \\\\server\\share\\folder",
            )
            return

        self._scanning = True
        # Cancel the deferred startup reload if it hasn't fired yet.
        # Without this, _startup_load fires ~50 ms after app open and
        # repopulates book_tree after we've just cleared it below.
        if getattr(self, "_startup_load_id", None):
            self.after_cancel(self._startup_load_id)
            self._startup_load_id = None
        self.scan_btn.config(text="■  Stop")
        self.dup_btn.config(state="disabled")
        self.export_files_btn.config(state="disabled")
        self.del_hash_dupes_btn.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        # Clear book groups — they belong to the previous scan's data
        self.book_tree.delete(*self.book_tree.get_children())
        self.books_status_var.set("Run 'Generate Target Paths' after scan completes.")
        for v in self._stat_vars.values():
            v.set("—")
        self.progress.start(12)
        self.status_var.set("Scanning…")

        threading.Thread(target=self._run_scan, args=(path,), daemon=True).start()
        self._poll()

    def _run_scan(self, path: str):
        def on_progress(count: int, name: str):
            self._queue.put(("progress", count, name))

        def on_stats(stats: dict):
            self._queue.put(("stats", stats))

        try:
            out_path = self.out_var.get().strip()
            valid_output = out_path if out_path and os.path.isdir(out_path) else None
            scanner.scan(
                path,
                on_progress=on_progress,
                on_stats=on_stats,
                clear_first=True,
                output_root=valid_output,
            )
            dup_count = database.flag_duplicates()
            database.build_book_groups(valid_output or "")
            self._queue.put(("done", dup_count))
        except Exception as e:
            log.exception("Scan error")
            self._queue.put(("error", str(e)))

    def _start_recompare(self):
        output = self.out_var.get()
        if not output or not os.path.isdir(output):
            messagebox.showwarning("Missing Output", "Please set a valid Output Root first.")
            return

        self.btn_recompare.config(state="disabled")
        # NOTE: deliberately do NOT clear book_tree here.  Wiping the list
        # before any results come back made it look like everything had
        # vanished.  Leave existing rows visible and update at the end.
        self.books_status_var.set("Re-comparing with output folder… (please wait)")

        # Snapshot dup folders BEFORE re-compare so we can diff afterwards and
        # act only on books THIS run flagged (not pre-existing duplicates).
        self._recompare_pre_dups = database.snapshot_duplicate_book_folders()

        # Use the scanner-tab progress bar as a visual cue — non-modal, never
        # blocks the UI thread (the earlier modal busy dialog approach was
        # found to freeze the main loop).  Popup at the end is the real
        # "it's done" signal.
        try:
            self.progress.start(12)
        except Exception:
            pass

        # Throttle progress callbacks so we don't queue 1700+ messages.
        # The walk emits per-file; we only queue every 25th file.
        self._recompare_last_seen = [0]

        def run():
            try:
                def on_progress(count: int, name: str):
                    # Throttle: only forward every 25 files so the queue
                    # processing never falls behind the worker thread.
                    if count - self._recompare_last_seen[0] >= 25:
                        self._recompare_last_seen[0] = count
                        self._queue.put(("recompare_progress", count, name))

                scanner.refresh_library_match(output_root=output, on_progress=on_progress)
                self._queue.put(("recompare_done",))
            except Exception as e:
                log.exception("Re-compare failed")
                self._queue.put(("recompare_error", str(e)))

        threading.Thread(target=run, daemon=True).start()
        # CRITICAL: kick off the queue poller so the recompare_* messages get
        # processed by the UI thread.  Without this, the worker queues
        # messages that nobody reads → the app silently goes nowhere.
        self._poll()

    def _poll(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "progress":
                    _, count, filename = msg
                    self.status_var.set(f"[{count:,}]  {filename}")

                elif kind == "progress_msg":
                    _, text = msg
                    self.status_var.set(text)

                elif kind == "stats":
                    _, stats = msg
                    for k, v in stats.items():
                        if k in self._stat_vars:
                            self._stat_vars[k].set(f"{v:,}")

                elif kind == "done":
                    dup_count = msg[1] if len(msg) > 1 else 0
                    self._on_done(dup_count)
                    return

                elif kind == "error":
                    _, err = msg
                    messagebox.showerror("Scan Error", err)
                    self._reset_controls()
                    return

                elif kind == "dup_done":
                    _, count = msg
                    dup_msg = f"{count:,} duplicate{'s' if count != 1 else ''} flagged."
                    self.status_var.set(f"Duplicate detection complete — {dup_msg}")
                    self._load_db_stats()
                    self._load_results()
                    self.dup_btn.config(state="normal")
                    self.del_hash_dupes_btn.config(state="normal")
                    return
                elif kind == "recompare_progress":
                    _, count, fname = msg
                    self.books_status_var.set(
                        f"Re-comparing… {count:,} files scanned ({fname})"
                    )
                    self.status_var.set(
                        f"Re-compare: {count:,} library files walked"
                    )

                elif kind == "recompare_done":
                    log.info("UI: received recompare_done")
                    self.btn_recompare.config(state="normal")
                    try:
                        self.progress.stop()
                        self.progress["value"] = 0
                    except Exception:
                        pass
                    self.status_var.set("Re-compare complete — duplicate flags updated.")
                    self.books_status_var.set("Re-compare complete. Duplicate flags updated.")
                    log.info("UI: calling _load_books")
                    self._load_books()
                    log.info("UI: calling _handle_recompare_new_dups")
                    # Diff against the pre-recompare snapshot, then act on the
                    # books that THIS run added to the duplicate set.
                    self._handle_recompare_new_dups()
                    log.info("UI: recompare_done handler finished")
                    # CRITICAL: stop polling.  Without this, _poll keeps running
                    # forever and will eat messages destined for other pollers
                    # (e.g. _poll_organize's org_done) because the else-clause
                    # below would otherwise drop unknown messages.
                    return

                elif kind == "recompare_error":
                    self.btn_recompare.config(state="normal")
                    try:
                        self.progress.stop()
                        self.progress["value"] = 0
                    except Exception:
                        pass
                    self.books_status_var.set("Error during re-compare.")
                    self.status_var.set("Re-compare failed.")
                    messagebox.showerror("Re-Compare Error", msg[1], parent=self)
                    # Stop polling — see comment in recompare_done handler above.
                    return

                else:
                    # Unknown message kind — put it back so a dedicated poller
                    # (e.g. _poll_organize, _poll_audible, _poll_gemini) can
                    # pick it up.  Without this, _poll would silently consume
                    # and discard messages destined for other workflows.
                    self._queue.put(msg)
                    break
        except queue.Empty:
            pass

        self.after(120, self._poll)

    def _on_done(self, dup_count: int = 0):
        self._scanning = False
        self.progress.stop()
        self.progress["value"] = 100
        self._load_db_stats()
        self._load_results()
        self.scan_btn.config(text="▶  Scan Directory")
        self.dup_btn.config(state="normal")
        self.export_files_btn.config(state="normal")
        self.del_hash_dupes_btn.config(state="normal")
        self._refresh_db_label()
        dup_msg = f"  ({dup_count:,} duplicate{'s' if dup_count != 1 else ''} flagged)" if dup_count else ""
        self.status_var.set(
            f"Scan complete{dup_msg}. Go to Book Review tab and click 'Generate Target Paths'."
        )

    def _reset_controls(self):
        self._scanning = False
        self.progress.stop()
        self.scan_btn.config(text="▶  Scan Directory")

    # ------------------------------------------------------------------
    # Duplicate detection
    # ------------------------------------------------------------------

    def _flag_duplicates(self):
        self.dup_btn.config(state="disabled")
        self.status_var.set("Running duplicate detection…")

        def _run():
            try:
                count = database.flag_duplicates()
                self._queue.put(("dup_done", count))
            except Exception as e:
                log.exception("Duplicate detection error")
                self._queue.put(("error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll()

    # ------------------------------------------------------------------
    # Results display
    # ------------------------------------------------------------------

    def _load_db_stats(self):
        stats = database.get_stats()
        for k, var in self._stat_vars.items():
            val = stats.get(k)
            var.set(f"{val:,}" if val is not None else "0")

    def _load_results(self, rows=None):
        if rows is None:
            rows = database.fetch_preview(limit=2000)

        self.tree.delete(*self.tree.get_children())
        for row in rows:
            is_dup = bool(row["is_duplicate"])
            conf   = row["confidence_score"] or 0
            tag    = "dup" if is_dup else ("low" if conf <= 50 else "")
            values = (
                row["extracted_author"] or "",
                row["extracted_title"]  or "",
                row["file_type"]        or "",
                conf,
                "DUP" if is_dup else "",
                row["extension"]        or "",
                _fmt_dur(row["duration_seconds"]),
                _fmt_size(row["file_size"]),
                row["abs_path"],
            )
            self.tree.insert("", "end", values=values, tags=(tag,) if tag else ())

        total = database.get_stats().get("total", 0)
        self.row_count_var.set(f"Showing {len(rows):,} of {total:,} total files")
        self.status_var.set(f"Loaded {len(rows):,} rows.")
        # Fit each column to the loaded data
        self._autosize_columns(self.tree)

    def _apply_filter(self):
        ftype = self.filt_type.get()
        fconf = self.filt_conf.get()
        dupes = self.filt_dupes.get()

        type_arg = None if ftype == "All" else ftype
        conf_arg = None
        if fconf == "Low (≤ 50)":
            conf_arg = 50
        elif fconf == "Mid (≤ 80)":
            conf_arg = 80

        rows = database.fetch_preview(
            limit=2000,
            filter_type=type_arg,
            max_confidence=conf_arg,
            dupes_only=dupes,
        )
        self._load_results(rows)

    def _sort_tree(self, col: str):
        # Toggle direction when clicking the same column; reset to asc on a new column
        if self._tree_sort_col == col:
            self._tree_sort_rev = not self._tree_sort_rev
        else:
            self._tree_sort_col = col
            self._tree_sort_rev = False
        reverse = self._tree_sort_rev

        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        try:
            items.sort(key=lambda x: float(x[0]) if x[0] else 0, reverse=reverse)
        except ValueError:
            items.sort(key=lambda x: x[0].lower(), reverse=reverse)
        for idx, (_, k) in enumerate(items):
            self.tree.move(k, "", idx)

        # Update all column headers: arrow on sorted column, plain on others
        for c in self.tree["columns"]:
            base = self._file_col_heads[c]
            arrow = (" ↓" if reverse else " ↑") if c == col else ""
            self.tree.heading(c, text=base + arrow)

    # ------------------------------------------------------------------
    # File export
    # ------------------------------------------------------------------

    def _export_files_csv(self):
        path = filedialog.asksaveasfilename(
            title="Save Files CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            count = database.export_csv(path)
            messagebox.showinfo("Export Complete", f"Exported {count:,} records to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))

    # ======================================================================
    # Tab 2 — Book Review logic
    # ======================================================================

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select Audiobookshelf Output Root")
        if path:
            self.out_var.set(path)
            config.save_settings("output_root", path)

    # ------------------------------------------------------------------
    # Ignored Folders panel
    # ------------------------------------------------------------------

    def _ignored_browse(self):
        path = filedialog.askdirectory(title="Select Folder to Ignore")
        if path:
            self.ign_entry_var.set(path)

    def _ignored_refresh(self):
        """Reload the listbox from the database."""
        self._skipped_folders = database.get_skipped_folders()
        self.ign_listbox.delete(0, "end")
        for p in sorted(self._skipped_folders):
            self.ign_listbox.insert("end", p)
        self.ign_count_var.set(
            f"{len(self._skipped_folders)} ignored"
        )

    def _ignored_add(self):
        """Add the path in the entry to the ignore list and purge any
        previously-scanned data that lives inside it."""
        path = self.ign_entry_var.get().strip()
        if not path:
            return
        # Normalise (strip trailing slashes)
        path = path.rstrip("/\\")
        if not path:
            return
        if path in self._skipped_folders:
            messagebox.showinfo(
                "Already Ignored",
                f"This path is already in the ignore list:\n\n{path}",
                parent=self,
            )
            return

        database.add_skipped_folder(path)
        files_deleted, books_deleted = database.purge_under_path(path)
        self.ign_entry_var.set("")
        self._ignored_refresh()

        # Refresh dependent tabs so the user sees the rows vanish immediately
        try:
            self._apply_filter()
        except Exception:
            pass
        try:
            self._load_books()
        except Exception:
            pass

        if files_deleted or books_deleted:
            messagebox.showinfo(
                "Folder Ignored",
                f"Added to ignore list:\n  {path}\n\n"
                f"Removed {files_deleted:,} file row(s) and "
                f"{books_deleted:,} book row(s) that were inside it.",
                parent=self,
            )

    def _ignored_remove(self):
        """Remove the selected paths from the ignore list."""
        sel = self.ign_listbox.curselection()
        if not sel:
            return
        paths = [self.ign_listbox.get(i) for i in sel]
        for p in paths:
            database.remove_skipped_folder(p)
        self._ignored_refresh()
        # Note: removed rows are NOT re-scanned automatically — user must rescan.

    def _generate_target_paths(self):
        out_root = self.out_var.get().strip()
        if not out_root:
            messagebox.showwarning(
                "No Output Root",
                "Enter an output root directory first.",
                parent=self,
            )
            return

        self._output_root = out_root
        self.books_status_var.set("Building book groups…")
        self.gen_btn.config(state="disabled")

        def _run():
            try:
                count = database.build_book_groups(out_root)
                self._queue.put(("books_done", count))
            except Exception as e:
                log.exception("build_book_groups error")
                self._queue.put(("books_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_books()

    def _poll_books(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "books_done":
                    _, count = msg
                    self.books_status_var.set(
                        f"Generated target paths for {count} book groups."
                    )
                    self.gen_btn.config(state="normal")
                    self.export_books_btn.config(state="normal")
                    self._load_books()
                    return

                elif kind == "books_error":
                    _, err = msg
                    messagebox.showerror("Error", err)
                    self.gen_btn.config(state="normal")
                    return

                # Forward scan messages to regular poll handler in case
                # the user hasn't switched tabs yet
                else:
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(120, self._poll_books)

    def _load_books(self):
        review_only  = self.show_review_var.get()
        rows = database.fetch_books(limit=5000, review_only=review_only)
        self.book_tree.delete(*self.book_tree.get_children())
        self._book_row_cache = []
        # Refresh the cached skip set so right-click and row tags stay accurate
        self._skipped_folders = database.get_skipped_folders()

        for b in rows:
            conf          = b["confidence_score"] or 0
            needs_rev     = bool(b["manual_review"])
            lm_sync       = b["llm_synced"] or 0
            cat_sync      = b["cat_synced"] or 0
            user_edit     = b["user_edited"] or 0
            src           = b["metadata_source"] or ""
            lm_indicator  = {1: "✓", 2: "?"}.get(lm_sync, "")
            cat_indicator = {1: "✓", 2: "?"}.get(cat_sync, "")
            usr_indicator = "✎" if user_edit else ""
            # tag priority: dup > review > user_locked > lm_diff > cat_diff > lm_ok > cat_ok > google > low > normal
            if b["is_duplicate"]:
                tag = "dup"
            elif needs_rev:
                tag = "review"
            elif user_edit:
                tag = "user_locked"
            elif lm_sync == 2:
                tag = "lm_diff"
            elif cat_sync == 2:
                tag = "cat_diff"
            elif lm_sync == 1:
                tag = "lm_ok"
            elif cat_sync == 1:
                tag = "cat_ok"
            elif src == "google":
                tag = "google"
            elif conf < 80:
                tag = "low"
            else:
                tag = ""
            dur       = _fmt_dur(b["total_duration"])
            adur      = _fmt_mins(b["audible_runtime_min"])
            id_val    = b["asin"] or b["isbn"] or ""
            src_label = {"audible": "Aud", "google": "Ggl", "gemini": "Gem",
                         "groq": "Grq", "claude": "Cld", "chatgpt": "GPT", "local": "LLM",
                         "openlibrary": "OLib", "hardcover": "HC",
                         "user": "Usr"}.get(src, "")
            target  = b["target_abs_path"] or ""
            current = Path(b["parent_folder"]).name if b["parent_folder"] else ""
            values = (
                b["best_author"]      or "",
                b["series_name"]      or "",
                b["best_title"]       or "",
                b["series_sequence"]  or "",
                b["narrator"]         or "",
                id_val,
                b["file_count"]       or 0,
                dur,
                adur,
                conf,
                src_label,
                "!" if needs_rev else "",
                lm_indicator,
                cat_indicator,
                usr_indicator,
                target,
                current,
            )
            # Determine chip category for this row
            chip_cat = self._chip_category(tag, conf, src_label)
            self._book_row_cache.append((b["parent_folder"], values, tag, chip_cat))

        # Apply current chip filter (rebuilds visible rows from cache)
        self._apply_book_chip_filter(reset_chip=False)

        self.edit_btn.config(state="disabled")
        if rows:
            self.audible_btn.config(state="normal")
            self.google_btn.config(state="normal")
            self.gemini_btn.config(state="normal")
            self.groq_btn.config(state="normal")
            self.claude_btn.config(state="normal")
            self.chatgpt_btn.config(state="normal")
            self.local_btn.config(state="normal")
            self.openlibrary_btn.config(state="normal")
            self.hardcover_btn.config(state="normal")
            self.organize_btn.config(state="normal")
            # Re-apply saved sort order if one is set.
            # Pre-set state so the toggle inside _sort_book_tree produces
            # the correct direction without flipping it.
            if self._btree_sort_col:
                # _sort_book_tree toggles rev when col matches, so prime it
                # to the opposite value so the toggle lands on the saved value.
                self._btree_sort_rev = not self._btree_sort_rev
                self._sort_book_tree(self._btree_sort_col)

    def _patch_book_row(self, parent_folder: str):
        """Re-fetch a single book from the DB and update its treeview row in place."""
        b = database.fetch_book(parent_folder)
        if b is None:
            return
        conf          = b["confidence_score"] or 0
        needs_rev     = bool(b["manual_review"])
        lm_sync       = b["llm_synced"] or 0
        cat_sync      = b["cat_synced"] or 0
        user_edit     = b["user_edited"] or 0
        src           = b["metadata_source"] or ""
        lm_indicator  = {1: "✓", 2: "?"}.get(lm_sync, "")
        cat_indicator = {1: "✓", 2: "?"}.get(cat_sync, "")
        usr_indicator = "✎" if user_edit else ""
        if b["is_duplicate"]:
            tag = "dup"
        elif needs_rev:
            tag = "review"
        elif user_edit:
            tag = "user_locked"
        elif lm_sync == 2:
            tag = "lm_diff"
        elif cat_sync == 2:
            tag = "cat_diff"
        elif lm_sync == 1:
            tag = "lm_ok"
        elif cat_sync == 1:
            tag = "cat_ok"
        elif src == "google":
            tag = "google"
        elif conf < 80:
            tag = "low"
        else:
            tag = ""
        id_val    = b["asin"] or b["isbn"] or ""
        src_label = {"audible": "Aud", "google": "Ggl", "gemini": "Gem",
                     "groq": "Grq", "claude": "Cld", "chatgpt": "GPT", "local": "LLM",
                     "openlibrary": "OLib", "hardcover": "HC",
                     "user": "Usr"}.get(src, "")
        values = (
            b["best_author"]     or "",
            b["series_name"]     or "",
            b["best_title"]      or "",
            b["series_sequence"] or "",
            b["narrator"]        or "",
            id_val,
            b["file_count"]      or 0,
            _fmt_dur(b["total_duration"]),
            _fmt_mins(b["audible_runtime_min"]),
            conf,
            src_label,
            "!" if needs_rev else "",
            lm_indicator,
            cat_indicator,
            usr_indicator,
            b["target_abs_path"] or "",
            Path(b["parent_folder"]).name if b["parent_folder"] else "",
        )
        chip_cat = self._chip_category(tag, conf, src_label)
        # Update the row cache so chip filters stay accurate
        for idx, entry in enumerate(self._book_row_cache):
            if entry[0] == parent_folder:
                self._book_row_cache[idx] = (parent_folder, values, tag, chip_cat)
                break
        else:
            # Not in cache yet — this book is new, add it
            self._book_row_cache.append((parent_folder, values, tag, chip_cat))
        # If the row passes the current chip filter, update / insert it in the tree;
        # otherwise remove it if it was previously visible.
        passes = self._book_chip in ("all", chip_cat)
        try:
            if passes:
                self.book_tree.item(parent_folder, values=values,
                                    tags=(tag,) if tag else ())
            else:
                self.book_tree.delete(parent_folder)
        except tk.TclError:
            if passes:
                # Row was hidden (filtered out before) — insert it now
                self.book_tree.insert("", "end", iid=parent_folder, values=values,
                                      tags=(tag,) if tag else ())
        self._refresh_chip_counts()

    # ------------------------------------------------------------------
    # Chip filter helpers — book treeview
    # ------------------------------------------------------------------

    @staticmethod
    def _chip_category(tag: str, conf: float, src_label: str) -> str:
        """Map a row's (tag, conf, src_label) → chip category id."""
        if tag == "dup":
            return "dup"
        if tag == "review":
            return "review"
        # "unmatched" = no metadata source at all
        if not src_label and conf == 0:
            return "unmatched"
        # "low" = low confidence (incl. lm_diff / cat_diff / lm_ok / google rows below 80)
        if tag in ("low", "lm_diff", "cat_diff") or conf < 80:
            return "low"
        return "all"   # matched + high confidence → only shows under "All"

    def _set_book_chip(self, chip_id: str):
        """User clicked a chip — switch active filter and refresh the tree."""
        self._book_chip = chip_id
        self._apply_book_chip_filter(reset_chip=False)

    def _apply_book_chip_filter(self, *, reset_chip: bool = True):
        """
        Rebuild the book treeview from _book_row_cache, showing only rows
        that match _book_chip.  Also updates chip button styles and counts.
        reset_chip=True resets to "all" first (used by review-only checkbox).
        """
        if reset_chip:
            self._book_chip = "all"

        chip = self._book_chip
        self.book_tree.delete(*self.book_tree.get_children())
        for (iid, values, tag, cat) in self._book_row_cache:
            if chip == "all" or cat == chip:
                self.book_tree.insert("", "end", iid=iid, values=values,
                                      tags=(tag,) if tag else ())

        self._refresh_chip_counts()
        # Fit columns to whatever ended up visible
        self._autosize_columns(self.book_tree)

    def _refresh_chip_counts(self):
        """Update chip button labels with current category counts and active style."""
        # Count rows per category in the full cache
        counts = {"all": len(self._book_row_cache),
                  "unmatched": 0, "low": 0, "dup": 0, "review": 0}
        for (_, _, _, cat) in self._book_row_cache:
            if cat in counts:
                counts[cat] += 1

        chip_labels = {
            "all":       "All",
            "unmatched": "Unmatched",
            "low":       "Low Conf",
            "dup":       "Duplicates",
            "review":    "Needs Review",
        }
        chip_colors = {
            "all":       SPINE["fg2"],
            "unmatched": SPINE["bad"],
            "low":       SPINE["warn"],
            "dup":       SPINE["info"],
            "review":    SPINE["bad"],
        }
        for cid, btn in self._chip_btns.items():
            n = counts.get(cid, 0)
            label = f"{chip_labels[cid]}  {n:,}" if n else chip_labels[cid]
            is_active = (cid == self._book_chip)
            btn.config(
                text=label,
                relief="solid" if is_active else "flat",
                bd=1 if is_active else 0,
                bg=SPINE["surface"] if is_active else SPINE["surface3"],
                font=("Segoe UI", 9, "bold") if is_active else ("Segoe UI", 9),
                fg=chip_colors[cid],
            )

        # Update book count label to reflect visible rows
        visible = self.book_tree.get_children()
        total = len(self._book_row_cache)
        if len(visible) == total:
            self.book_count_var.set(f"{total:,} books")
        else:
            self.book_count_var.set(f"{len(visible):,} of {total:,} books")

    # ------------------------------------------------------------------
    # Deselect on click — both treeviews
    # ------------------------------------------------------------------

    def _toggle_deselect_files(self, event):
        """Click on empty space → deselect all.
        Plain click on the sole selected row → also deselect (toggle off).

        Heading and separator clicks are passed through so sort and column-resize
        work.  Ctrl/Shift clicks are passed through so extended-mode multi-select
        behaves natively.
        """
        if self.tree.identify_region(event.x, event.y) in ("heading", "separator"):
            return  # let sort command and resize handler take over
        iid = self.tree.identify_row(event.y)
        if not iid:
            self.tree.selection_remove(self.tree.selection())
            return "break"   # suppress default behaviour on empty space
        # Plain click (no Ctrl / Shift) on the already-selected lone row → deselect
        ctrl  = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        sel   = self.tree.selection()
        if not ctrl and not shift and iid in sel and len(sel) == 1:
            self.tree.selection_remove(iid)
            return "break"

    def _toggle_deselect_books(self, event):
        """Click on empty space → deselect all.
        Plain click on the sole selected row → also deselect (toggle off).
        Heading and separator clicks are passed through for sort and resize."""
        if self.book_tree.identify_region(event.x, event.y) in ("heading", "separator"):
            return
        iid = self.book_tree.identify_row(event.y)
        if not iid:
            self.book_tree.selection_remove(self.book_tree.selection())
            self._on_book_select()
            return "break"
        # Plain click (no Ctrl / Shift) on the already-selected lone row → deselect
        ctrl  = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        sel   = self.book_tree.selection()
        if not ctrl and not shift and iid in sel and len(sel) == 1:
            self.book_tree.selection_remove(iid)
            self._on_book_select()
            return "break"

    # ------------------------------------------------------------------
    # Delete / right-click — File Scanner (Tab 1)
    # ------------------------------------------------------------------

    def _delete_selected_files(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        n = len(sel)

        if not self._settings.get("skip_delete_file_confirm"):
            if n == 1:
                abs_path = self.tree.set(sel[0], "path")
                msg = (
                    f"Remove this file from the library?\n\n{abs_path}\n\n"
                    "The original file is NOT deleted from disk."
                )
            else:
                msg = (
                    f"Remove {n:,} files from the library?\n\n"
                    "Original files are NOT deleted from disk."
                )
            dlg = _ConfirmDeleteDialog(self, msg, title="Remove File" if n == 1 else "Remove Files")
            self.wait_window(dlg)
            if not dlg.confirmed:
                return
            if dlg.dont_show:
                self._settings["skip_delete_file_confirm"] = True
                config.save_settings("skip_delete_file_confirm", True)

        errors = 0
        for iid in sel:
            abs_path = self.tree.set(iid, "path")
            try:
                database.delete_file(abs_path)
                self.tree.delete(iid)
            except Exception as e:
                log.error(f"Delete failed [{Path(abs_path).name}]: {e}")
                errors += 1

        if errors:
            messagebox.showerror(
                "Delete Failed",
                f"{errors} of {n} file(s) could not be removed — see Activity Log.",
                parent=self,
            )

        # Update stats label
        remaining = len(self.tree.get_children())
        total = database.get_stats().get("total", 0)
        self.row_count_var.set(f"Showing {remaining:,} of {total:,} total files")

    def _show_context_menu_files(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        # If the right-clicked row is not already in the selection, select only it
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        n = len(self.tree.selection())
        label = f"Remove {n:,} Files from Library" if n > 1 else "Remove from Library"
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=label, command=self._delete_selected_files)
        menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------------
    # Delete / right-click — Book Review (Tab 2)
    # ------------------------------------------------------------------

    def _delete_books_by_folder(self, folders: list):
        """Remove a specific list of book folders from the DB and tree."""
        for folder in folders:
            try:
                database.delete_book(folder)
                if self.book_tree.exists(folder):
                    self.book_tree.delete(folder)
            except Exception as e:
                messagebox.showerror("Delete Failed", str(e), parent=self)
        self.book_count_var.set(f"{len(self.book_tree.get_children()):,} books")

    def _delete_selected_books(self, _event=None):
        """Remove selected book group(s) from the DB only — files stay on disk."""
        sel = self.book_tree.selection()
        if not sel:
            return
        n = len(sel)
        if n == 1:
            vals = self.book_tree.item(sel[0], "values")
            display_name = vals[2] or Path(sel[0]).name
            msg = (f"Remove this book group from the library?\n\n{display_name}\n\n"
                   "Source files are NOT deleted from disk.")
        else:
            msg = (f"Remove {n} book groups from the library?\n\n"
                   "Source files are NOT deleted from disk.")

        if not self._settings.get("skip_delete_book_confirm"):
            dlg = _ConfirmDeleteDialog(self, msg, title="Remove Book Group")
            self.wait_window(dlg)
            if not dlg.confirmed:
                return
            if dlg.dont_show:
                self._settings["skip_delete_book_confirm"] = True
                config.save_settings("skip_delete_book_confirm", True)

        for parent_folder in sel:
            try:
                database.delete_book(parent_folder)
                self.book_tree.delete(parent_folder)
            except Exception as e:
                messagebox.showerror("Delete Failed", str(e), parent=self)

        self.book_count_var.set(f"{len(self.book_tree.get_children()):,} books")

    # ------------------------------------------------------------------
    # Cleanup — Delete Empty Folders / Delete Hash Duplicates
    # ------------------------------------------------------------------

    def _delete_empty_folders(self):
        """Scan the source directory for empty folders and delete them.

        Repeats up to 5 passes so that parent folders left empty by the first
        pass are caught by the next — stops early when a pass finds nothing.
        """
        scan_root = self.dir_var.get().strip()

        if not scan_root:
            messagebox.showwarning(
                "No Source Directory",
                "Set a Source Directory first.",
                parent=self,
            )
            return

        if not Path(scan_root).is_dir():
            messagebox.showerror(
                "Directory Not Found",
                f"Could not find:\n{scan_root}",
                parent=self,
            )
            return

        # ── Initial scan — show what will be deleted and ask for confirmation
        self.status_var.set("Scanning for empty folders…")
        self.update_idletasks()

        def _find_empty(root: str) -> list[str]:
            """Return all empty subdirectories under root (bottom-up)."""
            found = []
            root_abs = os.path.abspath(root)
            for dirpath, _, _ in os.walk(root, topdown=False):
                if os.path.abspath(dirpath) == root_abs:
                    continue
                try:
                    if not os.listdir(dirpath):
                        found.append(dirpath)
                except PermissionError:
                    pass
            return found

        initial = _find_empty(scan_root)

        if not initial:
            self.status_var.set("No empty folders found.")
            # Still offer the small-folder review — that's the more useful
            # half of this workflow anyway.
            self._review_small_folders(scan_root)
            return

        preview = "\n".join(initial[:10])
        if len(initial) > 10:
            preview += f"\n…and {len(initial) - 10} more"

        if not messagebox.askyesno(
            "Delete Empty Folders",
            f"Found {len(initial):,} empty folder(s) in source directory:\n\n"
            f"{preview}\n\n"
            f"Permanently delete these from disk? This cannot be undone.",
            parent=self,
        ):
            self.status_var.set("")
            return

        # ── Delete loop — up to 5 passes ──────────────────────────────
        total_deleted = total_errors = 0
        MAX_PASSES = 5

        for pass_num in range(1, MAX_PASSES + 1):
            empty_dirs = _find_empty(scan_root)
            if not empty_dirs:
                break

            self.status_var.set(
                f"Pass {pass_num}/{MAX_PASSES} — removing {len(empty_dirs)} empty folder(s)…"
            )
            self.update_idletasks()

            for d in empty_dirs:
                try:
                    os.rmdir(d)
                    log.info(f"Removed empty folder (pass {pass_num}): {d}")
                    total_deleted += 1
                except Exception as exc:
                    log.error(f"Could not remove empty folder [{d}]: {exc}")
                    total_errors += 1

        msg = f"Deleted {total_deleted:,} empty folder(s) across up to {MAX_PASSES} passes."
        if total_errors:
            msg += f" {total_errors} failed — see Activity Log."
        self.status_var.set(msg)
        log.info(msg)

        # Chain into the small-folder review sweep
        self._review_small_folders(scan_root)

    # ------------------------------------------------------------------
    # Small-folder review sweep — runs after empty folders are deleted
    # ------------------------------------------------------------------

    def _review_small_folders(self, scan_root: str):
        """
        Find folders under *scan_root* whose recursive total size is below
        a user-picked threshold, then walk through them one at a time
        asking the user to Keep or Delete each.  Respects the Ignored
        Folders skip list.
        """
        import shutil

        if not os.path.isdir(scan_root):
            return

        if not messagebox.askyesno(
            "Review Small Folders?",
            "Empty folders are done.\n\n"
            "Now scan for SMALL folders (under a size threshold you pick) "
            "so you can review and optionally delete each one?",
            parent=self,
        ):
            return

        # ── Threshold picker ─────────────────────────────────────────
        default = int(self._settings.get("small_folder_threshold", 1024) or 1024)
        thr_dlg = _ThresholdDialog(self, default_bytes=default)
        self.wait_window(thr_dlg)
        if thr_dlg.result is None:
            self.status_var.set("Small-folder review cancelled.")
            return
        threshold = thr_dlg.result
        # Remember choice
        self._settings["small_folder_threshold"] = threshold
        config.save_settings("small_folder_threshold", threshold)

        # ── Build skip-prefix list ───────────────────────────────────
        skip_paths = database.get_skipped_folders()
        skip_norm = [
            s.replace("/", os.sep).rstrip(os.sep) for s in skip_paths
        ]

        def _is_skipped(path: str) -> bool:
            p = path.replace("/", os.sep).rstrip(os.sep)
            for s in skip_norm:
                if p == s or p.startswith(s + os.sep):
                    return True
            return False

        # ── Walk scan_root, collect candidates ───────────────────────
        self.status_var.set(f"Finding folders under {_fmt_size(threshold)}…")
        self.update_idletasks()

        candidates = []  # list[(folder, total_bytes, [(rel_name, size), ...])]
        scan_root_abs = os.path.abspath(scan_root)
        try:
            for dirpath, _, filenames in os.walk(scan_root):
                # Don't review the scan root itself
                if os.path.abspath(dirpath) == scan_root_abs:
                    continue
                if _is_skipped(dirpath):
                    continue
                # Recursively sum size + collect file list
                total_size = 0
                file_entries = []
                try:
                    for sub_root, _, sub_files in os.walk(dirpath):
                        for fn in sub_files:
                            fp = os.path.join(sub_root, fn)
                            try:
                                sz = os.path.getsize(fp)
                            except OSError:
                                sz = 0
                            total_size += sz
                            rel = os.path.relpath(fp, dirpath)
                            file_entries.append((rel, sz))
                except OSError:
                    continue
                if 0 < total_size < threshold:
                    file_entries.sort(key=lambda x: x[0].lower())
                    candidates.append((dirpath, total_size, file_entries))
        except Exception as e:
            log.exception("Small-folder scan failed")
            messagebox.showerror("Scan Error", str(e), parent=self)
            return

        # Sort candidates: smallest first (user can blow through the obvious ones)
        candidates.sort(key=lambda c: (c[1], c[0]))

        if not candidates:
            self.status_var.set(
                f"No folders smaller than {_fmt_size(threshold)} found."
            )
            messagebox.showinfo(
                "No Small Folders",
                f"No folders smaller than {_fmt_size(threshold)} "
                f"({threshold:,} bytes) were found under:\n{scan_root}",
                parent=self,
            )
            return

        # ── Walk through, prompting Keep / Delete on each ────────────
        deleted = kept = errors = 0
        cancelled_at = None
        total = len(candidates)

        for i, (folder, size, files) in enumerate(candidates, 1):
            # Folder may have been wiped already as a child of an earlier delete
            if not os.path.isdir(folder):
                continue

            dlg = _SmallFolderDialog(self, folder, size, files, i, total)
            self.wait_window(dlg)
            choice = dlg.result

            if choice == "cancel":
                cancelled_at = i
                break

            if choice == "delete":
                try:
                    shutil.rmtree(folder)
                    log.info(
                        f"Small-folder review: deleted {folder} "
                        f"({_fmt_size(size)})"
                    )
                    deleted += 1
                    try:
                        database.purge_under_path(folder)
                    except Exception as e:
                        log.warning(
                            f"DB purge failed for {folder!r}: {e}"
                        )
                except Exception as e:
                    log.error(
                        f"Small-folder review: delete failed [{folder}]: {e}"
                    )
                    errors += 1
            else:
                kept += 1
                log.info(f"Small-folder review: kept {folder} ({_fmt_size(size)})")

        # ── Summary ───────────────────────────────────────────────────
        parts = [
            f"Reviewed {(cancelled_at - 1) if cancelled_at else total} of "
            f"{total} small folder(s)."
        ]
        if deleted:
            parts.append(f"Deleted {deleted}.")
        if kept:
            parts.append(f"Kept {kept}.")
        if errors:
            parts.append(f"{errors} delete error(s) — see Activity Log.")
        if cancelled_at:
            parts.append(
                f"Cancelled — {total - cancelled_at + 1} folder(s) "
                f"left untouched."
            )
        summary = "  ".join(parts)
        self.status_var.set(summary)
        log.info(summary)
        messagebox.showinfo("Small-Folder Review Complete", summary, parent=self)

        # Refresh books tab if anything was deleted
        if deleted:
            try:
                self._load_books()
                self._load_db_stats()
            except Exception:
                pass

    def _delete_hash_dupes(self):
        """Delete hash-exact duplicate files from disk and remove from the database."""
        dupes = database.get_hash_duplicate_files()

        if not dupes:
            messagebox.showinfo(
                "No Hash Duplicates",
                "No hash-exact duplicate files are flagged in the database.\n\n"
                "Run the scanner and then 'Flag Duplicates' before using this action.",
                parent=self,
            )
            return

        total_size = sum(r["file_size"] or 0 for r in dupes)
        preview_paths = [r["abs_path"] for r in dupes[:8]]
        preview = "\n".join(preview_paths)
        if len(dupes) > 8:
            preview += f"\n…and {len(dupes) - 8} more"

        if not messagebox.askyesno(
            "Delete Hash Duplicates",
            f"Found {len(dupes):,} hash-exact duplicate file(s) "
            f"({_fmt_size(total_size)} total).\n\n"
            f"The higher-quality originals are kept. "
            f"These files will be permanently deleted from disk:\n\n"
            f"{preview}\n\n"
            f"This cannot be undone. Continue?",
            parent=self,
        ):
            return

        deleted = missing = errors = 0
        for row in dupes:
            path = row["abs_path"]
            try:
                p = Path(path)
                if p.exists():
                    p.unlink()
                    log.info(f"Disk-deleted hash dupe: {p.name}")
                    deleted += 1
                else:
                    log.warning(f"Hash dupe already missing on disk: {path}")
                    missing += 1
                database.delete_file(path)
            except Exception as exc:
                log.error(f"Could not delete hash dupe [{path}]: {exc}")
                errors += 1

        msg = f"Deleted {deleted:,} hash duplicate(s) from disk."
        if missing:
            msg += f" {missing} already missing (removed from DB)."
        if errors:
            msg += f" {errors} failed — see Activity Log."
        self.status_var.set(msg)
        log.info(msg)

        # Refresh stats so the file count updates immediately.
        self._load_db_stats()

    # ------------------------------------------------------------------
    # Re-Compare → post-run duplicate handling
    # ------------------------------------------------------------------

    def _handle_recompare_new_dups(self):
        """
        After Re-Compare finishes, ask the user what to do with any books that
        THIS run newly flagged as duplicates: hide / move-to-ABL_duplicates /
        delete from disk / cancel (un-flag).

        If a remembered choice exists in settings, apply it silently.
        """
        pre  = getattr(self, "_recompare_pre_dups", set()) or set()
        post = database.snapshot_duplicate_book_folders()
        new_dups = sorted(post - pre)
        self._recompare_pre_dups = None  # consumed
        log.info(
            f"UI: _handle_recompare_new_dups — pre={len(pre)}, post={len(post)}, "
            f"new_dups={len(new_dups)}"
        )

        if not new_dups:
            log.info("UI: showing 'No new duplicates' popup")
            self.books_status_var.set(
                "Re-compare complete — no new duplicates found."
            )
            messagebox.showinfo(
                "Re-Compare Complete",
                "No new duplicates were found.\n\n"
                "Your source library does not contain any books that already "
                "exist in the output library (beyond ones already flagged).",
                parent=self,
            )
            log.info("UI: 'No new duplicates' popup dismissed")
            return

        # Sum disk size across all source files in these folders
        total_bytes = 0
        try:
            with database._conn() as c:
                placeholders = ",".join("?" * len(new_dups))
                row = c.execute(
                    f"SELECT COALESCE(SUM(file_size),0) AS total "
                    f"FROM files "
                    f"WHERE is_library_file = 0 "
                    f"AND parent_folder IN ({placeholders})",
                    new_dups,
                ).fetchone()
                total_bytes = int(row["total"] or 0)
        except Exception as e:
            log.warning(f"Could not sum dup sizes: {e}")

        # Honour remembered choice
        remembered = self._settings.get("recompare_dup_action", "prompt")
        if remembered in ("hide", "move", "delete"):
            log.info(
                f"Re-compare: applying remembered action {remembered!r} to "
                f"{len(new_dups)} new duplicate(s)."
            )
            self._apply_recompare_action(remembered, new_dups)
            return

        # Else open the modal
        dlg = _RecompareDupDialog(self, len(new_dups), total_bytes)
        self.wait_window(dlg)
        action = dlg.result   # "cancel" | "hide" | "move" | "delete" | None

        if dlg.remember and action in ("hide", "move", "delete"):
            self._settings["recompare_dup_action"] = action
            config.save_settings("recompare_dup_action", action)
            log.info(f"Re-compare: remembered action set to {action!r}")

        if action and action != "cancel":
            self._apply_recompare_action(action, new_dups)
        else:
            # Cancel (or window closed) — un-flag so books reappear
            n = database.unflag_duplicate_books(new_dups)
            self.books_status_var.set(
                f"Re-compare cancelled — restored {n:,} book(s) to the list."
            )
            self._load_books()

    def _apply_recompare_action(self, action: str, folders: list):
        """Dispatch to the chosen action handler."""
        if action == "hide":
            self.books_status_var.set(
                f"Re-compare complete — {len(folders):,} new duplicate(s) hidden."
            )
            return
        if action == "move":
            self._recompare_move_to_quarantine(folders)
            return
        if action == "delete":
            self._recompare_delete_folders(folders)
            return
        log.warning(f"Unknown recompare action: {action!r}")

    # ------------------------------------------------------------------
    # Action: move duplicate folders into <source>/ABL_duplicates and
    # add that path to the skip list so future scans ignore them.
    # ------------------------------------------------------------------
    def _recompare_move_to_quarantine(self, folders: list):
        import shutil

        source_root = self.dir_var.get().strip()
        if not source_root or not os.path.isdir(source_root):
            messagebox.showerror(
                "Missing Source",
                "Cannot quarantine — the scanned source directory is not "
                "currently accessible. Set a valid Source Directory first.",
                parent=self,
            )
            return

        quarantine = Path(source_root) / "ABL_duplicates"
        try:
            quarantine.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror(
                "Cannot Create Quarantine Folder",
                f"Failed to create:\n  {quarantine}\n\n{e}",
                parent=self,
            )
            return

        moved = failed = 0
        total_bytes = 0
        skipped_outside = 0

        for folder in folders:
            src = Path(folder)
            if not src.exists():
                log.warning(f"Quarantine: source folder missing, skipping: {src}")
                failed += 1
                continue

            # Refuse to move folders that aren't actually under source_root —
            # could happen if the user changed Source after the scan.
            try:
                src.resolve().relative_to(Path(source_root).resolve())
            except ValueError:
                log.warning(
                    f"Quarantine: {src} is not inside source root {source_root!r}, "
                    "skipping to avoid moving unrelated data."
                )
                skipped_outside += 1
                continue

            # Resolve name collision in destination
            base_name = src.name
            dest = quarantine / base_name
            n = 2
            while dest.exists():
                dest = quarantine / f"{base_name} ({n})"
                n += 1

            try:
                # Sum size before moving
                for p in src.rglob("*"):
                    if p.is_file():
                        try:
                            total_bytes += p.stat().st_size
                        except OSError:
                            pass
                shutil.move(str(src), str(dest))
                log.info(f"Quarantined: {src} -> {dest}")
                moved += 1
            except Exception as e:
                log.error(f"Quarantine failed [{src}]: {e}")
                failed += 1

        # Register the quarantine folder on the skip list and purge stale rows
        database.add_skipped_folder(str(quarantine))
        # The original file/book rows still reference the old paths — drop them.
        for folder in folders:
            try:
                database.purge_under_path(folder)
            except Exception as e:
                log.warning(f"purge_under_path failed for {folder!r}: {e}")
        # Refresh the Ignored Folders panel + book list
        try:
            self._ignored_refresh()
        except Exception:
            pass
        self._load_books()
        self._load_db_stats()

        parts = [f"Moved {moved:,} duplicate folder(s) ({_fmt_size(total_bytes)}) "
                 f"to ABL_duplicates."]
        if failed:
            parts.append(f"{failed} failed (see Activity Log).")
        if skipped_outside:
            parts.append(f"{skipped_outside} outside source — left in place.")
        msg = " ".join(parts)
        self.books_status_var.set(msg)
        messagebox.showinfo(
            "Move Complete",
            msg + f"\n\nQuarantine folder added to the Ignored Folders list:\n"
                  f"  {quarantine}",
            parent=self,
        )

    # ------------------------------------------------------------------
    # Action: permanently delete duplicate folders from disk.
    # ------------------------------------------------------------------
    def _recompare_delete_folders(self, folders: list):
        import shutil

        # Second confirmation — this one is irreversible
        if not messagebox.askyesno(
            "Confirm Permanent Deletion",
            f"Permanently delete {len(folders):,} duplicate folder(s) "
            f"from disk?\n\nThis cannot be undone. "
            f"Make sure your output library copy is correct first.",
            parent=self,
            icon="warning",
        ):
            # User backed out — leave is_duplicate=1 (i.e. hide behaviour)
            self.books_status_var.set(
                f"Deletion cancelled — {len(folders):,} duplicate(s) remain hidden."
            )
            return

        deleted = failed = 0
        total_bytes = 0

        for folder in folders:
            src = Path(folder)
            if not src.exists():
                log.warning(f"Delete: source folder missing, skipping: {src}")
                failed += 1
                continue
            # Refuse if it doesn't sit under the scanned source root
            source_root = self.dir_var.get().strip()
            if source_root:
                try:
                    src.resolve().relative_to(Path(source_root).resolve())
                except ValueError:
                    log.warning(
                        f"Delete: {src} is not inside source root, skipping."
                    )
                    failed += 1
                    continue
            try:
                for p in src.rglob("*"):
                    if p.is_file():
                        try:
                            total_bytes += p.stat().st_size
                        except OSError:
                            pass
                shutil.rmtree(src)
                log.info(f"Deleted duplicate folder: {src}")
                deleted += 1
            except Exception as e:
                log.error(f"Delete failed [{src}]: {e}")
                failed += 1

        # Purge stale rows
        for folder in folders:
            try:
                database.purge_under_path(folder)
            except Exception as e:
                log.warning(f"purge_under_path failed for {folder!r}: {e}")

        self._load_books()
        self._load_db_stats()

        msg = f"Deleted {deleted:,} duplicate folder(s) ({_fmt_size(total_bytes)})."
        if failed:
            msg += f"  {failed} failed (see Activity Log)."
        self.books_status_var.set(msg)
        messagebox.showinfo("Delete Complete", msg, parent=self)

    def _show_context_menu_books(self, event):
        iid = self.book_tree.identify_row(event.y)
        if not iid:
            return
        # If right-clicked row is not already in the selection, select just it
        if iid not in self.book_tree.selection():
            self.book_tree.selection_set(iid)
        sel = self.book_tree.selection()
        n   = len(sel)

        menu = tk.Menu(self, tearoff=0)

        if n == 1:
            # ── Single-book menu ──────────────────────────────────────
            is_dup       = "dup"         in self.book_tree.item(sel[0], "tags")
            is_locked    = "user_locked" in self.book_tree.item(sel[0], "tags")
            menu.add_command(label="Edit Book…", command=self._edit_book,
                             state="disabled" if is_dup else "normal")
            if is_locked:
                menu.add_command(label="🔓 Unlock (allow auto-update)",
                                 command=self._unlock_book)
            menu.add_command(label="Open Source Folder in Explorer",
                             command=lambda f=sel[0]: self._open_folder(f))
            menu.add_separator()
            menu.add_command(label="Sync with Audible (this book)",
                             command=lambda: self._sync_single("audible"),
                             state="disabled" if is_dup else "normal")
            menu.add_command(label="Sync with Google Books (this book)",
                             command=lambda: self._sync_single("google"),
                             state="disabled" if is_dup else "normal")
            menu.add_command(label="Smart Sync with Gemini (this book)",
                             command=lambda: self._sync_single("gemini"),
                             state="disabled" if is_dup else "normal")
            menu.add_command(label="Turbo Sync with Groq (this book)",
                             command=lambda: self._sync_single("groq"),
                             state="disabled" if is_dup else "normal")
            menu.add_command(label="Sync with Claude (this book)",
                             command=lambda: self._sync_single("claude"),
                             state="disabled" if is_dup else "normal")
            menu.add_command(label="Sync with ChatGPT (this book)",
                             command=lambda: self._sync_single("chatgpt"),
                             state="disabled" if is_dup else "normal")
            menu.add_command(label="Local Sync / LM Studio / Ollama (this book)",
                             command=lambda: self._sync_single("local"),
                             state="disabled" if is_dup else "normal")
            menu.add_command(label="Open Library Sync (this book)",
                             command=lambda: self._sync_single("openlibrary"),
                             state="disabled" if is_dup else "normal")
            menu.add_command(label="Hardcover Sync (this book)",
                             command=lambda: self._sync_single("hardcover"),
                             state="disabled" if is_dup else "normal")
            menu.add_separator()
            menu.add_command(label="View Database Record…",
                             command=lambda f=sel[0]: _BookRecordDialog(self, f))
            menu.add_separator()
            label = "Remove Duplicate from List" if is_dup else "Remove Book Group"
            menu.add_command(label=label, command=self._delete_selected_books)
        else:
            # ── Multi-book menu ───────────────────────────────────────
            folders = list(sel)
            menu.add_command(
                label=f"Sync {n} books with Audible",
                command=lambda f=folders: self._sync_selected_books("audible", f))
            menu.add_command(
                label=f"Sync {n} books with Google Books",
                command=lambda f=folders: self._sync_selected_books("google", f))
            menu.add_command(
                label=f"Smart Sync {n} books with Gemini",
                command=lambda f=folders: self._sync_selected_books("gemini", f))
            menu.add_command(
                label=f"Turbo Sync {n} books with Groq",
                command=lambda f=folders: self._sync_selected_books("groq", f))
            menu.add_command(
                label=f"Sync {n} books with Claude",
                command=lambda f=folders: self._sync_selected_books("claude", f))
            menu.add_command(
                label=f"Sync {n} books with ChatGPT",
                command=lambda f=folders: self._sync_selected_books("chatgpt", f))
            menu.add_command(
                label=f"Local Sync {n} books (LM Studio/Ollama)",
                command=lambda f=folders: self._sync_selected_books("local", f))
            menu.add_command(
                label=f"Open Library Sync {n} books",
                command=lambda f=folders: self._sync_selected_books("openlibrary", f))
            menu.add_command(
                label=f"Hardcover Sync {n} books",
                command=lambda f=folders: self._sync_selected_books("hardcover", f))
            menu.add_separator()
            menu.add_separator()
            dup_sel = [f for f in sel
                       if "dup" in self.book_tree.item(f, "tags")]
            if dup_sel:
                menu.add_command(
                    label=f"Remove {len(dup_sel)} Duplicate(s) from List",
                    command=lambda d=dup_sel: self._delete_books_by_folder(d))
            menu.add_separator()
            menu.add_command(
                label=f"Select all ({len(self.book_tree.get_children())})",
                command=lambda: self.book_tree.selection_set(
                    self.book_tree.get_children()))
            menu.add_command(
                label="Deselect all",
                command=lambda: self.book_tree.selection_remove(
                    self.book_tree.selection()))

        menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------------
    # Single-book sync helper
    # ------------------------------------------------------------------

    def _get_selected_book_row(self) -> dict | None:
        """Return a dict of the currently selected book row, or None."""
        sel = self.book_tree.selection()
        if not sel:
            return None
        parent_folder = sel[0]
        book = database.fetch_book(parent_folder)
        if book:
            return dict(book)
        # Fallback: reconstruct from treeview values
        vals = self.book_tree.item(parent_folder, "values")
        bcols = ("best_author", "series_name", "best_title", "series_sequence",
                 "narrator", "id", "file_count", "total_duration",
                 "confidence_score", "target_abs_path")
        row = dict(zip(bcols, vals))
        row["parent_folder"] = parent_folder
        id_val = row.pop("id", "")
        if id_val.startswith("B0") or (len(id_val) == 10 and id_val.isalnum()):
            row["asin"] = id_val
            row["isbn"] = ""
        else:
            row["isbn"] = id_val
            row["asin"] = ""
        return row

    def _sync_selected_books(self, source: str, folders: list):
        """
        Bulk-sync a specific list of book folders using *source*.
        Runs in a background thread, live-updating each row as it finishes.
        """
        if not folders:
            return

        # Validate API keys before starting
        api_key = ""
        model   = ""
        if source == "gemini":
            api_key = self.gemini_key_var.get().strip()
            if not api_key:
                messagebox.showwarning("No API Key",
                    "Enter and save your Gemini API key first.", parent=self)
                return
        elif source == "groq":
            api_key = self.groq_key_var.get().strip()
            if not api_key:
                messagebox.showwarning("No API Key",
                    "Enter and save your Groq API key first.", parent=self)
                return
        elif source == "claude":
            api_key = self.claude_key_var.get().strip()
            if not api_key:
                messagebox.showwarning("No API Key",
                    "Enter and save your Anthropic Claude API key first.", parent=self)
                return
        elif source == "chatgpt":
            api_key = self.chatgpt_key_var.get().strip()
            if not api_key:
                messagebox.showwarning("No API Key",
                    "Enter and save your OpenAI API key first.", parent=self)
                return
        elif source == "local":
            api_key = self.lm_url_var.get().strip() or "http://127.0.0.1:1234/v1"
            model   = "local-model"
        elif source == "hardcover":
            api_key = self.hardcover_key_var.get().strip()
            if not api_key:
                messagebox.showwarning("No API Key",
                    "Enter and save your Hardcover API key first.", parent=self)
                return

        self._set_sync_running()
        n = len(folders)
        self.books_status_var.set(f"Syncing {n} selected books with {source.title()}…")

        def _run():
            from pathlib import Path as _P
            import time

            for idx, folder in enumerate(folders, 1):
                if self._sync_stop.is_set():
                    break
                book = database.fetch_book(folder)
                if not book:
                    continue

                best_title  = book["best_title"]  or ""
                best_author = book["best_author"] or ""
                folder_name = str(_P(folder).name)
                clean_title = database._sanitize_title_for_search(best_title) or \
                              database._sanitize_title_for_search(folder_name)

                self._queue.put(("book_progress", idx, n,
                                 f"[{source}] {clean_title or folder_name}"))

                result = None
                try:
                    if source == "audible":
                        from audible_validator import fetch_audible_data
                        known_dur = book["total_duration"] or None
                        result = fetch_audible_data(
                            f"{clean_title} {best_author}".strip(),
                            known_duration_sec=known_dur)
                        time.sleep(0.5)
                    elif source == "google":
                        from google_books_api import fetch_google_books_data
                        result = fetch_google_books_data(clean_title, best_author)
                        time.sleep(0.3)
                    elif source == "gemini":
                        from gemini_validator import parse_book_metadata_gemini
                        samples = database._get_sample_filenames_direct(folder)
                        context = database._build_llm_context(folder, samples)
                        result  = parse_book_metadata_gemini(context, api_key=api_key)
                        time.sleep(4.0)
                    elif source == "groq":
                        from groq_validator import parse_book_metadata_groq
                        samples = database._get_sample_filenames_direct(folder)
                        context = database._build_llm_context(folder, samples)
                        result  = parse_book_metadata_groq(context, api_key=api_key)
                        time.sleep(2.0)
                    elif source == "claude":
                        from claude_validator import parse_book_metadata_claude
                        samples = database._get_sample_filenames_direct(folder)
                        context = database._build_llm_context(folder, samples)
                        result  = parse_book_metadata_claude(context, api_key=api_key)
                        time.sleep(0.3)
                    elif source == "chatgpt":
                        from chatgpt_validator import parse_book_metadata_chatgpt
                        samples = database._get_sample_filenames_direct(folder)
                        context = database._build_llm_context(folder, samples)
                        result  = parse_book_metadata_chatgpt(context, api_key=api_key)
                        time.sleep(0.5)
                    elif source == "local":
                        from local_llm_validator import parse_book_metadata_local
                        samples = database._get_sample_filenames_direct(folder)
                        context = database._build_llm_context(folder, samples)
                        result  = parse_book_metadata_local(
                            context, base_url=api_key, model=model or "local-model")
                    elif source == "openlibrary":
                        from openlibrary_validator import fetch_openlibrary_data
                        result = fetch_openlibrary_data(clean_title, best_author)
                        time.sleep(1.0)
                    elif source == "hardcover":
                        from hardcover_validator import fetch_hardcover_data
                        result = fetch_hardcover_data(clean_title, best_author, api_key=api_key)
                        time.sleep(0.5)
                except RateLimitError as e:
                    log.error(f"Rate limit hit during selection sync [{source}]: {e}")
                    self._sync_stop.set()
                    self._queue.put(("sel_sync_rate_limit", str(e)))
                    return
                except TokenLimitError as e:
                    log.error(f"Token limit hit during selection sync [{source}]: {e}")
                    self._sync_stop.set()
                    self._queue.put(("sel_sync_token_limit", str(e)))
                    return
                except Exception as e:
                    log.warning(f"Selection sync [{source}] error for {folder_name!r}: {e}")
                    continue

                if not result:
                    continue

                # Build updates dict the same way each full-sync function does
                output_root = database._infer_output_root(
                    book["target_abs_path"] or "", best_author)
                updates = {
                    "best_author":  result.get("author") or best_author,
                    "best_title":   result.get("title")  or best_title,
                    "series_name":  result.get("series_name", ""),
                    "series_sequence": result.get("series_sequence", ""),
                    "narrator":     result.get("narrator", ""),
                    "isbn":         result.get("isbn", ""),
                    "target_abs_path": database.build_target_path(
                        output_root,
                        result.get("author") or best_author,
                        result.get("title")  or best_title,
                        result.get("series_name", ""),
                        result.get("series_sequence", ""),
                    ),
                }
                if source == "audible":
                    updates["asin"]               = result.get("asin", "")
                    updates["audible_runtime_min"] = result.get("runtime_min") or 0
                    updates["metadata_source"]     = "audible"
                elif source == "google":
                    updates["asin"]            = result.get("asin", "")
                    updates["google_title"]    = result.get("title", "")
                    updates["google_author"]   = result.get("author", "")
                    updates["google_isbn"]     = result.get("isbn", "")
                    updates["metadata_source"] = "google"
                elif source in ("gemini", "groq", "claude", "chatgpt", "local"):
                    updates["asin"]            = result.get("asin", "")
                    updates["metadata_source"] = source
                elif source in ("openlibrary", "hardcover"):
                    updates["metadata_source"] = source

                database.update_book(folder, updates)
                self._queue.put(("book_update", folder))

            self._queue.put(("sel_sync_done", source, n))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_sel_sync()

    def _poll_sel_sync(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "book_progress":
                    _, current, total, label = msg
                    self.books_status_var.set(
                        f"[{current}/{total}]  {label[:60]}")
                elif kind == "book_update":
                    _, folder = msg
                    self._patch_book_row(folder)
                elif kind == "sel_sync_done":
                    _, source, n = msg
                    stopped = self._sync_stop.is_set()
                    self.books_status_var.set(
                        f"{'Stopped' if stopped else 'Done'} — "
                        f"{source.title()} sync of {n} selected books complete.")
                    self._set_sync_idle()
                    self._load_books()
                    return
                elif kind == "sel_sync_rate_limit":
                    _, err_msg = msg
                    messagebox.showerror(
                        "Rate Limit Reached — Sync Stopped", err_msg, parent=self)
                    self.books_status_var.set("Sync stopped — API rate/quota limit reached.")
                    self._set_sync_idle()
                    return
                elif kind == "sel_sync_token_limit":
                    _, err_msg = msg
                    messagebox.showerror(
                        "Token Limit Reached — Sync Stopped", err_msg, parent=self)
                    self.books_status_var.set("Sync stopped — token limit reached.")
                    self._set_sync_idle()
                    self._load_books()
                    return
                else:
                    self._queue.put(msg)
                    break
        except queue.Empty:
            pass
        self.after(150, self._poll_sel_sync)

    def _sync_single(self, source: str):
        """Open SingleSyncDialog for the currently selected book."""
        book_row = self._get_selected_book_row()
        if not book_row:
            return
        api_key = ""
        model   = ""
        if source == "gemini":
            api_key = self.gemini_key_var.get().strip()
            if not api_key:
                messagebox.showwarning("No API Key",
                    "Enter and save your Gemini API key first.", parent=self)
                return
        elif source == "groq":
            api_key = self.groq_key_var.get().strip()
            if not api_key:
                messagebox.showwarning("No API Key",
                    "Enter and save your Groq API key first.", parent=self)
                return
        elif source == "claude":
            api_key = self.claude_key_var.get().strip()
            if not api_key:
                messagebox.showwarning("No API Key",
                    "Enter and save your Anthropic Claude API key first.", parent=self)
                return
        elif source == "chatgpt":
            api_key = self.chatgpt_key_var.get().strip()
            if not api_key:
                messagebox.showwarning("No API Key",
                    "Enter and save your OpenAI API key first.", parent=self)
                return
        elif source == "local":
            # api_key carries the base_url; model is always "local-model" (server picks it)
            api_key = self.lm_url_var.get().strip() or "http://127.0.0.1:1234/v1"
            model   = "local-model"
        elif source == "hardcover":
            api_key = self.hardcover_key_var.get().strip()
            if not api_key:
                messagebox.showwarning("No API Key",
                    "Enter and save your Hardcover API key first.", parent=self)
                return

        dlg = SingleSyncDialog(
            self, book_row, source,
            api_key=api_key,
            output_root=self._output_root or self.out_var.get(),
            model=model,
        )
        self.wait_window(dlg)
        if dlg._applied:
            self._patch_book_row(book_row["parent_folder"])

    def _on_book_select(self, _event=None):
        sel = self.book_tree.selection()
        # Edit is only meaningful for a single selection
        self.edit_btn.config(state="normal" if len(sel) == 1 else "disabled")

    def _edit_book(self):
        sel = self.book_tree.selection()
        if len(sel) != 1:
            return

        parent_folder = sel[0]   # iid is parent_folder
        # Build a dict from the current treeview row values
        vals = self.book_tree.item(parent_folder, "values")
        bcols = ("best_author", "series_name", "best_title", "series_sequence",
                 "narrator", "id",
                 "file_count", "total_duration", "adur", "conf",
                 "src", "manual_review", "llm_synced", "cat_synced", "user_edited",
                 "target_abs_path", "current_path")
        book_row = dict(zip(bcols, vals))
        book_row["parent_folder"] = parent_folder
        # "id" column holds ASIN or ISBN — split back out for the dialog
        id_val = book_row.pop("id", "")
        if id_val.startswith("B0") or (len(id_val) == 10 and id_val.isalnum()):
            book_row["asin"] = id_val
            book_row["isbn"] = ""
        else:
            book_row["isbn"] = id_val
            book_row["asin"] = ""

        dlg = EditBookDialog(self, book_row, self._output_root or self.out_var.get())
        self.wait_window(dlg)

        if dlg._saved:
            # Refresh just this row from the DB
            self._load_books()

    def _unlock_book(self):
        """Clear user_edited flag so automated sources can update this book again."""
        sel = self.book_tree.selection()
        if len(sel) != 1:
            return
        parent_folder = sel[0]
        database.update_book(parent_folder, {
            "user_edited":    0,
            "user_edited_at": "",
        })
        self._patch_book_row(parent_folder)
        self.status_var.set(f"Unlocked: {Path(parent_folder).name}")

    # ------------------------------------------------------------------
    # Gemini sync
    # ------------------------------------------------------------------

    def _save_gemini_key(self):
        key = self.gemini_key_var.get().strip()
        if not key:
            messagebox.showwarning("No Key", "Enter a Gemini API key first.", parent=self)
            return
        config.save_settings("gemini_api_key", key)
        self._show_key_saved("gemini")

    def _sync_gemini(self):
        api_key = self.gemini_key_var.get().strip()
        if not api_key:
            messagebox.showwarning(
                "No API Key",
                "Enter and save your Gemini API key first.",
                parent=self,
            )
            return

        # Single-selection → open detail dialog instead of bulk sync
        if len(self.book_tree.selection()) == 1:
            self._sync_single("gemini")
            return

        self._sync_source = "gemini"
        self._set_sync_running()
        self.books_status_var.set("Connecting to Gemini…")

        def _run():
            def on_progress(current, total, folder_name):
                self._queue.put(("gemini_progress", current, total, folder_name))

            def on_update(folder):
                self._queue.put(("book_update", folder))

            def on_result(folder, sent, result, outcome):
                self._queue.put(("sync_result", folder, sent, result, outcome))

            try:
                stats = database.gemini_validate_and_fix_books(
                    api_key=api_key,
                    on_progress=on_progress,
                    on_update=on_update,
                    on_result=on_result,
                    stop_event=self._sync_stop,
                    request_delay=4.0,
                )
                self._queue.put(("gemini_done", stats))
            except RateLimitError as e:
                log.error(f"Gemini rate limit: {e}")
                self._queue.put(("gemini_rate_limit", str(e)))
            except TokenLimitError as e:
                log.error(f"Gemini token limit: {e}")
                self._queue.put(("gemini_token_limit", str(e)))
            except Exception as e:
                log.exception("Gemini sync error")
                self._queue.put(("gemini_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_gemini()

    def _poll_gemini(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "gemini_progress":
                    _, current, total, folder_name = msg
                    self._sync_current = current
                    self._sync_total   = total
                    self.books_status_var.set(
                        f"Gemini: {current} / {total}  —  {folder_name[:55]}"
                    )

                elif kind == "book_update":
                    _, folder = msg
                    self._patch_book_row(folder)

                elif kind == "sync_result":
                    _, folder, sent, result, outcome = msg
                    folder_name = Path(folder).name
                    self._update_sync_detail(sent, result, folder_name, outcome)

                elif kind == "gemini_done":
                    _, stats = msg
                    stopped = self._sync_stop.is_set()
                    self.books_status_var.set(
                        f"{'Gemini sync stopped' if stopped else 'Gemini sync complete'} — "
                        f"{stats['updated']} updated, {stats['skipped']} no-hit, {stats['total']} total."
                    )
                    self._set_sync_idle()
                    self._load_books()
                    return

                elif kind == "gemini_rate_limit":
                    _, err = msg
                    messagebox.showerror("Gemini Rate Limit — Sync Stopped", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Gemini sync stopped — rate/quota limit reached.")
                    return

                elif kind == "gemini_token_limit":
                    _, err = msg
                    messagebox.showerror("Gemini Token Limit — Sync Stopped", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Gemini sync stopped — response token limit hit.")
                    return

                elif kind == "gemini_error":
                    _, err = msg
                    messagebox.showerror("Gemini Sync Error", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Gemini sync failed — see log for details.")
                    return

                else:
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(150, self._poll_gemini)

    # ------------------------------------------------------------------
    # Groq sync
    # ------------------------------------------------------------------

    def _save_groq_key(self):
        key = self.groq_key_var.get().strip()
        if not key:
            messagebox.showwarning("No Key", "Enter a Groq API key first.", parent=self)
            return
        config.save_settings("groq_api_key", key)
        self._show_key_saved("groq")

    def _sync_groq(self):
        api_key = self.groq_key_var.get().strip()
        if not api_key:
            messagebox.showwarning(
                "No API Key",
                "Enter and save your Groq API key first.",
                parent=self,
            )
            return

        # Single-selection → open detail dialog instead of bulk sync
        if len(self.book_tree.selection()) == 1:
            self._sync_single("groq")
            return

        self._sync_source = "groq"
        self._set_sync_running()
        self.books_status_var.set("Connecting to Groq…")

        def _run():
            def on_progress(current, total, folder_name):
                self._queue.put(("groq_progress", current, total, folder_name))

            def on_update(folder):
                self._queue.put(("book_update", folder))

            def on_result(folder, sent, result, outcome):
                self._queue.put(("sync_result", folder, sent, result, outcome))

            try:
                stats = database.groq_validate_and_fix_books(
                    api_key=api_key,
                    on_progress=on_progress,
                    on_update=on_update,
                    on_result=on_result,
                    stop_event=self._sync_stop,
                    request_delay=2.0,
                )
                self._queue.put(("groq_done", stats))
            except RateLimitError as e:
                log.error(f"Groq rate limit: {e}")
                self._queue.put(("groq_rate_limit", str(e)))
            except TokenLimitError as e:
                log.error(f"Groq token limit: {e}")
                self._queue.put(("groq_token_limit", str(e)))
            except Exception as e:
                log.exception("Groq sync error")
                self._queue.put(("groq_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_groq()

    def _poll_groq(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "groq_progress":
                    _, current, total, folder_name = msg
                    self._sync_current = current
                    self._sync_total   = total
                    self.books_status_var.set(
                        f"Groq: {current} / {total}  —  {folder_name[:55]}"
                    )

                elif kind == "book_update":
                    _, folder = msg
                    self._patch_book_row(folder)

                elif kind == "sync_result":
                    _, folder, sent, result, outcome = msg
                    folder_name = Path(folder).name
                    self._update_sync_detail(sent, result, folder_name, outcome)

                elif kind == "groq_done":
                    _, stats = msg
                    stopped = self._sync_stop.is_set()
                    self.books_status_var.set(
                        f"{'Groq sync stopped' if stopped else 'Groq sync complete'} — "
                        f"{stats['updated']} updated, {stats['skipped']} no-hit, {stats['total']} total."
                    )
                    self._set_sync_idle()
                    self._load_books()
                    return

                elif kind == "groq_rate_limit":
                    _, err = msg
                    messagebox.showerror("Groq Rate Limit — Sync Stopped", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Groq sync stopped — rate/quota limit reached.")
                    return

                elif kind == "groq_token_limit":
                    _, err = msg
                    messagebox.showerror("Groq Token Limit — Sync Stopped", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Groq sync stopped — response token limit hit.")
                    return

                elif kind == "groq_error":
                    _, err = msg
                    messagebox.showerror("Groq Sync Error", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Groq sync failed — see log for details.")
                    return

                else:
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(150, self._poll_groq)

    # ------------------------------------------------------------------
    # Claude sync
    # ------------------------------------------------------------------

    def _save_claude_key(self):
        key = self.claude_key_var.get().strip()
        if not key:
            messagebox.showwarning("No Key", "Enter an Anthropic API key first.", parent=self)
            return
        config.save_settings("claude_api_key", key)
        self._show_key_saved("claude")

    def _save_hardcover_key(self):
        key = self.hardcover_key_var.get().strip()
        if not key:
            messagebox.showwarning("No Key", "Enter a Hardcover API key first.", parent=self)
            return
        config.save_settings("hardcover_api_key", key)
        self._show_key_saved("hardcover")

    def _sync_claude(self):
        api_key = self.claude_key_var.get().strip()
        if not api_key:
            messagebox.showwarning(
                "No API Key",
                "Enter and save your Anthropic Claude API key first.",
                parent=self,
            )
            return

        # Single-selection → open detail dialog instead of bulk sync
        if len(self.book_tree.selection()) == 1:
            self._sync_single("claude")
            return

        self._sync_source = "claude"
        self._set_sync_running()
        self.books_status_var.set("Connecting to Anthropic Claude…")

        def _run():
            def on_progress(current, total, folder_name):
                self._queue.put(("claude_progress", current, total, folder_name))

            def on_update(folder):
                self._queue.put(("book_update", folder))

            def on_result(folder, sent, result, outcome):
                self._queue.put(("sync_result", folder, sent, result, outcome))

            try:
                stats = database.claude_validate_and_fix_books(
                    api_key=api_key,
                    on_progress=on_progress,
                    on_update=on_update,
                    on_result=on_result,
                    stop_event=self._sync_stop,
                    request_delay=0.5,
                )
                self._queue.put(("claude_done", stats))
            except RateLimitError as e:
                log.error(f"Claude rate limit: {e}")
                self._queue.put(("claude_rate_limit", str(e)))
            except TokenLimitError as e:
                log.error(f"Claude token limit: {e}")
                self._queue.put(("claude_token_limit", str(e)))
            except Exception as e:
                log.exception("Claude sync error")
                self._queue.put(("claude_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_claude()

    def _poll_claude(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "claude_progress":
                    _, current, total, folder_name = msg
                    self._sync_current = current
                    self._sync_total   = total
                    self.books_status_var.set(
                        f"Claude: {current} / {total}  —  {folder_name[:55]}"
                    )

                elif kind == "book_update":
                    _, folder = msg
                    self._patch_book_row(folder)

                elif kind == "sync_result":
                    _, folder, sent, result, outcome = msg
                    folder_name = Path(folder).name
                    self._update_sync_detail(sent, result, folder_name, outcome)

                elif kind == "claude_done":
                    _, stats = msg
                    stopped = self._sync_stop.is_set()
                    self.books_status_var.set(
                        f"{'Claude sync stopped' if stopped else 'Claude sync complete'} — "
                        f"{stats['updated']} updated, {stats['skipped']} no-hit, {stats['total']} total."
                    )
                    self._set_sync_idle()
                    self._load_books()
                    return

                elif kind == "claude_rate_limit":
                    _, err = msg
                    messagebox.showerror("Claude Rate Limit — Sync Stopped", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Claude sync stopped — rate/quota limit reached.")
                    return

                elif kind == "claude_token_limit":
                    _, err = msg
                    messagebox.showerror("Claude Token Limit — Sync Stopped", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Claude sync stopped — response token limit hit.")
                    return

                elif kind == "claude_error":
                    _, err = msg
                    messagebox.showerror("Claude Sync Error", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Claude sync failed — see log for details.")
                    return

                else:
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(150, self._poll_claude)

    # ------------------------------------------------------------------
    # ChatGPT sync
    # ------------------------------------------------------------------

    def _save_chatgpt_key(self):
        key = self.chatgpt_key_var.get().strip()
        if not key:
            messagebox.showwarning("No Key", "Enter an OpenAI API key first.", parent=self)
            return
        config.save_settings("chatgpt_api_key", key)
        self._show_key_saved("chatgpt")

    def _sync_chatgpt(self):
        api_key = self.chatgpt_key_var.get().strip()
        if not api_key:
            messagebox.showwarning(
                "No API Key",
                "Enter and save your OpenAI API key first.",
                parent=self,
            )
            return

        # Single-selection → open detail dialog instead of bulk sync
        if len(self.book_tree.selection()) == 1:
            self._sync_single("chatgpt")
            return

        self._sync_source = "chatgpt"
        self._set_sync_running()
        self.books_status_var.set("Connecting to OpenAI ChatGPT…")

        def _run():
            def on_progress(current, total, folder_name):
                self._queue.put(("chatgpt_progress", current, total, folder_name))

            def on_update(folder):
                self._queue.put(("book_update", folder))

            def on_result(folder, sent, result, outcome):
                self._queue.put(("sync_result", folder, sent, result, outcome))

            try:
                stats = database.chatgpt_validate_and_fix_books(
                    api_key=api_key,
                    on_progress=on_progress,
                    on_update=on_update,
                    on_result=on_result,
                    stop_event=self._sync_stop,
                    request_delay=0.5,
                )
                self._queue.put(("chatgpt_done", stats))
            except RateLimitError as e:
                log.error(f"ChatGPT rate limit: {e}")
                self._queue.put(("chatgpt_rate_limit", str(e)))
            except TokenLimitError as e:
                log.error(f"ChatGPT token limit: {e}")
                self._queue.put(("chatgpt_token_limit", str(e)))
            except Exception as e:
                log.exception("ChatGPT sync error")
                self._queue.put(("chatgpt_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_chatgpt()

    def _poll_chatgpt(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "chatgpt_progress":
                    _, current, total, folder_name = msg
                    self._sync_current = current
                    self._sync_total   = total
                    self.books_status_var.set(
                        f"ChatGPT: {current} / {total}  —  {folder_name[:55]}"
                    )

                elif kind == "book_update":
                    _, folder = msg
                    self._patch_book_row(folder)

                elif kind == "sync_result":
                    _, folder, sent, result, outcome = msg
                    folder_name = Path(folder).name
                    self._update_sync_detail(sent, result, folder_name, outcome)

                elif kind == "chatgpt_done":
                    _, stats = msg
                    stopped = self._sync_stop.is_set()
                    self.books_status_var.set(
                        f"{'ChatGPT sync stopped' if stopped else 'ChatGPT sync complete'} — "
                        f"{stats['updated']} updated, {stats['skipped']} no-hit, {stats['total']} total."
                    )
                    self._set_sync_idle()
                    self._load_books()
                    return

                elif kind == "chatgpt_rate_limit":
                    _, err = msg
                    messagebox.showerror("ChatGPT Rate Limit — Sync Stopped", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("ChatGPT sync stopped — rate/quota limit reached.")
                    return

                elif kind == "chatgpt_token_limit":
                    _, err = msg
                    messagebox.showerror("ChatGPT Token Limit — Sync Stopped", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("ChatGPT sync stopped — response token limit hit.")
                    return

                elif kind == "chatgpt_error":
                    _, err = msg
                    messagebox.showerror("ChatGPT Sync Error", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("ChatGPT sync failed — see log for details.")
                    return

                else:
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(150, self._poll_chatgpt)

    # ------------------------------------------------------------------
    # Local LLM sync (LM Studio)
    # ------------------------------------------------------------------

    def _sync_local(self):
        # Single-selection → open detail dialog
        if len(self.book_tree.selection()) == 1:
            self._sync_single("local")
            return

        base_url = self.lm_url_var.get().strip() or "http://127.0.0.1:1234/v1"

        self._sync_source = "local"
        self._set_sync_running()
        self.books_status_var.set("Connecting to local LLM server…")

        def _run():
            def on_progress(current, total, folder_name):
                self._queue.put(("local_progress", current, total, folder_name))

            def on_update(folder):
                self._queue.put(("book_update", folder))

            def on_result(folder, sent, result, outcome):
                self._queue.put(("sync_result", folder, sent, result, outcome))

            try:
                stats = database.local_validate_and_fix_books(
                    on_progress=on_progress,
                    on_update=on_update,
                    on_result=on_result,
                    stop_event=self._sync_stop,
                    base_url=base_url,
                    model="local-model",
                )
                self._queue.put(("local_done", stats))
            except TokenLimitError as e:
                log.error(f"Local LLM token limit: {e}")
                self._queue.put(("local_token_limit", str(e)))
            except Exception as e:
                log.exception("Local LLM sync error")
                self._queue.put(("local_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_local()

    def _poll_local(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "local_progress":
                    _, current, total, folder_name = msg
                    self._sync_current = current
                    self._sync_total   = total
                    self.books_status_var.set(
                        f"Local LLM: {current} / {total}  —  {folder_name[:55]}"
                    )

                elif kind == "book_update":
                    _, folder = msg
                    self._patch_book_row(folder)

                elif kind == "sync_result":
                    _, folder, sent, result, outcome = msg
                    folder_name = Path(folder).name
                    self._update_sync_detail(sent, result, folder_name, outcome)

                elif kind == "local_done":
                    _, stats = msg
                    stopped = self._sync_stop.is_set()
                    self.books_status_var.set(
                        f"{'Local sync stopped' if stopped else 'Local sync complete'} — "
                        f"{stats['confirmed']} confirmed, {stats['diverged']} diverged, "
                        f"{stats['skipped']} no-hit, {stats['total']} total."
                    )
                    self._set_sync_idle()
                    self._load_books()
                    return

                elif kind == "local_token_limit":
                    _, err = msg
                    messagebox.showerror("Local LLM Token Limit — Sync Stopped", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Local sync stopped — response token limit hit.")
                    return

                elif kind == "local_error":
                    _, err = msg
                    if "couldn't find the local server" in err or "local server" in err.lower():
                        messagebox.showerror(
                            "Local Server Not Found", err, parent=self
                        )
                        self.books_status_var.set(
                            "Local sync failed — server unreachable. Check URL and start LM Studio."
                        )
                    else:
                        messagebox.showerror("Local LLM Sync Error", err, parent=self)
                        self.books_status_var.set(
                            "Local sync failed — see log for details."
                        )
                    self._set_sync_idle()
                    return

                else:
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(150, self._poll_local)

    def _sort_book_tree(self, col: str):
        # Toggle direction when clicking the same column; reset to asc on a new column
        if self._btree_sort_col == col:
            self._btree_sort_rev = not self._btree_sort_rev
        else:
            self._btree_sort_col = col
            self._btree_sort_rev = False
        reverse = self._btree_sort_rev

        items = [(self.book_tree.set(k, col), k) for k in self.book_tree.get_children("")]
        try:
            items.sort(key=lambda x: float(x[0]) if x[0] else 0, reverse=reverse)
        except ValueError:
            items.sort(key=lambda x: x[0].lower(), reverse=reverse)
        for idx, (_, k) in enumerate(items):
            self.book_tree.move(k, "", idx)

        # Update all column headers: arrow on sorted column, plain on others
        for c in self.book_tree["columns"]:
            base = self._book_col_heads[c]
            arrow = (" ↓" if reverse else " ↑") if c == col else ""
            self.book_tree.heading(c, text=base + arrow)

    # ------------------------------------------------------------------
    # Sync helpers — shared stop control
    # ------------------------------------------------------------------

    def _stop_sync(self):
        self._sync_stop.set()
        self.stop_sync_btn.config(state="disabled")
        self.books_status_var.set("Stopping — finishing current request…")

    def _set_sync_running(self):
        """Disable all sync/generate buttons, enable Stop, and clear the log."""
        self._sync_stop.clear()
        self.audible_btn.config(state="disabled")
        self.google_btn.config(state="disabled")
        self.gemini_btn.config(state="disabled")
        self.groq_btn.config(state="disabled")
        self.claude_btn.config(state="disabled")
        self.chatgpt_btn.config(state="disabled")
        self.local_btn.config(state="disabled")
        self.openlibrary_btn.config(state="disabled")
        self.hardcover_btn.config(state="disabled")
        self.gen_btn.config(state="disabled")
        self.edit_btn.config(state="disabled")
        self.stop_sync_btn.config(state="normal")
        self._clear_sync_log()
        self._sync_detail_hdr.set("Sync starting…")

    def _set_sync_idle(self):
        """Re-enable sync/generate buttons and disable Stop."""
        self.audible_btn.config(state="normal")
        self.google_btn.config(state="normal")
        self.gemini_btn.config(state="normal")
        self.groq_btn.config(state="normal")
        self.claude_btn.config(state="normal")
        self.chatgpt_btn.config(state="normal")
        self.local_btn.config(state="normal")
        self.openlibrary_btn.config(state="normal")
        self.hardcover_btn.config(state="normal")
        self.gen_btn.config(state="normal")
        self.edit_btn.config(state="normal")
        self.stop_sync_btn.config(state="disabled")
        self._sync_detail_hdr.set("Sync complete.")

    # ------------------------------------------------------------------
    # Audible sync
    # ------------------------------------------------------------------

    def _sync_audible(self):
        # Single-selection → open detail dialog instead of bulk sync
        if len(self.book_tree.selection()) == 1:
            self._sync_single("audible")
            return

        self._sync_source = "audible"
        self._set_sync_running()
        self.books_status_var.set("Connecting to Audible…")

        def _run():
            def on_progress(current, total, book_title):
                self._queue.put(("audible_progress", current, total, book_title))

            def on_update(folder):
                self._queue.put(("book_update", folder))

            def on_result(folder, sent, result, outcome):
                self._queue.put(("sync_result", folder, sent, result, outcome))

            try:
                stats = database.validate_and_fix_books(
                    on_progress=on_progress,
                    on_update=on_update,
                    on_result=on_result,
                    stop_event=self._sync_stop,
                    request_delay=0.5,
                )
                self._queue.put(("audible_done", stats))
            except Exception as e:
                log.exception("Audible sync error")
                self._queue.put(("audible_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_audible()

    def _poll_audible(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "audible_progress":
                    _, current, total, title = msg
                    self._sync_current = current
                    self._sync_total   = total
                    self.books_status_var.set(
                        f"Checking Audible: {current} / {total}  —  {title[:55]}"
                    )

                elif kind == "book_update":
                    _, folder = msg
                    self._patch_book_row(folder)

                elif kind == "sync_result":
                    _, folder, sent, result, outcome = msg
                    folder_name = Path(folder).name
                    self._update_sync_detail(sent, result, folder_name, outcome)

                elif kind == "audible_done":
                    _, stats = msg
                    stopped = self._sync_stop.is_set()
                    g_note  = (f", {stats.get('google_hits', 0)} via Google Books"
                               if stats.get("google_hits") else "")
                    self.books_status_var.set(
                        f"{'Audible sync stopped' if stopped else 'Audible sync complete'} — "
                        f"{stats['updated']} updated{g_note}, {stats['skipped']} no-hit, {stats['total']} total."
                    )
                    self._set_sync_idle()
                    self._load_books()
                    return

                elif kind == "audible_error":
                    _, err = msg
                    messagebox.showerror("Audible Sync Error", err)
                    self._set_sync_idle()
                    self.books_status_var.set("Audible sync failed — see log for details.")
                    return

                else:
                    # Not an audible message — put it back for other pollers
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(150, self._poll_audible)

    # ------------------------------------------------------------------
    # Google Books sync
    # ------------------------------------------------------------------

    def _sync_google(self):
        if len(self.book_tree.selection()) == 1:
            self._sync_single("google")
            return

        self._sync_source = "google"
        self._set_sync_running()
        self.books_status_var.set("Querying Google Books…")

        def on_progress(current, total, label):
            self._queue.put(("book_progress", current, total, label))

        def on_update(folder):
            self._queue.put(("book_update", folder))

        def on_result(folder, sent, result, outcome):
            self._queue.put(("sync_result", folder, sent, result, outcome))

        def _run():
            try:
                stats = database.google_validate_and_fix_books(
                    on_progress=on_progress,
                    on_update=on_update,
                    on_result=on_result,
                    stop_event=self._sync_stop,
                    request_delay=0.3,
                )
                self._queue.put(("google_done", stats))
            except Exception as e:
                log.exception("Google Books sync error")
                self._queue.put(("google_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_google()

    def _poll_google(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "book_progress":
                    _, current, total, label = msg
                    self._sync_current = current
                    self._sync_total   = total
                    self.books_status_var.set(
                        f"Google Books [{current}/{total}]  {label[:50]}"
                    )

                elif kind == "book_update":
                    _, folder = msg
                    self._patch_book_row(folder)

                elif kind == "sync_result":
                    _, folder, sent, result, outcome = msg
                    folder_name = Path(folder).name
                    self._update_sync_detail(sent, result, folder_name, outcome)

                elif kind == "google_done":
                    _, stats = msg
                    stopped = self._sync_stop.is_set()
                    self.books_status_var.set(
                        f"{'Google sync stopped' if stopped else 'Google Books sync complete'} — "
                        f"{stats['updated']} updated, {stats['skipped']} no-hit, "
                        f"{stats['total']} total."
                    )
                    self._set_sync_idle()
                    self._load_books()
                    return

                elif kind == "google_error":
                    _, err = msg
                    messagebox.showerror("Google Books Sync Error", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Google Books sync failed — see log for details.")
                    return

                else:
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(150, self._poll_google)

    # ------------------------------------------------------------------
    # Open Library sync
    # ------------------------------------------------------------------

    def _sync_openlibrary(self):
        if len(self.book_tree.selection()) == 1:
            self._sync_single("openlibrary")
            return

        self._sync_source = "openlibrary"
        self._set_sync_running()
        self.books_status_var.set("Querying Open Library…")

        def on_progress(current, total, label):
            self._queue.put(("openlibrary_progress", current, total, label))

        def on_update(folder):
            self._queue.put(("book_update", folder))

        def on_result(folder, sent, result, outcome):
            self._queue.put(("sync_result", folder, sent, result, outcome))

        def _run():
            try:
                stats = database.openlibrary_validate_and_fix_books(
                    on_progress=on_progress,
                    on_update=on_update,
                    on_result=on_result,
                    stop_event=self._sync_stop,
                    request_delay=1.0,
                )
                self._queue.put(("openlibrary_done", stats))
            except Exception as e:
                log.exception("Open Library sync error")
                self._queue.put(("openlibrary_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_openlibrary()

    def _poll_openlibrary(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "openlibrary_progress":
                    _, current, total, label = msg
                    self._sync_current = current
                    self._sync_total   = total
                    self.books_status_var.set(
                        f"Open Library [{current}/{total}]  {label[:50]}"
                    )

                elif kind == "book_update":
                    _, folder = msg
                    self._patch_book_row(folder)

                elif kind == "sync_result":
                    _, folder, sent, result, outcome = msg
                    folder_name = Path(folder).name
                    self._update_sync_detail(sent, result, folder_name, outcome)

                elif kind == "openlibrary_done":
                    _, stats = msg
                    stopped = self._sync_stop.is_set()
                    self.books_status_var.set(
                        f"{'Open Library sync stopped' if stopped else 'Open Library sync complete'} — "
                        f"{stats['updated']} updated, {stats['skipped']} no-hit, "
                        f"{stats['total']} total."
                    )
                    self._set_sync_idle()
                    self._load_books()
                    return

                elif kind == "openlibrary_error":
                    _, err = msg
                    messagebox.showerror("Open Library Sync Error", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Open Library sync failed — see log for details.")
                    return

                else:
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(150, self._poll_openlibrary)

    # ------------------------------------------------------------------
    # Hardcover sync
    # ------------------------------------------------------------------

    def _sync_hardcover(self):
        api_key = self.hardcover_key_var.get().strip()
        if not api_key:
            messagebox.showwarning(
                "No API Key",
                "Enter and save your Hardcover API key first.",
                parent=self,
            )
            return

        if len(self.book_tree.selection()) == 1:
            self._sync_single("hardcover")
            return

        self._sync_source = "hardcover"
        self._set_sync_running()
        self.books_status_var.set("Querying Hardcover…")

        def on_progress(current, total, label):
            self._queue.put(("hardcover_progress", current, total, label))

        def on_update(folder):
            self._queue.put(("book_update", folder))

        def on_result(folder, sent, result, outcome):
            self._queue.put(("sync_result", folder, sent, result, outcome))

        def _run():
            try:
                stats = database.hardcover_validate_and_fix_books(
                    api_key=api_key,
                    on_progress=on_progress,
                    on_update=on_update,
                    on_result=on_result,
                    stop_event=self._sync_stop,
                    request_delay=0.5,
                )
                self._queue.put(("hardcover_done", stats))
            except Exception as e:
                log.exception("Hardcover sync error")
                self._queue.put(("hardcover_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_hardcover()

    def _poll_hardcover(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "hardcover_progress":
                    _, current, total, label = msg
                    self._sync_current = current
                    self._sync_total   = total
                    self.books_status_var.set(
                        f"Hardcover [{current}/{total}]  {label[:50]}"
                    )

                elif kind == "book_update":
                    _, folder = msg
                    self._patch_book_row(folder)

                elif kind == "sync_result":
                    _, folder, sent, result, outcome = msg
                    folder_name = Path(folder).name
                    self._update_sync_detail(sent, result, folder_name, outcome)

                elif kind == "hardcover_done":
                    _, stats = msg
                    stopped = self._sync_stop.is_set()
                    self.books_status_var.set(
                        f"{'Hardcover sync stopped' if stopped else 'Hardcover sync complete'} — "
                        f"{stats['updated']} updated, {stats['skipped']} no-hit, "
                        f"{stats['total']} total."
                    )
                    self._set_sync_idle()
                    self._load_books()
                    return

                elif kind == "hardcover_error":
                    _, err = msg
                    messagebox.showerror("Hardcover Sync Error", err, parent=self)
                    self._set_sync_idle()
                    self.books_status_var.set("Hardcover sync failed — see log for details.")
                    return

                else:
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(150, self._poll_hardcover)

    # ------------------------------------------------------------------
    # Organize Library
    # ------------------------------------------------------------------

    def _organize_library(self):
        out_root = self.out_var.get().strip()
        if not out_root:
            messagebox.showwarning("No Output Root",
                                   "Set an Output Root directory first.", parent=self)
            return

        dlg = _TransferModeDialog(self)
        self.wait_window(dlg)
        if dlg.mode is None:
            return  # user cancelled

        transfer_mode = dlg.mode

        self.organize_btn.config(state="disabled")
        self.gen_btn.config(state="disabled")
        self.org_progress["value"] = 0
        self.org_progress["maximum"] = 100
        self.org_status_var.set("Starting…")

        def _run():
            def on_progress(current, total, label):
                pct = int(current / total * 100) if total else 0
                self._queue.put(("org_progress", current, total, pct, label))

            try:
                stats = file_mover.execute_move(on_progress=on_progress,
                                                mode=transfer_mode)
                self._queue.put(("org_done", stats))
            except Exception as e:
                log.exception("Organize Library error")
                self._queue.put(("org_error", str(e)))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_organize()

    def _poll_organize(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "org_progress":
                    _, current, total, pct, label = msg
                    self.org_progress["value"] = pct
                    self.org_status_var.set(f"{current}/{total}  {label[:45]}")

                elif kind == "org_done":
                    _, stats = msg
                    self.org_progress["value"] = 100
                    dup_note = (f", {stats.get('duplicates_skipped', 0)} duplicates skipped"
                                if stats.get("duplicates_skipped") else "")
                    mode_label = {"copy": "copied", "move": "moved"}.get(
                        stats.get("mode", ""), "transferred")
                    files_done = stats.get("files_copied", 0) + stats.get("files_moved", 0)
                    self.org_status_var.set(
                        f"Done — {stats['ok']} books organised, "
                        f"{files_done} files {mode_label}"
                        + dup_note
                        + (f", {stats['errors']} errors" if stats["errors"] else "")
                    )
                    self.organize_btn.config(state="normal")
                    self.gen_btn.config(state="normal")
                    file_lines = ""
                    if stats.get("files_copied", 0):
                        file_lines += f"  Files copied      : {stats['files_copied']}\n"
                    if stats.get("files_moved", 0):
                        file_lines += f"  Files moved       : {stats['files_moved']}\n"
                    if stats.get("folders_removed", 0):
                        file_lines += f"  Source folders removed: {stats['folders_removed']}\n"
                    messagebox.showinfo(
                        "Organize Complete",
                        f"Library organised successfully!\n\n"
                        f"  Transfer method   : {stats.get('mode', '').title()}\n"
                        f"  Books processed   : {stats['ok']}\n"
                        f"  Duplicates skipped: {stats.get('duplicates_skipped', 0)}\n"
                        f"  Skipped (other)   : {stats['skipped']}\n"
                        f"  Errors            : {stats['errors']}\n"
                        f"{file_lines}\n"
                        f"Output: {self.out_var.get()}",
                        parent=self,
                    )
                    return

                elif kind == "org_error":
                    _, err = msg
                    self.org_status_var.set("Error — see log for details.")
                    self.organize_btn.config(state="normal")
                    self.gen_btn.config(state="normal")
                    messagebox.showerror("Organize Error", err, parent=self)
                    return

                else:
                    self._queue.put(msg)
                    break

        except queue.Empty:
            pass

        self.after(150, self._poll_organize)

    # ------------------------------------------------------------------
    # Books export
    # ------------------------------------------------------------------

    def _export_books_csv(self):
        path = filedialog.asksaveasfilename(
            title="Save Books CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            count = database.export_books_csv(path)
            messagebox.showinfo("Export Complete", f"Exported {count:,} book records to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))

    # ======================================================================
    # Misc
    # ======================================================================

    def _show_key_input(self, which: str):
        """Reveal the entry field for a key (hide the 'saved' indicator)."""
        if which == "gemini":
            self._gemini_saved_frame.pack_forget()
            self._gemini_input_frame.pack(fill="x")
        elif which == "groq":
            self._groq_saved_frame.pack_forget()
            self._groq_input_frame.pack(fill="x")
        elif which == "chatgpt":
            self._chatgpt_saved_frame.pack_forget()
            self._chatgpt_input_frame.pack(fill="x")
        elif which == "hardcover":
            self._hardcover_saved_frame.pack_forget()
            self._hardcover_input_frame.pack(fill="x")
        else:  # claude
            self._claude_saved_frame.pack_forget()
            self._claude_input_frame.pack(fill="x")

    def _show_key_saved(self, which: str):
        """Hide the entry field and show the compact 'Key saved' indicator."""
        if which == "gemini":
            self._gemini_input_frame.pack_forget()
            self._gemini_saved_frame.pack(side="left")
        elif which == "groq":
            self._groq_input_frame.pack_forget()
            self._groq_saved_frame.pack(side="left")
        elif which == "chatgpt":
            self._chatgpt_input_frame.pack_forget()
            self._chatgpt_saved_frame.pack(side="left")
        elif which == "hardcover":
            self._hardcover_input_frame.pack_forget()
            self._hardcover_saved_frame.pack(side="left")
        else:  # claude
            self._claude_input_frame.pack_forget()
            self._claude_saved_frame.pack(side="left")

    def _apply_settings(self):
        """Pre-fill UI fields from saved settings and restore GUI state."""
        s = self._settings

        # ── Paths & keys ────────────────────────────────────────────────
        if s.get("source_dir"):
            self.dir_var.set(s["source_dir"])
        if s.get("output_root"):
            self.out_var.set(s["output_root"])
        self.lm_url_var.set(s.get("lm_studio_url", "http://127.0.0.1:1234/v1"))
        if s.get("gemini_api_key"):
            self.gemini_key_var.set(s["gemini_api_key"])
            self._show_key_saved("gemini")
        else:
            self._show_key_input("gemini")
        if s.get("groq_api_key"):
            self.groq_key_var.set(s["groq_api_key"])
            self._show_key_saved("groq")
        else:
            self._show_key_input("groq")
        if s.get("claude_api_key"):
            self.claude_key_var.set(s["claude_api_key"])
            self._show_key_saved("claude")
        else:
            self._show_key_input("claude")
        if s.get("chatgpt_api_key"):
            self.chatgpt_key_var.set(s["chatgpt_api_key"])
            self._show_key_saved("chatgpt")
        else:
            self._show_key_input("chatgpt")
        if s.get("hardcover_api_key"):
            self.hardcover_key_var.set(s["hardcover_api_key"])
            self._show_key_saved("hardcover")
        else:
            self._show_key_input("hardcover")

        # ── Window geometry ──────────────────────────────────────────────
        geom = s.get("window_geometry", "")
        if geom:
            try:
                self.geometry(geom)
            except Exception:
                pass  # ignore invalid saved geometry

        # ── Active tab ───────────────────────────────────────────────────
        try:
            tab_idx = int(s.get("active_tab", 0))
            self.nb.select(tab_idx)
        except Exception:
            pass

        # ── Book filter / sort ───────────────────────────────────────────
        self.show_review_var.set(bool(s.get("show_review_only", False)))

        sort_col = s.get("book_sort_col", "")
        sort_rev = s.get("book_sort_rev", False)
        if sort_col:
            self._btree_sort_col = sort_col
            self._btree_sort_rev = bool(sort_rev)

        # ── Log level ────────────────────────────────────────────────────
        log_level = s.get("log_level", "INFO")
        if log_level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self._log_level_var.set(log_level)
            self._apply_log_level()

        # ── Column widths ────────────────────────────────────────────────
        for col, w in (s.get("col_widths_books") or {}).items():
            try:
                self.book_tree.column(col, width=int(w))
            except Exception:
                pass
        for col, w in (s.get("col_widths_files") or {}).items():
            try:
                self.tree.column(col, width=int(w))
            except Exception:
                pass

        # ── Auto-load from database if data already exists ───────────────
        # Use `after` so the window is fully drawn before we populate the trees.
        # Store the ID so _start_scan can cancel it if the user scans immediately.
        self._startup_load_id = self.after(50, self._startup_load)

    def _startup_load(self):
        """
        Populate both treeviews from the database on startup.
        Only runs if the database actually contains data so a fresh install
        shows the normal empty-state prompts.

        Aborts silently if a scan has already started — _start_scan cancels
        the after() ID, but if it fired in the same event tick we guard here
        too so we never overwrite a freshly-cleared tree.
        """
        self._startup_load_id = None  # mark as fired / no longer pending
        if self._scanning:            # scan already started — don't overwrite
            return
        stats = database.get_stats()
        file_count = stats.get("total", 0)
        book_count = 0
        try:
            import sqlite3 as _sq
            with _sq.connect(str(database.DB_PATH)) as _c:
                book_count = _c.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        except Exception:
            pass

        if file_count:
            self._load_db_stats()
            self._load_results()
            self.dup_btn.config(state="normal")
            self.export_files_btn.config(state="normal")
            self.del_hash_dupes_btn.config(state="normal")
            log.info(f"Startup: loaded {file_count:,} files from database.")

        if book_count:
            self._load_books()
            self.export_books_btn.config(state="normal")
            log.info(f"Startup: loaded {book_count:,} book groups from database.")

    def _open_folder(self, folder: str):
        """Open *folder* in Windows Explorer, or show a warning if it doesn't exist."""
        import subprocess
        from pathlib import Path as _P
        p = _P(folder)
        if p.is_dir():
            subprocess.Popen(["explorer", str(p)])
        else:
            messagebox.showwarning(
                "Folder Not Found",
                f"The source folder no longer exists:\n\n{folder}",
                parent=self,
            )

    # ------------------------------------------------------------------
    # Help menu actions
    # ------------------------------------------------------------------

    def _open_help(self):
        """Show the bundled USAGE.md in a scrollable read-only window."""
        usage_path = Path(__file__).parent / "USAGE.md"
        if not usage_path.exists():
            messagebox.showwarning(
                "User Guide Missing",
                f"Could not find USAGE.md alongside the application:\n\n"
                f"  {usage_path}\n\n"
                "Reinstall to restore it.",
                parent=self,
            )
            return
        try:
            content = usage_path.read_text(encoding="utf-8")
        except Exception as e:
            messagebox.showerror(
                "Could Not Read User Guide",
                f"Error reading {usage_path}:\n\n{e}",
                parent=self,
            )
            return
        _HelpDialog(self, content)

    def _open_about(self):
        messagebox.showinfo(
            "About Audiobook Librarian",
            "Audiobook Librarian\n\n"
            "A desktop tool to scan, identify, and organise messy audiobook "
            "collections into a clean library structure.\n\n"
            "Settings + database: ~/.bookorganizer/\n\n"
            "Help → User Guide for full documentation.",
            parent=self,
        )

    def _on_review_filter_change(self):
        """Checkbox toggled — reload books, reset chip to All, persist state."""
        config.save_settings("show_review_only", self.show_review_var.get())
        self._book_chip = "all"
        self._load_books()

    def _save_gui_state(self):
        """Collect all transient GUI state and write it to settings in one pass."""
        col_widths_books = {}
        try:
            col_widths_books = {
                col: self.book_tree.column(col, "width")
                for col in self.book_tree["columns"]
            }
        except Exception:
            pass

        col_widths_files = {}
        try:
            col_widths_files = {
                col: self.tree.column(col, "width")
                for col in self.tree["columns"]
            }
        except Exception:
            pass

        config.save_settings_batch({
            "window_geometry":  self.geometry(),
            "active_tab":       self.nb.index("current"),
            "show_review_only": self.show_review_var.get(),
            "log_level":        self._log_level_var.get(),
            "col_widths_books": col_widths_books,
            "col_widths_files": col_widths_files,
            "book_sort_col":    self._btree_sort_col,
            "book_sort_rev":    self._btree_sort_rev,
        })

    def _on_close(self):
        """Save GUI state, then exit cleanly."""
        self._save_gui_state()
        self.destroy()

    def _refresh_db_label(self):
        self.db_label.config(text=f"DB: {database.DB_PATH}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = App()
    app.mainloop()
