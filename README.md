# Audiobook Librarian — User Guide

A desktop app that scans a messy audiobook collection, identifies what each
book actually is (with help from online catalogues and LLMs), then organises
the files into a clean library structure ready for Audiobookshelf or any
similar player.

---

## Quick Start (5 steps)

1. **Set your folders.** On the **File Scanner** tab, set:
   - **Source Directory** — the messy folder full of audiobooks you want to clean up.
   - **Output Root** — where the cleaned library will live (e.g. your Audiobookshelf library path).
2. **Click ▶ Scan Directory.** The app walks the source, reads every audio /
   ebook / image file, and builds an internal database. Takes a few minutes
   on first run.
3. **Switch to the Books tab.** Click **Generate Target Paths** so the app
   plans where each book will go in the output library.
4. **Sync metadata.** Click one of the sync buttons — **Audible** is the most
   reliable for audiobooks; **Smart Sync with Gemini** is fastest if you have
   a Google API key. The app fills in author, title, series, narrator, ASIN.
5. **Click Organize Library.** A dialog asks Copy or Move. Done.

That's the happy path. Most books work first try. The rest of this guide
covers what to do when a book gets it wrong, how to review duplicates, how
to use the LLM helpers, and every button explained.

---

## End-to-End Walkthrough

### Day 1 — Scan

The **File Scanner** tab is the entry point.

1. Set **Source Directory** to the root of your messy audiobook collection.
   Network paths work (`\\server\share`).
2. Set **Output Root** to where you want the organised library to live. If
   it's empty, that's fine — the app will fill it.
3. **Ignored Folders** (optional) — if you have folders you NEVER want
   touched (e.g. work-in-progress rips), add their full path here. Anything
   under those paths is invisible to the scanner.
4. Click **▶ Scan Directory**. Watch the live counter and per-format stats.
5. When the scan finishes, the **Results** table fills with every file the
   scanner found. The **Activity Log** tab shows what happened.
6. Click **Flag Duplicates** to mark byte-identical and title+author
   duplicates (cross-folder aware: keeps the higher-quality copy and flags
   the rest). The Duplicates stat updates.

### Day 2 — Identify the Books

Switch to the **Book Review** tab.

1. Click **Generate Target Paths** — this groups the scanned audio files
   into **book groups** (one per source folder of audio) and computes where
   each one would go in your output library, based on its currently-known
   author / title / series.
2. The **books table** now shows one row per audiobook. Columns at a glance:
   - **Author / Series / Title / Seq / Narrator** — current best-known metadata.
   - **ISBN / ASIN** — catalogue identifier.
   - **Conf** — confidence score (0-100) from the initial file-tag extraction.
   - **Src** — which source provided the latest metadata (Aud / Gem / GPT etc.).
   - **LM** / **Cat** / **Usr** — LLM, catalogue, and user-edit flags (✓ ok, ? diverged, blank none).
   - **Target Path** — planned destination after Organize.
   - **Current Path** — folder name where the source audio lives now.
3. Click a sync button:
   - **Audible** — fastest, no API key, great for popular audiobooks. Uses
     duration matching to pick the right edition.
   - **Smart Sync with Gemini** — requires a Google API key. Excellent at
     parsing messy folder names ("ASOIAF 1" → A Game of Thrones).
   - **Sync with Claude** / **Sync with ChatGPT** — same as Gemini, different
     providers. Use whichever key you have.
   - **Local Sync (LM Studio / Ollama)** — runs against a local LLM. Free,
     private, slower. Set the URL and model name in settings first.
   - **Open Library Sync** / **Hardcover Sync** — free catalogue lookups.
4. Watch the **Sync Log** at the bottom of the Books tab as books are
   confirmed (green ✓) or flagged for review (amber ?).
5. **Right-click a book** to:
   - Sync just that book with any single source — opens a detail dialog
     showing what was sent and what came back. For Audible it shows up to
     10 candidate books in a chooser so you can pick the right one.
   - Edit Book… — manually override any field.
   - Open Source Folder in Explorer.
   - View Database Record… — full SQL row dump for power users.

