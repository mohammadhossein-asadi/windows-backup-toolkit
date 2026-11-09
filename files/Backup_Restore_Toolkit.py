# -*- coding: utf-8 -*-
"""Windows Backup & Restore Toolkit (Python) — v3
Enhanced: parallel stages, deferred blob restore, resume tokens,
decorated progress/detail log, cancellation safety, ZIP/7-Zip archive,
cross-version plumbing, structured state, pluggable logger.

Run as Administrator for drivers / hosts / full restore.
"""

from __future__ import annotations

import atexit
import ctypes
import csv
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
)

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import hashlib
except ImportError:
    hashlib = None

VERSION = "3.1.0"

# ---------------------------------------------------------------------------
# Diagnostics / execution policy
# ---------------------------------------------------------------------------
_IS_RUNNING_ON_CI = bool(
    os.environ.get("GITHUB_ACTIONS")
    or os.environ.get("CI")
    or os.environ.get("TF_BUILD")
    or os.environ.get("BUILD_ID")
)

# Where robust, run io-bound stages concurrently; fall back to serial.
_MAX_CPU_COUNT = min(8, (os.cpu_count() or 1) + 4)
_NTHREADS = max(1, min(_MAX_CPU_COUNT, int(os.environ.get("BKUP_THREADS", "0") or "0") or 1))

_PNPUTIL = shutil.which("pnputil") or os.path.expandvars(
    r"%SystemRoot%\System32\pnputil.exe"
)
_POWERCFG = shutil.which("powercfg") or os.path.expandvars(r"%SystemRoot%\System32\powercfg.exe")
_WINREG = "winreg"
_REG = shutil.which("reg") or os.path.expandvars(r"%SystemRoot%\System32\reg.exe")
_NETSH = shutil.which("netsh") or os.path.expandvars(r"%SystemRoot%\System32\netsh.exe")
_SYSTEMINFO = shutil.which("systeminfo") or os.path.expandvars(r"%SystemRoot%\System32\systeminfo.exe")

# Known verbose / dangerous / self-referential commands, blocked before spawning.
_BLOCKED_SHELL_PATTERNS = [
    re.compile(r"(?i)(?<![\w-])reg\s+(import|load|restore|save|export)\b"),
    re.compile(r"(?i)\bpowercfg\s+/(?:s|setacvalue|/setdcvalue)\b"),
    re.compile(r"(?i)\bpowercfg\s+/?h\b"),
    re.compile(r"(?i)(?<![\w-])netsh\s+(wlan\s+add\s+profile|wlan\s+export)\b"),
    re.compile(r"(?i)\bxnostartmenu\b|\bxquickrespring\b|\bxkcseed\b"),
    re.compile(r"\.(py|pyw|ps1|bat|cmd|com|scr|vbs|js|jse|vbw|ws|wsf)\s*$", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def _expand_env_var(value: str) -> str:
    """Expand %VAR% and $env:VAR style tokens, then normalize surrounding quotes."""
    if not value:
        return value
    value = os.path.expandvars(value)
    value = os.path.expanduser(value)
    value = value.replace("${", "$").replace("}", "")
    value = re.sub(r"(?:^['\"]|['\"]$)+", "", value)
    return value


def long_path(p: Path) -> str:
    """Return a Windows extended-length path for deep paths; a plain path elsewhere."""
    text = os.fspath(p)
    if os.name != "nt":
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def get_desktop() -> Path:
    """Resolve real Desktop (handles OneDrive / redirection)."""
    candidates: List[Path] = []
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            for name in ("Desktop", "Personal"):
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                expanded = _expand_env_var(val)
                if expanded:
                    candidates.append(Path(expanded))
    except Exception:
        pass

    for base in (Path.home(), Path.home() / "OneDrive", Path.home() / "Documents"):
        for name in ("Desktop", "Romaizi", "רומיזי", "Desktop"):
            candidate = base / name
            if candidate.exists():
                candidates.append(candidate)

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    return Path.home().resolve()


DESKTOP = get_desktop()
CHECKPOINT_FILE = DESKTOP / "WindowsBackup_Checkpoint.json"

# Remembered restore sources (persisted between runs).
RECENT_SOURCES_FILE = (
    Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
    / "WindowsBackupToolkit"
    / "recent_sources.json"
)
MAX_RECENT_SOURCES = 25

# Persisted UI preferences.
SETTINGS_FILE = (
    Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
    / "WindowsBackupToolkit"
    / "settings.json"
)
def normalize_skip_list(value: Any) -> List[str]:
    """Normalise comma/semicolon/newline separated names into a sorted unique list."""
    if isinstance(value, str):
        parts = []
        for chunk in value.replace(";", ",").splitlines():
            parts.extend(chunk.split(","))
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        return []
    cleaned: List[str] = []
    for part in parts:
        name = Path(str(part).strip()).name.strip().lower()
        if name and name not in cleaned:
            cleaned.append(name)
    return sorted(cleaned)


def normalize_backup_mode(value: Any, incremental_compat: Any = None) -> str:
    v = str(value or "").strip().lower()
    if v in {"compact", "changed", "changed-only", "changed_only", "delta"}:
        return "compact"
    if v in {"incremental", "hardlink", "hardlinks"}:
        return "incremental"
    if v in {"full", "complete", "normal", ""}:
        if bool(incremental_compat):
            return "incremental"
        return "full"
    if bool(incremental_compat):
        return "incremental"
    return "full"


def normalize_skip_extensions(value: Any) -> List[str]:
    """Normalise extensions like ISO, .vmdk, *.mp4 into sorted unique tokens (no dot)."""
    if isinstance(value, str):
        parts = []
        for chunk in value.replace(";", ",").splitlines():
            parts.extend(chunk.split(","))
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        return []
    cleaned: List[str] = []
    for part in parts:
        ext = str(part).strip().lower()
        if not ext:
            continue
        ext = ext.lstrip("*")
        if ext.startswith("."):
            ext = ext[1:]
        ext = Path(ext).suffix.lstrip(".") or ext
        ext = ext.strip(".")
        if ext and ext not in cleaned:
            cleaned.append(ext)
    return sorted(cleaned)


DEFAULT_SETTINGS: Dict[str, Any] = {
    "theme": "dark",
    "copy_workers": 4,
    "incremental": False,
    "backup_mode": "full",
    "skip_dirs": [],
    "skip_files": [],
    "skip_extensions": [],
    "retention_enabled": False,
    "retention_keep_last": 5,
    "retention_max_age_days": 0,
    "retention_auto_after_backup": False,
    "window_geometry": "",
    "last_page": "Dashboard",
    "backup_parent": "",
}

MAX_LOG_LINES = 20000

# Better defaults for the common "always copy this much" sets.
USER_FOLDERS = [
    "Documents", "Desktop", "Pictures", "Videos", "Music",
    "Favorites", "Links", "Saved Games", "Searches",
]
EXCLUDE_TOP_FOLDERS = {"Downloads"}
ROAMING_FOLDERS = [
    r"Microsoft\Windows\Start Menu",
    r"Microsoft\Windows\Recent",
    r"Microsoft\Windows\SendTo",
    "Code", "Code - Insiders", "Notepad++",
    "Telegram Desktop", "discord", "Slack", "Spotify",
    "Zoom", r"Microsoft\Office",
    "obs-studio", "ShareX", "Everything",
    "PotPlayerMini64", "qBittorrent", "FileZilla",
    "WinRAR", "7-Zip",
]
LOCAL_FOLDERS = [r"Microsoft\Windows\Explorer"]
EXTRA_PROFILE_ITEMS = [".ssh", ".gitconfig", ".wslconfig"]

IGNORE_DIR_NAMES: Set[str] = {
    "node_modules", ".git", ".svn", ".hg", ".bzr",
    "bower_components", "vendor",
    "bin", "obj", "build", "dist", "out", "target",
    "debug", "release",
    ".vs", ".idea", ".vscode",
    "__pycache__", ".pytest_cache", ".mypy_cache",
    ".next", ".nuxt", ".cache", "cache", "cacheddata",
    "code cache", "gpucache", "shadercache", "shader cache",
    "service worker", "serviceworker",
    "temp", "tmp", "logs", "log",
    ".parcel-cache", ".turbo", "coverage",
    ".gradle", ".nuget",
    "ipch",
    "node-gyp", ".yarn", ".pnpm-store", "pnpm-store",
    "bower", "jspm_packages",
    ".tox", ".eggs",
    "site-packages",
    "blob_storage", "databases",
    "indexeddb", "file system",
    "crashpad", "crash dumps", "minidump",
    "media cache", "video decode",
}
IGNORE_DIR_SUFFIXES = (".egg-info",)
# Names ignored regardless of whether they appear as files or directories during backup copy.
IGNORE_ENTRY_NAMES = {".git"}
IGNORE_FILE_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
INTEGRITY_FILE = "integrity.json"
INTEGRITY_META_FILES = {INTEGRITY_FILE, "manifest.json", "performance.json", "selection.json", "BackupRestore.log"}
SKIP_FILE_NAMES = {"desktop.ini"}

ALL_STAGES = ["Drivers", "Packages", "UserData", "Bookmarks", "Settings", "CodingTools"]

# Structured log level names
LOG_LEVELS = ("DEBUG", "INFO", "OK", "WARN", "ERROR", "FATAL")

# Retry configuration (filesystem driven steps only)
DEFAULT_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0)
MAX_FS_RETRIES = 3


def _is_ci_like() -> bool:
    return _IS_RUNNING_ON_CI


def _is_terminal_passthrough() -> bool:
    """Best-effort: are we writing to a real console?"""
    if _is_ci_like():
        return False
    return sys.stderr.isatty() or sys.stdout.isatty()


def _get_status_line() -> str:
    if _is_ci_like() or not _is_terminal_passthrough():
        return ""
    try:
        if os.name == "nt":
            return f"\r\033[?25h\033[K"
        return f"\r\033[?25h\033[2K"
    except Exception:
        return ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def now_local() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, n))
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} B"
    return f"{value:0.1f} {units[idx]}"


def format_speed(bps: float) -> str:
    return f"{format_bytes(bps)}/s" if bps > 0 else ""


