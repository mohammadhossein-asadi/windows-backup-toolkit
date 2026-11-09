# Windows Backup & Restore Toolkit — Modernization Plan

Target: `Backup_Restore_Toolkit.py` (v3.0.0, single file, ~2,780 lines, tkinter/ttk, Windows-only, standard library only)
Goal: modern UI/UX, faster backups/restores, live visibility into every running process, and a stronger feature set — without breaking existing backups or restore correctness.

---

## 1. Where the code stands today (audit)

**UI**
- One window, fixed two-column layout (Backup | Restore & Tools), activity log at the bottom.
- Dark theme via `ttk` styles; header chips (version/admin/mode).
- Progress bar is **indeterminate** (spinner). No real percentage, bytes, speed, or ETA.
- Completion/errors use blocking `messagebox` dialogs.
- `MAX_LOG_LINES = 20000` is defined but **not enforced**; the log text grows for the whole session.
- No search/filter, no settings screen, no theme switch, no keyboard shortcuts.

**Engine**
- Backup runs on one background thread (`_run_bg`); restore does the same.
- Copying uses `shutil.copytree(..., ignore=...)` per top-level folder, with an item-by-item fallback and per-file retry.
- `ThreadPoolExecutor` is created/cleaned up but **not used for copying** — copies are effectively serial.
- Progress counters (`set_total`, `_done_files`) are essentially unused; only `_total_files` is incremented per directory.
- External tools (`pnputil`, `winget`, `choco`, extension listing) run through `subprocess.run(capture_output=True)`, so **nothing is visible until the command finishes**; timeouts up to 900s look frozen.
- Stop is cooperative between files; running child processes are not tracked or terminated.
- Logging opens the log file(s) on every line; the UI path also opens the log file again. Heavy logging means repeated open/close and heavy Tk event traffic.
- Manifest stats are collected by a **second full directory walk** after the backup (`rglob`), duplicating I/O on large trees.

**Restore**
- Preflight + per-step error containment + summary already exist (good).
- File restore is serial with retries; no per-file progress, no conflict policy, no restore-to-alternate-location.

**Data/config**
- `manifest.json` (v2) now records session log + per-stage file counts/sizes.
- Remembered restore sources live in `%APPDATA%\WindowsBackupToolkit\recent_sources.json`.
- No general settings file yet.

---

## 2. Design principles

1. **Engine never touches Tk.** The engine emits typed events; one UI bridge drains a queue on `after(50)` and updates widgets in batches.
2. **Measure before claiming.** Ship a benchmark harness; publish before/after files-per-second, MB/s, and UI latency.
3. **Safe by default.** Every new behavior is opt-in or reversible; restore keeps preflight, confirmation, and per-step containment.
4. **No surprise dependencies.** Stay standard-library by default. Optional integrations (7-Zip, PySide6) are explicit, detected, and documented.
5. **One source of truth.** A versioned settings/config file, one job model, one progress model.

---

## 3. Phase 0 — Foundations (refactor) · ~2–3 days

**Structure** (keep a single-file build for distribution)
```
wbk/
  __init__.py
  app.py            # entry point, DPI, theme bootstrap
  core/
    events.py       # Event dataclasses + typed constants
    jobs.py         # Job model, stage timeline, history
    engine.py       # orchestration, pause/stop, checkpoints
    backup.py       # stage implementations
    restore.py      # stage implementations + conflict policy
    copy.py         # scanner, worker pool, retries, long paths
    processes.py    # process tracker + streamed output
    config.py       # settings.json + %APPDATA% paths + migration
    logging.py      # queue-based rotating logger
    manifest.py     # read/write/verify
  platform/
    win.py          # admin check, desktop resolution, VSS, taskkill, schtasks
  ui/
    main_window.py  # shell: sidebar + header + status bar
    pages/          # dashboard, backup, restore, tools, logs, settings
    widgets/        # job bar, jobs table, toast, log view, stage list
    theme.py        # dark/light palettes, scaling
  tests/
```
- `build_single_file.py` concatenates the package into `Backup_Restore_Toolkit.py` (or PyInstaller spec) so the user still gets one portable file.
- Keep Python 3.7+ syntax (no `X | Y` annotations), keep current public behaviors and file layout.

**Event model**
- `JobState(job_id, kind, stage, state, started_at)`
- `Progress(stage, items_done, items_total, bytes_done, bytes_total, speed_bps, eta_s, current_item)`
- `ProcessInfo(pid, stage, command, started_at, state, exit_code, last_line)`
- `LogRecord(level, message, ts)`
- `JobFinished(job_id, ok, errors, report_path)`