### Day 3 — Review & Organize

1. The chip filters above the books table sort by status:
   - **All** — everything.
   - **Unmatched** — no metadata source identified the book.
   - **Low Conf** — confidence below 80.
   - **Duplicates** — flagged duplicates.
   - **Needs Review** — books with internal conflicts (e.g. ASIN mismatch).
2. Click any of those chips to focus only on the books that need attention.
3. Right-click → **Edit Book…** to fix titles by hand. The Target Path
   recomputes as you type.
4. When happy, click **Organize Library**.
   - The mode dialog asks **Copy** (safe, default) or **Move** (destructive).
   - Copy duplicates files into the output root — original source is
     untouched. Use the disk space.
   - Move walks files out of the source into the output root, then removes
     empty source folders. No undo.
5. Watch the progress bar. On completion you get a summary popup.

### When things go wrong

- **The wrong book gets matched** → Right-click → Sync with [source]. The
  detail dialog shows the raw response. For Audible, pick a different
  candidate from the chooser. Or right-click → Edit Book… and type the
  right answer manually.
- **Bulk sync looks "stuck"** → Check the Activity Log tab. Most operations
  log per-book progress.
- **"Diverged" everywhere after bulk sync** → The LLM returned cleaner
  titles than your messy `best_title` and the auto-confirm couldn't tell if
  they matched. Single-click each book → sync individually to accept.
- **Re-Compare with Output flagged a bunch of duplicates** → A modal asks
  what to do: **Hide** (keep files, just hide from list), **Move to
  ABL_duplicates** (recommended — quarantines them in `<source>/ABL_duplicates`
  and auto-skips on future scans), or **Delete** (permanent). You can tick
  "Remember my choice" to skip the prompt next time.

---

## Feature Reference

### The Three Tabs

| Tab | Purpose |
|---|---|
| **File Scanner** | Set paths, scan, flag duplicates, clean up empty / cruft folders, view per-file results. |
| **Book Review** | One row per book. Sync metadata, edit, organize. The main working area. |
| **Activity Log** | Time-stamped log of everything the app has done. Useful for debugging. |

### File Scanner Tab — Buttons

| Button | What it does |
|---|---|
| **Browse…** (Source Directory) | Pick the messy audiobook root. Persisted across runs. |
| **Browse…** (Output Root) | Pick the destination library root. Persisted. |
| **Ignored Folders** panel | Add paths the scanner / books tab / sync should completely ignore. Survives DB clears. See section below. |
| **▶ Scan Directory** | Walks the source (and output, if set) recursively. Inserts every file into the DB. Click again while running = Stop. |
| **Flag Duplicates** | Re-evaluates duplicate flags using a quality score (FLAC > M4B > MP3, weighted by bitrate). Cross-folder aware. |
| **Export Files CSV** | Dump the full files table to CSV for spreadsheet review. |
| **Delete Empty Folders** | Removes truly empty folders, up to 5 passes (to catch parents emptied by children). Then chains into the **Small-Folder Review** sweep — picks a threshold and walks you through every folder under that size for manual keep/delete. |
| **Delete Hash Duplicates** | Permanently deletes audio files flagged by `Flag Duplicates` as byte-identical lower-quality copies. |
| **↻ Re-Compare with Output** | Re-walks the output library only and re-runs duplicate detection. Use when you've added books to the output by hand and want the source-vs-library duplicate flags refreshed. Triggers the Hide / Move / Delete chooser when new duplicates are found. |

### Ignored Folders Panel

Lives on the File Scanner tab between Output Root and the action buttons.

- **Path entry + Browse + + Add** — type or browse a folder path, then Add.
  Anything under that path becomes invisible to the scanner and LLM sync.