def format_timedelta(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m {secs}s"


def raise_if_blocked_shell(cmd: str, label: str) -> None:
    """Refuse to execute commands that are known to be self-destructive or noisy."""
    if _is_ci_like() or not _is_terminal_passthrough():
        return
    lowered = cmd.lower()
    for pattern in _BLOCKED_SHELL_PATTERNS:
        if pattern.search(lowered):
            raise RuntimeError(
                f"{label}: blocked cmd because of pattern {pattern.pattern!r}"
            )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class BackupLog:
    """Thread-safe logger with UI output and optional session log file(s)."""

    def __init__(self, log_file: Optional[Path] = None, stdout: Optional[Callable[[str], None]] = None):
        self._stdout = stdout or (lambda s: None)
        self._lock = threading.Lock()
        self._log_files: List[Path] = []
        if log_file is not None:
            self._log_files.append(Path(log_file))
        self._total = 0
        self._done = 0
        self._detail: List[Tuple[str, str]] = []
        self._detail_limit = 2000

    @property
    def log_file(self) -> Optional[Path]:
        return self._log_files[0] if self._log_files else None

    @log_file.setter
    def log_file(self, value: Optional[Path]) -> None:
        if value is None:
            self._log_files = []
        else:
            p = Path(value)
            self._log_files = [p] + [x for x in self._log_files if x != p]

    def add_log_file(self, path: Path) -> None:
        p = Path(path)
        with self._lock:
            if p not in self._log_files:
                self._log_files.append(p)

    def session_log_path(self) -> Optional[Path]:
        return self.log_file

    def _write(self, level: str, msg: str, indent: int = 0) -> None:
        line = f"[{now_local()}] [{level}] {msg}"
        if indent:
            line = "  " * indent + line
        try:
            self._stdout(line)
        except Exception:
            pass
        with self._lock:
            for lf in list(self._log_files):
                try:
                    lf.parent.mkdir(parents=True, exist_ok=True)
                    with open(lf, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except Exception:
                    pass

    def _record(self, level: str, msg: str, indent: int = 0) -> None:
        self._write(level, msg, indent)
        if level in ("DEBUG", "INFO", "OK", "WARN", "ERROR", "FATAL"):
            with self._lock:
                if len(self._detail) >= self._detail_limit:
                    self._detail.pop(0)
                self._detail.append((level, msg))

    def _save_detail_to_file(self, path: Path) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                for level, msg in self._detail:
                    f.write(f"[{level}] {msg}\n")
        except Exception:
            pass

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = total

    def inc(self, n: int = 1) -> int:
        with self._lock:
            self._done += n
            done, total = self._done, self._total
        if total > 0 and done % max(1, total // 8) == 0:
            self._stdout(f"\rProgress: {done}/{total} ({done*100//max(1,total)}%)")

    def finish_progress(self) -> None:
        self._stdout(_get_status_line())

    def _dpi(self, level: str, msg: str, indent: int = 0, **extra) -> None:
        if level == "DEBUG" and not _is_terminal_passthrough():
            return
        self._record(level, msg, indent)

    def debug(self, msg: str, **extra) -> None:
        self._dpi("DEBUG", msg, **extra)

    def info(self, msg: str, **extra) -> None:
        self._record("INFO", msg)

    def ok(self, msg: str, **extra) -> None:
        self._record("OK", msg)

    def warn(self, msg: str, **extra) -> None:
        self._record("WARN", msg)

    def error(self, msg: str, **extra) -> None:
        self._record("ERROR", msg)

    def fatal(self, msg: str, **extra) -> None:
        self._record("FATAL", msg)

    def log_line(self, level: str, msg: str, indent: int = 0) -> None:
        self._record(level, msg, indent)

    def progress(self, msg: str, total: Optional[int] = None, current: Optional[int] = None, suffix: str = "") -> None:
        if total is not None and current is not None:
            self._stdout(f"\rProgress: {current}/{total} {suffix}")

    def header(self, title: str, width: int = 64) -> None:
        self._record("INFO", "=" * width)
        self._record("INFO", f" {title}")
        self._record("INFO", "=" * width)

    def footer(self, title: str, width: int = 64) -> None:
        self._record("INFO", "=" * width)
        self._record("INFO", f" {title}")
        self._record("INFO", "=" * width)

    def write(self, level: str, msg: str, indent: int = 0) -> None:
        if level not in LOG_LEVELS:
            level = "INFO"
        self._record(level, msg, indent)


class BackupToolkit:
    def __init__(self, logger: Optional[Any] = None):
        if isinstance(logger, BackupLog):
            self.log_obj = logger
        elif callable(logger):
            self.log_obj = BackupLog(None, stdout=logger)
        else:
            self.log_obj = BackupLog(None)
        self.backup_root: Optional[Path] = None
        self.log_file: Optional[Path] = None
        self.is_running = False
        self.is_paused = False
        self.is_cancelled = False
        self.completed_stages: List[str] = []
        self.failed_stages: List[str] = []
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._startup_lock = threading.Lock()
        self._started = False
        self._started_at: Optional[float] = None
        self._total_files = 0
        self._done_files = 0
        self._copy_executor: Optional[ThreadPoolExecutor] = None
        self._job_files_done = 0
        self._job_bytes_done = 0
        try:
            self.copy_workers = max(1, min(16, int(os.environ.get("BKUP_THREADS", "4"))))
        except Exception:
            self.copy_workers = 4
        self.extra_skip_dirs: Set[str] = set()
        self.extra_skip_files: Set[str] = set()
        self.extra_skip_extensions: Set[str] = set()
        self.incremental_enabled = False
        self.backup_mode = "full"
        self._incremental_base: Optional[Path] = None
        self._base_backup: Optional[Path] = None
        self._linked_files = 0
        self._skipped_files = 0
        self._compact_skipped = 0
        self._copied_bytes = 0
        self._linked_bytes = 0
        self._skipped_existing_bytes = 0
        self._compact_skipped_bytes = 0

        # ---- live progress state ----
        self._progress_lock = threading.Lock()
        self._job_kind = ""
        self._stage = ""
        self._stage_phase = "idle"
        self._stage_index = 0
        self._stage_count = 0
        self._stage_files_done = 0
        self._stage_files_total = 0
        self._stage_bytes_done = 0
        self._stage_bytes_total = 0
        self._current_item = ""
        self._job_started = time.perf_counter()
        self._stage_started = time.perf_counter()
        self._speed_bps = 0.0
        self._speed_sample_t = time.perf_counter()
        self._speed_sample_bytes = 0

        # ---- process tracker (subprocesses with live output) ----
        self._process_lock = threading.Lock()
        self._processes: Dict[int, Dict[str, Any]] = {}
        self._process_order: List[int] = []

    # ---------- live progress ----------
    def begin_job(self, kind: str, stage_count: int) -> None:
        with self._progress_lock:
            self._job_kind = kind
            self._stage_count = max(1, int(stage_count))
            self._stage_index = 0
            self._job_files_done = 0
            self._job_bytes_done = 0
            self._incremental_base = None
            self._base_backup = None
            self._linked_files = 0
            self._skipped_files = 0
            self._compact_skipped = 0
            self._copied_bytes = 0
            self._linked_bytes = 0
            self._skipped_existing_bytes = 0
            self._compact_skipped_bytes = 0
            self._stage = ""
            self._stage_phase = "starting"
            self._stage_files_done = 0
            self._stage_files_total = 0
            self._stage_bytes_done = 0
            self._stage_bytes_total = 0
            self._current_item = ""
            self._job_started = time.perf_counter()
            self._stage_started = time.perf_counter()
            self._speed_bps = 0.0
            self._speed_sample_t = time.perf_counter()
            self._speed_sample_bytes = 0

    def begin_stage(self, name: str, phase: str = "working") -> None:
        with self._progress_lock:
            self._stage_index += 1
            self._stage = name
            self._stage_phase = phase
            self._stage_files_done = 0
            self._stage_files_total = 0
            self._stage_bytes_done = 0
            self._stage_bytes_total = 0
            self._current_item = ""
            self._stage_started = time.perf_counter()
            self._speed_bps = 0.0
            self._speed_sample_t = time.perf_counter()
            self._speed_sample_bytes = 0

    def end_stage(self, phase: str = "done") -> None:
        with self._progress_lock:
            self._stage_phase = phase
            if self._stage_bytes_total > 0:
                self._stage_bytes_done = self._stage_bytes_total
            if self._stage_files_total > 0:
                self._stage_files_done = self._stage_files_total

    def add_stage_totals(self, files: int, total_bytes: int) -> None:
        with self._progress_lock:
            self._stage_files_total += max(0, int(files))
            self._stage_bytes_total += max(0, int(total_bytes))

    def set_phase(self, phase: str, item: str = "") -> None:
        with self._progress_lock:
            self._stage_phase = phase
            if item:
                self._current_item = item

    def progress_add(self, nbytes: int = 0, files: int = 0, item: str = "") -> None:
        with self._progress_lock:
            self._stage_bytes_done += max(0, int(nbytes))
            self._stage_files_done += max(0, int(files))
            self._job_bytes_done += max(0, int(nbytes))
            self._job_files_done += max(0, int(files))
            if item:
                self._current_item = item
            now = time.perf_counter()
            dt = now - self._speed_sample_t
            if dt >= 0.5:
                inst = (self._stage_bytes_done - self._speed_sample_bytes) / dt
                if inst > 0:
                    self._speed_bps = inst if self._speed_bps <= 0 else (0.65 * self._speed_bps + 0.35 * inst)
                self._speed_sample_t = now
                self._speed_sample_bytes = self._stage_bytes_done

    def progress_snapshot(self) -> Dict[str, Any]:
        with self._progress_lock:
            stage_frac = 0.0
            if self._stage_bytes_total > 0:
                stage_frac = min(1.0, self._stage_bytes_done / self._stage_bytes_total)
            elif self._stage_files_total > 0:
                stage_frac = min(1.0, self._stage_files_done / self._stage_files_total)
            overall = 0.0
            if self._stage_count > 0:
                overall = min(1.0, (max(0, self._stage_index - 1) + stage_frac) / self._stage_count)
            eta = None
            if self._speed_bps > 1024 and self._stage_bytes_total > 0:
                eta = max(0.0, (self._stage_bytes_total - self._stage_bytes_done) / self._speed_bps)
            return {
                "running": bool(self.is_running),
                "kind": self._job_kind,
                "stage": self._stage,
                "phase": self._stage_phase,
                "stage_index": self._stage_index,
                "stage_count": self._stage_count,
                "stage_percent": stage_frac * 100.0,
                "overall_percent": overall * 100.0,
                "has_totals": self._stage_bytes_total > 0 or self._stage_files_total > 0,
                "files_done": self._stage_files_done,
                "files_total": self._stage_files_total,
                "bytes_done": self._stage_bytes_done,
                "bytes_total": self._stage_bytes_total,
                "job_files_done": self._job_files_done,
                "job_bytes_done": self._job_bytes_done,
                "speed_bps": self._speed_bps,
                "eta_s": eta,
                "current_item": self._current_item,
                "stage_elapsed": time.perf_counter() - self._stage_started,
                "job_elapsed": time.perf_counter() - self._job_started,
            }

    def scan_totals(self, paths: Iterable[Path]) -> Tuple[int, int]:
        """Fast scan (no copy) to estimate file count and bytes for ETA."""
        files = 0
        total = 0
        stack = [Path(p) for p in paths]
        while stack:
            if self.is_cancelled:
                raise RuntimeError("Cancelled by user")
            p = stack.pop()
            try:
                if p.is_file():
                    files += 1
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
                    continue
                with os.scandir(str(p)) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if should_ignore_dir(entry.name) or entry.is_symlink():
                                    continue
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                if entry.name.lower() in IGNORE_FILE_NAMES:
                                    continue
                                files += 1
                                try:
                                    total += entry.stat(follow_symlinks=False).st_size
                                except OSError:
                                    pass
                        except OSError:
                            continue
            except OSError:
                continue
        return files, total

    # ---------- process tracker ----------
    def _register_process(self, proc: subprocess.Popen, label: str, cmd_text: str) -> None:
        rec = {
            "pid": proc.pid,
            "label": label,
            "command": cmd_text,
            "started": time.time(),
            "finished": None,
            "status": "running",
            "exit_code": None,
            "last_line": "",
            "output": [],
            "proc": proc,
        }
        with self._process_lock:
            self._processes[proc.pid] = rec
            self._process_order.append(proc.pid)
            while len(self._process_order) > 40:
                old = self._process_order.pop(0)
                rec_old = self._processes.get(old)
                if rec_old is not None and rec_old.get("status") != "running":
                    self._processes.pop(old, None)
        self.log_obj.info(f"[{label}] started (PID {proc.pid}): {cmd_text}")

    def _update_process(self, pid: int, line: str) -> None:
        with self._process_lock:
            rec = self._processes.get(pid)
            if rec is None:
                return
            rec["last_line"] = line
            out = rec["output"]
            out.append(line)
            if len(out) > 400:
                del out[:-400]

    def _finish_process(self, pid: int, rc: int, status: Optional[str] = None) -> None:
        with self._process_lock:
            rec = self._processes.get(pid)
            if rec is None:
                return
            rec["exit_code"] = rc
            rec["finished"] = time.time()
            rec["status"] = status or ("ok" if rc == 0 else "failed")
            label = rec["label"]
        self.log_obj.info(f"[{label}] finished (PID {pid}, exit {rc})")

    def process_snapshot(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._process_lock:
            rows = []
            for rec in self._processes.values():
                end = rec.get("finished") or now
                rows.append({
                    "pid": rec["pid"],
                    "label": rec["label"],
                    "status": rec["status"],
                    "elapsed": max(0.0, end - rec["started"]),
                    "exit_code": rec.get("exit_code"),
                    "last_line": rec["last_line"],
                    "command": rec["command"],
                })
        rows.sort(key=lambda r: (r["status"] != "running", -r["elapsed"]))
        return rows

    def process_output(self, pid: int) -> List[str]:
        with self._process_lock:
            rec = self._processes.get(pid)
            return list(rec["output"]) if rec else []

    def _kill_process_tree(self, proc: Optional[subprocess.Popen]) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=_no_window_flags(),
                )
            else:
                proc.terminate()
        except Exception as e:
            self.log_obj.warn(f"Could not stop PID {proc.pid}: {e}")
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _kill_all_processes(self) -> None:
        with self._process_lock:
            procs = [rec.get("proc") for rec in self._processes.values() if rec.get("status") == "running"]
        for proc in procs:
            self._kill_process_tree(proc)

    # ---------- lifecycle ----------
    def _ensure_started(self) -> None:
        with self._startup_lock:
            if self._started:
                return
            self._started = True
            atexit.register(self._cleanup_on_exit)

    def _cleanup_on_exit(self) -> None:
        if self._copy_executor is not None:
            self._copy_executor.shutdown(wait=False, cancel_futures=True)
        self._save_state_to_file()

    def pause(self):
        self.is_paused = True
        self._pause_event.clear()
        self.log_obj.warn("PAUSED — click Resume to continue")

    def resume(self):
        self.is_paused = False
        self._pause_event.set()
        self.log_obj.info("RESUMED")

    def stop(self):
        self.is_cancelled = True
        self.is_paused = False
        self._pause_event.set()
        self.log_obj.warn("STOP requested — stopping current process")
        self._kill_all_processes()

    def wait_if_paused(self) -> None:
        while self.is_paused and not self.is_cancelled:
            self._pause_event.wait(timeout=0.3)
        if self.is_cancelled:
            raise RuntimeError("Cancelled by user")

    def _ensure_dir(self, p: Path) -> None:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            if os.name != "nt":
                raise
            os.makedirs(long_path(p), exist_ok=True)

    # ---------- execution helpers ----------
    def _run(self, args: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return self._run_streaming(args, timeout=timeout, shell=False)

    def _run_shell(self, cmd: str, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return self._run_streaming(cmd, timeout=timeout, shell=True)

    def _run_streaming(self, args: Any, timeout: Optional[int] = None, shell: bool = False) -> subprocess.CompletedProcess:
        """Run a command with live output, PID tracking, cancellation and timeout."""
        self.wait_if_paused()
        if shell:
            cmd_display = str(args)
            popen_args: Any = str(args)
            label = "shell"
        else:
            arg_list = [str(a) for a in args]
            cmd_display = subprocess.list2cmdline(arg_list) if os.name == "nt" else " ".join(arg_list)
            popen_args = arg_list
            label = Path(arg_list[0]).name if arg_list else "command"
            if label.lower().endswith(".exe"):
                label = label[:-4]
        try:
            proc = subprocess.Popen(
                popen_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                shell=shell,
                creationflags=_no_window_flags(),
            )
        except FileNotFoundError as e:
            self.log_obj.error(f"Command not found: {cmd_display} ({e})")
            raise
        self._register_process(proc, label, cmd_display)
        self.set_phase(f"running {label}", cmd_display)

        out_lines: List[str] = []
        reader_error: List[str] = []

        def _reader() -> None:
            try:
                if proc.stdout is None:
                    return
                for raw in proc.stdout:
                    line = raw.rstrip("\r\n")
                    if not line:
                        continue
                    out_lines.append(line)
                    self._update_process(proc.pid, line)
            except Exception as e:
                reader_error.append(str(e))

        reader = threading.Thread(target=_reader, name=f"proc-{proc.pid}", daemon=True)
        reader.start()
        deadline = time.perf_counter() + timeout if timeout else None
        rc: Optional[int] = None
        status: Optional[str] = None
        try:
            while True:
                if self.is_cancelled:
                    self._kill_process_tree(proc)
                    status = "stopped"
                    self.log_obj.warn(f"Stopped [{label}] (PID {proc.pid})")
                    break
                if self.is_paused:
                    self._pause_event.wait(0.2)
                    continue
                rc = proc.poll()
                if rc is not None:
                    break
                if deadline is not None and time.perf_counter() >= deadline:
                    self.log_obj.error(f"Command timed out after {timeout}s: {cmd_display}")
                    self._kill_process_tree(proc)
                    status = "timeout"
                    rc = -1
                    break
                time.sleep(0.15)
        finally:
            try:
                reader.join(timeout=3)
            except Exception:
                pass
            if rc is None:
                rc = proc.poll()
                if rc is None:
                    rc = -1
            self._finish_process(proc.pid, rc, status)
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass
        output = "\n".join(out_lines)
        if reader_error:
            output += ("\n" if output else "") + f"[reader error: {reader_error[0]}]"
        return subprocess.CompletedProcess(args, rc if rc is not None else -1, stdout=output, stderr="")

    # ---------- file copy with retry, progress, and cancellation ----------
    def copy_tree_safe(self, src: Path, dst: Path, label: str, logger: Optional[BackupLog] = None,
                       skip_top_names: Optional[Set[str]] = None,
                       skip_top_prefixes: Tuple[str, ...] = ()) -> None:
        log = logger or self.log_obj
        if not src.exists():
            log.ok(f"Skip (missing): {label}")
            return
        self.wait_if_paused()
        log.info(f"Copying {label}...")
        try:
            if src.is_file():
                try:
                    size = src.stat().st_size
                except OSError:
                    size = 0
                self.add_stage_totals(1, size)
                self._ensure_dir(dst.parent)
                self._copy_file_one(src, dst, label, size=size)
            else:
                file_jobs, dirs, files, total = self.scan_tree(
                    src, dst, label, skip_top_names=skip_top_names, skip_top_prefixes=skip_top_prefixes
                )
                self.add_stage_totals(files, total)
                self.set_phase("copying", str(src))
                log.debug(f"{label}: {files} files, {format_bytes(total)} to copy")
                for folder in dirs:
                    self._ensure_dir(folder)
                self._copy_file_jobs(file_jobs)
            log.ok(f"OK: {label}")
        except RuntimeError:
            raise
        except Exception as e:
            log.error(f"Failed (continuing): {label} — {e}")
        self.wait_if_paused()

    def scan_tree(self, src: Path, dst: Path, label: str,
                  skip_top_names: Optional[Set[str]] = None,
                  skip_top_prefixes: Tuple[str, ...] = ()) -> Tuple[List[Tuple[Path, Path, str, int]], List[Path], int, int]:
        """Walk a tree once and return (file jobs, folders, file count, total bytes).

        skip_top_names / skip_top_prefixes filter entries in the source root only;
        nested levels keep the normal ignore rules.
        """
        file_jobs: List[Tuple[Path, Path, str, int]] = []
        directories: List[Path] = [Path(dst)]
        root_skip_names = skip_top_names or set()
        extra_dirs = getattr(self, "extra_skip_dirs", set())
        extra_files = getattr(self, "extra_skip_files", set())
        extra_exts = getattr(self, "extra_skip_extensions", set())
        stack: List[Tuple[Path, Path, str, int]] = [(Path(src), Path(dst), label, 0)]
        while stack:
            if self.is_cancelled:
                raise RuntimeError("Cancelled by user")
            cur_src, cur_dst, cur_label, depth = stack.pop()
            try:
                with os.scandir(long_path(cur_src)) as it:
                    entries = list(it)
            except OSError as e:
                self.log_obj.warn(f"Cannot read {cur_src}: {e}")
                continue
            for entry in entries:
                if self.is_cancelled:
                    raise RuntimeError("Cancelled by user")
                try:
                    if depth == 0 and (
                        entry.name in root_skip_names
                        or any(entry.name.startswith(prefix) for prefix in skip_top_prefixes)
                    ):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if should_ignore_dir(entry.name) or entry.name.lower() in extra_dirs or entry.is_symlink():
                            continue
                        child_dst = cur_dst / entry.name
                        directories.append(child_dst)
                        stack.append((Path(entry.path), child_dst, f"{cur_label}\\{entry.name}", depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        low = entry.name.lower()
                        ext = Path(low).suffix.lstrip(".")
                        if low in IGNORE_FILE_NAMES or low in extra_files or (ext and ext in extra_exts):
                            continue
                        try:
                            size = entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            size = 0
                        file_jobs.append((Path(entry.path), cur_dst / entry.name, f"{cur_label}\\{entry.name}", size))
                except OSError:
                    continue
        total = sum(job[3] for job in file_jobs)
        return file_jobs, directories, len(file_jobs), total

    def _copy_file_jobs(self, jobs: List[Tuple[Path, Path, str, int]], max_workers: Optional[int] = None) -> None:
        """Copy a scanned file list with a bounded pool; one worker stays serial."""
        if not jobs:
            return
        workers = int(max_workers if max_workers is not None else (self.copy_workers or 1))
        workers = max(1, min(16, workers, len(jobs)))
        if workers == 1:
            for src, dst, label, size in jobs:
                if self.is_cancelled:
                    raise RuntimeError("Cancelled by user")
                self.wait_if_paused()
                try:
                    self._copy_file_one(src, dst, label, size=size)
                except RuntimeError:
                    raise
                except Exception as e:
                    self.log_obj.error(f"Copy failed: {label} — {e}")
            return

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="copy") as pool:
            pending = set()
            index = 0
            while index < len(jobs) or pending:
                while index < len(jobs) and len(pending) < workers * 4:
                    src, dst, label, size = jobs[index]
                    index += 1
                    pending.add(pool.submit(self._copy_file_one, src, dst, label, size))
                if self.is_cancelled:
                    for future in pending:
                        future.cancel()
                    raise RuntimeError("Cancelled by user")
                if self.is_paused:
                    self._pause_event.wait(0.2)
                    continue
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        future.result()
                    except RuntimeError:
                        raise
                    except Exception as e:
                        self.log_obj.error(f"Parallel copy failed: {e}")

    @staticmethod
    def _same_file(a: Path, b: Path) -> bool:
        """True when size and mtime match closely (copy2 preserves mtime)."""
        try:
            sa = a.stat()
            sb = b.stat()
        except OSError:
            return False
        if sa.st_size != sb.st_size:
            return False
        try:
            if b.is_dir():
                return False
        except OSError:
            return False
        return abs(sa.st_mtime - sb.st_mtime) <= 2.0

    @staticmethod
    def _manifest_get(manifest: Dict[str, Any], *keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in manifest and manifest.get(key) not in (None, ""):
                return manifest.get(key)
        return default

    def _backup_mode_name(self) -> str:
        mode = str(getattr(self, "backup_mode", "") or "").strip().lower()
        if mode == "compact":
            return "compact"
        if mode == "incremental" or bool(getattr(self, "incremental_enabled", False)):
            return "incremental"
        return "full"

    def _reuse_existing(self, src: Path, dst: Path, size: int) -> bool:
        """Resume an interrupted copy, hardlink unchanged files (incremental), or omit unchanged files (compact)."""
        try:
            if dst.exists() and self._same_file(src, dst):
                self.progress_add(nbytes=size, files=1, item=str(src))
                with self._progress_lock:
                    self._skipped_files += 1
                    self._skipped_existing_bytes += max(0, int(size or 0))
                return True
        except OSError:
            pass
        base = getattr(self, "_base_backup", None) or getattr(self, "_incremental_base", None)
        if base is None or self.backup_root is None:
            return False
        try:
            ref = base / dst.relative_to(self.backup_root)
            if not ref.exists() or not self._same_file(src, ref):
                return False
            mode = self._backup_mode_name()
            if mode == "compact":
                self.progress_add(nbytes=size, files=1, item=str(src))
                with self._progress_lock:
                    self._compact_skipped += 1
                    self._compact_skipped_bytes += max(0, int(size or 0))
                self.log_obj.detail(f"Compact: unchanged, omitted from delta — {src}")
                return True
            self._ensure_dir(dst.parent)
            os.link(long_path(ref), long_path(dst))
            self.progress_add(nbytes=size, files=1, item=str(src))
            with self._progress_lock:
                self._linked_files += 1
                self._linked_bytes += max(0, int(size or 0))
            return True
        except (OSError, ValueError):
            return False

    def _copy_file_one(self, src: Path, dst: Path, label: str, size: Optional[int] = None) -> None:
        log = self.log_obj
        if size is None:
            try:
                size = src.stat().st_size
            except OSError:
                size = 0
        if self._reuse_existing(src, dst, size):
            return
        for attempt in range(MAX_FS_RETRIES):
            if self.is_cancelled:
                raise RuntimeError("Cancelled by user")
            try:
                self._ensure_dir(dst.parent)
                shutil.copy2(long_path(src), long_path(dst))
                self.progress_add(nbytes=size, files=1, item=str(src))
                with self._progress_lock:
                    self._copied_bytes = int(getattr(self, "_copied_bytes", 0) or 0) + max(0, int(size or 0))
                return
            except (OSError, IOError) as e:
                if attempt == MAX_FS_RETRIES - 1:
                    raise
                delay = DEFAULT_BACKOFF_SECONDS[min(attempt, len(DEFAULT_BACKOFF_SECONDS) - 1)]
                log.warn(f"Retry {attempt+1}/{MAX_FS_RETRIES} for {label}: {e}")
                time.sleep(delay)

    def _copy_tree(self, src: Path, dst: Path, label: str) -> None:
        """Iterative copy with per-file progress (skips ignored names, junctions)."""
        log = self.log_obj
        self._ensure_dir(dst)
        stack: List[Tuple[Path, Path, str]] = [(src, dst, label)]
        while stack:
            cur_src, cur_dst, cur_label = stack.pop()
            self.wait_if_paused()
            try:
                with os.scandir(str(cur_src)) as it:
                    entries = list(it)
            except OSError as e:
                log.warn(f"Cannot read {cur_src}: {e}")
                continue
            for entry in entries:
                if self.is_cancelled:
                    raise RuntimeError("Cancelled by user")
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if should_ignore_dir(entry.name) or entry.is_symlink():
                            continue
                        child_dst = cur_dst / entry.name
                        self._ensure_dir(child_dst)
                        stack.append((Path(entry.path), child_dst, f"{cur_label}\\{entry.name}"))
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name.lower() in IGNORE_FILE_NAMES:
                            continue
                        self._copy_file_one(Path(entry.path), cur_dst / entry.name, f"{cur_label}\\{entry.name}")
                except RuntimeError:
                    raise
                except Exception as e:
                    log.error(f"item failed: {cur_label}\\{entry.name} — {e}")

    # ---------- checkpoint / state ----------
    def save_checkpoint(self) -> None:
        data = {
            "BackupRoot": str(self.backup_root) if self.backup_root else "",
            "Completed": list(self.completed_stages),
            "Failed": list(self.failed_stages),
            "Time": now_local(),
            "Version": VERSION,
            "Cancelled": self.is_cancelled,
        }
        try:
            CHECKPOINT_FILE.write_text(json_dumps(data), encoding="utf-8")
        except Exception as e:
            self.log_obj.warn(f"Checkpoint save failed: {e}")

    def clear_checkpoint(self) -> None:
        try:
            if CHECKPOINT_FILE.exists():
                CHECKPOINT_FILE.unlink()
        except Exception:
            pass

    @staticmethod
    def load_checkpoint() -> Optional[Dict[str, Any]]:
        if not CHECKPOINT_FILE.exists():
            return None
        try:
            return json_loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None


    @staticmethod
    def check_hardlink_support(src_dir: Path, dst_dir: Path) -> Tuple[bool, str]:
        """Return (supported, reason). Hardlinks require same filesystem/volume."""
        src_dir = Path(src_dir)
        dst_dir = Path(dst_dir)
        src_dir.mkdir(parents=True, exist_ok=True)
        dst_dir.mkdir(parents=True, exist_ok=True)
        probe = src_dir / f".wbk-hardlink-probe-{os.getpid()}-{time.time_ns()}.tmp"
        link = dst_dir / f".wbk-hardlink-link-{os.getpid()}-{time.time_ns()}.tmp"
        try:
            probe.write_bytes(b"probe")
            os.link(str(probe), str(link))
            try:
                same = link.exists() and link.read_bytes() == b"probe"
            except Exception:
                same = link.exists()
            return bool(same), ""
        except OSError as e:
            reason = str(e)
            winerr = getattr(e, "winerror", None)
            if winerr == 17:
                reason = "different volumes/filesystems (hardlinks require the same volume)"
            elif winerr in (1, 50):
                reason = "filesystem does not support hardlinks"
            elif winerr == 5:
                reason = "permission denied while testing hardlinks"
            return False, reason
        except Exception as e:
            return False, str(e)
        finally:
            for item in (link, probe):
                try:
                    if item.exists():
                        item.unlink()
                except Exception:
                    pass

    def find_backup_base(self, parent: Path, exclude: Optional[Path] = None) -> Optional[Path]:
        try:
            current = Path(exclude).resolve() if exclude else None
            candidates = [
                item for item in Path(parent).iterdir()
                if item.is_dir() and (item / "manifest.json").exists() and (current is None or item.resolve() != current)
            ]
        except OSError:
            return None
        if not candidates:
            return None
        def stamp(item: Path) -> float:
            try:
                return (item / "manifest.json").stat().st_mtime
            except OSError:
                return 0.0
        return max(candidates, key=stamp)

    def incremental_hardlink_warning(self, base: Optional[Path], dest_parent: Path) -> Optional[str]:
        if self._backup_mode_name() != "incremental" or base is None:
            return None
        try:
            ok, reason = self.check_hardlink_support(base, Path(dest_parent))
        except Exception as e:
            ok, reason = False, str(e)
        if ok:
            return None
        detail = f" ({reason})" if reason else ""
        return (
            "Incremental hardlinks are not supported between the previous backup and this destination"
            f"{detail}. Unchanged files will be copied in full instead of linked, so this backup may be much slower and use as much space as a full backup. "
            "Choose a destination on the same NTFS volume as the previous backup for true incremental hardlinks, or use Full/Compact mode."
        )

    def prepare_backup_base(self) -> Optional[Path]:
        """Pick the newest earlier completed backup for incremental/compact reuse."""
        self._incremental_base = None
        self._base_backup = None
        mode = self._backup_mode_name()
        if mode not in {"incremental", "compact"} or not self.backup_root:
            return None
        parent = self.backup_root.parent
        try:
            current = self.backup_root.resolve()
            candidates = [
                item for item in parent.iterdir()
                if item.is_dir() and item.resolve() != current and (item / "manifest.json").exists()
            ]
        except OSError:
            return None
        if not candidates:
            msg = "Compact" if mode == "compact" else "Incremental"
            self.log_obj.info(f"{msg}: no previous backup found — copying everything")
            return None

        def stamp(item: Path) -> float:
            try:
                return (item / "manifest.json").stat().st_mtime
            except OSError:
                return 0.0

        base = max(candidates, key=stamp)
        self._incremental_base = base
        self._base_backup = base
        if mode == "compact":
            self.log_obj.info(
                f"Compact: changed files only will be copied; unchanged files omitted (restore will merge from {base.name})"
            )
        else:
            self.log_obj.info(f"Incremental: only changed files will be copied; unchanged files linked from {base.name}")
            warn = self.incremental_hardlink_warning(base, self.backup_root.parent if self.backup_root else parent)
            if warn:
                self.log_obj.warn(warn)
        return base

    def prepare_incremental_base(self) -> Optional[Path]:
        return self.prepare_backup_base()

    @staticmethod
    def _backup_manifest_time(path: Path) -> float:
        for candidate in (path / "manifest.json", path / "performance.json"):
            try:
                return candidate.stat().st_mtime
            except OSError:
                continue
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def list_backup_folders(self, parent: Path) -> List[Dict[str, Any]]:
        """List backup folders under parent, newest first, with manifest status."""
        items: List[Dict[str, Any]] = []
        try:
            entries = list(Path(parent).iterdir())
        except OSError:
            return items
        for item in entries:
            if not item.is_dir() or not item.name.startswith("WindowsBackup_"):
                continue
            manifest_path = item / "manifest.json"
            data: Dict[str, Any] = {}
            if manifest_path.exists():
                try:
                    loaded = json_loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except Exception:
                    data = {}
            in_progress = bool(data.get("InProgress")) or not manifest_path.exists()
            items.append({
                "path": item,
                "name": item.name,
                "mtime": self._backup_manifest_time(item),
                "in_progress": in_progress,
                "cancelled": bool(data.get("Cancelled")),
                "failed": bool(data.get("FailedStages")),
                "linked_files": int(data.get("LinkedFiles") or 0),
                "incremental_base": str(data.get("IncrementalBase") or ""),
                "manifest": data,
            })
        items.sort(key=lambda it: (it["mtime"], it["name"]), reverse=True)
        return items

    def plan_retention(self, parent: Path, keep_last: int = 5, max_age_days: int = 0,
                       protect: Optional[Iterable[Path]] = None) -> Dict[str, Any]:
        """Choose old completed backups to remove.

        Safe by design: never removes in-progress backups, never removes the
        protected/current backup, and only considers WindowsBackup_* folders in
        the selected backup location. Incremental backups are self-contained
        (unchanged files are hardlinked), so deleting an older backup does not
        remove data still referenced by newer backups.
        """
        keep_last = max(0, int(keep_last or 0))
        max_age_days = max(0, int(max_age_days or 0))
        protected: Set[str] = set()
        for item in protect or ():
            try:
                protected.add(str(Path(item).resolve()))
            except Exception:
                protected.add(str(item))
        backups = self.list_backup_folders(parent)
        keep: List[Dict[str, Any]] = []
        remove: List[Dict[str, Any]] = []
        reasons: Dict[str, str] = {}
        now = time.time()
        completed_seen = 0
        for backup in backups:
            path_key = str(backup["path"].resolve()) if backup["path"].exists() else str(backup["path"])
            if path_key in protected:
                keep.append(backup)
                reasons[backup["name"]] = "protected/current"
                continue
            if backup["in_progress"]:
                keep.append(backup)
                reasons[backup["name"]] = "in progress"
                continue
            completed_seen += 1
            too_old = bool(max_age_days and (now - float(backup["mtime"] or 0.0)) > max_age_days * 86400)
            over_count = bool(keep_last and completed_seen > keep_last)
            if too_old or over_count:
                remove.append(backup)
                why = []
                if over_count:
                    why.append(f"older than keep-last {keep_last}")
                if too_old:
                    why.append(f"older than {max_age_days} days")
                reasons[backup["name"]] = ", ".join(why)
            else:
                keep.append(backup)
                reasons[backup["name"]] = "kept by policy"
        # Protect bases required by kept compact/changed-only backups.
        by_path = {}
        for b in backups:
            try:
                by_path[str(Path(b["path"]).resolve())] = b
            except Exception:
                by_path[str(b["path"])] = b
        keep_keys = set()
        for b in keep:
            try:
                keep_keys.add(str(Path(b["path"]).resolve()))
            except Exception:
                keep_keys.add(str(b["path"]))
        changed = True
        while changed:
            changed = False
            for b in list(keep):
                mf = b.get("manifest") or self._read_backup_manifest(Path(b["path"]))
                base_ref = str(self._manifest_get(mf, "BaseBackup", "IncrementalBase", "Base", default="") or "").strip()
                compact = str(mf.get("BackupMode") or "").lower() == "compact" or bool(mf.get("Compact")) or mf.get("SelfContained") is False
                if not compact or not base_ref:
                    continue
                base = Path(base_ref)
                if not base.is_absolute():
                    base = (Path(b["path"]).parent / base)
                try:
                    base_key = str(base.resolve())
                except Exception:
                    base_key = str(base)
                if base_key in keep_keys:
                    continue
                # move base from remove to keep if present
                moved = None
                for i, rb in enumerate(list(remove)):
                    try:
                        rk = str(Path(rb["path"]).resolve())
                    except Exception:
                        rk = str(rb["path"])
                    if rk == base_key:
                        moved = remove.pop(i)
                        break
                if moved is not None:
                    keep.append(moved)
                    keep_keys.add(base_key)
                    reasons[moved["name"]] = f"base required by compact backup {b['name']}"
                    changed = True
                elif base_key in by_path and base_key not in keep_keys:
                    # known backup not in lists? keep it.
                    kb = by_path[base_key]
                    keep.append(kb)
                    keep_keys.add(base_key)
                    reasons[kb["name"]] = f"base required by compact backup {b['name']}"
                    changed = True
        return {
            "parent": str(parent),
            "keep_last": keep_last,
            "max_age_days": max_age_days,
            "keep": keep,
            "remove": remove,
            "reasons": reasons,
            "total": len(backups),
        }

    def apply_retention(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Delete backups selected by plan_retention. Returns removed/failed lists."""
        removed: List[str] = []
        failed: List[str] = []
        for backup in plan.get("remove") or []:
            path = Path(backup["path"])
            if not path.exists():
                removed.append(str(path))
                continue
            if not path.name.startswith("WindowsBackup_"):
                failed.append(f"{path} (unexpected name)")
                continue
            try:
                shutil.rmtree(path)
                removed.append(str(path))
                self.log_obj.info(f"Retention removed old backup: {path.name}")
            except Exception as e:
                failed.append(f"{path}: {e}")
                self.log_obj.warn(f"Retention could not remove {path.name}: {e}")
        return {"removed": removed, "failed": failed}

    def performance_report(self, elapsed: float) -> Dict[str, Any]:
        """Summarise throughput for the job that just finished."""
        elapsed = max(0.001, float(elapsed or 0.0))
        files_done = int(getattr(self, "_job_files_done", 0))
        bytes_done = int(getattr(self, "_job_bytes_done", 0))
        return {
            "Version": VERSION,
            "Time": now_local(),
            "Kind": self._job_kind or "backup",
            "ElapsedSeconds": round(elapsed, 3),
            "Files": files_done,
            "Bytes": bytes_done,
            "MBps": round(bytes_done / elapsed / (1024 * 1024), 2),
            "FilesPerSecond": round(files_done / elapsed, 1),
            "Workers": int(getattr(self, "copy_workers", 1)),
            "Computer": os.environ.get("COMPUTERNAME", ""),
            "Platform": platform.platform(),
            "BackupMode": self._backup_mode_name(),
            "BaseBackup": str(getattr(self, "_base_backup", None) or self._incremental_base or ""),
            "SelfContained": self._backup_mode_name() != "compact",
            "IncrementalBase": str(self._incremental_base or ""),
            "CopiedBytes": int(getattr(self, "_copied_bytes", 0) or 0),
            "LinkedFiles": int(getattr(self, "_linked_files", 0)),
            "LinkedBytes": int(getattr(self, "_linked_bytes", 0) or 0),
            "CompactSkippedFiles": int(getattr(self, "_compact_skipped", 0)),
            "CompactSkippedBytes": int(getattr(self, "_compact_skipped_bytes", 0) or 0),
            "SkippedExistingBytes": int(getattr(self, "_skipped_existing_bytes", 0) or 0),
            "SkippedFiles": int(getattr(self, "_skipped_files", 0)),
        }

    def run_benchmark(self, file_count: int = 200, size_kb: int = 128) -> Dict[str, Any]:
        """Copy a synthetic tree serially and in parallel, then write a Markdown report."""
        import tempfile

        root = Path(tempfile.mkdtemp(prefix="wbk_bench_"))
        workers = max(1, min(16, int(getattr(self, "copy_workers", 4) or 4)))
        file_count = max(10, int(file_count))
        size_kb = max(4, int(size_kb))
        try:
            src = root / "src"
            serial_dst = root / "serial"
            parallel_dst = root / "parallel"
            payload = b"x" * (size_kb * 1024)
            for i in range(file_count):
                folder = src / f"dir{i % 10:02d}"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"file{i:04d}.bin").write_bytes(payload)

            self.begin_job("benchmark", 3)
            self.begin_stage("Scan", "scanning")
            scan_start = time.perf_counter()
            file_jobs, dirs, count, total = self.scan_tree(src, serial_dst, "benchmark")
            scan_s = max(0.001, time.perf_counter() - scan_start)
            self.add_stage_totals(count, total)
            self.end_stage()
            for folder in dirs:
                self._ensure_dir(folder)

            self.begin_stage("Serial copy")
            self.add_stage_totals(count, total)
            serial_start = time.perf_counter()
            self._copy_file_jobs(file_jobs, max_workers=1)
            serial_s = max(0.001, time.perf_counter() - serial_start)
            self.end_stage()

            self.begin_stage(f"Parallel copy ({workers} workers)")
            self.add_stage_totals(count, total)
            parallel_jobs = [
                (source, parallel_dst / dest.relative_to(serial_dst), label, size)
                for source, dest, label, size in file_jobs
            ]
            for folder in dirs:
                self._ensure_dir(parallel_dst / folder.relative_to(serial_dst))
            parallel_start = time.perf_counter()
            self._copy_file_jobs(parallel_jobs, max_workers=workers)
            parallel_s = max(0.001, time.perf_counter() - parallel_start)
            self.end_stage()

            copy_mb = max(0.001, total / (1024 * 1024))
            report: Dict[str, Any] = {
                "Time": now_local(),
                "Version": VERSION,
                "Platform": platform.platform(),
                "CPUs": os.cpu_count() or 1,
                "Files": count,
                "Bytes": total,
                "ScanSeconds": round(scan_s, 3),
                "SerialSeconds": round(serial_s, 3),
                "ParallelSeconds": round(parallel_s, 3),
                "SerialMBps": round(copy_mb / serial_s, 2),
                "ParallelMBps": round(copy_mb / parallel_s, 2),
                "Workers": workers,
                "Speedup": round(serial_s / parallel_s, 2),
                "IncrementalBase": str(self._incremental_base or ""),
                "LinkedFiles": int(getattr(self, "_linked_files", 0)),
                "CompactSkippedFiles": int(getattr(self, "_compact_skipped", 0)),
                "CopiedBytes": int(getattr(self, "_copied_bytes", 0) or 0),
                "LinkedBytes": int(getattr(self, "_linked_bytes", 0) or 0),
                "CompactSkippedBytes": int(getattr(self, "_compact_skipped_bytes", 0) or 0),
                "SkippedExistingBytes": int(getattr(self, "_skipped_existing_bytes", 0) or 0),
                "SkippedFiles": int(getattr(self, "_skipped_files", 0)),
            }
            report_dir = SETTINGS_FILE.parent / "benchmarks"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / f"benchmark-{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            report_path.write_text(self._benchmark_markdown(report), encoding="utf-8")
            report["report_path"] = str(report_path)
            self.log_obj.ok(
                f"Benchmark: serial {report['SerialMBps']} MB/s, parallel "
                f"{report['ParallelMBps']} MB/s ({report['Speedup']}x) — {report_path}"
            )
            return report
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @staticmethod
    def _benchmark_markdown(report: Dict[str, Any]) -> str:
        return "\n".join([
            "# Backup Toolkit — Copy Benchmark",
            "",
            f"- Date: {report['Time']}",
            f"- Version: {report['Version']}",
            f"- Platform: {report['Platform']} ({report['CPUs']} CPUs)",
            f"- Files: {report['Files']} ({format_bytes(report['Bytes'])})",
            f"- Scan: {report['ScanSeconds']} s",
            "",
            "| Copy mode | Time | Throughput |",
            "| --- | --- | --- |",
            f"| Serial | {report['SerialSeconds']} s | {report['SerialMBps']} MB/s |",
            f"| Parallel ({report['Workers']} workers) | {report['ParallelSeconds']} s | {report['ParallelMBps']} MB/s |",
            "",
            f"**Measured speedup:** {report['Speedup']}x",
            "",
            "The benchmark uses a temporary synthetic tree (removed afterwards). Speeds depend on disk, CPU, antivirus, and file sizes.",
            "",
        ])

    def write_in_progress_manifest(self, completed: Optional[List[str]] = None) -> None:
        """Write a lightweight marker so a crash can be resumed without a checkpoint."""
        if not self.backup_root:
            return
        data = {
            "ManifestVersion": 2,
            "Version": VERSION,
            "Time": now_local(),
            "Computer": os.environ.get("COMPUTERNAME", ""),
            "User": os.environ.get("USERNAME", ""),
            "BackupRoot": str(self.backup_root),
            "InProgress": True,
            "CompletedStages": list(completed or []),
            "FailedStages": [],
            "Cancelled": False,
            "IsAdmin": is_admin(),
            "SessionLog": str(self.log_file) if self.log_file else "",
        }
        try:
            (self.backup_root / "manifest.json").write_text(json_dumps(data), encoding="utf-8")
        except Exception as e:
            self.log_obj.warn(f"In-progress manifest save failed: {e}")

    def find_interrupted_backup(self, parent: Path) -> Optional[Path]:
        """Return the newest backup folder marked interrupted, cancelled, or failed."""
        try:
            choices = []
            for item in Path(parent).iterdir():
                if not item.is_dir():
                    continue
                manifest = item / "manifest.json"
                if not manifest.exists():
                    continue
                try:
                    data = json_loads(manifest.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("InProgress") or data.get("Cancelled") or data.get("FailedStages"):
                    try:
                        choices.append((manifest.stat().st_mtime, item))
                    except OSError:
                        choices.append((0.0, item))
            if not choices:
                return None
            return max(choices, key=lambda pair: pair[0])[1]
        except OSError:
            return None

    def write_manifest(self, failed: List[str], cancelled: bool) -> None:
        if not self.backup_root:
            return
        stage_stats = self._collect_stage_stats()
        manifest = {
            "ManifestVersion": 2,
            "Version": VERSION,
            "Time": now_local(),
            "Computer": os.environ.get("COMPUTERNAME", ""),
            "User": os.environ.get("USERNAME", ""),
            "BackupRoot": str(self.backup_root),
            "CompletedStages": list(self.completed_stages),
            "FailedStages": list(failed),
            "Cancelled": cancelled,
            "InProgress": False,
            "IsAdmin": is_admin(),
            "SessionLog": str(self.log_file) if self.log_file else "",
            "BackupMode": self._backup_mode_name(),
            "Incremental": bool(self._incremental_base) and self._backup_mode_name() == "incremental",
            "Compact": self._backup_mode_name() == "compact",
            "BaseBackup": str(getattr(self, "_base_backup", None) or self._incremental_base or ""),
            "SelfContained": self._backup_mode_name() != "compact",
            "IncrementalBase": str(self._incremental_base or ""),
            "LinkedFiles": int(getattr(self, "_linked_files", 0)),
            "CompactSkippedFiles": int(getattr(self, "_compact_skipped", 0)),
            "CopiedBytes": int(getattr(self, "_copied_bytes", 0) or 0),
            "LinkedBytes": int(getattr(self, "_linked_bytes", 0) or 0),
            "CompactSkippedBytes": int(getattr(self, "_compact_skipped_bytes", 0) or 0),
            "SkippedExistingBytes": int(getattr(self, "_skipped_existing_bytes", 0) or 0),
            "SkippedFiles": int(getattr(self, "_skipped_files", 0)),
            "StageStats": stage_stats,
        }
        try:
            (self.backup_root / "manifest.json").write_text(
                json_dumps(manifest), encoding="utf-8"
            )
            self.log_obj.info(f"Manifest saved: {self.backup_root / 'manifest.json'}")
        except Exception as e:
            self.log_obj.warn(f"Manifest save failed: {e}")

    def _collect_stage_stats(self) -> Dict[str, Any]:
        """Best-effort file counts/sizes for backup verification."""
        if not self.backup_root:
            return {}
        stage_dirs = {
            "Drivers": self.backup_root / "Drivers",
            "Packages": self.backup_root / "Apps",
            "UserData": self.backup_root / "UserData",
            "Bookmarks": self.backup_root / "BrowserBookmarks",
            "Settings": self.backup_root / "Settings",
            "CodingTools": self.backup_root / "CodingTools",
        }
        stats: Dict[str, Any] = {}
        for stage, path in stage_dirs.items():
            if not path.exists():
                continue
            files = 0
            bytes_total = 0
            try:
                for p in path.rglob("*"):
                    if p.is_file():
                        files += 1
                        try:
                            bytes_total += p.stat().st_size
                        except OSError:
                            pass
            except Exception:
                pass
            stats[stage] = {"files": files, "bytes": bytes_total, "path": path.name}
        return stats

    @staticmethod
    def hash_file(path: Path, algorithm: str = "blake2b", chunk_size: int = 1024 * 1024) -> str:
        if hashlib is None:
            raise RuntimeError("hashlib is unavailable")
        h = hashlib.new(algorithm)
        with open(long_path(Path(path)), "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _integrity_candidates(root: Path) -> List[Path]:
        candidates: List[Path] = []
        for stage_name in ("Drivers", "Apps", "UserData", "BrowserBookmarks", "Settings", "CodingTools"):
            stage = root / stage_name
            if not stage.exists():
                continue
            try:
                for item in stage.rglob("*"):
                    if item.is_file() and item.name not in INTEGRITY_META_FILES:
                        candidates.append(item)
            except OSError:
                continue
        return sorted(set(candidates), key=lambda p: str(p.relative_to(root)).lower())

    def write_integrity_sample(self, root: Path, sample_count: int = 100, max_bytes: int = 256 * 1024 * 1024) -> Dict[str, Any]:
        root = Path(root)
        candidates = self._integrity_candidates(root)
        if not candidates:
            report = {"Time": now_local(), "Version": VERSION, "SampleCount": 0, "Files": []}
            try:
                (root / INTEGRITY_FILE).write_text(json_dumps(report), encoding="utf-8")
            except Exception:
                pass
            return report
        chosen: List[Path] = []
        if len(candidates) <= sample_count:
            chosen = candidates
        else:
            for i in range(sample_count):
                idx = round(i * (len(candidates) - 1) / max(1, sample_count - 1))
                item = candidates[idx]
                if item not in chosen:
                    chosen.append(item)
            for item in candidates:
                if len(chosen) >= sample_count:
                    break
                if item not in chosen:
                    chosen.append(item)
        files = []
        hashed_bytes = 0
        for path in chosen:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if max_bytes and hashed_bytes and (hashed_bytes + size) > max_bytes and files:
                break
            try:
                digest = self.hash_file(path)
                files.append({"path": str(path.relative_to(root)), "size": size, "hash": f"blake2b:{digest}"})
                hashed_bytes += size
            except Exception as e:
                files.append({"path": str(path.relative_to(root)), "size": size, "hash": "", "error": str(e)})
        report = {
            "Time": now_local(),
            "Version": VERSION,
            "Algorithm": "blake2b",
            "CandidateFiles": len(candidates),
            "SampleCount": len(files),
            "HashedBytes": hashed_bytes,
            "Files": files,
        }
        try:
            (root / INTEGRITY_FILE).write_text(json_dumps(report), encoding="utf-8")
        except Exception as e:
            report["write_error"] = str(e)
        return report

    @staticmethod
    def verify_integrity_sample(root: Path) -> Tuple[List[str], List[str], Dict[str, Any]]:
        root = Path(root)
        errors: List[str] = []
        warnings: List[str] = []
        info: Dict[str, Any] = {"checked": 0, "ok": 0, "changed": 0, "missing": 0}
        ipath = root / INTEGRITY_FILE
        if not ipath.exists():
            warnings.append(f"No integrity sample found ({INTEGRITY_FILE}). Generate one to detect later changes size/time may miss.")
            return errors, warnings, info
        try:
            data = json_loads(ipath.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"{INTEGRITY_FILE} is unreadable: {e}")
            return errors, warnings, info
        entries = data.get("Files") or []
        if not entries:
            warnings.append("Integrity sample contains no files.")
            return errors, warnings, info
        for item in entries:
            rel = str(item.get("path") or "")
            expected = str(item.get("hash") or "")
            if not rel or not expected:
                continue
            full = root / rel
            info["checked"] += 1
            if not full.exists():
                info["missing"] += 1
                warnings.append(f"Integrity: missing file {rel}")
                continue
            try:
                actual = "blake2b:" + BackupToolkit.hash_file(full)
            except Exception as e:
                info["changed"] += 1
                warnings.append(f"Integrity: could not read {rel}: {e}")
                continue
            if actual != expected:
                info["changed"] += 1
                warnings.append(f"Integrity: hash changed for {rel} (size/mtime may not show this)")
            else:
                info["ok"] += 1
        if info["changed"] or info["missing"]:
            errors.append(f"Integrity sample found {info['changed']} changed and {info['missing']} missing file(s).")
        elif info["checked"]:
            warnings.append(f"Integrity sample OK: {info['ok']}/{info['checked']} sampled file(s) match recorded hashes.")
        return errors, warnings, info

    @staticmethod
    def verify_backup(root: Path) -> Tuple[List[str], List[str]]:
        """Verify backup structure/manifest. Returns (errors, warnings)."""
        errors: List[str] = []
        warnings: List[str] = []
        root = Path(root)
        if not root.exists() or not root.is_dir():
            return [f"Backup folder not found: {root}"], warnings
        manifest_path = root / "manifest.json"
        if not manifest_path.exists():
            warnings.append("manifest.json is missing (older backup or incomplete).")
            return errors, warnings
        try:
            data = json_loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"manifest.json is unreadable: {e}")
            return errors, warnings
        if data.get("Cancelled"):
            warnings.append("Manifest indicates the backup was cancelled/incomplete.")
        for s in (data.get("FailedStages") or []):
            warnings.append(f"Manifest reports failed stage during backup: {s}")
        stage_dirs = {
            "Drivers": root / "Drivers",
            "Packages": root / "Apps",
            "UserData": root / "UserData",
            "Bookmarks": root / "BrowserBookmarks",
            "Settings": root / "Settings",
            "CodingTools": root / "CodingTools",
        }
        completed = set(data.get("CompletedStages") or [])
        for stage in completed:
            p = stage_dirs.get(stage)
            if p and not p.exists():
                warnings.append(f"Completed stage '{stage}' has no folder: {p.name}")
        recorded = data.get("StageStats") or {}
        for stage, rec in recorded.items():
            p = stage_dirs.get(stage)
            if not p or not p.exists():
                warnings.append(f"StageStats references missing stage folder: {stage}")
                continue
            try:
                files = sum(1 for x in p.rglob("*") if x.is_file())
            except Exception:
                files = -1
            exp = rec.get("files")
            if isinstance(exp, int) and files >= 0 and files != exp:
                warnings.append(f"{stage}: file count changed since backup (manifest {exp}, now {files})")
        if not errors and not warnings:
            warnings.append("Basic manifest/structure checks passed.")
        return errors, warnings

    # ---------- stage: drivers ----------
    def backup_drivers(self) -> None:
        self.wait_if_paused()
        self.log_obj.info("=== Exporting Drivers ===")
        if not is_admin():
            self.log_obj.warn("Not admin — driver export may fail or be incomplete")
        driver_path = self.backup_root / "Drivers"
        self._ensure_dir(driver_path)
        try:
            self._run(["pnputil", "/export-driver", "*", str(driver_path)], timeout=900)
            count = sum(1 for _ in driver_path.rglob("*.inf"))
            self.log_obj.ok(f"Drivers exported ({count} INF files)")
        except Exception as e:
            self.log_obj.error(f"Driver export failed (continuing): {e}")

    # ---------- stage: packages ----------
    def backup_packages(self) -> None:
        self.wait_if_paused()
        self.log_obj.info("=== Package Managers ===")
        apps = self.backup_root / "Apps"
        self._ensure_dir(apps)

        winget = shutil.which("winget")
        if not winget:
            candidates = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "winget.exe",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps" / "Microsoft.DesktopAppInstaller_*",
            ]
            for c in candidates:
                if c.exists():
                    winget = str(c)
                    break

        if winget or shutil.which("winget"):
            try:
                out = apps / "winget-packages.json"
                self._run([winget or "winget", "export", "-o", str(out), "--include-versions", "--accept-source-agreements"], timeout=300)
                if out.exists() and out.stat().st_size > 10:
                    self.log_obj.ok("Winget packages exported")
                else:
                    self.log_obj.warn("Winget export produced empty/no file")
            except Exception as e:
                self.log_obj.warn(f"Winget export failed (continuing): {e}")
        else:
            self.log_obj.warn("Winget not found — skipping")

        self.wait_if_paused()

        if shutil.which("choco"):
            try:
                r = self._run(["choco", "list", "-l", "--limit-output"], timeout=120)
                lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
                (apps / "chocolatey-packages.txt").write_text("\n".join(lines), encoding="utf-8")
                pkgs = []
                for ln in lines:
                    name = ln.split("|")[0].strip()
                    if name and name.lower() != "packages":
                        pkgs.append(f"choco install {name} -y")
                (apps / "chocolatey-install.ps1").write_text("\n".join(pkgs) + "\n", encoding="utf-8")
                self.log_obj.ok(f"Chocolatey packages exported ({len(pkgs)} packages)")
            except Exception as e:
                self.log_obj.warn(f"Chocolatey export failed (continuing): {e}")
        else:
            self.log_obj.warn("Chocolatey not found — skipping")

        try:
            self._export_installed_programs(apps)
            self.log_obj.ok("Programs list saved")
        except Exception as e:
            self.log_obj.warn(f"Programs list failed (continuing): {e}")

    def _export_installed_programs(self, apps: Path) -> None:
        try:
            import winreg
        except ImportError:
            return
        rows = []
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        skip_bits = ("Update for", "Hotfix", "Security Update", "KB")
        for hive, path in roots:
            try:
                with winreg.OpenKey(hive, path) as key:
                    i = 0
                    while True:
                        try:
                            sub = winreg.EnumKey(key, i)
                            i += 1
                            with winreg.OpenKey(key, sub) as sk:
                                def _get(n, k=sk):
                                    try:
                                        return winreg.QueryValueEx(k, n)[0]
                                    except OSError:
                                        return ""
                                name = str(_get("DisplayName") or "")
                                if not name or any(s in name for s in skip_bits):
                                    continue
                                rows.append({
                                    "DisplayName": name,
                                    "DisplayVersion": str(_get("DisplayVersion") or ""),
                                    "Publisher": str(_get("Publisher") or ""),
                                    "InstallLocation": str(_get("InstallLocation") or ""),
                                })
                        except OSError:
                            break
            except OSError:
                continue
        seen = set()
        unique = []
        for r in sorted(rows, key=lambda x: x["DisplayName"].lower()):
            if r["DisplayName"] not in seen:
                seen.add(r["DisplayName"])
                unique.append(r)
        with (apps / "InstalledPrograms.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["DisplayName", "DisplayVersion", "Publisher", "InstallLocation"])
            w.writeheader()
            w.writerows(unique)
        lines = [f"{r['DisplayName']}  |  {r['DisplayVersion']}  |  {r['Publisher']}" for r in unique]
        (apps / "InstalledPrograms.txt").write_text("\n".join(lines), encoding="utf-8")

    # ---------- stage: user data ----------
    def backup_user_data(self, selected_user_folders=None, selected_roaming=None, include_dotfiles=True) -> None:
        self.wait_if_paused()
        self.log_obj.info("=== User Data (smart excludes) ===")
        self.log_obj.debug("Skipping: node_modules, build, bin, obj, .git, Cache, dist, ...")
        self.log_obj.debug("Browser full profiles skipped (bookmarks backed up separately)")
        user_data = self.backup_root / "UserData"
        self._ensure_dir(user_data)
        profile = Path.home()

        folders = selected_user_folders if selected_user_folders is not None else [
            f for f in USER_FOLDERS if f not in EXCLUDE_TOP_FOLDERS
        ]
        for folder in folders:
            self.wait_if_paused()
            if folder in EXCLUDE_TOP_FOLDERS:
                self.log_obj.warn(f"Skipped (top exclusion): {folder}")
                continue
            src = profile / folder
            if folder == "Desktop" and src.resolve() == DESKTOP.resolve():
                self._copy_desktop_filtered(src, user_data / "Desktop")
            else:
                self.copy_tree_safe(src, user_data / folder, folder)

        roaming_list = selected_roaming if selected_roaming is not None else list(ROAMING_FOLDERS)
        roaming_src = profile / "AppData" / "Roaming"
        roaming_dst = user_data / "AppData_Roaming"
        self._ensure_dir(roaming_dst)
        for item in roaming_list:
            self.wait_if_paused()
            src = roaming_src / item
            if src.exists():
                self.copy_tree_safe(src, roaming_dst / item, f"Roaming\\{item}")

        local_src = profile / "AppData" / "Local"
        local_dst = user_data / "AppData_Local"
        self._ensure_dir(local_dst)
        for item in LOCAL_FOLDERS:
            self.wait_if_paused()
            src = local_src / item
            if src.exists():
                self.copy_tree_safe(src, local_dst / item, f"Local\\{item}")

        if include_dotfiles:
            for item in EXTRA_PROFILE_ITEMS:
                self.wait_if_paused()
                src = profile / item
                if src.exists():
                    dst = user_data / "ProfileDotFiles" / item
                    self.copy_tree_safe(src, dst, item)

    def _copy_desktop_filtered(self, src: Path, dst: Path) -> None:
        self.wait_if_paused()
        self.log_obj.info("Copying Desktop (skipping WindowsBackup_*)...")
        self.copy_tree_safe(
            src, dst, "Desktop",
            skip_top_names={"WindowsBackup_Checkpoint.json"},
            skip_top_prefixes=("WindowsBackup_",),
        )

    # ---------- stage: bookmarks ----------
    def backup_bookmarks(self) -> None:
        self.wait_if_paused()
        self.log_obj.info("=== Browser Bookmarks only (light) ===")
        bm = self.backup_root / "BrowserBookmarks"
        self._ensure_dir(bm)
        profile = Path.home()
        targets = {
            "Chrome_Bookmarks": profile / "AppData/Local/Google/Chrome/User Data/Default/Bookmarks",
            "Edge_Bookmarks": profile / "AppData/Local/Microsoft/Edge/User Data/Default/Bookmarks",
            "Brave_Bookmarks": profile / "AppData/Local/BraveSoftware/Brave-Browser/User Data/Default/Bookmarks",
        }
        for name, src in list(targets.items()):
            if not src.exists():
                alt = src.parent.parent / "Profile 1" / "Bookmarks"
                if alt.exists():
                    targets[name] = alt
        for name, src in targets.items():
            self.wait_if_paused()
            if src.exists():
                try:
                    shutil.copy2(src, bm / name)
                    self.log_obj.ok(f"{name} saved")
                except Exception as e:
                    self.log_obj.warn(f"{name} failed: {e}")

        ff = profile / "AppData/Roaming/Mozilla/Firefox/Profiles"
        if ff.exists():
            for prof in ff.iterdir():
                if not prof.is_dir():
                    continue
                places = prof / "places.sqlite"
                if places.exists():
                    try:
                        shutil.copy2(places, bm / f"Firefox_{prof.name}_places.sqlite")
                        self.log_obj.ok(f"Firefox ({prof.name}) saved")
                    except Exception as e:
                        self.log_obj.warn(f"Firefox copy failed: {e}")

    # ---------- stage: settings ----------
    def backup_settings(self) -> None:
        self.wait_if_paused()
        self.log_obj.info("=== Windows Settings ===")
        settings = self.backup_root / "Settings"
        self._ensure_dir(settings)

        try:
            out = settings / "StartLayout.xml"
            self._run_shell(
                f'powershell -NoProfile -Command "Export-StartLayout -Path \'{out}\'"',
                timeout=60,
            )
            if out.exists():
                self.log_obj.ok("Start layout exported")
            else:
                self.log_obj.warn("Start layout skipped")
        except Exception:
            self.log_obj.warn("Start layout skipped")

        try:
            r = self._run([_POWERCFG, "/list"], timeout=30)
            (settings / "PowerPlans.txt").write_text(r.stdout or "", encoding="utf-8", errors="replace")
            self._run([_POWERCFG, "/export", str(settings / "ActivePowerPlan.pow"), "SCHEME_CURRENT"], timeout=30)
            self.log_obj.ok("Power plans exported")
        except Exception as e:
            self.log_obj.warn(f"Power plan failed: {e}")

        try:
            self._run_shell(f'{_NETSH} wlan export profile key=clear folder="{settings}"', timeout=60)
            xmls = list(settings.glob("*.xml"))
            self.log_obj.ok(f"Wi-Fi profiles exported ({len(xmls)} files)")
        except Exception as e:
            self.log_obj.warn(f"Wi-Fi failed: {e}")

        try:
            hosts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
            if hosts.exists():
                shutil.copy2(hosts, settings / "hosts")
                self.log_obj.ok("Hosts copied")
        except Exception as e:
            self.log_obj.warn(f"Hosts failed: {e}")

        try:
            user_env = []
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(key, i)
                        user_env.append(f"{name}={val}")
                        i += 1
                    except OSError:
                        break
            (settings / "EnvironmentVariables_User.env").write_text("\n".join(user_env), encoding="utf-8")
            self.log_obj.ok("Env vars saved")
        except Exception as e:
            self.log_obj.warn(f"Env vars failed: {e}")

        reg_keys = {
            "Explorer_Advanced": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "Themes": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes",
            "Desktop": r"HKCU\Control Panel\Desktop",
            "Colors": r"HKCU\Control Panel\Colors",
            "Mouse": r"HKCU\Control Panel\Mouse",
            "Keyboard": r"HKCU\Control Panel\Keyboard",
            "Search": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search",
            "Personalize": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            "International": r"HKCU\Control Panel\International",
            "Taskband": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Taskband",
        }
        for name, key in reg_keys.items():
            self.wait_if_paused()
            out = settings / f"{name}.reg"
            try:
                self._run([_REG, "export", key, str(out), "/y"], timeout=30)
                if out.exists():
                    self.log_obj.ok(f"Registry: {name}")
            except Exception:
                self.log_obj.warn(f"Registry failed: {name}")

        try:
            r = self._run_shell(
                'powershell -NoProfile -Command "Get-Printer | Select-Object Name,DriverName,PortName,Shared | ConvertTo-Csv -NoTypeInformation"',
                timeout=60,
            )
            if r.stdout and "Name" in r.stdout:
                (settings / "Printers.csv").write_text(r.stdout, encoding="utf-8", errors="replace")
                self.log_obj.ok("Printers saved")
        except Exception:
            self.log_obj.warn("Printers failed")

        try:
            r = self._run([_SYSTEMINFO], timeout=120)
            (settings / "SystemInfo.txt").write_text(r.stdout or "", encoding="utf-8", errors="replace")
        except Exception:
            pass

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
                wp, _ = winreg.QueryValueEx(key, "Wallpaper")
            if wp and Path(wp).exists():
                ext = Path(wp).suffix or ".jpg"
                shutil.copy2(wp, settings / f"CurrentWallpaper{ext}")
                self.log_obj.ok("Wallpaper copied")
        except Exception:
            pass

    # ---------- stage: coding tools (FreeBuff, Kilo Code, editors, terminals) ----------
    def backup_coding_tools(self) -> None:
        self.wait_if_paused()
        self.log_obj.info("=== Coding Tool Configurations ===")
        root = self.backup_root / "CodingTools"
        self._ensure_dir(root)
        profile = Path.home()
        appdata = Path(os.environ.get("APPDATA", profile / "AppData" / "Roaming"))
        localappdata = Path(os.environ.get("LOCALAPPDATA", profile / "AppData" / "Local"))
        docs = Path(os.environ.get("USERPROFILE", profile)) / "Documents"

        # Small config folders/files (safe to copy whole folder when present)
        folder_targets = [
            ("FreeBuff_Desktop", appdata / "Freebuff"),
            ("FreeBuff_CLI", profile / ".config" / "manicode"),
            ("KiloCode_Config", profile / ".config" / "kilo"),
            ("VSCode_User", appdata / "Code" / "User"),
            ("VSCodeInsiders_User", appdata / "Code - Insiders" / "User"),
            ("Cursor_User", appdata / "Cursor" / "User"),
            ("Windsurf_User", appdata / "Windsurf" / "User"),
            ("JetBrains_Config", appdata / "JetBrains"),
            ("Neovim_Config", localappdata / "nvim"),
        ]
        # Skip heavy/cache subdirs inside editor User folders
        editor_user_names = {"VSCode_User", "VSCodeInsiders_User", "Cursor_User", "Windsurf_User"}

        for label, src in folder_targets:
            self.wait_if_paused()
            if not src.exists():
                self.log_obj.debug(f"Skip (not found): {label}")
                continue
            dst = root / label
            try:
                if label in editor_user_names and src.is_dir():
                    self._copy_editor_user(src, dst, label)
                else:
                    self.copy_tree_safe(src, dst, label)
            except Exception as e:
                self.log_obj.error(f"{label} failed (continuing): {e}")

        # Kilo Code VS Code extension storage (settings/state) — can be useful
        for code_name, base in [("Code", appdata / "Code"), ("CodeInsiders", appdata / "Code - Insiders"), ("Cursor", appdata / "Cursor")]:
            gs = base / "User" / "globalStorage" / "kilocode.kilo-code"
            if gs.exists():
                self.copy_tree_safe(gs, root / f"KiloCode_GlobalStorage_{code_name}", f"Kilo Code storage ({code_name})")

        # Windows Terminal
        wt_pkg = localappdata / "Packages"
        if wt_pkg.exists():
            for pkg in wt_pkg.glob("Microsoft.WindowsTerminal_*"):
                ls = pkg / "LocalState"
                if ls.exists():
                    self.copy_tree_safe(ls, root / "WindowsTerminal_LocalState", "Windows Terminal settings")
                    break

        # PowerShell profiles
        ps_targets = [
            ("PowerShell_Profile", docs / "PowerShell"),
            ("WindowsPowerShell_Profile", docs / "WindowsPowerShell"),
        ]
        for label, src in ps_targets:
            if src.exists():
                self.copy_tree_safe(src, root / label, label)

        # Small config files
        file_targets = [
            ("npmrc", profile / ".npmrc"),
            ("yarnrc", profile / ".yarnrc"),
            ("pip_ini", appdata / "pip" / "pip.ini"),
            ("cargo_config", profile / ".cargo" / "config.toml"),
            ("cargo_config_alt", profile / ".cargo" / "config"),
            ("vimrc", profile / "_vimrc"),
            ("gvimrc", profile / "_gvimrc"),
        ]
        files_dir = root / "ConfigFiles"
        for label, src in file_targets:
            self.wait_if_paused()
            if not src.exists() or not src.is_file():
                continue
            try:
                self._ensure_dir(files_dir)
                shutil.copy2(src, files_dir / f"{label}{src.suffix or '.txt'}")
                self.log_obj.ok(f"Saved config file: {src.name}")
            except Exception as e:
                self.log_obj.warn(f"Config file {src.name} failed: {e}")

        # Extension lists (lightweight; avoids copying huge extension binaries)
        ext_dir = root / "ExtensionLists"
        self._ensure_dir(ext_dir)
        cli_cmds = [
            ("vscode_extensions", "code", ["--list-extensions", "--show-versions"]),
            ("vscode_insiders_extensions", "code-insiders", ["--list-extensions", "--show-versions"]),
            ("cursor_extensions", "cursor", ["--list-extensions", "--show-versions"]),
            ("windsurf_extensions", "windsurf", ["--list-extensions", "--show-versions"]),
        ]
        for label, exe, args in cli_cmds:
            self.wait_if_paused()
            if not shutil.which(exe):
                continue
            try:
                r = self._run([exe] + args, timeout=120)
                out = (r.stdout or "").strip()
                if out:
                    (ext_dir / f"{label}.txt").write_text(out + "\n", encoding="utf-8")
                    self.log_obj.ok(f"Exported {label.replace('_', ' ')}")
            except Exception as e:
                self.log_obj.warn(f"{exe} extension list failed: {e}")

        self.log_obj.ok("Coding tool configurations backup finished")

    def _copy_editor_user(self, src: Path, dst: Path, label: str) -> None:
        """Copy editor User configs through the shared scan + parallel copy pipeline."""
        skip_names = {
            "Cache", "CachedData", "Code Cache", "GPUCache", "DawnCache",
            "Service Worker", "logs", "Crashpad", "blob_storage",
            "workspaceStorage", "History", "Backups",
        }
        self.copy_tree_safe(src, dst, label, skip_top_names=skip_names)

    # ---------- full backup orchestration ----------
    def full_backup(self, skip_stages: Optional[List[str]] = None, selection: Optional[Dict[str, Any]] = None) -> Tuple[List[str], bool]:
        skip_stages = skip_stages or []
        selection = selection or {
            "Drivers": True, "Packages": True, "UserData": True, "Bookmarks": True, "Settings": True, "CodingTools": True,
            "user_folders": [f for f in USER_FOLDERS if f not in EXCLUDE_TOP_FOLDERS and f not in ("Videos", "Pictures", "Music")],
            "roaming": list(ROAMING_FOLDERS),
            "dotfiles": True,
        }
        self.is_running = True
        self.is_paused = False
        self.is_cancelled = False
        self._pause_event.set()
        self.completed_stages = []
        self.failed_stages = []
        self.set_total(0)
        self._started_at = time.perf_counter()

        if not self.backup_root:
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
            self.backup_root = DESKTOP / f"WindowsBackup_{ts}"
        self._ensure_dir(self.backup_root)
        self._ensure_dir(self.backup_root / "logs")
        self.log_file = self.backup_root / "BackupRestore.log"
        self.log_obj.log_file = self.log_file
        session_log = self.backup_root / "logs" / f"backup-session-{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        self.log_obj.add_log_file(session_log)

        self.log_obj.header(f"FULL BACKUP STARTED (v{VERSION})")
        self.log_obj.info(f"Target: {self.backup_root}")
        self.log_obj.info(f"Admin: {is_admin()}")
        self.log_obj.info(f"Session log: {session_log}")
        self.log_obj.info("Errors are logged and skipped — backup continues")
        if skip_stages:
            self.log_obj.info(f"Resuming — skip: {', '.join(skip_stages)}")
        self.log_obj.header("")
        self.write_in_progress_manifest(skip_stages)

        try:
            (self.backup_root / "selection.json").write_text(json_dumps(selection), encoding="utf-8")
        except Exception:
            pass

        def run_userdata():
            self.backup_user_data(
                selected_user_folders=selection.get("user_folders"),
                selected_roaming=selection.get("roaming"),
                include_dotfiles=bool(selection.get("dotfiles", True)),
            )

        stage_map = {
            "Drivers": self.backup_drivers,
            "Packages": self.backup_packages,
            "UserData": run_userdata,
            "Bookmarks": self.backup_bookmarks,
            "Settings": self.backup_settings,
            "CodingTools": self.backup_coding_tools,
        }

        stages_to_run = [s for s in ALL_STAGES if selection.get(s, True)]
        self.begin_job("backup", len(stages_to_run))
        self.prepare_incremental_base()

        for stage in stages_to_run:
            if self.is_cancelled:
                self.log_obj.warn(f"Stopped before stage: {stage}")
                break
            if stage in skip_stages:
                self.log_obj.info(f"Skip (already done): {stage}")
                self.completed_stages.append(stage)
                self.begin_stage(stage, "skipped")
                self.end_stage("skipped")
                continue

            self.begin_stage(stage)
            self.log_obj.header(f"--- Stage: {stage} ---")
            try:
                self.wait_if_paused()
                stage_map[stage]()
                self.completed_stages.append(stage)
                self.log_obj.ok(f"Stage finished: {stage}")
            except RuntimeError as e:
                if "Cancelled" in str(e):
                    self.log_obj.warn(f"Stopped during: {stage}")
                    break
                self.log_obj.error(f"Stage FAILED (continuing): {stage} — {e}")
                self.failed_stages.append(stage)
            except Exception as e:
                self.log_obj.error(f"Stage FAILED (continuing): {stage} — {e}")
                self.failed_stages.append(stage)

            self.end_stage("failed" if stage in self.failed_stages else "done")
            self.save_checkpoint()
            self.write_in_progress_manifest(self.completed_stages)
            self.log_obj.info(f"Checkpoint: {', '.join(self.completed_stages) or '(none)'}")

        cancelled = self.is_cancelled
        if not cancelled:
            self.clear_checkpoint()
        self.write_manifest(self.failed_stages, cancelled)

        elapsed = time.perf_counter() - self._started_at if self._started_at else 0.0
        performance = self.performance_report(elapsed)
        try:
            (self.backup_root / "performance.json").write_text(json_dumps(performance), encoding="utf-8")
            self.log_obj.info(
                f"Performance: {performance['MBps']} MB/s • {performance['Files']} files • "
                f"{performance['ElapsedSeconds']}s • {performance['Workers']} workers — saved performance.json"
            )
        except Exception as e:
            self.log_obj.warn(f"Performance report failed: {e}")
        if not cancelled and self.backup_root:
            try:
                report = self.write_integrity_sample(self.backup_root)
                n = int(report.get("SampleCount") or 0)
                if n:
                    self.log_obj.info(f"Integrity: recorded hashes for {n} sampled file(s) — {INTEGRITY_FILE}")
            except Exception as e:
                self.log_obj.warn(f"Integrity sample failed: {e}")
        if self._linked_files or self._skipped_files or getattr(self, "_compact_skipped", 0):
            parts = []
            if self._linked_files:
                parts.append(f"{self._linked_files} linked from the previous backup")
            if self._skipped_files:
                parts.append(f"{self._skipped_files} already present")
            if getattr(self, "_compact_skipped", 0):
                parts.append(f"{self._compact_skipped} unchanged omitted from compact delta")
            self.log_obj.info("Reused files: " + ", ".join(parts))
        self.log_obj.footer(
            f"FULL BACKUP FINISHED ({format_timedelta(elapsed)})" + (" — STOPPED" if cancelled else "")
        )
        if cancelled:
            self.log_obj.warn("Use Resume interrupted backup to continue")
        elif self.failed_stages:
            self.log_obj.warn(f"Stages with errors: {', '.join(self.failed_stages)}")
        else:
            self.log_obj.ok("All selected stages OK")
            try:
                self._auto_retention_after_backup()
            except Exception as e:
                self.log_obj.warn(f"Retention check failed: {e}")

        self.is_running = False
        self.is_paused = False
        return self.failed_stages, cancelled

    def _auto_retention_after_backup(self) -> None:
        settings = load_settings()
        if not settings.get("retention_enabled") or not settings.get("retention_auto_after_backup"):
            return
        keep_last = int(settings.get("retention_keep_last") or 0)
        max_age_days = int(settings.get("retention_max_age_days") or 0)
        if not keep_last and not max_age_days:
            return
        parent = self.backup_root.parent if self.backup_root else None
        if parent is None:
            return
        plan = self.plan_retention(parent, keep_last=keep_last, max_age_days=max_age_days, protect=[self.backup_root])
        if not plan.get("remove"):
            return
        self.log_obj.info(f"Retention: removing {len(plan['remove'])} old backup(s) after successful backup")
        result = self.apply_retention(plan)
        if result.get("failed"):
            self.log_obj.warn(f"Retention finished with {len(result['failed'])} issue(s)")

    # ---------- restore helpers ----------
    @staticmethod
    def _read_backup_manifest(root: Path) -> Dict[str, Any]:
        try:
            mp = Path(root) / "manifest.json"
            if mp.exists():
                data = json_loads(mp.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def resolve_restore_chain(self, root: Path) -> Tuple[List[Path], List[str]]:
        """Return (roots_to_apply_oldest_to_newest, warnings).

        Compact/changed-only backups are applied after their base backup so restore
        sees the merged result. Full/incremental(hardlinked) backups are self-contained.
        """
        root = Path(root)
        warnings: List[str] = []
        manifest = self._read_backup_manifest(root)
        mode = str(self._manifest_get(manifest, "BackupMode", "Mode", default="") or "").strip().lower()
        compact = mode == "compact" or bool(manifest.get("Compact")) or manifest.get("SelfContained") is False
        if not compact:
            return [root], warnings
        base_ref = str(self._manifest_get(manifest, "BaseBackup", "IncrementalBase", "Base", default="") or "").strip()
        if not base_ref:
            warnings.append("Compact backup has no base reference; restoring only files present in this backup.")
            return [root], warnings
        base = Path(base_ref)
        if not base.is_absolute():
            base = (root.parent / base).resolve()
        if not base.exists():
            warnings.append(f"Compact backup base is missing: {base}. Restore may be incomplete.")
            return [root], warnings
        base_chain, base_warnings = self.resolve_restore_chain(base)
        warnings.extend(base_warnings)
        chain = base_chain + [root]
        # de-dupe while preserving order
        seen = set()
        out = []
        for item in chain:
            try:
                key = str(item.resolve())
            except Exception:
                key = str(item)
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out, warnings

    def _restore_source_exists(self, chain: List[Path], rel: str) -> Optional[Path]:
        for root in reversed(chain):
            p = root / rel
            if p.exists():
                return p
        return None

    def _restore_has_any(self, chain: List[Path], rel: str) -> bool:
        return self._restore_source_exists(chain, rel) is not None

    def _restore_preflight(self, root: Path, chosen: List[str]) -> Tuple[List[str], List[str]]:
        """Validate backup folder and environment before restore. Returns (errors, warnings)."""
        errors: List[str] = []
        warnings: List[str] = []
        root = Path(root)
        if not root.exists() or not root.is_dir():
            return [f"Backup folder not found: {root}"], warnings

        chain, chain_warnings = self.resolve_restore_chain(root)
        warnings.extend(chain_warnings)
        if len(chain) > 1:
            names = " -> ".join(p.name for p in chain)
            self.log_obj.info(f"Compact restore merge chain: {names}")

        manifest = self._read_backup_manifest(root)
        if manifest:
            self.log_obj.info(f"Backup manifest: v{manifest.get('Version','?')} from {manifest.get('Computer','?')}\\{manifest.get('User','?')} at {manifest.get('Time','?')} mode={manifest.get('BackupMode') or ('incremental' if manifest.get('Incremental') else 'full')}")
            if manifest.get("Cancelled"):
                warnings.append("This backup was marked as cancelled/incomplete; some data may be missing.")
            failed = manifest.get("FailedStages") or []
            if failed:
                warnings.append(f"Backup had failed stages: {', '.join(failed)}")
            if str(manifest.get('BackupMode','')).lower() == 'compact' or manifest.get('Compact') or manifest.get('SelfContained') is False:
                warnings.append("Compact/changed-only backup: restore merges missing files from the base backup.")
        else:
            warnings.append("manifest.json not found; backup may be older or incomplete.")

        needs_admin = [s for s in chosen if s in {"Drivers", "Settings"}]
        if needs_admin and not is_admin():
            warnings.append("Not running as Administrator: driver install and hosts/system restore may fail or be partial.")
        if "Winget" in chosen and not shutil.which("winget"):
            warnings.append("Winget is not available. Install 'App Installer' from Microsoft Store or restore packages manually.")
        if "Chocolatey" in chosen and not shutil.which("choco"):
            warnings.append("Chocolatey is not installed. Chocolatey package restore will be skipped unless you install it first.")

        expected = {
            "Drivers": "Drivers",
            "Winget": "Apps/winget-packages.json",
            "Chocolatey": "Apps/chocolatey-install.ps1",
            "UserData": "UserData",
            "Bookmarks": "BrowserBookmarks",
            "Settings": "Settings",
            "CodingTools": "CodingTools",
        }
        for item in chosen:
            rel = expected.get(item)
            if rel and not self._restore_has_any(chain, rel):
                warnings.append(f"{item}: expected backup data missing ({Path(rel).name}) in backup/base chain — that part will be skipped.")
        return errors, warnings

    def preview_restore_plan(self, root: Path, chosen: List[str]) -> Tuple[List[str], List[str], List[str]]:
        """Read-only restore preview: (errors, warnings, plan_lines)."""
        errors, warnings = self._restore_preflight(root, chosen)
        plan: List[str] = []
        root = Path(root)
        chain, chain_warnings = self.resolve_restore_chain(root)
        for w in chain_warnings:
            if w not in warnings:
                warnings.append(w)
        if len(chain) > 1:
            plan.append("Compact merge: " + " -> ".join(p.name for p in chain))
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            try:
                data = json_loads(manifest_path.read_text(encoding="utf-8"))
                mode = data.get('BackupMode') or ('incremental' if data.get('Incremental') else 'full')
                plan.append(f"Backup: {data.get('Computer','?')}\\{data.get('User','?')} | v{data.get('Version','?')} | {mode} | {data.get('Time','?')}")
            except Exception:
                plan.append("Backup manifest present but unreadable.")
        expected = {
            "Drivers": ("Drivers", "Install/exported driver packages from Drivers (pnputil)."),
            "Winget": ("Apps/winget-packages.json", "Import Winget packages/apps list (best effort)."),
            "Chocolatey": ("Apps/chocolatey-install.ps1", "Run Chocolatey install script if present/choco available."),
            "UserData": ("UserData", f"Copy user folders/files into {Path(os.environ.get('USERPROFILE','~'))} (may overwrite)."),
            "Bookmarks": ("BrowserBookmarks", "Copy browser bookmark/profile data into local browser locations."),
            "Settings": ("Settings", "Restore supported system/user settings (may need admin/reboot)."),
            "CodingTools": ("CodingTools", "Restore coding tool configs (FreeBuff/Kilo Code/VS Code family/Terminal/profiles) to current user paths."),
        }
        for item in chosen:
            rel, action = expected.get(item, (None, "Unknown restore action."))
            if rel is None:
                plan.append(f"[{item}] {action}")
                continue
            src = self._restore_source_exists(chain, rel)
            if src is not None:
                detail = ""
                if src.is_dir():
                    try:
                        count = sum(1 for rr in chain if (rr / rel).exists() for _ in (rr / rel).rglob("*") if _.is_file())
                        detail = f" (files detected across merged chain: ~{count})"
                    except Exception:
                        detail = f" (found in {src.parent.name}/{src.name})"
                else:
                    detail = f" (from {src.parent.name})"
                plan.append(f"[READY] {item}: {action} Source: {src.name}{detail}")
            else:
                plan.append(f"[SKIP/MISSING] {item}: {action} Missing: {Path(rel).name}")
        plan.append("")
        plan.append("Dry-run only: no files copied, no apps installed, no settings changed.")
        return errors, warnings, plan

    def restore_diff(self, root: Path, chosen: List[str]) -> Tuple[List[str], Dict[str, Any]]:
        """Read-only comparison of a backup against current user paths."""
        errors: List[str] = []
        report: Dict[str, Any] = {}
        root = Path(root)
        if not root.exists():
            return [f"Backup folder not found: {root}"], report
        profile = Path.home()
        appdata = Path(os.environ.get("APPDATA", profile / "AppData" / "Roaming"))
        localappdata = Path(os.environ.get("LOCALAPPDATA", profile / "AppData" / "Local"))
        userprofile = Path(os.environ.get("USERPROFILE", profile))

        def new_entry(diff: bool = True, note: str = "") -> Dict[str, Any]:
            return {
                "new": 0, "overwrite": 0, "same": 0, "files": 0, "bytes": 0,
                "samples": [], "diff": diff, "note": note,
            }

        def merge(into: Dict[str, Any], part: Optional[Dict[str, Any]]) -> None:
            if not part:
                return
            for key in ("new", "overwrite", "same", "files", "bytes"):
                into[key] += int(part.get(key) or 0)
            room = 12 - len(into["samples"])
            if room > 0:
                into["samples"].extend(part.get("samples", [])[:room])

        def compare_single(src_file: Path, dst_file: Path, label: str) -> Optional[Dict[str, Any]]:
            try:
                if not src_file.exists():
                    return None
                size = src_file.stat().st_size
            except OSError:
                return None
            entry = new_entry()
            entry["files"] = 1
            entry["bytes"] = size
            if not dst_file.exists():
                entry["new"] = 1
                status = "new"
            else:
                try:
                    same = dst_file.stat().st_size == size
                except OSError:
                    same = False
                if same:
                    entry["same"] = 1
                    status = "same"
                else:
                    entry["overwrite"] = 1
                    status = "overwrite"
            entry["samples"].append((status, label))
            return entry

        def compare_dir(src_dir: Path, dst_dir: Path, rel_prefix: str = "") -> Optional[Dict[str, Any]]:
            try:
                if not src_dir.exists():
                    return None
                if src_dir.is_file():
                    return compare_single(src_dir, dst_dir, src_dir.name)
            except OSError:
                return None
            entry = new_entry()
            stack: List[Tuple[Path, Path, str]] = [(src_dir, dst_dir, rel_prefix)]
            while stack:
                if self.is_cancelled:
                    return entry
                cur_src, cur_dst, rel = stack.pop()
                try:
                    entries = list(os.scandir(str(cur_src)))
                except OSError:
                    continue
                for item in entries:
                    child_rel = f"{rel}/{item.name}" if rel else item.name
                    try:
                        if item.is_dir(follow_symlinks=False):
                            stack.append((Path(item.path), cur_dst / item.name, child_rel))
                        elif item.is_file(follow_symlinks=False):
                            entry["files"] += 1
                            try:
                                size = item.stat(follow_symlinks=False).st_size
                            except OSError:
                                size = 0
                            entry["bytes"] += size
                            target = cur_dst / item.name
                            if not target.exists():
                                entry["new"] += 1
                                status = "new"
                            else:
                                try:
                                    same = target.stat().st_size == size
                                except OSError:
                                    same = False
                                if same:
                                    entry["same"] += 1
                                    status = "same"
                                else:
                                    entry["overwrite"] += 1
                                    status = "overwrite"
                            if len(entry["samples"]) < 12:
                                entry["samples"].append((status, child_rel))
                    except OSError:
                        continue
            return entry

        def count_tree(path: Path, pattern: str = "*") -> Tuple[int, int]:
            files = 0
            total = 0
            try:
                if path.is_file():
                    return 1, path.stat().st_size
                for p in path.rglob(pattern):
                    if p.is_file():
                        files += 1
                        try:
                            total += p.stat().st_size
                        except OSError:
                            pass
            except Exception:
                pass
            return files, total

        if "UserData" in chosen:
            ud = root / "UserData"
            entry = new_entry(True, f"Restores into {profile}")
            if ud.exists():
                try:
                    items = sorted(ud.iterdir(), key=lambda p: p.name.lower())
                except OSError:
                    items = []
                for item in items:
                    if item.name == "AppData_Roaming":
                        merge(entry, compare_dir(item, appdata))
                    elif item.name == "AppData_Local":
                        merge(entry, compare_dir(item, localappdata))
                    elif item.name == "ProfileDotFiles":
                        merge(entry, compare_dir(item, profile))
                    elif item.is_file():
                        merge(entry, compare_single(item, profile / item.name, item.name))
                    else:
                        merge(entry, compare_dir(item, profile / item.name))
            else:
                entry["note"] = "Source missing — will be skipped"
            report["UserData"] = entry

        if "CodingTools" in chosen:
            ct = root / "CodingTools"
            entry = new_entry(True, "Restores coding tool configs")
            ct_map = [
                ("FreeBuff_Desktop", appdata / "Freebuff"),
                ("FreeBuff_CLI", profile / ".config" / "manicode"),
                ("KiloCode_Config", profile / ".config" / "kilo"),
                ("VSCode_User", appdata / "Code" / "User"),
                ("VSCodeInsiders_User", appdata / "Code - Insiders" / "User"),
                ("Cursor_User", appdata / "Cursor" / "User"),
                ("Windsurf_User", appdata / "Windsurf" / "User"),
                ("JetBrains_Config", appdata / "JetBrains"),
                ("Neovim_Config", localappdata / "nvim"),
                ("PowerShell_Profile", userprofile / "Documents" / "PowerShell"),
                ("WindowsPowerShell_Profile", userprofile / "Documents" / "WindowsPowerShell"),
            ]
            if ct.exists():
                for label, dst in ct_map:
                    merge(entry, compare_dir(ct / label, dst))
                try:
                    children = sorted(ct.iterdir())
                except OSError:
                    children = []
                for child in children:
                    if child.name.startswith("KiloCode_GlobalStorage_") and child.is_dir():
                        suffix = child.name.replace("KiloCode_GlobalStorage_", "")
                        base = {
                            "Code": appdata / "Code",
                            "CodeInsiders": appdata / "Code - Insiders",
                            "Cursor": appdata / "Cursor",
                        }.get(suffix)
                        if base:
                            merge(entry, compare_dir(child, base / "User" / "globalStorage" / "kilocode.kilo-code"))
                    elif child.name == "WindowsTerminal_LocalState":
                        files, total = count_tree(child)
                        entry["files"] += files
                        entry["bytes"] += total
                        entry["note"] = "Windows Terminal files restore to the package LocalState folder"
            else:
                entry["note"] = "Source missing — will be skipped"
            report["CodingTools"] = entry

        if "Bookmarks" in chosen:
            bm = root / "BrowserBookmarks"
            entry = new_entry(True, "Browser bookmark files")
            bm_map = {
                "Chrome_Bookmarks": profile / "AppData/Local/Google/Chrome/User Data/Default/Bookmarks",
                "Edge_Bookmarks": profile / "AppData/Local/Microsoft/Edge/User Data/Default/Bookmarks",
                "Brave_Bookmarks": profile / "AppData/Local/BraveSoftware/Brave-Browser/User Data/Default/Bookmarks",
            }
            if bm.exists():
                for name, dst in bm_map.items():
                    merge(entry, compare_single(bm / name, dst, name))
                firefox = list(bm.glob("Firefox_*"))
                if firefox:
                    files, total = count_tree(bm, "Firefox_*")
                    entry["files"] += files
                    entry["bytes"] += total
                    entry["note"] = "Firefox bookmark files are reported for manual copy"
            else:
                entry["note"] = "Source missing — will be skipped"
            report["Bookmarks"] = entry

        if "Settings" in chosen:
            settings_dir = root / "Settings"
            entry = new_entry(False, "Registry, Wi-Fi, hosts, and power plan steps")
            if settings_dir.exists():
                files, total = count_tree(settings_dir)
                entry["files"] = files
                entry["bytes"] = total
            else:
                entry["note"] = "Source missing — will be skipped"
            report["Settings"] = entry

        if "Drivers" in chosen:
            drivers = root / "Drivers"
            entry = new_entry(False, "Installed with pnputil (admin recommended)")
            if drivers.exists():
                files, total = count_tree(drivers, "*.inf")
                entry["files"] = files
                entry["bytes"] = total
            else:
                entry["note"] = "Source missing — will be skipped"
            report["Drivers"] = entry

        if "Winget" in chosen:
            src = root / "Apps" / "winget-packages.json"
            entry = new_entry(False, "App install step (winget import)")
            if src.exists():
                entry["files"] = 1
                try:
                    entry["bytes"] = src.stat().st_size
                except OSError:
                    pass
            else:
                entry["note"] = "Package list missing — will be skipped"
            report["Winget"] = entry

        if "Chocolatey" in chosen:
            src = root / "Apps" / "chocolatey-install.ps1"
            entry = new_entry(False, "App install step (Chocolatey script)")
            if src.exists():
                entry["files"] = 1
                try:
                    entry["bytes"] = src.stat().st_size
                except OSError:
                    pass
            else:
                entry["note"] = "Chocolatey script missing — will be skipped"
            report["Chocolatey"] = entry

        return errors, report

    def _copy_restore_path(self, src: Path, dst: Path, label: str, stats: Dict[str, int]) -> None:
        """Copy file/dir during restore with retries and stats; never raises to caller."""
        try:
            if src.is_dir():
                self._ensure_dir(dst)
                for child in src.iterdir():
                    if self.is_cancelled:
                        return
                    self._copy_restore_path(child, dst / child.name, f"{label}\\{child.name}", stats)
            else:
                self._ensure_dir(dst.parent)
                for attempt in range(MAX_FS_RETRIES):
                    if self.is_cancelled:
                        return
                    try:
                        try:
                            size = src.stat().st_size
                        except OSError:
                            size = 0
                        shutil.copy2(long_path(src), long_path(dst))
                        stats["files"] = stats.get("files", 0) + 1
                        self.progress_add(nbytes=size, files=1, item=str(src))
                        return
                    except Exception as e:
                        if attempt == MAX_FS_RETRIES - 1:
                            stats["errors"] = stats.get("errors", 0) + 1
                            self.log_obj.error(f"{label} failed: {e}")
                            return
                        time.sleep(DEFAULT_BACKOFF_SECONDS[min(attempt, len(DEFAULT_BACKOFF_SECONDS) - 1)])
        except Exception as e:
            stats["errors"] = stats.get("errors", 0) + 1
            self.log_obj.error(f"{label} failed: {e}")

    # ---------- restore ----------
    def restore_drivers(self, root: Path) -> None:
        path = root / "Drivers"
        if not path.exists():
            self.log_obj.warn("Drivers folder missing — skip")
            return
        infs = list(path.rglob("*.inf"))
        if not infs:
            self.log_obj.warn("No .inf files in Drivers — skip")
            return
        if not is_admin():
            self.log_obj.warn("Admin recommended for driver install")
        self.log_obj.info(f"Installing drivers ({len(infs)} INF files)...")
        try:
            r = self._run_shell(
                f'pnputil /add-driver "{path}\\*.inf" /subdirs /install',
                timeout=1800,
            )
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            if r.returncode not in (0, None):
                self.log_obj.warn(f"pnputil exit {r.returncode} (may still have installed some)")
                if out:
                    self.log_obj.warn(out[:500])
            else:
                self.log_obj.ok("Drivers install finished — reboot recommended")
                if out:
                    self.log_obj.info(out[:300])
        except Exception as e:
            self.log_obj.error(f"Driver restore error (continuing): {e}")

    def restore_winget(self, root: Path) -> None:
        j = root / "Apps" / "winget-packages.json"
        if not j.exists():
            self.log_obj.warn("winget-packages.json missing — skip")
            return
        if not shutil.which("winget"):
            self.log_obj.error("Winget not available — install App Installer from Store")
            return
        self.log_obj.info("Restoring Winget packages (may take a long time)...")
        try:
            r = self._run(
                ["winget", "import", "-i", str(j), "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
                timeout=None,
            )
            if r.returncode not in (0, None):
                self.log_obj.warn(f"Winget import exit code {r.returncode}")
                err = (r.stderr or r.stdout or "").strip()
                if err:
                    self.log_obj.warn(err[:500])
                self.log_obj.warn("Some packages may have failed — check winget manually")
            else:
                self.log_obj.ok("Winget import finished")
        except Exception as e:
            self.log_obj.error(f"Winget failed (continuing): {e}")

    def restore_chocolatey(self, root: Path) -> None:
        script = root / "Apps" / "chocolatey-install.ps1"
        if not script.exists():
            self.log_obj.warn("chocolatey-install.ps1 missing — skip")
            return
        if not shutil.which("choco"):
            self.log_obj.error("Chocolatey not installed — skip")
            return
        body = script.read_text(encoding="utf-8", errors="replace").strip()
        if not body:
            self.log_obj.warn("Chocolatey script empty — skip")
            return
        self.log_obj.info("Running Chocolatey install script...")
        try:
            r = self._run_shell(
                f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"',
                timeout=None,
            )
            if r.returncode not in (0, None):
                self.log_obj.warn(f"Chocolatey exit {r.returncode} (some packages may have failed)")
            else:
                self.log_obj.ok("Chocolatey done")
        except Exception as e:
            self.log_obj.error(f"Chocolatey error (continuing): {e}")

    def restore_user_data(self, root: Path) -> None:
        ud = root / "UserData"
        if not ud.exists():
            self.log_obj.warn("UserData missing — skip")
            return
        profile = Path.home()
        self.log_obj.info("Restoring user data (existing files will be overwritten)...")
        for item in sorted(ud.iterdir(), key=lambda p: p.name.lower()):
            if not item.exists():
                continue
            name = item.name
            try:
                if name == "AppData_Roaming":
                    dest_base = profile / "AppData" / "Roaming"
                    self._ensure_dir(dest_base)
                    for child in item.iterdir():
                        try:
                            target = dest_base / child.name
                            if child.is_dir():
                                shutil.copytree(str(child), str(target), dirs_exist_ok=True, copy_function=shutil.copy2)
                            else:
                                shutil.copy2(child, target)
                            self.log_obj.ok(f"Restored Roaming\\{child.name}")
                        except Exception as e:
                            self.log_obj.error(f"Roaming\\{child.name} failed: {e}")
                    continue
                if name == "AppData_Local":
                    dest_base = profile / "AppData" / "Local"
                    self._ensure_dir(dest_base)
                    for child in item.iterdir():
                        try:
                            target = dest_base / child.name
                            if child.is_dir():
                                shutil.copytree(str(child), str(target), dirs_exist_ok=True, copy_function=shutil.copy2)
                            else:
                                shutil.copy2(child, target)
                            self.log_obj.ok(f"Restored Local\\{child.name}")
                        except Exception as e:
                            self.log_obj.error(f"Local\\{child.name} failed: {e}")
                    continue
                if name == "ProfileDotFiles":
                    for child in item.iterdir():
                        try:
                            dest = profile / child.name
                            if child.is_dir():
                                shutil.copytree(str(child), str(dest), dirs_exist_ok=True, copy_function=shutil.copy2)
                            else:
                                self._ensure_dir(dest.parent)
                                shutil.copy2(child, dest)
                            self.log_obj.ok(f"Restored {child.name}")
                        except Exception as e:
                            self.log_obj.error(f"{child.name} failed: {e}")
                    continue
                dest = profile / name
                if item.is_dir():
                    self._ensure_dir(dest)
                    shutil.copytree(str(item), str(dest), dirs_exist_ok=True, copy_function=shutil.copy2)
                else:
                    shutil.copy2(item, dest)
                self.log_obj.ok(f"Restored {name}")
            except Exception as e:
                self.log_obj.error(f"Restore {name} failed (continuing): {e}")

    def restore_bookmarks(self, root: Path) -> None:
        bm = root / "BrowserBookmarks"
        if not bm.exists():
            self.log_obj.warn("BrowserBookmarks missing — skip")
            return
        profile = Path.home()
        mapping = {
            "Chrome_Bookmarks": profile / "AppData/Local/Google/Chrome/User Data/Default/Bookmarks",
            "Edge_Bookmarks": profile / "AppData/Local/Microsoft/Edge/User Data/Default/Bookmarks",
            "Brave_Bookmarks": profile / "AppData/Local/BraveSoftware/Brave-Browser/User Data/Default/Bookmarks",
        }
        any_ok = False
        for name, dst in mapping.items():
            src = bm / name
            if not src.exists():
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(long_path(src), long_path(dst))
                self.log_obj.ok(f"{name} restored")
                any_ok = True
            except Exception as e:
                self.log_obj.warn(f"{name} restore failed (close the browser and retry): {e}")
        for src in bm.glob("Firefox_*_places.sqlite"):
            self.log_obj.info(f"Firefox file present: {src.name} — copy manually into your Firefox profile if needed")
        if not any_ok and not list(bm.glob("Firefox_*")):
            self.log_obj.warn("No browser bookmark files found in backup")

    def restore_settings(self, root: Path) -> None:
        settings = root / "Settings"
        if not settings.exists():
            self.log_obj.warn("Settings missing — skip")
            return
        for reg in sorted(settings.glob("*.reg")):
            try:
                r = self._run([_REG, "import", str(reg)], timeout=60)
                if r.returncode not in (0, None):
                    self.log_obj.warn(f"Import failed {reg.name}: {(r.stderr or '')[:200]}")
                else:
                    self.log_obj.ok(f"Imported {reg.name}")
            except Exception as e:
                self.log_obj.warn(f"Failed {reg.name}: {e}")
        for xml in settings.glob("*.xml"):
            if xml.name.lower() == "startlayout.xml":
                continue
            try:
                r = self._run_shell(
                    f'netsh wlan add profile filename="{xml}" user=all',
                    timeout=30,
                )
                if r.returncode in (0, None):
                    self.log_obj.ok(f"Wi-Fi profile: {xml.name}")
                else:
                    self.log_obj.warn(f"Wi-Fi skip {xml.name}: {(r.stderr or r.stdout or '')[:150]}")
            except Exception as e:
                self.log_obj.warn(f"Wi-Fi {xml.name} failed: {e}")
        hosts = settings / "hosts"
        if hosts.exists():
            try:
                dest = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
                shutil.copy2(hosts, dest)
                self.log_obj.ok("Hosts restored")
            except Exception as e:
                self.log_obj.warn(f"Hosts restore failed (need Admin): {e}")
        pow_file = settings / "ActivePowerPlan.pow"
        if pow_file.exists():
            try:
                r = self._run([_POWERCFG, "/import", str(pow_file)], timeout=30)
                if r.returncode in (0, None):
                    self.log_obj.ok("Power plan imported (activate manually if needed)")
            except Exception:
                pass
        self.log_obj.ok("Settings done — logoff/reboot recommended for some changes")

    def restore_coding_tools(self, root: Path) -> None:
        ct = root / "CodingTools"
        if not ct.exists():
            self.log_obj.warn("CodingTools folder missing — skip")
            return
        self.log_obj.info("=== Restoring Coding Tool Configurations ===")
        self.log_obj.warn("Close FreeBuff, Kilo Code, VS Code, Cursor, terminals before restoring for best results.")
        profile = Path.home()
        appdata = Path(os.environ.get("APPDATA", profile / "AppData" / "Roaming"))
        localappdata = Path(os.environ.get("LOCALAPPDATA", profile / "AppData" / "Local"))
        docs = Path(os.environ.get("USERPROFILE", profile)) / "Documents"

        folder_map = [
            ("FreeBuff_Desktop", appdata / "Freebuff"),
            ("FreeBuff_CLI", profile / ".config" / "manicode"),
            ("KiloCode_Config", profile / ".config" / "kilo"),
            ("VSCode_User", appdata / "Code" / "User"),
            ("VSCodeInsiders_User", appdata / "Code - Insiders" / "User"),
            ("Cursor_User", appdata / "Cursor" / "User"),
            ("Windsurf_User", appdata / "Windsurf" / "User"),
            ("JetBrains_Config", appdata / "JetBrains"),
            ("Neovim_Config", localappdata / "nvim"),
            ("PowerShell_Profile", docs / "PowerShell"),
            ("WindowsPowerShell_Profile", docs / "WindowsPowerShell"),
        ]
        for label, dst in folder_map:
            self.wait_if_paused()
            src = ct / label
            if not src.exists():
                continue
            try:
                self._ensure_dir(dst)
                shutil.copytree(str(src), str(dst), dirs_exist_ok=True, copy_function=shutil.copy2)
                self.log_obj.ok(f"Restored: {label}")
            except Exception as e:
                self.log_obj.error(f"Restore {label} failed (continuing): {e}")

        # Kilo global storage variants
        for child in sorted(ct.iterdir()):
            if not child.name.startswith("KiloCode_GlobalStorage_") or not child.is_dir():
                continue
            suffix = child.name.replace("KiloCode_GlobalStorage_", "")
            base_map = {"Code": appdata / "Code", "CodeInsiders": appdata / "Code - Insiders", "Cursor": appdata / "Cursor"}
            base = base_map.get(suffix)
            if not base:
                continue
            dst = base / "User" / "globalStorage" / "kilocode.kilo-code"
            try:
                self._ensure_dir(dst)
                shutil.copytree(str(child), str(dst), dirs_exist_ok=True, copy_function=shutil.copy2)
                self.log_obj.ok(f"Restored Kilo Code storage ({suffix})")
            except Exception as e:
                self.log_obj.warn(f"Kilo storage {suffix} failed: {e}")

        # Windows Terminal
        wt = ct / "WindowsTerminal_LocalState"
        if wt.exists():
            wt_pkg = localappdata / "Packages"
            try:
                for pkg in wt_pkg.glob("Microsoft.WindowsTerminal_*"):
                    dst = pkg / "LocalState"
                    if pkg.exists():
                        self._ensure_dir(dst)
                        shutil.copytree(str(wt), str(dst), dirs_exist_ok=True, copy_function=shutil.copy2)
                        self.log_obj.ok("Restored Windows Terminal settings")
                        break
                else:
                    self.log_obj.warn("Windows Terminal package not found — settings copied to backup only")
            except Exception as e:
                self.log_obj.warn(f"Windows Terminal restore failed: {e}")

        # Config files
        cf = ct / "ConfigFiles"
        if cf.exists():
            file_restore = {
                "npmrc": profile / ".npmrc",
                "yarnrc": profile / ".yarnrc",
                "pip_ini": appdata / "pip" / "pip.ini",
                "cargo_config": profile / ".cargo" / "config.toml",
                "cargo_config_alt": profile / ".cargo" / "config",
                "vimrc": profile / "_vimrc",
                "gvimrc": profile / "_gvimrc",
            }
            for key, dst in file_restore.items():
                matches = list(cf.glob(f"{key}*"))
                if not matches:
                    continue
                try:
                    self._ensure_dir(dst.parent)
                    shutil.copy2(matches[0], dst)
                    self.log_obj.ok(f"Restored config file: {dst.name}")
                except Exception as e:
                    self.log_obj.warn(f"Config file {dst.name} failed: {e}")

        ext_dir = ct / "ExtensionLists"
        if ext_dir.exists():
            self.log_obj.info(f"Extension lists saved in: {ext_dir}")
            self.log_obj.info("To reinstall VS Code extensions: for /f %i in (vscode_extensions.txt) do code --install-extension %i")
        self.log_obj.ok("Coding tool configurations restore finished")


# ---------------------------------------------------------------------------
# Dynamic step scheduler: runs EXACTLY the stages requested and adaptively
# determines the number of worker threads (not to exceed _NTHREADS).
# ---------------------------------------------------------------------------
def _scheduler(names: Iterable[str]) -> List[str]:
    return [n for n in names]


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Windows Backup & Restore Toolkit v{VERSION}")
        try:
            _icon_png = Path(__file__).with_name("app_icon.png")
            if _icon_png.exists():
                self._app_icon_image = tk.PhotoImage(file=str(_icon_png))
                self.iconphoto(True, self._app_icon_image)
        except Exception:
            pass
        self.geometry("1180x840")
        self.minsize(1020, 720)
        self.resizable(True, True)
        self.settings = load_settings()
        theme = str(self.settings.get("theme") or "dark").lower()
        self.theme_name = "light" if theme == "light" else "dark"
        self._colors = app_palette(self.theme_name)
        self.configure(bg=self._colors["bg"])
        self.toolkit = BackupToolkit(self.append_log)
        try:
            self.toolkit.copy_workers = max(1, min(16, int(self.settings.get("copy_workers") or 4)))
        except Exception:
            self.toolkit.copy_workers = 4
        self.toolkit.backup_mode = normalize_backup_mode(self.settings.get("backup_mode"), self.settings.get("incremental"))
        self.toolkit.incremental_enabled = self.toolkit.backup_mode == "incremental"
        self._apply_skip_rules_to_toolkit()
        self.backup_parent = DESKTOP
        saved_parent = str(self.settings.get("backup_parent") or "")
        if saved_parent:
            try:
                p = Path(saved_parent)
                if p.exists() and p.is_dir():
                    self.backup_parent = p
            except Exception:
                pass
        self.recent_sources = load_recent_sources()
        self._log_history: List[Tuple[str, str]] = []
        self._current_page = str(self.settings.get("last_page") or "Dashboard")
        self._build()
        self._proc_sig: Optional[Tuple[Any, ...]] = None
        geo = str(self.settings.get("window_geometry") or "")
        if geo:
            try:
                self.geometry(geo)
            except Exception:
                pass
        self.after(300, self._tick_progress)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.append_log(f"GUI loaded v{VERSION}")
        self.append_log(f"Desktop path: {DESKTOP}")
        self.append_log(f"Admin: {is_admin()}")
        if not is_admin():
            self.append_log("Tip: Run as Administrator for drivers & hosts", "WARN")
        self.append_log("Includes: Drivers, Packages, UserData, Bookmarks, Settings, Coding Tools (FreeBuff/Kilo Code)")
        if BackupToolkit.load_checkpoint():
            self.append_log("Interrupted backup found — use Resume interrupted backup", "WARN")

    def _apply_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        c = self._colors
        style.configure("App.TFrame", background=c["bg"])
        style.configure("Panel.TFrame", background=c["panel"])
        style.configure("Card.TLabelframe", background=c["card"], foreground=c["text"], borderwidth=1, relief="solid")
        style.configure("Card.TLabelframe.Label", background=c["card"], foreground=c["accent2"], font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background=c["bg"], foreground=c["text"], font=("Segoe UI", 9))
        style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"], font=("Segoe UI", 9))
        style.configure("Panel.TLabel", background=c["panel"], foreground=c["text"], font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=c["bg"], foreground=c["text"], font=("Segoe UI", 18, "bold"))
        style.configure("Sub.TLabel", background=c["bg"], foreground=c["muted"], font=("Segoe UI", 10))
        style.configure("Accent.Horizontal.TProgressbar", troughcolor=c["panel2"], background=c["accent2"], bordercolor=c["panel2"], lightcolor=c["accent2"], darkcolor=c["accent"])
        style.configure("TNotebook", background=c["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=c["panel2"], foreground=c["muted"], padding=(12, 5))
        style.map("TNotebook.Tab", background=[("selected", c["card"])], foreground=[("selected", c["accent2"])])
        style.configure(
            "Treeview",
            background=c["log_bg"], fieldbackground=c["log_bg"], foreground=c["text"],
            rowheight=22, borderwidth=0, font=("Segoe UI", 9),
        )
        style.configure("Treeview.Heading", background=c["panel2"], foreground=c["text"], relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", c["accent"])], foreground=[("selected", "white")])

    def _chip(self, parent: tk.Widget, text: str, fg: str) -> tk.Label:
        c = self._colors
        lbl = tk.Label(parent, text=text, bg=c["panel2"], fg=fg, font=("Segoe UI", 9, "bold"), padx=10, pady=4)
        lbl.pack(side="left", padx=(0, 8))
        return lbl

    def _mk_button(self, parent: tk.Widget, text: str, command: Callable, bg: str, fg: str = "white", height: int = 1, width: Optional[int] = None, state: str = "normal") -> tk.Button:
        opts = {
            "text": text, "command": command, "bg": bg, "fg": fg,
            "activebackground": bg, "activeforeground": fg,
            "relief": "flat", "bd": 0, "cursor": "hand2",
            "font": ("Segoe UI", 9, "bold"), "height": height, "state": state,
            "padx": 10, "pady": 4,
        }
        if width is not None:
            opts["width"] = width
        return tk.Button(parent, **opts)

    def _build(self):
        self._apply_style()
        c = self._colors
        self._pages: Dict[str, tk.Frame] = {}
        self._nav_buttons: Dict[str, tk.Button] = {}
        self._current_page = getattr(self, "_current_page", None) or "Dashboard"

        shell = tk.Frame(self, bg=c["bg"])
        shell.pack(fill="both", expand=True)

        # ---------------- sidebar ----------------
        sidebar = tk.Frame(shell, bg=c["panel"], width=210)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=c["panel"])
        brand.pack(fill="x", padx=16, pady=(18, 6))
        tk.Label(brand, text="WINDOWS", bg=c["panel"], fg=c["accent2"], font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x")
        tk.Label(brand, text="Backup Toolkit", bg=c["panel"], fg=c["text"], font=("Segoe UI", 16, "bold"), anchor="w").pack(fill="x")
        tk.Label(brand, text=f"v{VERSION}", bg=c["panel"], fg=c["muted"], font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(3, 0))

        sep = tk.Frame(sidebar, bg=c["panel2"], height=1)
        sep.pack(fill="x", padx=14, pady=(10, 8))

        nav = tk.Frame(sidebar, bg=c["panel"])
        nav.pack(fill="x")
        for name, icon in (
            ("Dashboard", "◉"),
            ("Backup", "↓"),
            ("Restore", "↩"),
            ("Tools", "⚙"),
            ("Settings", "☰"),
        ):
            btn = tk.Button(
                nav,
                text=f"  {icon}   {name}",
                command=lambda n=name: self._show_page(n),
                bg=c["panel"], fg=c["muted"],
                activebackground=c["panel2"], activeforeground=c["text"],
                relief="flat", bd=0, anchor="w", padx=12, pady=9,
                font=("Segoe UI", 10), cursor="hand2",
            )
            btn.pack(fill="x", padx=8, pady=1)
            self._nav_buttons[name] = btn

        side_bottom = tk.Frame(sidebar, bg=c["panel"])
        side_bottom.pack(side="bottom", fill="x", padx=14, pady=12)
        tk.Frame(side_bottom, bg=c["panel2"], height=1).pack(fill="x", pady=(0, 8))
        self.admin_chip = tk.Label(
            side_bottom,
            text="Admin: Yes" if is_admin() else "Admin: No (limited)",
            bg=c["panel2"], fg=c["ok"] if is_admin() else c["warn"],
            font=("Segoe UI", 8, "bold"), anchor="w", padx=8, pady=5,
        )
        self.admin_chip.pack(fill="x", pady=2)
        self.mode_chip = tk.Label(
            side_bottom, text="Ready", bg=c["panel2"], fg=c["ok"],
            font=("Segoe UI", 8, "bold"), anchor="w", padx=8, pady=5,
        )
        self.mode_chip.pack(fill="x", pady=2)

        # ---------------- main area ----------------
        main = tk.Frame(shell, bg=c["bg"])
        main.pack(side="left", fill="both", expand=True)

        topbar = tk.Frame(main, bg=c["bg"])
        topbar.pack(fill="x", padx=18, pady=(14, 2))
        tk.Label(
            topbar, text="Windows Backup & Restore Toolkit", bg=c["bg"], fg=c["text"],
            font=("Segoe UI", 17, "bold"), anchor="w",
        ).pack(side="left")
        self._secondary_button(
            topbar,
            "Light theme" if self.theme_name == "dark" else "Dark theme",
            self._toggle_theme,
            width=15,
        ).pack(side="right")

        self.page_title = tk.Label(
            main, text="Dashboard — overview", bg=c["bg"], fg=c["muted"],
            font=("Segoe UI", 9), anchor="w",
        )
        self.page_title.pack(fill="x", padx=18, pady=(0, 6))

        progress_wrap = ttk.Frame(main, style="App.TFrame")
        progress_wrap.pack(fill="x", padx=18, pady=(0, 8))
        top_row = ttk.Frame(progress_wrap, style="App.TFrame")
        top_row.pack(fill="x")
        self.progress_label = ttk.Label(top_row, text="Idle — select a backup or restore action", style="Muted.TLabel")
        self.progress_label.pack(side="left", anchor="w")
        self.progress_pct = ttk.Label(top_row, text="", style="Muted.TLabel")
        self.progress_pct.pack(side="right")
        self.progress = ttk.Progressbar(progress_wrap, mode="determinate", maximum=100, style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(4, 2))
        meta = ttk.Frame(progress_wrap, style="App.TFrame")
        meta.pack(fill="x")
        self.progress_speed = ttk.Label(meta, text="", style="Muted.TLabel")
        self.progress_speed.pack(side="left")
        self.progress_eta = ttk.Label(meta, text="", style="Muted.TLabel")
        self.progress_eta.pack(side="left", padx=(14, 0))
        self.progress_files = ttk.Label(meta, text="", style="Muted.TLabel")
        self.progress_files.pack(side="right")

        host = tk.Frame(main, bg=c["bg"])
        host.pack(fill="both", expand=True, padx=14)
        self._page_host = host
        self._build_dashboard_page(host)
        self._build_backup_page(host)
        self._build_restore_page(host)
        self._build_tools_page(host)
        self._build_settings_page(host)

        lower = ttk.Notebook(main)
        lower.pack(fill="x", padx=14, pady=(8, 4))

        log_wrap = tk.Frame(lower, bg=c["card"])
        lower.add(log_wrap, text="  Activity Log  ")
        log_inner = tk.Frame(log_wrap, bg=c["card"])
        log_inner.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt = tk.Text(
            log_inner, height=8, bg=c["log_bg"], fg=c["log_fg"],
            insertbackground=c["text"], selectbackground=c["accent"],
            font=("Consolas", 9), wrap="word", relief="flat", bd=0, padx=8, pady=8,
        )
        sb = ttk.Scrollbar(log_inner, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.txt.tag_configure("INFO", foreground="#86efac")
        self.txt.tag_configure("OK", foreground="#4ade80")
        self.txt.tag_configure("WARN", foreground="#fbbf24")
        self.txt.tag_configure("ERROR", foreground="#f87171")
        self.txt.tag_configure("FATAL", foreground="#fb7185")
        self.txt.tag_configure("DEBUG", foreground="#94a3b8")
        self.txt.configure(state="disabled")

        proc_wrap = tk.Frame(lower, bg=c["card"])
        lower.add(proc_wrap, text="  Processes  ")
        tk.Label(
            proc_wrap,
            text="Live external commands started by the toolkit. Double-click a row for full output.",
            bg=c["card"], fg=c["muted"], anchor="w", font=("Segoe UI", 8),
        ).pack(side="bottom", fill="x", padx=6, pady=(0, 4))
        cols = ("pid", "label", "elapsed", "status", "last")
        self.proc_tree = ttk.Treeview(proc_wrap, columns=cols, show="headings", height=6)
        for key, text, width, anchor, stretch in (
            ("pid", "PID", 70, "center", False),
            ("label", "Tool / Step", 150, "w", False),
            ("elapsed", "Elapsed", 90, "center", False),
            ("status", "Status", 110, "center", False),
            ("last", "Last output line", 520, "w", True),
        ):
            self.proc_tree.heading(key, text=text)
            self.proc_tree.column(key, width=width, anchor=anchor, stretch=stretch)
        proc_scroll = ttk.Scrollbar(proc_wrap, command=self.proc_tree.yview)
        self.proc_tree.configure(yscrollcommand=proc_scroll.set)
        self.proc_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        proc_scroll.pack(side="right", fill="y", pady=6)
        self.proc_tree.bind("<Double-1>", lambda _e: self._show_process_output())

        self.status = tk.Label(
            main,
            text=f"Ready  •  Backup location: {self.backup_parent}",
            bg=c["panel"], fg=c["muted"], anchor="w", font=("Segoe UI", 9),
            padx=14, pady=6,
        )
        self.status.pack(fill="x", side="bottom")

        self._action_buttons = [self.btn_full, self.btn_resume_cp, self.btn_restore]
        self._show_page(self._current_page)

    # ---------------- pages ----------------
    def _secondary_button(self, parent: tk.Widget, text: str, command: Callable, width: Optional[int] = None, height: int = 1) -> tk.Button:
        c = self._colors
        return self._mk_button(parent, text, command, c["panel2"], fg=c["text"], width=width, height=height)

    def _stat_card(self, parent: tk.Widget, title: str) -> tk.Label:
        c = self._colors
        card = tk.Frame(parent, bg=c["card"], highlightbackground=c["panel2"], highlightthickness=1)
        card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        tk.Label(
            card, text=title.upper(), bg=c["card"], fg=c["muted"],
            font=("Segoe UI", 8, "bold"), anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 0))
        value = tk.Label(
            card, text="—", bg=c["card"], fg=c["text"],
            font=("Segoe UI", 13, "bold"), anchor="w", justify="left", wraplength=220,
        )
        value.pack(fill="x", padx=12, pady=(2, 10))
        return value

    def _build_dashboard_page(self, host: tk.Widget) -> None:
        c = self._colors
        page = tk.Frame(host, bg=c["bg"])
        self._pages["Dashboard"] = page

        row = tk.Frame(page, bg=c["bg"])
        row.pack(fill="x", pady=(4, 10))
        self.dash_latest = self._stat_card(row, "Latest backup")
        self.dash_count = self._stat_card(row, "Backups found")
        self.dash_size = self._stat_card(row, "Latest size")
        self.dash_efficiency = self._stat_card(row, "Data written / reused")
        self.dash_free = self._stat_card(row, "Free space")

        actions = tk.Frame(page, bg=c["bg"])
        actions.pack(fill="x", pady=(0, 10))
        self._mk_button(actions, "⚡  New Full Backup", self._start_full, c["accent"], height=2).pack(side="left", padx=(0, 8))
        self._mk_button(actions, "↩  Restore...", self._restore_dialog, c["ok"], height=2).pack(side="left", padx=(0, 8))
        self._secondary_button(actions, "Preview differences", self._diff_dialog, height=2).pack(side="left", padx=(0, 8))
        self._secondary_button(actions, "Refresh", self._refresh_dashboard, height=2).pack(side="left")

        health_row = tk.Frame(page, bg=c["bg"])
        health_row.pack(fill="x", pady=(0, 8))
        self.dash_health = tk.Label(
            health_row, text="Backup health: —", bg=c["bg"], fg=c["muted"],
            font=("Segoe UI", 9), anchor="w",
        )
        self.dash_health.pack(side="left")

        sources = ttk.LabelFrame(page, text=" Remembered restore sources ", style="Card.TLabelframe", padding=8)
        sources.pack(fill="both", expand=True)
        self.dash_sources = tk.Listbox(
            sources, bg=c["log_bg"], fg=c["text"], selectbackground=c["accent"],
            font=("Consolas", 9), height=6, relief="flat", bd=0,
            highlightthickness=0,
        )
        self.dash_sources.pack(fill="both", expand=True)
        src_btns = tk.Frame(sources, bg=c["card"])
        src_btns.pack(fill="x", pady=(6, 0))
        self._secondary_button(src_btns, "Restore selected", self._dashboard_restore_selected).pack(side="left")
        self._secondary_button(src_btns, "Forget selected", self._dashboard_forget_selected).pack(side="left", padx=6)
        self._secondary_button(src_btns, "Add source...", self._dashboard_add_source).pack(side="left")

    def _build_backup_page(self, host: tk.Widget) -> None:
        c = self._colors
        page = tk.Frame(host, bg=c["bg"])
        self._pages["Backup"] = page

        left = ttk.LabelFrame(page, text=" Start a backup ", style="Card.TLabelframe", padding=12)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=4)
        self.btn_full = self._mk_button(left, "⚡  Full Backup (choose items)", self._start_full, c["accent"], height=2)
        self.btn_full.pack(fill="x", pady=(0, 8))

        ctrl = tk.Frame(left, bg=c["card"])
        ctrl.pack(fill="x", pady=2)
        self.btn_pause = self._mk_button(ctrl, "Pause", self._toggle_pause, c["warn"], width=14, state="disabled")
        self.btn_pause.pack(side="left", padx=(0, 6))
        self.btn_stop = self._mk_button(ctrl, "Stop", self._stop, c["danger"], width=14, state="disabled")
        self.btn_stop.pack(side="left")

        self.btn_resume_cp = self._secondary_button(left, "Resume interrupted backup", self._resume_checkpoint)
        self.btn_resume_cp.pack(fill="x", pady=(10, 4))

        tk.Label(
            left,
            text="Works from a checkpoint or the latest unfinished backup; files already copied are skipped.",
            bg=c["card"], fg=c["muted"], font=("Segoe UI", 8), anchor="w", justify="left", wraplength=360,
        ).pack(fill="x", pady=(2, 8))

        self.backup_parent_label = tk.Label(
            left, text=f"Backup location: {self.backup_parent}",
            bg=c["card"], fg=c["muted"], font=("Segoe UI", 8), anchor="w", justify="left", wraplength=360,
        )
        self.backup_parent_label.pack(fill="x", pady=(4, 4))
        self._secondary_button(left, "Choose backup location...", self._choose_location).pack(fill="x")

        right = ttk.LabelFrame(page, text=" Quick single-stage backups ", style="Card.TLabelframe", padding=12)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        grid = tk.Frame(right, bg=c["card"])
        grid.pack(fill="x")
        stages = [
            ("Drivers", "Drivers"), ("Packages", "Packages"), ("User Data", "UserData"),
            ("Bookmarks", "Bookmarks"), ("Settings", "Settings"), ("Coding Tools", "CodingTools"),
        ]
        for idx, (txt, stage) in enumerate(stages):
            btn = self._secondary_button(grid, txt, lambda s=stage: self._partial(s), width=16)
            btn.grid(row=idx // 2, column=idx % 2, padx=4, pady=4, sticky="ew")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        tk.Label(
            right,
            text=("Tips for a clean backup:\n"
                  "• Close big apps when possible (browsers, editors).\n"
                  "• Smart excludes skip .git, node_modules, build/cache folders.\n"
                  "• Run as Administrator for drivers and system settings.\n"
                  "• Coding tool configs are backed up automatically."),
            bg=c["card"], fg=c["muted"], font=("Segoe UI", 8), anchor="w", justify="left", wraplength=420,
        ).pack(fill="x", pady=(12, 0))

    def _build_restore_page(self, host: tk.Widget) -> None:
        c = self._colors
        page = tk.Frame(host, bg=c["bg"])
        self._pages["Restore"] = page

        left = ttk.LabelFrame(page, text=" Restore ", style="Card.TLabelframe", padding=12)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=4)
        self.btn_restore = self._mk_button(left, "↩  Restore from Backup", self._restore_dialog, c["ok"], height=2)
        self.btn_restore.pack(fill="x", pady=(0, 8))
        self._secondary_button(left, "Preview differences (no changes)", self._diff_dialog, height=2).pack(fill="x", pady=(0, 8))
        self._secondary_button(left, "Browse backup folder to Restore...", self._restore_browse).pack(fill="x", pady=2)
        tk.Label(
            left,
            text=("The restore window lists remembered sources first (marked OK or MISSING). "
                  "Use 'Add source...' there to pick any backup folder, including external drives.\n\n"
                  "A dry-run preview shows new, overwritten, and identical files before anything is changed."),
            bg=c["card"], fg=c["muted"], font=("Segoe UI", 8), anchor="w", justify="left", wraplength=380,
        ).pack(fill="x", pady=(10, 0))

        right = ttk.LabelFrame(page, text=" Remembered sources ", style="Card.TLabelframe", padding=8)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        self.restore_sources_list = tk.Listbox(
            right, bg=c["log_bg"], fg=c["text"], selectbackground=c["accent"],
            font=("Consolas", 9), height=8, relief="flat", bd=0, highlightthickness=0,
        )
        self.restore_sources_list.pack(fill="both", expand=True)
        btns = tk.Frame(right, bg=c["card"])
        btns.pack(fill="x", pady=(6, 0))
        self._secondary_button(btns, "Restore selected", self._restore_selected_source).pack(side="left")
        self._secondary_button(btns, "Forget selected", self._restore_forget_selected).pack(side="left", padx=6)

    def _build_tools_page(self, host: tk.Widget) -> None:
        c = self._colors
        page = tk.Frame(host, bg=c["bg"])
        self._pages["Tools"] = page
        box = ttk.LabelFrame(page, text=" Maintenance & tools ", style="Card.TLabelframe", padding=12)
        box.pack(fill="both", expand=True, pady=4)
        grid = tk.Frame(box, bg=c["card"])
        grid.pack(fill="x")
        tools = [
            ("Verify backup...", self._verify_backup_dialog),
            ("Check integrity (hash sample)...", self._verify_integrity_dialog),
            ("Run copy benchmark", self._run_benchmark),
            ("Clean up old backups...", self._retention_dialog),
            ("Export backup to ZIP", self._export_zip),
            ("Import backup ZIP", self._import_zip),
            ("Open latest backup folder", self._open_latest),
            ("Install / check Winget + Chocolatey", self._install_tools),
            ("Open settings folder", self._open_settings_folder),
        ]
        for idx, (text, cmd) in enumerate(tools):
            btn = self._secondary_button(grid, text, cmd, width=30, height=2)
            btn.grid(row=idx // 2, column=idx % 2, padx=5, pady=5, sticky="ew")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        tk.Label(
            box,
            text=("Verification checks the manifest and stage file counts for the selected backup. "
                  "Export creates a compressed copy; Import unpacks one so it appears in the restore list.\n"
                  "Winget and Chocolatey restore app lists; admin is recommended."),
            bg=c["card"], fg=c["muted"], font=("Segoe UI", 8), anchor="w", justify="left", wraplength=720,
        ).pack(fill="x", pady=(12, 0))

    def _build_settings_page(self, host: tk.Widget) -> None:
        c = self._colors
        page = tk.Frame(host, bg=c["bg"])
        self._pages["Settings"] = page

        appearance = ttk.LabelFrame(page, text=" Appearance ", style="Card.TLabelframe", padding=12)
        appearance.pack(fill="x", pady=4)
        tk.Label(
            appearance, text="Theme", bg=c["card"], fg=c["text"],
            font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(fill="x")
        self._theme_var = tk.StringVar(value=self.theme_name)
        theme_row = tk.Frame(appearance, bg=c["card"])
        theme_row.pack(fill="x", pady=(4, 0))
        for value, label in (("dark", "Dark"), ("light", "Light")):
            tk.Radiobutton(
                theme_row, text=label, variable=self._theme_var, value=value,
                command=lambda v=value: self._set_theme(v),
                bg=c["card"], fg=c["text"], selectcolor=c["panel2"],
                activebackground=c["card"], activeforeground=c["text"],
                font=("Segoe UI", 9), anchor="w",
            ).pack(side="left", padx=(0, 16))
        tk.Label(
            appearance,
            text="The theme is saved and restored the next time you open the toolkit.",
            bg=c["card"], fg=c["muted"], font=("Segoe UI", 8), anchor="w",
        ).pack(fill="x", pady=(4, 0))

        performance = ttk.LabelFrame(page, text=" Copy performance ", style="Card.TLabelframe", padding=12)
        performance.pack(fill="x", pady=4)
        self.workers_label = tk.Label(
            performance, text=f"Parallel copy workers: {self.toolkit.copy_workers}",
            bg=c["card"], fg=c["text"], font=("Segoe UI", 9, "bold"), anchor="w",
        )
        self.workers_label.pack(fill="x")
        worker_row = tk.Frame(performance, bg=c["card"])
        worker_row.pack(fill="x", pady=(4, 0))
        for count in (2, 4, 8):
            self._secondary_button(
                worker_row, f"{count} workers", lambda n=count: self._set_workers(n)
            ).pack(side="left", padx=(0, 6))
        tk.Label(
            performance,
            text=("More workers help on SSDs; fewer are safer on slow USB drives or network shares. "
                  "Copies are bounded so the disk is never flooded."),
            bg=c["card"], fg=c["muted"], font=("Segoe UI", 8), anchor="w", justify="left", wraplength=760,
        ).pack(fill="x", pady=(4, 0))

        behavior = ttk.LabelFrame(page, text=" Backup behavior ", style="Card.TLabelframe", padding=12)
        behavior.pack(fill="x", pady=4)
        self.backup_mode_var = tk.StringVar(value=normalize_backup_mode(self.settings.get("backup_mode"), self.settings.get("incremental")))
        self.incremental_var = tk.BooleanVar(value=self.backup_mode_var.get() == "incremental")
        tk.Label(behavior, text="Backup mode:", bg=c["card"], fg=c["text"], font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x")
        for mode, label in (
            ("full", "Full backup — copy everything selected"),
            ("incremental", "Incremental — complete backup; unchanged files are hardlinked from the previous backup"),
            ("compact", "Compact changed-only — leave unchanged files out entirely; restore merges from the base backup"),
        ):
            tk.Radiobutton(
                behavior, text=label, value=mode, variable=self.backup_mode_var, command=self._set_backup_mode,
                bg=c["card"], fg=c["text"], selectcolor=c["panel2"],
                activebackground=c["card"], activeforeground=c["text"],
                font=("Segoe UI", 9), anchor="w", justify="left", wraplength=760,
            ).pack(fill="x", anchor="w")
        self.skip_summary = tk.Label(
            behavior, text=self._skip_summary_text(), bg=c["card"], fg=c["muted"],
            font=("Segoe UI", 8), anchor="w", justify="left", wraplength=760,
        )
        self.skip_summary.pack(fill="x", pady=(4, 0))
        tk.Label(
            behavior, text="Saved custom rules (folders and files skipped by name):",
            bg=c["card"], fg=c["text"], font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(fill="x", pady=(6, 2))
        list_frame = tk.Frame(behavior, bg=c["card"])
        list_frame.pack(fill="x")
        self.skip_list = tk.Listbox(
            list_frame, height=4, bg=c["log_bg"], fg=c["text"],
            selectbackground=c["accent"], highlightthickness=0, bd=0,
            font=("Consolas", 9),
        )
        self.skip_list.pack(fill="x", expand=True)
        self._refresh_skip_list()
        self._secondary_button(
            behavior, "Edit custom skip rules...", self._skip_rules_dialog
        ).pack(anchor="w", pady=(6, 0))

        retention = ttk.LabelFrame(page, text=" Retention (old backups) ", style="Card.TLabelframe", padding=12)
        retention.pack(fill="x", pady=4)
        self.retention_enabled_var = tk.BooleanVar(value=bool(self.settings.get("retention_enabled")))
        tk.Checkbutton(
            retention,
            text="Enable retention policy for old backups in the backup location",
            variable=self.retention_enabled_var, command=self._save_retention_settings,
            bg=c["card"], fg=c["text"], selectcolor=c["panel2"],
            activebackground=c["card"], activeforeground=c["text"],
            font=("Segoe UI", 9), anchor="w",
        ).pack(fill="x")
        row = tk.Frame(retention, bg=c["card"])
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text="Keep last backups:", bg=c["card"], fg=c["text"], font=("Segoe UI", 9)).pack(side="left")
        self.retention_keep_var = tk.StringVar(value=str(int(self.settings.get("retention_keep_last") or 5)))
        tk.Entry(row, textvariable=self.retention_keep_var, width=5, bg=c["log_bg"], fg=c["text"], insertbackground=c["text"]).pack(side="left", padx=(6, 14))
        tk.Label(row, text="Keep days (0 = ignore):", bg=c["card"], fg=c["text"], font=("Segoe UI", 9)).pack(side="left")
        self.retention_days_var = tk.StringVar(value=str(int(self.settings.get("retention_max_age_days") or 0)))
        tk.Entry(row, textvariable=self.retention_days_var, width=5, bg=c["log_bg"], fg=c["text"], insertbackground=c["text"]).pack(side="left", padx=(6, 0))
        self.retention_auto_var = tk.BooleanVar(value=bool(self.settings.get("retention_auto_after_backup")))
        tk.Checkbutton(
            retention,
            text="Run retention automatically after a successful full backup",
            variable=self.retention_auto_var, command=self._save_retention_settings,
            bg=c["card"], fg=c["text"], selectcolor=c["panel2"],
            activebackground=c["card"], activeforeground=c["text"],
            font=("Segoe UI", 9), anchor="w",
        ).pack(fill="x", pady=(6, 0))
        tk.Label(
            retention,
            text=("Manual cleanup shows a preview before deleting. Interrupted backups are never removed. "
                  "Incremental backups are self-contained hardlink trees, so removing an older backup "
                  "does not remove data still present in newer backups."),
            bg=c["card"], fg=c["muted"], font=("Segoe UI", 8), anchor="w", justify="left", wraplength=760,
        ).pack(fill="x", pady=(4, 0))
        action_row = tk.Frame(retention, bg=c["card"])
        action_row.pack(fill="x", pady=(8, 0))
        self._secondary_button(action_row, "Save retention settings", self._save_retention_settings).pack(side="left")
        self._secondary_button(action_row, "Preview / clean up old backups...", self._retention_dialog).pack(side="left", padx=6)

        location = ttk.LabelFrame(page, text=" Backup location ", style="Card.TLabelframe", padding=12)
        location.pack(fill="x", pady=4)
        self.settings_location_label = tk.Label(
            location, text=str(self.backup_parent), bg=c["card"], fg=c["text"],
            font=("Consolas", 9), anchor="w", justify="left", wraplength=700,
        )
        self.settings_location_label.pack(fill="x")
        self._secondary_button(location, "Change backup location...", self._choose_location).pack(anchor="w", pady=(6, 0))

        about = ttk.LabelFrame(page, text=" About & files ", style="Card.TLabelframe", padding=12)
        about.pack(fill="x", pady=4)
        tk.Label(
            about,
            text=(f"Version {VERSION}  •  Settings: {SETTINGS_FILE}\n"
                  "Settings and remembered restore sources are stored in your user profile."),
            bg=c["card"], fg=c["muted"], font=("Segoe UI", 8), anchor="w", justify="left", wraplength=760,
        ).pack(fill="x")
        row = tk.Frame(about, bg=c["card"])
        row.pack(fill="x", pady=(8, 0))
        self._secondary_button(row, "Open settings folder", self._open_settings_folder).pack(side="left")
        self._secondary_button(row, "Open logs folder", self._open_logs_folder).pack(side="left", padx=6)

    # ---------------- navigation / theme ----------------
    def _show_page(self, name: str) -> None:
        page = self._pages.get(name)
        if page is None:
            name = "Dashboard"
            page = self._pages.get(name)
            if page is None:
                return
        for other in self._pages.values():
            other.pack_forget()
        page.pack(fill="both", expand=True)
        self._current_page = name
        c = self._colors
        for nav_name, btn in self._nav_buttons.items():
            if nav_name == name:
                btn.configure(bg=c["panel2"], fg=c["accent2"], font=("Segoe UI", 10, "bold"))
            else:
                btn.configure(bg=c["panel"], fg=c["muted"], font=("Segoe UI", 10))
        if hasattr(self, "page_title"):
            titles = {
                "Dashboard": "Dashboard — overview and recent sources",
                "Backup": "Backup — choose items or a single stage",
                "Restore": "Restore — remembered sources and diff preview",
                "Tools": "Tools — verify, archive, and install helpers",
                "Settings": "Settings — appearance and locations",
            }
            self.page_title.configure(text=titles.get(name, name))
        try:
            self.settings["last_page"] = name
        except Exception:
            pass
        if name == "Dashboard":
            self._refresh_dashboard()
        elif name == "Restore":
            self._refresh_restore_sources()

    def _toggle_theme(self) -> None:
        self._set_theme("light" if self.theme_name == "dark" else "dark")

    def _set_theme(self, name: str) -> None:
        name = "light" if str(name).lower() == "light" else "dark"
        if name == self.theme_name:
            return
        self.theme_name = name
        try:
            self.settings["theme"] = name
            save_settings(self.settings)
        except Exception:
            pass
        self._colors = app_palette(name)
        self._rebuild_ui()

    def _set_backup_mode(self) -> None:
        try:
            mode = normalize_backup_mode(getattr(self, "backup_mode_var", tk.StringVar(value="full")).get(), False)
            self.settings["backup_mode"] = mode
            self.settings["incremental"] = mode == "incremental"
            self.toolkit.backup_mode = mode
            self.toolkit.incremental_enabled = mode == "incremental"
            if hasattr(self, "incremental_var"):
                self.incremental_var.set(mode == "incremental")
            save_settings(self.settings)
            labels = {
                "full": "Full backup mode",
                "incremental": "Incremental mode (complete backup with hardlinks)",
                "compact": "Compact changed-only mode (restore merges from base backup)",
            }
            self.append_log(f"{labels.get(mode, mode)} enabled")
        except Exception:
            pass

    def _toggle_incremental(self) -> None:
        try:
            if hasattr(self, "backup_mode_var"):
                self.backup_mode_var.set("incremental" if bool(self.incremental_var.get()) else "full")
                self._set_backup_mode()
                return
            enabled = bool(self.incremental_var.get())
            self.settings["incremental"] = enabled
            self.settings["backup_mode"] = "incremental" if enabled else "full"
            self.toolkit.backup_mode = self.settings["backup_mode"]
            self.toolkit.incremental_enabled = enabled
            save_settings(self.settings)
            self.append_log(f"Incremental mode {'enabled' if enabled else 'disabled'}")
        except Exception:
            pass

    def _save_retention_settings(self) -> None:
        try:
            self.settings["retention_enabled"] = bool(self.retention_enabled_var.get())
            self.settings["retention_keep_last"] = max(0, min(999, int(self.retention_keep_var.get() or 0)))
            self.settings["retention_max_age_days"] = max(0, min(36500, int(self.retention_days_var.get() or 0)))
            self.settings["retention_auto_after_backup"] = bool(self.retention_auto_var.get())
            self.retention_keep_var.set(str(self.settings["retention_keep_last"]))
            self.retention_days_var.set(str(self.settings["retention_max_age_days"]))
            save_settings(self.settings)
        except Exception as e:
            messagebox.showerror("Retention", f"Could not save retention settings: {e}")

    def _retention_dialog(self) -> None:
        self._save_retention_settings()
        plan = self.toolkit.plan_retention(
            self.backup_parent,
            keep_last=int(self.settings.get("retention_keep_last") or 0),
            max_age_days=int(self.settings.get("retention_max_age_days") or 0),
            protect=[self.toolkit.backup_root] if self.toolkit.backup_root else None,
        )
        remove = plan.get("remove") or []
        if not remove:
            messagebox.showinfo("Retention", "No old backups match the current retention policy.")
            return
        lines = [f"Backup location: {plan['parent']}", "", "Will remove:"]
        for item in remove[:40]:
            reason = plan["reasons"].get(item["name"], "")
            lines.append(f"• {item['name']}" + (f" — {reason}" if reason else ""))
        if len(remove) > 40:
            lines.append(f"• ... and {len(remove) - 40} more")
        lines.append("")
        lines.append("Interrupted backups are kept. Incremental backups are self-contained, so newer backups keep their data.")
        if not messagebox.askyesno("Clean up old backups", "\n".join(lines)):
            return

        def job() -> None:
            self.toolkit.is_running = True
            self.toolkit.is_cancelled = False
            result = self.toolkit.apply_retention(plan)
            removed = len(result.get("removed") or [])
            failed = result.get("failed") or []
            msg = f"Removed {removed} old backup(s)."
            if failed:
                msg += "\n\nIssues:\n" + "\n".join(failed[:10])
            self.after(0, lambda: messagebox.showinfo("Retention", msg))

        self._run_bg(job)

    def _refresh_skip_list(self) -> None:
        if not hasattr(self, "skip_list"):
            return
        try:
            self.skip_list.delete(0, "end")
            dirs = self.settings.get("skip_dirs") or []
            files = self.settings.get("skip_files") or []
            exts = self.settings.get("skip_extensions") or []
            if not dirs and not files and not exts:
                self.skip_list.insert("end", "(none — using built-in skips only)")
                return
            for name in dirs:
                self.skip_list.insert("end", f"[folder]     {name}")
            for name in files:
                self.skip_list.insert("end", f"[file]       {name}")
            for ext in exts:
                self.skip_list.insert("end", f"[extension]  .{ext}")
        except Exception:
            pass

    def _apply_skip_rules_to_toolkit(self) -> None:
        try:
            self.settings["skip_dirs"] = normalize_skip_list(self.settings.get("skip_dirs"))
            self.settings["skip_files"] = normalize_skip_list(self.settings.get("skip_files"))
            self.settings["skip_extensions"] = normalize_skip_extensions(self.settings.get("skip_extensions"))
            self.toolkit.extra_skip_dirs = set(self.settings["skip_dirs"])
            self.toolkit.extra_skip_files = set(self.settings["skip_files"])
            self.toolkit.extra_skip_extensions = set(self.settings["skip_extensions"])
            if hasattr(self, "skip_summary"):
                self.skip_summary.configure(text=self._skip_summary_text())
            self._refresh_skip_list()
        except Exception:
            pass

    def _skip_summary_text(self) -> str:
        dirs = self.settings.get("skip_dirs") or []
        files = self.settings.get("skip_files") or []
        exts = self.settings.get("skip_extensions") or []
        if not dirs and not files and not exts:
            return "No custom skip rules. Built-in caches, .git, node_modules, and temp folders are always skipped."
        parts = []
        if dirs:
            parts.append("folders: " + ", ".join(dirs))
        if files:
            parts.append("files: " + ", ".join(files))
        if exts:
            parts.append("extensions: " + ", ".join("." + e for e in exts))
        return "Extra skips — " + " • ".join(parts)

    def _skip_rules_dialog(self) -> None:
        c = self._colors
        win = tk.Toplevel(self)
        win.title("Custom skip rules")
        win.configure(bg=c["bg"])
        win.geometry("620x400")
        try:
            win.transient(self)
        except Exception:
            pass
        body = ttk.LabelFrame(win, text=" Skip during backup ", style="Card.TLabelframe", padding=12)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        tk.Label(
            body, text="Folder names (comma, semicolon, or newline separated)",
            bg=c["card"], fg=c["text"], font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(fill="x")
        dirs_entry = tk.Entry(body, bg=c["log_bg"], fg=c["text"], insertbackground=c["text"])
        dirs_entry.insert(0, ", ".join(self.settings.get("skip_dirs") or []))
        dirs_entry.pack(fill="x", pady=(2, 10))
        tk.Label(
            body, text="File names",
            bg=c["card"], fg=c["text"], font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(fill="x")
        files_entry = tk.Entry(body, bg=c["log_bg"], fg=c["text"], insertbackground=c["text"])
        files_entry.insert(0, ", ".join(self.settings.get("skip_files") or []))
        files_entry.pack(fill="x", pady=(2, 8))
        tk.Label(
            body, text="File extensions (large types — e.g. iso, vmdk, mp4)",
            bg=c["card"], fg=c["text"], font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(fill="x")
        exts_entry = tk.Entry(body, bg=c["log_bg"], fg=c["text"], insertbackground=c["text"])
        exts_entry.insert(0, ", ".join(self.settings.get("skip_extensions") or []))
        exts_entry.pack(fill="x", pady=(2, 8))
        tk.Label(
            body,
            text=("Folder/file names match at any depth. Extensions accept .iso, iso, or *.iso in one entry. "
                  "Built-in caches, .git, node_modules, and temp folders are always skipped. "
                  "Changes take effect on the next backup."),
            bg=c["card"], fg=c["muted"], font=("Segoe UI", 8), anchor="w", justify="left", wraplength=560,
        ).pack(fill="x", pady=(0, 10))

        def save() -> None:
            self.settings["skip_dirs"] = normalize_skip_list(dirs_entry.get())
            self.settings["skip_files"] = normalize_skip_list(files_entry.get())
            self.settings["skip_extensions"] = normalize_skip_extensions(exts_entry.get())
            self._apply_skip_rules_to_toolkit()
            save_settings(self.settings)
            self.append_log(
                f"Skip rules saved: {len(self.settings['skip_dirs'])} folders, "
                f"{len(self.settings['skip_files'])} files, "
                f"{len(self.settings['skip_extensions'])} extensions"
            )
            win.destroy()

        row = tk.Frame(body, bg=c["card"])
        row.pack(fill="x")
        self._secondary_button(row, "Save rules", save).pack(side="left")
        self._secondary_button(row, "Cancel", win.destroy).pack(side="left", padx=6)

    def _set_workers(self, count: int) -> None:
        try:
            self.toolkit.copy_workers = max(1, min(16, int(count)))
            self.settings["copy_workers"] = self.toolkit.copy_workers
            save_settings(self.settings)
            if hasattr(self, "workers_label"):
                self.workers_label.configure(text=f"Parallel copy workers: {self.toolkit.copy_workers}")
        except Exception:
            pass

    def _rebuild_ui(self) -> None:
        history = list(getattr(self, "_log_history", []))
        current = getattr(self, "_current_page", "Dashboard")
        for child in self.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        self.configure(bg=self._colors["bg"])
        self._build()
        try:
            self.txt.configure(state="normal")
            self.txt.delete("1.0", "end")
            for line, tag in history:
                self.txt.insert("end", line, tag)
            self.txt.see("end")
            self.txt.configure(state="disabled")
        except Exception:
            pass
        self._proc_sig = None
        self._show_page(current)
        self._set_running_ui(self.toolkit.is_running)

    # ---------------- dashboard data ----------------
    def _backup_efficiency(self, path: Path) -> str:
        data = None
        for name in ("performance.json", "manifest.json"):
            fp = Path(path) / name
            if not fp.exists():
                continue
            try:
                obj = json_loads(fp.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    data = obj
                    break
            except Exception:
                continue
        if not data:
            return "—"
        mode = str(data.get("BackupMode") or ("incremental" if data.get("Incremental") else ("compact" if data.get("Compact") else "full"))).lower()
        copied = int(data.get("CopiedBytes") or 0)
        linked = int(data.get("LinkedBytes") or 0)
        omitted = int(data.get("CompactSkippedBytes") or 0)
        linked_files = int(data.get("LinkedFiles") or 0)
        omitted_files = int(data.get("CompactSkippedFiles") or 0)
        # backward compat: if only file counts exist, be explicit
        if not copied and not linked and not omitted and not linked_files and not omitted_files and mode == "full":
            return "Full copy"
        parts = [f"wrote {format_bytes(copied)}"]
        if mode == "incremental" or linked or linked_files:
            extra = f"linked {format_bytes(linked)}" if linked else f"linked {linked_files} files"
            parts.append(f"{extra} from previous")
        if mode == "compact" or omitted or omitted_files:
            extra = f"omitted {format_bytes(omitted)}" if omitted else f"omitted {omitted_files} files"
            parts.append(f"{extra} (compact)")
        return "\n".join(parts) if parts else "—"

    def _backup_summary(self, path: Path) -> Dict[str, Any]:
        info = {"path": path, "size": 0, "files": 0, "health": "Unknown", "date": "", "efficiency": "—"}
        try:
            info["date"] = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            pass
        manifest = path / "manifest.json"
        info["efficiency"] = self._backup_efficiency(path)
        if not manifest.exists():
            info["health"] = "No manifest"
            return info
        try:
            data = json_loads(manifest.read_text(encoding="utf-8"))
            stats = data.get("StageStats") or {}
            info["size"] = sum(int(v.get("bytes") or 0) for v in stats.values() if isinstance(v, dict))
            info["files"] = sum(int(v.get("files") or 0) for v in stats.values() if isinstance(v, dict))
            info["health"] = "Warnings" if (data.get("Cancelled") or data.get("FailedStages")) else "OK"
        except Exception:
            info["health"] = "Unreadable manifest"
        return info

    def _refresh_dashboard(self) -> None:
        if not hasattr(self, "dash_latest"):
            return
        backups = self._list_backups()
        try:
            backups.sort(key=lambda b: b.stat().st_mtime, reverse=True)
        except Exception:
            pass
        if backups:
            latest = backups[0]
            info = self._backup_summary(latest)
            self.dash_latest.configure(text=f"{latest.name}\n{info['date']}")
            self.dash_size.configure(text=format_bytes(info["size"]) if info["size"] else "n/a")
            try:
                self.dash_efficiency.configure(text=info.get("efficiency") or "—")
            except Exception:
                pass
            health = info["health"]
        else:
            self.dash_latest.configure(text="No backups yet")
            self.dash_size.configure(text="—")
            try:
                self.dash_efficiency.configure(text="—")
            except Exception:
                pass
            health = "—"
        self.dash_count.configure(text=str(len(backups)))
        try:
            base = self.backup_parent if self.backup_parent.exists() else DESKTOP
            self.dash_free.configure(text=format_bytes(shutil.disk_usage(str(base)).free))
        except Exception:
            self.dash_free.configure(text="n/a")
        color = self._colors["ok"] if health == "OK" else self._colors["warn"] if health == "Warnings" else self._colors["muted"]
        self.dash_health.configure(text=f"Backup health: {health}", fg=color)

        self.dash_source_paths = list(getattr(self, "recent_sources", []))
        try:
            self.dash_sources.delete(0, "end")
            for p in self.dash_source_paths[:20]:
                mark = "OK" if p.exists() else "MISSING"
                self.dash_sources.insert("end", f"[{mark}] {p}")
        except Exception:
            pass

    def _refresh_restore_sources(self) -> None:
        if not hasattr(self, "restore_sources_list"):
            return
        self.restore_source_paths = list(getattr(self, "recent_sources", []))
        try:
            self.restore_sources_list.delete(0, "end")
            for p in self.restore_source_paths[:20]:
                mark = "OK" if p.exists() else "MISSING"
                self.restore_sources_list.insert("end", f"[{mark}] {p}")
        except Exception:
            pass

    def _dashboard_restore_selected(self) -> None:
        sel = self.dash_sources.curselection()
        if not sel:
            return
        try:
            self._restore_dialog(preselect=self.dash_source_paths[sel[0]])
        except Exception:
            pass

    def _dashboard_forget_selected(self) -> None:
        sel = self.dash_sources.curselection()
        if not sel:
            return
        try:
            self._forget_source(self.dash_source_paths[sel[0]])
            self._refresh_dashboard()
        except Exception:
            pass

    def _dashboard_add_source(self) -> None:
        path = filedialog.askdirectory(title="Add a restore source", initialdir=str(DESKTOP))
        if not path:
            return
        self._remember_source(Path(path))
        self._refresh_dashboard()

    def _restore_selected_source(self) -> None:
        sel = self.restore_sources_list.curselection()
        if not sel:
            return
        try:
            self._restore_dialog(preselect=self.restore_source_paths[sel[0]])
        except Exception:
            pass

    def _restore_forget_selected(self) -> None:
        sel = self.restore_sources_list.curselection()
        if not sel:
            return
        try:
            self._forget_source(self.restore_source_paths[sel[0]])
            self._refresh_restore_sources()
        except Exception:
            pass

    def _open_path(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showwarning("Open folder", f"Could not open:\n{path}\n\n{e}", parent=self)

    def _open_settings_folder(self) -> None:
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._open_path(SETTINGS_FILE.parent)

    def _open_logs_folder(self) -> None:
        target = None
        if self.toolkit.backup_root:
            target = Path(self.toolkit.backup_root) / "logs"
        if target is None or not target.exists():
            target = SETTINGS_FILE.parent / "logs"
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._open_path(target)

    # ---------------- restore diff dialog ----------------
    def _diff_dialog(self) -> None:
        folders = self._list_backups()
        if not folders:
            messagebox.showwarning(
                "Diff preview",
                "No backups found. Add a source from the Restore dialog first.",
                parent=self,
            )
            return
        c = self._colors
        dlg = tk.Toplevel(self)
        dlg.title("Restore diff preview (dry-run)")
        dlg.geometry("720x560")
        dlg.transient(self)
        dlg.grab_set()
        tk.Label(dlg, text="Choose a backup source:", anchor="w").pack(fill="x", padx=12, pady=(10, 4))
        lb = tk.Listbox(dlg, height=7, font=("Consolas", 9))
        lb.pack(fill="x", padx=12)
        for f in folders:
            lb.insert("end", str(f))
        lb.selection_set(0)

        opts = tk.LabelFrame(dlg, text="Stages to compare", padx=8, pady=6)
        opts.pack(fill="x", padx=12, pady=10)
        diff_vars: Dict[str, tk.BooleanVar] = {}
        defaults = {"UserData", "CodingTools", "Bookmarks"}
        for idx, key in enumerate(("Drivers", "Winget", "Chocolatey", "UserData", "Bookmarks", "Settings", "CodingTools")):
            var = tk.BooleanVar(value=key in defaults)
            diff_vars[key] = var
            tk.Checkbutton(opts, text=key, variable=var).grid(
                row=idx // 4, column=idx % 4, sticky="w", padx=4, pady=2
            )

        def analyze() -> None:
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("Diff preview", "Select a backup source.", parent=dlg)
                return
            chosen = [k for k, v in diff_vars.items() if v.get()]
            if not chosen:
                messagebox.showwarning("Diff preview", "Select at least one stage.", parent=dlg)
                return
            root = folders[sel[0]]
            dlg.destroy()

            def job() -> None:
                self.toolkit.is_running = True
                self.toolkit.is_cancelled = False
                errors, report = self.toolkit.restore_diff(root, chosen)
                self.after(0, lambda: self._show_diff_report(root, errors, report))

            self._run_bg(job)

        bf = tk.Frame(dlg)
        bf.pack(pady=8)
        tk.Button(bf, text="Analyze", width=12, command=analyze).pack(side="left", padx=6)
        tk.Button(bf, text="Cancel", width=12, command=dlg.destroy).pack(side="left", padx=6)

    def _show_diff_report(self, root: Path, errors: List[str], report: Dict[str, Any]) -> None:
        c = self._colors
        dlg = tk.Toplevel(self)
        dlg.title("Restore diff preview")
        dlg.geometry("900x600")
        dlg.transient(self)
        tk.Label(
            dlg, text=f"Backup: {root}", bg=c["bg"], fg=c["text"], anchor="w", font=("Segoe UI", 9, "bold"),
        ).pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(
            dlg,
            text="Counts compare backup files with the current user profile. 'Installer' rows run commands instead of copying files.",
            bg=c["bg"], fg=c["muted"], anchor="w", font=("Segoe UI", 8),
        ).pack(fill="x", padx=12)

        table = ttk.Treeview(
            dlg, columns=("stage", "new", "overwrite", "same", "files", "note"),
            show="headings", height=8,
        )
        for key, text, width in (
            ("stage", "Stage", 130), ("new", "New", 70), ("overwrite", "Overwrite", 90),
            ("same", "Same", 70), ("files", "Files", 70), ("note", "Details", 420),
        ):
            table.heading(key, text=text)
            table.column(key, width=width, anchor="w" if key in ("stage", "note") else "center")
        table.pack(fill="x", padx=12, pady=8)
        for stage, rep in report.items():
            if rep.get("diff"):
                values = (
                    stage, rep.get("new", 0), rep.get("overwrite", 0),
                    rep.get("same", 0), rep.get("files", 0), rep.get("note", ""),
                )
            else:
                values = (stage, "—", "—", "—", rep.get("files", 0), rep.get("note", ""))
            table.insert("", "end", values=values)

        details = tk.Text(
            dlg, height=10, bg=c["log_bg"], fg=c["log_fg"], font=("Consolas", 9),
            wrap="none", relief="flat", bd=0, padx=8, pady=8,
        )
        details.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        for err in errors:
            details.insert("end", f"ERROR: {err}\n")
        for stage, rep in report.items():
            for status, rel in rep.get("samples", []):
                details.insert("end", f"{stage:<13} [{status:<9}] {rel}\n")
        if not errors and not report:
            details.insert("end", "Nothing to compare.\n")
        details.configure(state="disabled")

        btns = tk.Frame(dlg)
        btns.pack(fill="x", pady=(0, 8))
        tk.Button(btns, text="Close", width=12, command=dlg.destroy).pack(side="right", padx=12)
        tk.Button(btns, text="Restore this backup...", width=20, command=lambda: (dlg.destroy(), self._restore_dialog(preselect=root))).pack(side="right", padx=6)

    def append_log(self, msg: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}\n"
        tag = level if level in {"INFO", "OK", "WARN", "ERROR", "FATAL", "DEBUG"} else "INFO"
        hist = getattr(self, "_log_history", None)
        if hist is not None:
            hist.append((line, tag))
            if len(hist) > 800:
                del hist[:-800]

        def _ui():
            try:
                self.txt.configure(state="normal")
                self.txt.insert("end", line, tag)
                self.txt.see("end")
                self.txt.configure(state="disabled")
                if hasattr(self, "progress_label") and level in {"INFO", "WARN", "ERROR", "OK"} and self.toolkit.is_running:
                    short = msg if len(msg) <= 110 else msg[:107] + "..."
                    self.progress_label.configure(text=short)
            except Exception:
                pass

        try:
            self.after(0, _ui)
        except Exception:
            pass
        try:
            if self.toolkit.log_file and self.toolkit.backup_root:
                self.toolkit.backup_root.mkdir(parents=True, exist_ok=True)
                with open(self.toolkit.log_file, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass

    def _tick_progress(self) -> None:
        try:
            snap = self.toolkit.progress_snapshot()
            self._apply_progress_snapshot(snap)
            self._refresh_processes()
        except Exception:
            pass
        try:
            self.after(300, self._tick_progress)
        except Exception:
            pass

    def _apply_progress_snapshot(self, snap: Dict[str, Any]) -> None:
        if not snap.get("running"):
            return
        pct = float(snap.get("overall_percent", 0.0))
        has_totals = bool(snap.get("has_totals"))
        try:
            mode = str(self.progress.cget("mode"))
            if has_totals:
                if mode != "determinate":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                self.progress["value"] = max(0.0, min(100.0, pct))
            else:
                if mode != "indeterminate":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(14)
        except Exception:
            pass
        if has_totals:
            self.progress_pct.configure(text=f"{pct:0.0f}%")
        else:
            self.progress_pct.configure(
                text=f"Stage {snap.get('stage_index', 0)}/{snap.get('stage_count', 0)}"
            )
        speed = snap.get("speed_bps", 0.0)
        self.progress_speed.configure(text=format_speed(speed) if speed > 1024 else "")
        eta = snap.get("eta_s")
        self.progress_eta.configure(text=f"ETA {format_timedelta(eta)}" if eta is not None else "")
        if snap.get("has_totals"):
            self.progress_files.configure(
                text=f"{snap.get('files_done', 0):,} / {snap.get('files_total', 0):,} files"
            )
        else:
            self.progress_files.configure(text=f"{format_timedelta(snap.get('stage_elapsed', 0.0))} elapsed")
        stage = snap.get("stage") or "Starting"
        phase = snap.get("phase") or ""
        item = snap.get("current_item") or ""
        label = f"{stage} — {item}" if item else f"{stage} — {phase}" if phase else stage
        if len(label) > 130:
            label = label[:127] + "..."
        self.progress_label.configure(text=label)

    def _refresh_processes(self) -> None:
        if not hasattr(self, "proc_tree"):
            return
        rows = self.toolkit.process_snapshot()
        sig = tuple(
            (r["pid"], r["status"], r["last_line"], int(r["elapsed"] * 2))
            for r in rows
        )
        if sig == getattr(self, "_proc_sig", None):
            return
        self._proc_sig = sig
        try:
            existing = set(self.proc_tree.get_children())
            for item in existing - {str(r["pid"]) for r in rows}:
                self.proc_tree.delete(item)
            for r in rows:
                status = r["status"]
                if status == "running":
                    shown = "running"
                elif status == "ok":
                    shown = "ok"
                elif status == "stopped":
                    shown = "stopped"
                elif status == "timeout":
                    shown = "timeout"
                else:
                    shown = f"failed ({r.get('exit_code')})"
                values = (
                    r["pid"], r["label"], format_timedelta(r["elapsed"]), shown,
                    (r["last_line"] or r["command"])[:140],
                )
                iid = str(r["pid"])
                if iid in existing:
                    self.proc_tree.item(iid, values=values)
                else:
                    self.proc_tree.insert("", "end", iid=iid, values=values)
        except Exception:
            pass

    def _show_process_output(self) -> None:
        if not hasattr(self, "proc_tree"):
            return
        sel = self.proc_tree.selection()
        if not sel:
            return
        try:
            pid = int(sel[0])
        except ValueError:
            return
        lines = self.toolkit.process_output(pid)
        dlg = tk.Toplevel(self)
        dlg.title(f"Process output — PID {pid}")
        dlg.geometry("820x460")
        dlg.transient(self)
        btns = tk.Frame(dlg)
        btns.pack(side="bottom", fill="x")
        tk.Button(btns, text="Close", command=dlg.destroy, width=12).pack(pady=6)
        body = tk.Frame(dlg)
        body.pack(fill="both", expand=True)
        txt = tk.Text(
            body, bg=self._colors["log_bg"], fg=self._colors["log_fg"],
            font=("Consolas", 9), wrap="none", padx=8, pady=8,
        )
        scr = ttk.Scrollbar(body, command=txt.yview)
        txt.configure(yscrollcommand=scr.set)
        txt.pack(side="left", fill="both", expand=True)
        scr.pack(side="right", fill="y")
        txt.insert("1.0", "\n".join(lines) if lines else "(no output captured yet)")
        txt.configure(state="disabled")

    def _set_running_ui(self, running: bool) -> None:
        def _ui():
            c = getattr(self, "_colors", {})
            state = "disabled" if running else "normal"
            for b in self._action_buttons:
                try:
                    b.configure(state=state)
                except Exception:
                    pass
            self.btn_pause.configure(state="normal" if running else "disabled")
            self.btn_stop.configure(state="normal" if running else "disabled")
            if not running:
                self.btn_pause.configure(text="Pause", bg=c.get("warn", "#C88C00"))
            if hasattr(self, "progress"):
                try:
                    try:
                        self.progress.stop()
                    except Exception:
                        pass
                    if running:
                        self.progress.configure(mode="determinate", value=0)
                        self.mode_chip.configure(text="Running", fg=c.get("accent2", "#0ea5e9"))
                        self.progress_label.configure(text="Starting...")
                    else:
                        self.progress.configure(mode="determinate", value=0)
                        self.progress_pct.configure(text="")
                        self.progress_speed.configure(text="")
                        self.progress_eta.configure(text="")
                        self.progress_files.configure(text="")
                        self.mode_chip.configure(text="Ready", fg=c.get("ok", "#16a34a"))
                        self.progress_label.configure(text="Idle — select a backup or restore action")
                except Exception:
                    pass

        self.after(0, _ui)

    def _toggle_pause(self) -> None:
        if not self.toolkit.is_running:
            return
        if self.toolkit.is_paused:
            self.toolkit.resume()
            self.btn_pause.configure(text="Pause", bg="#C88C00")
        else:
            self.toolkit.pause()
            self.btn_pause.configure(text="Resume", bg="#107C10")

    def _stop(self) -> None:
        if self.toolkit.is_running:
            self.toolkit.stop()

    def _on_close(self) -> None:
        if self.toolkit.is_running:
            if not messagebox.askyesno("Confirm", "Backup/restore is running. Close anyway?"):
                return
            self.toolkit.stop()
        try:
            self.settings["window_geometry"] = self.geometry()
            self.settings["last_page"] = getattr(self, "_current_page", "Dashboard")
            self.settings["theme"] = self.theme_name
            self.settings["backup_parent"] = str(self.backup_parent)
            save_settings(self.settings)
        except Exception:
            pass
        self.destroy()

    def _choose_location(self) -> None:
        path = filedialog.askdirectory(title="Backup parent folder", initialdir=str(self.backup_parent))
        if path:
            self.backup_parent = Path(path)
            self.status.configure(text=f"Ready  •  Backup location: {self.backup_parent}")
            self.append_log(f"Backup parent set to: {self.backup_parent}")
            if hasattr(self, "backup_parent_label"):
                self.backup_parent_label.configure(text=f"Backup location: {self.backup_parent}")
            if hasattr(self, "settings_location_label"):
                self.settings_location_label.configure(text=str(self.backup_parent))
            self.settings["backup_parent"] = str(self.backup_parent)
            save_settings(self.settings)

    def _run_bg(self, fn: Callable, done_msg: Optional[str] = None) -> None:
        if self.toolkit.is_running:
            messagebox.showinfo("Busy", "Already running.")
            return

        def worker():
            self._set_running_ui(True)
            try:
                fn()
                if done_msg:
                    self.after(0, lambda: messagebox.showinfo("Done", done_msg))
            except Exception as e:
                self.append_log(f"ERROR: {e}", "ERROR")
                self.append_log(traceback.format_exc(), "ERROR")
                self.after(0, lambda: messagebox.showerror("Error", str(e)))
            finally:
                self.toolkit.is_running = False
                self.toolkit.is_paused = False
                self.toolkit.is_cancelled = False
                self._set_running_ui(False)

        threading.Thread(target=worker, daemon=True).start()

    def _show_backup_selection(self) -> Optional[Dict[str, Any]]:
        dlg = tk.Toplevel(self)
        dlg.title("Select what to backup")
        dlg.geometry("560x640")
        dlg.minsize(480, 520)
        dlg.transient(self)
        dlg.grab_set()
        result = {"ok": False, "selection": None}

        outer = tk.Frame(dlg)
        outer.pack(fill="both", expand=True, padx=8, pady=4)

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        mid = tk.Frame(canvas)
        mid.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=mid, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        frm_stages = tk.LabelFrame(mid, text="Main stages", padx=8, pady=6)
        frm_stages.pack(fill="x", padx=4, pady=4)
        stage_vars = {}
        defaults_stage = {
            "Drivers": True, "Packages": True, "UserData": True,
            "Bookmarks": True, "Settings": True, "CodingTools": True,
        }
        for i, (key, label) in enumerate([
            ("Drivers", "Drivers"),
            ("Packages", "Apps list (Winget / Chocolatey / Programs)"),
            ("UserData", "User Data (folders below)"),
            ("Bookmarks", "Browser bookmarks only"),
            ("Settings", "Windows settings / Wi-Fi / Registry"),
            ("CodingTools", "Coding tools (FreeBuff, Kilo Code, VS Code/Cursor, Terminal, profiles)"),
        ]):
            v = tk.BooleanVar(value=defaults_stage[key])
            stage_vars[key] = v
            tk.Checkbutton(frm_stages, text=label, variable=v).grid(row=i, column=0, sticky="w")

        frm_folders = tk.LabelFrame(
            mid, text="User folders (uncheck large ones to reduce size)", padx=8, pady=6
        )
        frm_folders.pack(fill="x", padx=4, pady=4)
        folder_vars = {}
        default_on = {"Documents", "Desktop", "Favorites", "Links", "Saved Games", "Searches"}
        for i, name in enumerate(USER_FOLDERS):
            if name in EXCLUDE_TOP_FOLDERS:
                continue
            v = tk.BooleanVar(value=(name in default_on))
            folder_vars[name] = v
            tk.Checkbutton(frm_folders, text=name, variable=v).grid(
                row=i // 3, column=i % 3, sticky="w", padx=6
            )

        frm_roam = tk.LabelFrame(mid, text="App settings (Roaming) — can be large", padx=8, pady=6)
        frm_roam.pack(fill="x", padx=4, pady=4)
        heavy_roam = {"Telegram Desktop", "discord", "Slack", "Spotify", "Zoom", r"Microsoft\Office"}
        roam_vars = {}
        for i, name in enumerate(ROAMING_FOLDERS):
            v = tk.BooleanVar(value=(name not in heavy_roam))
            roam_vars[name] = v
            tk.Checkbutton(frm_roam, text=name, variable=v).grid(
                row=i // 2, column=i % 2, sticky="w", padx=4
            )

        v_dot = tk.BooleanVar(value=True)
        tk.Checkbutton(mid, text="Include .ssh / .gitconfig / .wslconfig", variable=v_dot).pack(
            anchor="w", padx=8, pady=6
        )

        bottom = tk.Frame(dlg, pady=8)
        bottom.pack(side="bottom", fill="x", padx=12)

        def preset_essential():
            for k, v in stage_vars.items():
                v.set(True)
            for name, v in folder_vars.items():
                v.set(name in default_on)
            for name, v in roam_vars.items():
                v.set(name not in heavy_roam)
            v_dot.set(True)

        def preset_all():
            for v in stage_vars.values():
                v.set(True)
            for v in folder_vars.values():
                v.set(True)
            for v in roam_vars.values():
                v.set(True)
            v_dot.set(True)

        def preset_none_media():
            preset_essential()
            for name in ("Videos", "Pictures", "Music"):
                if name in folder_vars:
                    folder_vars[name].set(False)

        presets = tk.Frame(bottom)
        presets.pack(fill="x", pady=(0, 8))
        tk.Button(presets, text="Essential (small)", command=preset_essential).pack(side="left", padx=3)
        tk.Button(presets, text="All", command=preset_all).pack(side="left", padx=3)
        tk.Button(presets, text="No media", command=preset_none_media).pack(side="left", padx=3)

        def on_ok():
            sel = {k: v.get() for k, v in stage_vars.items()}
            sel["user_folders"] = [n for n, v in folder_vars.items() if v.get()]
            sel["roaming"] = [n for n, v in roam_vars.items() if v.get()]
            sel["dotfiles"] = v_dot.get()
            if not any(sel.get(s) for s in ALL_STAGES):
                messagebox.showwarning("Select", "Select at least one main stage.", parent=dlg)
                return
            result["ok"] = True
            result["selection"] = sel
            try:
                canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass
            dlg.destroy()

        def on_cancel():
            try:
                canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass
            dlg.destroy()

        actions = tk.Frame(bottom)
        actions.pack(fill="x")
        tk.Button(
            actions,
            text="✓  Confirm & Start Backup",
            width=28,
            height=2,
            bg="#0078D7",
            fg="white",
            font=("Segoe UI", 10, "bold"),
            command=on_ok,
        ).pack(side="left", padx=4)
        tk.Button(actions, text="Cancel", width=12, height=2, command=on_cancel).pack(side="left", padx=4)

        dlg.protocol("WM_DELETE_WINDOW", on_cancel)
        dlg.wait_window()
        return result["selection"] if result["ok"] else None

    def _start_full(self) -> None:
        selection = self._show_backup_selection()
        if not selection:
            return

        if self.toolkit._backup_mode_name() == "incremental":
            base = self.toolkit.find_backup_base(self.backup_parent)
            if base is not None:
                warn = self.toolkit.incremental_hardlink_warning(base, self.backup_parent)
                if warn:
                    if not messagebox.askyesno(
                        "Incremental hardlinks unavailable",
                        warn + "\n\nContinue anyway (unchanged files will be copied as a full copy)?",
                        icon="warning",
                    ):
                        return
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
        self.toolkit.backup_root = self.backup_parent / f"WindowsBackup_{ts}"

        def job():
            failed, cancelled = self.toolkit.full_backup(selection=selection)
            self._remember_source(self.toolkit.backup_root)
            if cancelled:
                msg = f"Backup stopped.\nUse Resume interrupted backup later.\n\n{self.toolkit.backup_root}"
            elif failed:
                msg = f"Finished with errors in:\n{', '.join(failed)}\n\n{self.toolkit.backup_root}"
            else:
                msg = f"Full backup completed!\n\n{self.toolkit.backup_root}"
            self.after(0, lambda: messagebox.showinfo("Backup", msg))

        self._run_bg(job)

    def _resume_checkpoint(self) -> None:
        checkpoint = BackupToolkit.load_checkpoint()
        root: Optional[Path] = None
        done: List[str] = []
        if checkpoint:
            raw_root = str(checkpoint.get("BackupRoot") or "")
            if raw_root:
                root = Path(raw_root)
                done = list(checkpoint.get("Completed") or [])
        if root is None or not root.exists():
            found = self.toolkit.find_interrupted_backup(self.backup_parent)
            if found is not None:
                root = found
                try:
                    data = json_loads((found / "manifest.json").read_text(encoding="utf-8"))
                    done = list(data.get("CompletedStages") or []) if isinstance(data, dict) else []
                except Exception:
                    done = []
        if root is None:
            messagebox.showinfo("Resume", "No checkpoint or interrupted backup found.")
            return
        if not root.exists():
            messagebox.showwarning("Resume", f"Backup folder missing:\n{root}")
            BackupToolkit(lambda *_: None).clear_checkpoint()
            return
        if not messagebox.askyesno(
            "Resume Backup",
            f"Resume this backup?\n\nFolder: {root}\nAlready done: {', '.join(done) or '(none)'}"
            "\n\nFiles already copied will be skipped.",
        ):
            return
        self.toolkit.backup_root = root

        selection = None
        sel_file = root / "selection.json"
        if sel_file.exists():
            try:
                selection = json_loads(sel_file.read_text(encoding="utf-8"))
            except Exception:
                selection = None

        def job():
            failed, cancelled = self.toolkit.full_backup(skip_stages=done, selection=selection)
            self._remember_source(self.toolkit.backup_root)
            msg = "Resume finished." if not cancelled else "Stopped again."
            if failed:
                msg += f"\nErrors: {', '.join(failed)}"
            msg += "\n\nAlready-copied files were skipped."
            self.after(0, lambda: messagebox.showinfo("Resume", msg))

        self._run_bg(job)

    def _partial(self, stage: str) -> None:

        if self.toolkit._backup_mode_name() == "incremental":
            base = self.toolkit.find_backup_base(self.backup_parent)
            if base is not None:
                warn = self.toolkit.incremental_hardlink_warning(base, self.backup_parent)
                if warn:
                    if not messagebox.askyesno(
                        "Incremental hardlinks unavailable",
                        warn + "\n\nContinue anyway (unchanged files will be copied as a full copy)?",
                        icon="warning",
                    ):
                        return
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
        self.toolkit.backup_root = self.backup_parent / f"WindowsBackup_{ts}"
        self.toolkit.log_file = self.toolkit.backup_root / "BackupRestore.log"
        try:
            (self.toolkit.backup_root / "logs").mkdir(parents=True, exist_ok=True)
            self.toolkit.log_obj.add_log_file(
                self.toolkit.backup_root / "logs" / f"backup-session-{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            )
        except Exception:
            pass
        mapping = {
            "Drivers": self.toolkit.backup_drivers,
            "Packages": self.toolkit.backup_packages,
            "UserData": self.toolkit.backup_user_data,
            "Bookmarks": self.toolkit.backup_bookmarks,
            "Settings": self.toolkit.backup_settings,
        }

        def job():
            self.toolkit.is_running = True
            self.toolkit.is_cancelled = False
            self.toolkit.is_paused = False
            self.toolkit._pause_event.set()
            self.toolkit.backup_root.mkdir(parents=True, exist_ok=True)
            self.append_log(f"Partial backup: {stage}")
            self.toolkit.begin_job("backup", 1)
            self.toolkit.prepare_incremental_base()
            self.toolkit.begin_stage(stage)
            started = time.perf_counter()
            try:
                mapping[stage]()
                self.append_log(f"{stage} done", "OK")
                self.toolkit.write_manifest([], False)
                try:
                    performance = self.toolkit.performance_report(time.perf_counter() - started)
                    (self.toolkit.backup_root / "performance.json").write_text(
                        json_dumps(performance), encoding="utf-8"
                    )
                except Exception:
                    pass
                self._remember_source(self.toolkit.backup_root)
            except Exception as e:
                self.append_log(f"{stage} error: {e}", "ERROR")
            finally:
                self.toolkit.end_stage()

        self._run_bg(job, f"{stage} backup done.\n{self.toolkit.backup_root}")

    @staticmethod
    def _looks_like_backup(path: Path) -> bool:
        path = Path(path)
        if not path.is_dir():
            return False
        if (path / "manifest.json").exists():
            return True
        return any(
            (path / name).exists()
            for name in ("Drivers", "Apps", "UserData", "BrowserBookmarks", "Settings", "CodingTools")
        )

    def _remember_source(self, path: Path) -> None:
        try:
            self.recent_sources = remember_recent_source(path)
            self.append_log(f"Remembered backup source: {path}")
        except Exception as e:
            self.append_log(f"Could not remember backup source: {e}", "WARN")

    def _forget_source(self, path: Path) -> None:
        try:
            self.recent_sources = forget_recent_source(path)
        except Exception as e:
            self.append_log(f"Could not update remembered sources: {e}", "WARN")

    def _list_backups(self) -> List[Path]:
        remembered: List[Path] = []
        for p in getattr(self, "recent_sources", []):
            if self._looks_like_backup(p) and p not in remembered:
                remembered.append(p)
        found: List[Path] = []
        for base in {DESKTOP, self.backup_parent, Path.home() / "Desktop"}:
            if not base.exists():
                continue
            for p in base.glob("WindowsBackup_*"):
                if p.is_dir() and p not in found:
                    found.append(p)
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return remembered + [p for p in found if p not in remembered]

    def _restore_dialog(self, preselect: Optional[Path] = None) -> None:
        folders = self._list_backups()
        if preselect and preselect not in folders:
            folders.insert(0, preselect)
        if preselect and not folders:
            folders = [preselect]

        dlg = tk.Toplevel(self)
        dlg.title("Select Backup to Restore")
        dlg.geometry("520x440")
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="Available backups (remembered sources + found folders):").pack(anchor="w", padx=10, pady=6)
        list_frame = tk.Frame(dlg)
        list_frame.pack(fill="both", expand=True, padx=10)
        lb = tk.Listbox(list_frame, font=("Consolas", 9))
        scroll = tk.Scrollbar(list_frame, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=scroll.set)
        lb.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def _refresh_sources(select: Optional[Path] = None) -> None:
            lb.delete(0, "end")
            for f in folders:
                mark = "OK" if f.exists() else "MISSING"
                label = f"[{mark}] {f}"
                if preselect is not None and f == preselect:
                    label += "  [selected source]"
                lb.insert("end", label)
            if not folders:
                return
            idx = 0
            if select is not None:
                try:
                    idx = folders.index(select)
                except ValueError:
                    idx = 0
            lb.selection_clear(0, "end")
            lb.selection_set(idx)
            lb.see(idx)

        _refresh_sources(preselect)

        def _add_source() -> None:
            path = filedialog.askdirectory(
                title="Select a backup folder to restore from", initialdir=str(DESKTOP)
            )
            if not path:
                return
            p = Path(path)
            if not self._looks_like_backup(p):
                if not messagebox.askyesno(
                    "Add restore source",
                    f"This folder doesn't look like a backup folder (no manifest.json or known stage folders):\n{p}\n\nUse it anyway?",
                    parent=dlg,
                ):
                    return
            if p not in folders:
                folders.insert(0, p)
            self._remember_source(p)
            _refresh_sources(p)

        def _forget_selected() -> None:
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("Restore", "Select a source to forget.", parent=dlg)
                return
            try:
                p = folders[sel[0]]
            except Exception:
                return
            self._forget_source(p)
            try:
                folders.remove(p)
            except ValueError:
                pass
            _refresh_sources()

        src_btns = tk.Frame(dlg)
        src_btns.pack(fill="x", padx=10, pady=(4, 0))
        tk.Button(src_btns, text="Add source...", command=_add_source).pack(side="left")
        tk.Button(src_btns, text="Forget selected", command=_forget_selected).pack(side="left", padx=6)

        opts = tk.LabelFrame(dlg, text="What to restore", padx=8, pady=6)
        opts.pack(fill="x", padx=10, pady=8)
        vars_: Dict[str, tk.BooleanVar] = {}
        items = [
            ("Drivers", "Drivers"), ("Winget", "Winget"), ("Chocolatey", "Chocolatey"),
            ("UserData", "User Data"), ("Bookmarks", "Bookmarks"), ("Settings", "Settings"),
            ("CodingTools", "Coding Tools"), ("All", "Everything"),
        ]
        for i, (key, label) in enumerate(items):
            v = tk.BooleanVar(value=(key == "All"))
            vars_[key] = v
            tk.Checkbutton(opts, text=label, variable=v).grid(
                row=i // 4, column=i % 4, sticky="w", padx=4
            )

        def _get_restore_selection():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("Restore", "Select a backup folder.", parent=dlg)
                return None, None
            try:
                root = folders[sel[0]]
            except Exception:
                messagebox.showerror("Restore", "Select a valid backup source.", parent=dlg)
                return None, None
            if not root.exists():
                messagebox.showerror("Restore", f"Folder not found:\n{root}", parent=dlg)
                return None, None
            do_all = vars_["All"].get()
            chosen = []
            if do_all or vars_["Drivers"].get():
                chosen.append("Drivers")
            if do_all or vars_["Winget"].get():
                chosen.append("Winget")
            if do_all or vars_["Chocolatey"].get():
                chosen.append("Chocolatey")
            if do_all or vars_["UserData"].get():
                chosen.append("UserData")
            if do_all or vars_["Bookmarks"].get():
                chosen.append("Bookmarks")
            if do_all or vars_["Settings"].get():
                chosen.append("Settings")
            if do_all or vars_["CodingTools"].get():
                chosen.append("CodingTools")
            if not chosen:
                messagebox.showwarning("Restore", "Select at least one item.", parent=dlg)
                return None, None
            return root, chosen

        def do_preview():
            root, chosen = _get_restore_selection()
            if not root or not chosen:
                return
            self._remember_source(root)
            errors, warnings, plan = self.toolkit.preview_restore_plan(root, chosen)
            self.append_log(f"=== RESTORE PREVIEW (DRY-RUN): {root} ===")
            for p in plan:
                self.append_log(p)
            for e in errors:
                self.append_log(f"PREVIEW ERROR: {e}", "ERROR")
            for w in warnings:
                self.append_log(f"PREVIEW WARNING: {w}", "WARN")
            body = "\n".join(plan[:40])
            if warnings:
                body += "\n\nWarnings:\n- " + "\n- ".join(warnings[:12])
            if errors:
                messagebox.showerror("Restore preview (dry-run)", "\n".join(errors) + "\n\n" + body[:3000], parent=dlg)
            else:
                messagebox.showinfo("Restore preview (dry-run)", body[:6000], parent=dlg)

        def do_restore():
            root, chosen = _get_restore_selection()
            if not root or not chosen:
                return
            self._remember_source(root)
            pre_errors, pre_warnings = self.toolkit._restore_preflight(root, chosen)
            if pre_errors:
                messagebox.showerror("Restore preflight failed", "\n".join(pre_errors), parent=dlg)
                return
            warn_text = ""
            if pre_warnings:
                warn_text = "\n\nPreflight warnings:\n- " + "\n- ".join(pre_warnings[:12])
                if len(pre_warnings) > 12:
                    warn_text += f"\n- ... and {len(pre_warnings) - 12} more"
            if not messagebox.askyesno(
                "Confirm Restore (Thorough)",
                f"Restore from:\n{root}\n\nItems: {', '.join(chosen)}\n\n"
                "The restore will continue item-by-item, skip missing data, and report errors in the log. "
                "Existing files may be overwritten. Close affected apps (browsers, VS Code, FreeBuff, terminals) first."
                f"{warn_text}\n\nContinue?",
                parent=dlg,
            ):
                return
            dlg.destroy()

            def job():
                self.toolkit.is_running = True
                self.toolkit.backup_root = root
                self.toolkit.log_file = root / "BackupRestore.log"
                try:
                    (root / "logs").mkdir(parents=True, exist_ok=True)
                    self.toolkit.log_obj.add_log_file(
                        root / "logs" / f"restore-session-{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
                    )
                except Exception:
                    pass
                self.append_log(f"=== RESTORE from {root} ===")
                self.append_log(f"Selected: {', '.join(chosen)}")
                self.append_log(f"Admin: {is_admin()} | Windows: {platform.platform()}")
                if self.toolkit.log_obj.session_log_path():
                    self.append_log(f"Log file: {self.toolkit.log_obj.session_log_path()}")
                for w in pre_warnings:
                    self.append_log(f"Preflight: {w}", "WARN")
                steps = [
                    ("Drivers", self.toolkit.restore_drivers),
                    ("Winget", self.toolkit.restore_winget),
                    ("Chocolatey", self.toolkit.restore_chocolatey),
                    ("UserData", self.toolkit.restore_user_data),
                    ("Bookmarks", self.toolkit.restore_bookmarks),
                    ("Settings", self.toolkit.restore_settings),
                    ("CodingTools", self.toolkit.restore_coding_tools),
                ]
                ok_steps: List[str] = []
                err_steps: List[str] = []
                stage_stats: Dict[str, Any] = {}
                try:
                    mf_data = json_loads((root / "manifest.json").read_text(encoding="utf-8"))
                    stage_stats = mf_data.get("StageStats") or {}
                except Exception:
                    stage_stats = {}
                restore_chain, chain_warnings = self.toolkit.resolve_restore_chain(root)
                for cw in chain_warnings:
                    self.append_log(f"Restore chain: {cw}", "WARN")
                if len(restore_chain) > 1:
                    self.append_log("Compact restore: merging base backup(s) then changed-only backup — " + " -> ".join(p.name for p in restore_chain))
                self.toolkit.begin_job("restore", len(chosen))
                single_file_stages = {"Winget", "Chocolatey"}
                merge_stages = {"Drivers", "UserData", "Bookmarks", "Settings", "CodingTools"}
                for name, fn in steps:
                    if name not in chosen:
                        continue
                    self.toolkit.begin_stage(name)
                    try:
                        stat_name = {"Winget": "Packages", "Chocolatey": "Packages"}.get(name, name)
                        rec = stage_stats.get(stat_name) or {}
                        if rec:
                            self.toolkit.add_stage_totals(int(rec.get("files") or 0), int(rec.get("bytes") or 0))
                    except Exception:
                        pass
                    try:
                        self.append_log(f"--- Restore: {name} ---")
                        if name in single_file_stages:
                            src_root = root
                            rel = "Apps/winget-packages.json" if name == "Winget" else "Apps/chocolatey-install.ps1"
                            for rr in reversed(restore_chain):
                                if (rr / rel).exists():
                                    src_root = rr
                                    break
                            fn(src_root)
                        elif name in merge_stages and len(restore_chain) > 1:
                            applied = False
                            for rr in restore_chain:
                                # apply only where relevant stage data exists, except always try merge stages safely
                                has = False
                                if name == "Drivers":
                                    has = (rr / "Drivers").exists()
                                elif name == "UserData":
                                    has = (rr / "UserData").exists()
                                elif name == "Bookmarks":
                                    has = (rr / "BrowserBookmarks").exists()
                                elif name == "Settings":
                                    has = (rr / "Settings").exists()
                                elif name == "CodingTools":
                                    has = (rr / "CodingTools").exists()
                                if has:
                                    self.append_log(f"Applying {name} from {rr.name}...")
                                    fn(rr)
                                    applied = True
                            if not applied:
                                fn(root)
                        else:
                            fn(root)
                        ok_steps.append(name)
                    except Exception as e:
                        err_steps.append(name)
                        self.append_log(f"{name} error (continuing): {e}", "ERROR")
                        self.append_log(traceback.format_exc(), "ERROR")
                    finally:
                        self.toolkit.end_stage()
                self.append_log("=== RESTORE FINISHED ===", "OK" if not err_steps else "WARN")
                self.append_log(f"Completed: {', '.join(ok_steps) or '(none)'}")
                if err_steps:
                    self.append_log(f"Steps with errors: {', '.join(err_steps)}", "WARN")
                self.append_log("Recommended: reboot Windows, then verify drivers, apps, browser sign-in/bookmarks, Wi-Fi, and coding tool settings.")
                summary = (
                    "Restore finished.\n\n"
                    f"Completed: {', '.join(ok_steps) or '(none)'}\n"
                    f"Errors: {', '.join(err_steps) if err_steps else 'none reported'}\n\n"
                    "Recommended next steps:\n"
                    "• Reboot Windows (important for drivers/settings)\n"
                    "• Open apps/browsers and confirm data\n"
                    "• For FreeBuff/Kilo Code/VS Code, reopen and verify settings/extensions\n"
                    "• Review the activity log for any skipped or failed items"
                )
                self.after(0, lambda: messagebox.showinfo("Restore", summary))

            self._run_bg(job)

        bf = tk.Frame(dlg)
        bf.pack(pady=8)
        tk.Button(bf, text="Preview (dry-run)", width=16, command=do_preview).pack(side="left", padx=6)
        tk.Button(bf, text="Restore", width=12, command=do_restore).pack(side="left", padx=6)
        tk.Button(bf, text="Cancel", width=12, command=dlg.destroy).pack(side="left", padx=6)

    def _run_benchmark(self) -> None:
        def job() -> None:
            self.toolkit.is_running = True
            self.toolkit.is_cancelled = False
            report = self.toolkit.run_benchmark()
            message = (
                f"Files: {report['Files']} ({format_bytes(report['Bytes'])})\n"
                f"Serial: {report['SerialMBps']} MB/s in {report['SerialSeconds']}s\n"
                f"Parallel ({report['Workers']} workers): {report['ParallelMBps']} MB/s in {report['ParallelSeconds']}s\n"
                f"Speedup: {report['Speedup']}x\n\nReport saved to:\n{report.get('report_path', '')}"
            )
            self.after(0, lambda: messagebox.showinfo("Copy benchmark", message))

        self._run_bg(job)

    def _verify_backup_dialog(self) -> None:
        path = filedialog.askdirectory(title="Select WindowsBackup_* folder to verify", initialdir=str(DESKTOP))
        if not path:
            return
        root = Path(path)
        errors, warnings = BackupToolkit.verify_backup(root)
        ierr, iwarn, info = BackupToolkit.verify_integrity_sample(root)
        self.append_log(f"=== VERIFY BACKUP: {root} ===")
        for e in errors + ierr:
            self.append_log(f"VERIFY ERROR: {e}", "ERROR")
        for w in warnings + iwarn:
            sev = "WARN" if any(k in w.lower() for k in ("fail", "missing", "changed", "unreadable", "integrity")) else "INFO"
            self.append_log(f"VERIFY: {w}", sev)
        all_errors = errors + ierr
        if all_errors:
            messagebox.showerror("Backup verification", "\n".join(all_errors[:20]), parent=self)
        else:
            all_warnings = warnings + iwarn
            messagebox.showinfo("Backup verification", "\n".join(all_warnings[:25]) or "Verification completed.", parent=self)

    def _verify_integrity_dialog(self) -> None:
        path = filedialog.askdirectory(title="Select WindowsBackup_* folder for hash integrity check", initialdir=str(DESKTOP))
        if not path:
            return
        root = Path(path)
        if not (root / INTEGRITY_FILE).exists():
            if messagebox.askyesno(
                "Integrity sample",
                f"No {INTEGRITY_FILE} found for this backup.\n\nGenerate a hash sample now? "
                "This reads a sample of backed-up files to catch later changes that size/time may miss.",
                parent=self,
            ):
                def gen() -> None:
                    try:
                        report = self.toolkit.write_integrity_sample(root)
                        n = int(report.get("SampleCount") or 0)
                        self.after(0, lambda: messagebox.showinfo(
                            "Integrity sample",
                            f"Recorded hashes for {n} sampled file(s).\n\n{root / INTEGRITY_FILE}",
                            parent=self,
                        ))
                    except Exception as e:
                        self.after(0, lambda: messagebox.showerror("Integrity sample", str(e), parent=self))
                runner = getattr(self, "_run_bg", None)
                if callable(runner):
                    runner(gen)
                else:
                    threading.Thread(target=gen, daemon=True).start()
            return
        ierr, iwarn, info = BackupToolkit.verify_integrity_sample(root)
        self.append_log(f"=== INTEGRITY CHECK: {root} ===")
        for e in ierr:
            self.append_log(f"INTEGRITY ERROR: {e}", "ERROR")
        for w in iwarn:
            sev = "WARN" if any(k in w.lower() for k in ("changed", "missing")) else "INFO"
            self.append_log(f"INTEGRITY: {w}", sev)
        if ierr:
            messagebox.showerror("Integrity check", "\n".join(ierr[:20]), parent=self)
        else:
            messagebox.showinfo("Integrity check", "\n".join(iwarn[:20]) or "Integrity check completed.", parent=self)

    def _restore_browse(self) -> None:
        path = filedialog.askdirectory(title="Select WindowsBackup_* folder", initialdir=str(DESKTOP))
        if path:
            self._restore_dialog(preselect=Path(path))

    def _export_zip(self) -> None:
        folders = self._list_backups()
        if not folders:
            messagebox.showwarning("Export", "No backup folders found.")
            return
        dlg = tk.Toplevel(self)
        dlg.title("Export to ZIP")
        dlg.geometry("500x300")
        dlg.transient(self)
        dlg.grab_set()
        tk.Label(dlg, text="Select backup:").pack(anchor="w", padx=10, pady=6)
        lb = tk.Listbox(dlg)
        lb.pack(fill="both", expand=True, padx=10)
        for f in folders:
            lb.insert("end", f.name)
        lb.selection_set(0)

        def go():
            sel = lb.curselection()
            if not sel:
                return
            src = folders[sel[0]]
            zip_path = self.backup_parent / f"{src.name}.zip"
            dlg.destroy()

            def job():
                self.append_log(f"Compressing {src.name} ...")
                try:
                    if zip_path.exists():
                        zip_path.unlink()
                    if shutil.which("7z") or shutil.which("7za") or shutil.which("7zr"):
                        self._run_shell(
                            f'7z a -tzip -mx=6 "{zip_path}" "{src}"',
                            timeout=3600,
                        )
                    else:
                        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                            for file in src.rglob("*"):
                                if file.is_file():
                                    zf.write(file, file.relative_to(src))
                    self.append_log(f"ZIP created: {zip_path}", "OK")
                    self.after(0, lambda: messagebox.showinfo("Export", f"ZIP created:\n{zip_path}"))
                except Exception as e:
                    self.append_log(f"ZIP failed: {e}", "ERROR")

            self._run_bg(job)

        tk.Button(dlg, text="Create ZIP", command=go).pack(pady=8)

    def _import_zip(self) -> None:
        path = filedialog.askopenfilename(
            title="Select backup ZIP",
            filetypes=[("ZIP", "*.zip")],
            initialdir=str(DESKTOP),
        )
        if not path:
            return
        zip_path = Path(path)
        dest = self.backup_parent / zip_path.stem
        if dest.exists():
            if not messagebox.askyesno("Import", f"Folder exists:\n{dest}\nOverwrite?"):
                return
            shutil.rmtree(dest, ignore_errors=True)

        def job():
            self.append_log(f"Extracting {zip_path.name} ...")
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(dest)
                self.append_log(f"Extracted to {dest}", "OK")
                self.after(0, lambda: messagebox.showinfo("Import", "Imported. You can Restore now."))
            except Exception as e:
                self.append_log(f"Extract failed: {e}", "ERROR")

        self._run_bg(job)

    def _open_latest(self) -> None:
        folders = self._list_backups()
        if not folders:
            messagebox.showinfo("Info", "No backup found.")
            return
        os.startfile(folders[0])

    def _install_tools(self) -> None:
        def job():
            self.append_log("=== Checking tools ===")
            if shutil.which("winget"):
                self.append_log("Winget is installed", "OK")
            else:
                self.append_log("Winget not found — install App Installer from Microsoft Store", "WARN")
            if shutil.which("choco"):
                self.append_log("Chocolatey is installed", "OK")
            else:
                self.append_log("Trying to install Chocolatey...", "WARN")
                try:
                    cmd = (
                        "powershell -NoProfile -ExecutionPolicy Bypass -Command "
                        '"Set-ExecutionPolicy Bypass -Scope Process -Force; '
                        "[Net.ServicePointManager]::SecurityProtocol = "
                        "[Net.ServicePointManager]::SecurityProtocol -bor 3072; "
                        "iex ((New-Object Net.WebClient).DownloadString('"
                        "https://community.chocolatey.org/install.ps1'))"
                    )
                    subprocess.run(cmd, shell=True, timeout=600, creationflags=_no_window_flags())
                    self.append_log("Chocolatey install attempted — restart terminal if needed", "OK")
                except Exception as e:
                    self.append_log(f"Chocolatey install failed: {e}", "ERROR")
            self.append_log("Tool check finished", "OK")
            self.after(0, lambda: messagebox.showinfo("Tools", "See log for details."))

        self._run_bg(job)


def json_dumps(obj: Any, **kwargs: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, **kwargs)


def json_loads(s: str) -> Any:
    return json.loads(s)


def load_recent_sources() -> List[Path]:
    """Load remembered restore sources (persisted in the user profile)."""
    try:
        if not RECENT_SOURCES_FILE.exists():
            return []
        data = json_loads(RECENT_SOURCES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    sources: List[Path] = []
    if isinstance(data, list):
        for item in data:
            try:
                p = Path(str(item))
            except Exception:
                continue
            if p not in sources:
                sources.append(p)
    return sources


def save_recent_sources(sources: Iterable[Path]) -> None:
    try:
        RECENT_SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
        RECENT_SOURCES_FILE.write_text(
            json_dumps([str(p) for p in sources]), encoding="utf-8"
        )
    except Exception:
        pass


def remember_recent_source(path: Path) -> List[Path]:
    """Move/add a source to the front of the remembered list and persist it."""
    target = Path(path)
    sources = [p for p in load_recent_sources() if str(p).lower() != str(target).lower()]
    sources.insert(0, target)
    sources = sources[:MAX_RECENT_SOURCES]
    save_recent_sources(sources)
    return sources


def forget_recent_source(path: Path) -> List[Path]:
    target = str(Path(path)).lower()
    sources = [p for p in load_recent_sources() if str(p).lower() != target]
    save_recent_sources(sources)
    return sources


def load_settings() -> Dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            raw = json_loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
    except Exception:
        pass
    data["backup_mode"] = normalize_backup_mode(data.get("backup_mode"), data.get("incremental"))
    data["incremental"] = data["backup_mode"] == "incremental"
    return data


def save_settings(data: Dict[str, Any]) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json_dumps(data), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
    except Exception:
        pass


def app_palette(name: str) -> Dict[str, str]:
    if str(name).lower() == "light":
        return {
            "bg": "#f1f5f9", "panel": "#ffffff", "panel2": "#e2e8f0", "card": "#ffffff",
            "accent": "#2563eb", "accent2": "#0369a1", "ok": "#15803d", "warn": "#b45309",
            "danger": "#b91c1c", "text": "#0f172a", "muted": "#475569",
            "log_bg": "#0f172a", "log_fg": "#86efac",
        }
    return {
        "bg": "#0f172a", "panel": "#111827", "panel2": "#1f2937", "card": "#162033",
        "accent": "#2563eb", "accent2": "#0ea5e9", "ok": "#16a34a", "warn": "#d97706",
        "danger": "#dc2626", "text": "#e5e7eb", "muted": "#94a3b8",
        "log_bg": "#020617", "log_fg": "#86efac",
    }


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _no_window_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def should_ignore_dir(name: str) -> bool:
    low = name.lower()
    if low in IGNORE_ENTRY_NAMES or low in IGNORE_DIR_NAMES:
        return True
    return any(low.endswith(suf) for suf in IGNORE_DIR_SUFFIXES)


def ignore_for_copytree(directory: str, contents: List[str]) -> List[str]:
    ignored = []
    for name in contents:
        full = os.path.join(directory, name)
        low = name.lower()
        if low in IGNORE_ENTRY_NAMES or low in IGNORE_FILE_NAMES:
            ignored.append(name)
            continue
        try:
            # Use lstat to avoid following symlinks
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode) and should_ignore_dir(name):
                ignored.append(name)
            elif stat.S_ISREG(st.st_mode) and low in IGNORE_FILE_NAMES:
                ignored.append(name)
        except OSError:
            # On stat errors, only ignore if name is explicitly ignorable.
            if should_ignore_dir(name) or low in IGNORE_FILE_NAMES:
                ignored.append(name)
    return ignored


def main() -> None:
    if sys.platform != "win32":
        print("This toolkit targets Windows.")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