**Config** — `%APPDATA%\WindowsBackupToolkit\settings.json`
- theme, font scale, concurrency, excludes, presets, retention, notifications, last backup parent, window geometry, restore defaults.
- Atomic writes (temp + replace), schema version + migration, corrupt-file fallback to defaults.

**Logging**
- Standard `logging` with a `QueueHandler`/`QueueListener` and `RotatingFileHandler` (10 MB × 3) in `logs/`.
- UI receives lines from the same queue; no per-line file open.
- Enforce `MAX_LOG_LINES` and add a "follow tail" toggle.

**Acceptance:** existing backup/restore/verify/preview behavior unchanged; all tests pass; single-file build still runs.

---

## 4. Phase 1 — Live process & progress visibility · ~3–4 days

**Pinned job bar (always visible)**
- Current stage + step ("Copying Documents", "Exporting drivers", "winget import").
- Determinate bar: overall % + per-stage %; file counts; bytes; **MB/s**; elapsed; **ETA**.
- Pause / Stop / Resume; Stop asks "finish current file" vs "stop now".

**Jobs panel**
- Live table, one row per running/finished stage: Stage, Status, Items, Size, Elapsed, Errors.
- Click a row → details drawer: recent log lines, current file, destination, retry count.
- Completed jobs kept in history (last 50) with duration and error counts; "Open report".

**Process monitor (the "live show any process" part)**
- Every external command goes through `ProcessTracker` using `subprocess.Popen` with stdout/stderr piped line-by-line.
- Table: PID, Tool/Command, Stage, Started, Elapsed, Status (running/ok/failed/killed), Exit code.
- Live output tail per process (last ~200 lines in UI; full output to the log file).
- Stop terminates the tracked process tree (`taskkill /PID <pid> /T /F`) and records it in the report.
- Timeouts produce partial output + clear message instead of a silent block.
- Copy operations also appear as "processes" (pseudo-rows: worker counts, active file) so the user always sees what is happening.

**Notifications**
- Non-blocking toasts for completion; modal only for destructive confirmations (restore/overwrite/delete).
- Optional Windows notification via PowerShell toast (falls back silently).

**Acceptance:** during a driver export or winget install, the UI shows the running command, PID, elapsed time and live output; Stop ends it within ~2 seconds; the window stays responsive (no >100 ms stalls).

---

## 5. Phase 2 — Modern UI/UX refactor · ~4–6 days

**Shell**
- Left sidebar: Dashboard · Backup · Restore · Tools · Logs · Settings.
- Header: title, machine/user, admin badge, running badge, storage free space.
- Bottom status bar: job summary, speed, ETA, log-file shortcut.
- Resizable panes with persisted geometry/splitter positions.

**Dashboard**
- "Last backup" card: date, size, stages, integrity badge (manifest + verify), one-click Verify/Restore/Open.
- Backup catalog: table of backups (location, date, size, stages, integrity, note/tag) with actions Restore, Verify, Export, Delete (with confirmation).
- Storage meter: free space on backup target; warning if projected size won't fit.
- Quick actions: Full backup, Quick data, Coding tools only, Restore.

**Backup page**
- Stage cards with include/exclude toggles and estimated size (pre-scan).
- Presets: Quick data · Developer · Full system · Custom; save/load custom presets.
- Exclude editor with glob patterns, validation, and "ignored folder" preview.
- Destination picker with recent destinations and free-space check.

**Restore page**
- Source selector: remembered sources (already implemented) + Add source + drop a folder onto the window.
- Stage checklist with per-stage size/date and availability (OK/MISSING).
- **Diff preview**: counts of new / overwrite / identical files per stage, with an expandable details list.
- Conflict policy: Ask · Overwrite · Skip existing · Keep both (rename).
- "Restore to alternate folder" option for safe inspection.
- Dry-run preview retained and shown inline (not just a dialog).

**Tools page**
- Verify backup (with progress + hash option), Export/Import ZIP, Open latest, Install/check Winget + Chocolatey, Generate new-PC bootstrap script, Diagnostics bundle.

**Logs page**
- Filter by level, free-text search, stage filter, copy selection, export, open log folder.
- Color-coded tags (existing), wrap toggle, timestamps toggle, follow-tail toggle.

**Settings page**
- Theme (Dark / Light / System), accent color, font scale (100–200%), density.
- Concurrency (Auto / 2 / 4 / 8) with a note about HDD vs SSD.
- Default stages, excludes, retention, notifications, confirmations, update log verbosity.