- **Listbox** — current ignored paths.
- **Remove Selected** — un-ignore. The folder will be visible again on the next Scan.
- When you add a path that's already been scanned, the existing rows for
  files inside it are purged from the DB so they vanish from the Books tab
  immediately. The disk is untouched.

### Book Review Tab — Buttons

| Button | What it does |
|---|---|
| **Generate Target Paths** | Builds book groups from scanned audio files and computes each book's planned destination path under Output Root. Must be run before Organize. |
| **Audible** | Bulk sync every book against the Audible catalogue. Uses duration matching when available. |
| **Google** | Bulk sync against Google Books. |
| **Smart Sync with Gemini** | Bulk sync via Google Gemini LLM. Requires API key. Best for messy folder names. |
| **Turbo Sync with Groq** | Bulk sync via Groq's LLaMA 3.3 70B. Fast and free with API key. |
| **Sync with Claude** | Bulk sync via Anthropic Claude. Requires API key. |
| **Sync with ChatGPT** | Bulk sync via OpenAI GPT-4o-mini. Requires API key. |
| **Local Sync** | Bulk sync via a locally-running LM Studio or Ollama server. Free, private. Set URL + model in settings. |
| **Open Library** | Free catalogue lookup. |
| **Hardcover** | Catalogue lookup via the Hardcover.app API. |
| **Stop** | Aborts a running sync. Already-processed books are kept. |
| **Organize Library** | After confirming Copy / Move, transfers all books to their target paths. |
| **Export Books CSV** | Dump the books table to CSV. |

### Chip Filters

Click to filter the books table. Counts update live.

| Chip | Shows |
|---|---|
| **All** | Every book in the database. |
| **Unmatched** | Books with no metadata source and zero confidence — likely need manual editing. |
| **Low Conf** | Books with confidence < 80, or where the LLM / catalogue disagreed with the current best title. |
| **Duplicates** | Books flagged as duplicates by Flag Duplicates or Re-Compare with Output. |
| **Needs Review** | Books with manual-review flag set (typically ASIN conflicts during sync). |

### Right-Click Menu on a Book

Single book selected:

- **Edit Book…** — modal to override any field. Target Path auto-recalculates.
- **🔓 Unlock** — only shown for user-locked books. Removes the lock so future syncs can update the row.
- **Open Source Folder in Explorer** — opens Windows Explorer at the source folder.
- **Sync with [each source] (this book)** — opens the single-book detail dialog for that source. Shows what was sent, raw response, and parsed result. Click Apply to write.
- **View Database Record…** — full SQL row dump including every shadow column.
- **Remove Book Group** — drops the book from the table. Disk untouched.

Multi-select:

- **Sync N books with [source]** — bulk sync just the selected rows.
- **Remove N Duplicate(s) from List** — only present when the selection includes dups.
- **Select all / Deselect all** — utility.

### Books Table Columns

| Column | Meaning |
|---|---|
| **Author** | Best-known author (auto-updated by syncs). |
| **Series** | Series name, if any. |
| **Title** | Best-known title. |
| **Seq** | Series sequence number ("1", "0.5"). |
| **Narrator** | Narrator (audiobook-specific; set by Audible). |
| **ISBN / ASIN** | Catalogue identifier — ASIN preferred, ISBN fallback. |
| **Files** | Count of audio files in this book group. |
| **Duration** | Sum of audio file durations (H:MM:SS). |
| **Audible** | Audible's published runtime in minutes. |
| **Conf** | Confidence score from initial extraction (0-100). |
| **Src** | Which source provided current metadata (Aud / Ggl / Gem / Grq / Cld / GPT / LLM / OLib / HC / Usr). |
| **Rev** | "!" if marked needs-review. |
| **LM** | LLM sync flag: ✓ confirmed, ? diverged, blank no LLM sync yet. |
| **Cat** | Catalogue sync flag (Audible / Google / OL / HC). |
| **Usr** | "✎" if user has manually edited this book — locks it from auto-updates. |
| **Target Path** | Planned destination under Output Root. |
| **Current Path** | Folder name where the audio currently lives. |

### Sync Detail Log (bottom of Books tab)

Scrollable log that appends one entry per book as syncs run. Colour-coded:
- **Green** — Confirmed (LLM/catalogue agreed with current title, or filled in an empty title).
- **Amber** — Diverged (LLM/catalogue suggested a different title; stored in shadow columns for review).
- **Grey** — Skipped (no match found).
- **Purple** — Locked (user-edited book; LLM result stored but not applied).

Each entry shows the context sent and the result returned, so you can see why the sync made the call it did.

### Single-Book Sync Dialog (right-click → Sync with X)

Opens for any source. Shows:
- The exact search term or context string sent.
- The raw API response (JSON or LLM output).
- The parsed result.

**For Audible specifically**: instead of a single result, a candidates table lists up to 10 matches with title / author / narrator / runtime (with Δ to your scanned duration) / series / ASIN. Click any row to select it, then Apply Changes writes that candidate's metadata.

### Edit Book Dialog

Right-click → Edit Book…. Modal with text fields for every metadata field plus a recomputed Target Path. Saving sets the user-edited flag, which **locks the book from automatic syncs** (the LLM result still gets stored in a shadow column for review but doesn't overwrite your edit).

### Re-Compare with Output → Action Chooser

When ↻ Re-Compare flags new duplicates, this modal appears:

| Action | Effect |
|---|---|
| **Hide only** | Just hide the rows. Files untouched. Will reappear after next Scan. |
| **Move to ABL_duplicates** (recommended) | Move each duplicate folder into `<source>/ABL_duplicates/` and auto-add that to the Ignored Folders list. Reversible — drag back out manually. |
| **Delete from disk** | Permanently `shutil.rmtree` the duplicate folders. Confirms twice. |
| **Cancel** | Un-flag the new duplicates. Rows reappear in the list. |

Tick **Remember my choice** to skip the dialog on future Re-Compares (the chosen action is saved to settings and applied silently).

### Settings / Config

Stored in `~/.bookorganizer/settings.json`. Edit in-app via the settings rows below each sync button, or by hand:

- `source_dir`, `output_root` — paths.
- `gemini_api_key`, `groq_api_key`, `claude_api_key`, `openai_api_key`, `hardcover_api_key` — API keys for each provider.
- `lm_studio_url`, `lm_studio_model` — local LLM endpoint + model name.
- `recompare_dup_action` — `"prompt"` (default) or remembered action.
- `small_folder_threshold` — last picked byte threshold for the small-folder sweep.

### Activity Log Tab

Real-time view of the same log written to `~/.bookorganizer/organizer.log`. Useful when something seems wrong — every API call, every match decision, every error is here.

---

## Tips & Tricks

- **Run Audible sync first**, then a Smart Sync with Gemini on whatever it missed. Audible nails popular audiobooks; the LLM mops up the long-tail and weirdly-named folders.
- **Don't delete the source until Organize has run with Copy mode and you've spot-checked the output.** Move mode is one-way.
- **The Ignored Folders panel survives DB clears** — perfect for permanently excluding a "WIP" folder you don't want the scanner touching.
- **Right-click → Audible** with the candidates chooser is the most reliable way to fix a single mis-matched book — far better than guessing search terms.
- **The user-edit lock (✎)** is sticky: once you Edit Book…, future bulk syncs leave the row alone but still record what they would have done in the shadow columns. Right-click → 🔓 Unlock to let auto-updates back in.

---

## Where Things Live on Disk

- **Database**: `~/.bookorganizer/library_manifest.db` (SQLite, WAL mode).
- **Settings**: `~/.bookorganizer/settings.json`.
- **Log file**: `~/.bookorganizer/organizer.log`.

Delete any of these to reset that aspect of the app. The actual audiobook files are never touched unless you explicitly click Organize (Move), Delete Empty Folders, Delete Hash Duplicates, or one of the Re-Compare duplicate actions.