**Polish / accessibility**
- Keyboard: F5 start, Esc stop, Ctrl+L logs, Ctrl+F search, Ctrl+, settings, Alt+1..6 pages.
- Focus rings, labeled controls, high-contrast palette, screen-reader names.
- DPI awareness (`SetProcessDpiAwareness`) and font scaling; min window size respected.
- Empty states with clear next actions; consistent spacing/typography.

**Acceptance:** all current actions reachable in ≤2 clicks from the sidebar; no blocking dialogs during normal work; window usable at 100–200% scaling on 1080p and 4K.

---

## 6. Phase 3 — Speed & performance · ~4–6 days

**Scan once, copy once**
- Pre-scan with `os.scandir` (no extra `stat` per entry) to compute totals and stage sizes; feed the same index into the copy planner.
- Remove the post-backup `rglob` in `_collect_stage_stats`; accumulate counts/bytes during copy.
- Disk-space preflight against projected size (with margin).

**Parallel copy (bounded)**
- Iterative walker + `ThreadPoolExecutor` (default 4 workers, configurable; "Auto" = 4 for SSD, 2 for HDD).
- Bounded queue (e.g., 256 entries) to keep memory flat on millions of files.
- Per-file: `copyfile` → `copystat` on success (skip metadata retries), retry with backoff, locked-file detection.
- Skip reparse points/junctions; never follow symlinks during backup.
- Long-path support via `\\?\` prefix on Windows.
- Optional "safe mode" (1 worker) for flaky USB/network targets.

**Reduce UI/log overhead**
- Coalesce progress events to ~10 Hz; never log per file (log per folder + errors).
- Batch text inserts (one `insert` per drain), enforce max lines, pause auto-scroll while the user is scrolled up.
- Queue-based logger; no file open per line.

**Hashing / verify**
- Streaming SHA-256 in worker threads, opt-in; write `checksums.jsonl` incrementally so verify can resume.
- Verify compares size + optional hash; reports first N mismatches.

**Benchmark harness** (`tests/bench_copy.py`)
- Synthetic tree: 10k small files + 50 large files (mixed sizes) on the target disk.
- Metrics: files/s, MB/s, peak memory, UI event latency (measure drain time), log overhead.
- Baseline captured before changes; report deltas honestly. Target: ~1.5–2.5× throughput on SSD with 4 workers, ≥90% fewer UI updates, no UI stall >100 ms.

**Acceptance:** benchmark report committed with before/after numbers; backup of a 50 GB tree shows steady progress, correct ETA, and Stop reacts within one file.

---

## 7. Phase 4 — Feature expansion · ~6–9 days

**Backup catalog & job history**
- Persist `index.json` of backups with stage/size/integrity/notes; dashboard reads it (fast startup).
- Tags/notes, "pin" important backups, delete with confirmation and recycle-bin option.

**Incremental backups + retention**
- Per-file index (relative path, size, mtime, optional hash) stored in the backup.
- "Incremental" mode copies only changed/new files into a dated folder linked to its base full backup; restore resolves the chain automatically.
- "Synthetic full" by copying/hardlinking unchanged files where the filesystem supports it.
- Retention: keep N daily / M weekly / K monthly; preview before deletion; never delete the last good full backup.

**Scheduling**
- Create/remove a Windows Task Scheduler job (`schtasks` XML) for unattended backups with a chosen preset and destination.
- Preflight check: destination reachable, free space, admin requirements.

**New-PC bootstrap**
- Generate `Setup-NewPC.ps1`: installs Winget/Chocolatey if missing, imports package lists, restores selected config stages, prints a checklist.
- Optional "restore configs only" and "apps only" variants.

**Restore improvements**
- Conflict policies (Ask / Overwrite / Skip / Keep both) with a per-run report.
- Restore-to-alternate-folder and "compare only" mode.
- Optional pre-restore snapshot of files about to be overwritten (config-sized files only).
- Per-item progress with bytes/ETA; resumable restore using a journal file.

**Locked/open files**
- Optional VSS shadow-copy read (admin) for browsers/Outlook-style locked files; falls back with a clear message when unavailable.
- Explicit list of skipped locked files in the report.

**Coding tools (expand)**
- More tools: JetBrains settings sync folder, Sublime, Neovim, Git config/SSH (already partial), Windows Terminal, PowerShell profiles, WSL `.wslconfig`/distro configs (read-only copy), VS Code forks.
- Extension reinstall: generate per-editor scripts and validate CLI availability; report version pin failures.
- Secrets warning: detect `.ssh`, tokens, credential files and require explicit opt-in.

**Security / integrity**
- Optional archive encryption: prefer 7-Zip AES-256 when installed; otherwise offer DPAPI-protected secret storage only (not archive encryption) and explain the limitation.
- Checksums on demand; tamper/truncation detection in verify.

**Multi-user**
- Back up all local profiles (admin) with per-user folder mapping; restore into the matching profile or a chosen one.

**Notifications & UX extras**
- Tray icon with minimize-to-tray, progress in taskbar title, "open backup folder on finish" setting.
- "What changed since last backup" summary on the dashboard.

**Acceptance:** incremental + retention + scheduling demoed end-to-end; bootstrap script produces a working restore on a clean VM; restore conflict policies verified with fixtures.

---

## 8. Phase 5 — Hardening & packaging · ~2–4 days

- Unit tests: ignore rules, path mapping, manifest/verify, config migration, incremental diff, conflict policy.
- Integration test: synthetic tree → backup → verify → restore to temp → hash compare (Windows CI or local script).
- Failure injection: permission denied, locked file, disk full, missing winget/choco, killed process, network drop.
- Diagnostics bundle: version, OS build, admin state, last 200 log lines, manifest summary (redact paths on request).
- Packaging: PyInstaller one-file EXE with icon/version resource, optional code signing, portable ZIP, README + changelog.
- Crash safety: top-level exception hook writes a report and preserves logs; no silent `except: pass` in critical paths.
- Migration guarantee: v3 backups restore with v4; v4 manifest readable by older verify (graceful unknown fields).

---

## 9. Prioritized roadmap

| Priority | Work | Impact | Effort | Risk |
|---|---|---|---|---|
| P0 | Phase 1: live job bar, process monitor, streamed output, real progress | Very high (trust/visibility) | 3–4 d | Low |
| P0 | Phase 3 quick wins: batch UI updates, queue logger, log cap, remove double walk | High (responsiveness) | 1–2 d | Low |
| P1 | Phase 2: sidebar shell, dashboard, restore diff, settings, themes | High (perceived quality) | 4–6 d | Medium |
| P1 | Phase 3: bounded parallel copy, long paths, disk preflight, benchmark | High (speed) | 3–4 d | Medium |
| P2 | Catalog + job history + retention | Medium-high | 2–3 d | Low |
| P2 | Incremental backups + scheduling | High (large trees) | 4–6 d | Medium-high |
| P3 | Bootstrap script, conflict policies, alternate-folder restore | Medium | 2–3 d | Medium |
| P3 | VSS, encryption, multi-user | Medium (niche) | 3–5 d | High |

Total: roughly 3–4 weeks of focused work; P0 alone is ~1 week and delivers the biggest visible change.

---

## 10. Decisions needed

1. **UI toolkit** — Stay on tkinter/ttk (zero dependencies, current distribution) with a custom modern theme, or move to PySide6/Qt for a genuinely current look (+50–70 MB, new dependency, rewrite of UI layer). Recommendation: stay tkinter for portability; the sidebar/dashboard design above is achievable with ttk + custom canvases. Revisit Qt only if pixel-perfect modern visuals are a hard requirement.
2. **Encryption** — Standard library cannot do AES practically. Choose: 7-Zip AES-256 when installed (recommended, no new dependency) or add `cryptography` (stronger control, new dependency).
3. **VSS** — Admin-only and environment-sensitive. Recommendation: Phase 4 opt-in, never default.
4. **Incremental** — Adds restore-chain complexity. Recommendation: ship after catalog/retention and only with tests + a "promote to full" escape hatch.
5. **Concurrency default** — 4 workers for SSD, 2 for HDD, surfaced in Settings with "Auto".

---

## 11. Verification strategy

- **Correctness first:** every phase keeps backup/restore round-trip tests green (hash compare), including `.git` exclusion, locked files, long paths, and non-admin runs.
- **Performance:** before/after benchmark numbers on the same machine and disk; no speedup claims without data.
- **UI:** manual checklist per page on Windows 10 and 11, 100%/150%/200% scaling, keyboard-only pass, screen-reader labels present.
- **Restore safety:** dry-run/diff preview must match the actual restore plan; preflight warnings must appear in the UI and report.

---

## 12. Suggested first slice (what to implement next)

1. Event bus + queue-based logger + batched UI updates (foundation).
2. Pinned job bar with determinate overall/stage progress, MB/s, ETA.
3. Process tracker: streamed output for `pnputil`/`winget`/`choco`, PID/elapsed/status table, Stop kills the tree.
4. Remove the post-backup double scan and enforce the log cap.
5. Benchmark harness + baseline numbers.

That slice is self-contained, low-risk, and immediately visible: real progress, live commands, faster UI, and measured performance.
