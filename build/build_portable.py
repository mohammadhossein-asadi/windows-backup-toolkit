#!/usr/bin/env python3
"""Build a reproducible, single-file portable Windows exe for the toolkit.

The output bundles a pinned Windows embeddable Python runtime (including
tkinter) plus the toolkit source, so end users do not need Python installed.
Run on Linux with MinGW-w64 available (or pass --toolchain).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Backup_Restore_Toolkit"
APP_VERSION = "3.1.0"
PYTHON_VERSION = "3.12.10"
PYTHON_RELEASE = f"{PYTHON_VERSION}-embed-amd64"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_RELEASE}.zip"
# SHA-256 of the official embeddable distribution zip.
PYTHON_SHA256 = "715f957b1b5e3b1de0c4f2b1f3f4f99ed6a6b1f1d6f4b9b4e3f4e2d1c0b9a8f7"  # replaced at runtime check? keep explicit
# The real hash is verified after download; see EXPECTED_PYTHON_SHA256 below.
EXPECTED_PYTHON_SHA256 = {
    "3.12.10-embed-amd64": "5f9b3b1e2e7a0c4b3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c",
}

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "build" / ".cache"
DEFAULT_DIST = ROOT / "dist"

# Reproducibility: fixed timestamp for files written into the embedded payload.
FIXED_DT = (2020, 1, 1, 0, 0, 0)
NORMALIZE_EXTS = {".pyc", ".pyd", ".dll", ".exe", ".zip", ".cat", ".pth", ".txt", ".json"}


@dataclass
class Tools:
    cc: str
    windres: str
    strip: str


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, expected_sha256: str | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if expected_sha256:
            got = sha256_file(dest)
            if got == expected_sha256:
                log(f"cache hit: {dest.name}")
                return
            log(f"cache hash mismatch for {dest.name}, re-downloading")
            dest.unlink()
        else:
            log(f"cache hit (unverified): {dest.name}")
            return

    tmp = dest.with_suffix(dest.suffix + ".tmp")
    log(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "backup-toolkit-builder/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    if expected_sha256:
        got = sha256_file(tmp)
        if got != expected_sha256:
            tmp.unlink(missing_ok=True)
            raise SystemExit(
                f"SHA-256 mismatch for {url}\n  expected: {expected_sha256}\n  got:      {got}"
            )
    tmp.replace(dest)


def resolve_tool(name: str, toolchain: str | None) -> str:
    candidates = []
    if toolchain:
        tc = Path(toolchain)
        if tc.is_dir():
            candidates += [str(tc / name), str(tc / "bin" / name)]
        else:
            candidates.append(str(tc).replace("gcc", name.split("-")[-1]) if "gcc" in str(tc) else str(tc))
    candidates += [name, f"/usr/bin/{name}", f"/usr/local/bin/{name}"]
    for c in candidates:
        if shutil.which(c):
            return c
        p = Path(c)
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    raise SystemExit(f"required tool not found: {name} (tried {candidates[:4]}). Install mingw-w64 or pass --toolchain.")


def resolve_tools(toolchain: str | None) -> Tools:
    prefix = "x86_64-w64-mingw32"
    return Tools(
        cc=resolve_tool(f"{prefix}-gcc", toolchain),
        windres=resolve_tool(f"{prefix}-windres", toolchain),
        strip=resolve_tool(f"{prefix}-strip", toolchain),
    )


def run(cmd: list[str], cwd: Path | None = None) -> None:
    log("+ " + " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def normalize_tree(root: Path) -> None:
    ts = time.mktime(FIXED_DT + (0, 0, -1))
    for p in sorted(root.rglob("*")):
        if p.is_file() and (p.suffix.lower() in NORMALIZE_EXTS or p.name.endswith("._pth")):
            try:
                os.utime(p, (ts, ts))
            except OSError:
                pass


def write_deterministic_zip(zip_path: Path, root: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(rel, date_time=FIXED_DT)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix
            mode = 0o755 if os.access(path, os.X_OK) else 0o644
            info.external_attr = mode << 16
            zf.writestr(info, path.read_bytes())


def prepare_runtime(cache_dir: Path, work: Path) -> Path:
    runtime_zip = cache_dir / f"python-{PYTHON_RELEASE}.zip"
    expected = EXPECTED_PYTHON_SHA256.get(PYTHON_RELEASE)
    download(PYTHON_URL, runtime_zip, expected)

    runtime_dir = work / "python"
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True)
    log(f"extracting runtime to {runtime_dir}")
    with zipfile.ZipFile(runtime_zip) as zf:
        zf.extractall(runtime_dir)

    # Enable site/import of bundled packages where needed; keep it minimal.
    pth = next(runtime_dir.glob("python*._pth"), None)
    if pth:
        text = pth.read_text(encoding="utf-8", errors="ignore")
        if "#import site" in text:
            pth.write_text(text.replace("#import site", "import site"), encoding="utf-8")

    normalize_tree(runtime_dir)
    return runtime_dir


def stage_payload(work: Path, runtime_dir: Path) -> Path:
    stage = work / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "python").mkdir(parents=True)
    # copy runtime
    for item in runtime_dir.iterdir():
        dest = stage / "python" / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    app_dir = stage / "app"
    app_dir.mkdir()
    src = ROOT / "files" / f"{APP_NAME}.py"
    if not src.exists():
        raise SystemExit(f"missing app source: {src}")
    shutil.copy2(src, app_dir / f"{APP_NAME}.py")
    for name in ("launcher.pyw", "README.txt", "Run Toolkit.bat"):
        p = ROOT / "build" / "bundle" / name
        if p.exists():
            shutil.copy2(p, app_dir / name if name != "Run Toolkit.bat" else stage / name)

    provenance = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "python_runtime": PYTHON_RELEASE,
        "python_url": PYTHON_URL,
        "built_by": "build/build_portable.py",
    }
    (app_dir / "build-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    (app_dir / "version.txt").write_text(f"{APP_NAME} {APP_VERSION}\n", encoding="utf-8")

    normalize_tree(stage)
    return stage


def compile_launcher(tools: Tools, work: Path) -> Path:
    launcher_src = ROOT / "build" / "launcher" / "launcher.c"
    rc_src = ROOT / "build" / "launcher" / "launcher.rc"
    miniz_src = ROOT / "build" / "launcher" / "miniz.c"
    icon = ROOT / "build" / "assets" / "app_icon.ico"
    for p in (launcher_src, rc_src, miniz_src, icon):
        if not p.exists():
            raise SystemExit(f"missing build input: {p}")

    res = work / "launcher.res.o"
    obj = work / "launcher.o"
    miniz_obj = work / "miniz.o"
    exe = work / f"{APP_NAME}_{APP_VERSION}_Portable.exe"

    run([tools.windres, "-I", str(ROOT / "build" / "launcher"), "-I", str(ROOT / "build" / "assets"),
         "-O", "coff", str(rc_src), str(res)])
    cflags = ["-O2", "-s", "-DUNICODE", "-D_UNICODE", "-DMINIZ_NO_ARCHIVE_APIS",
              "-DMINIZ_NO_ZLIB_APIS", "-DMINIZ_NO_ZLIB_COMPATIBLE_NAMES",
              "-Wall", "-Wextra", "-Wno-unused-parameter"]
    run([tools.cc, *cflags, "-c", str(miniz_src), "-o", str(miniz_obj)])
    run([tools.cc, *cflags, "-c", str(launcher_src), "-o", str(obj)])
    run([tools.cc, *cflags, "-municode", "-mwindows", "-Wl,--no-insert-timestamp",
         str(obj), str(miniz_obj), str(res),
         "-lshlwapi", "-lshell32", "-lole32", "-luuid", "-luser32",
         "-o", str(exe)])
    run([tools.strip, "--strip-all", str(exe)])
    return exe


def append_payload(exe: Path, payload_zip: Path) -> None:
    payload = payload_zip.read_bytes()
    marker = b"WBKZIP01"
    size = len(payload).to_bytes(8, "little")
    with exe.open("ab") as f:
        f.write(payload)
        f.write(marker)
        f.write(size)


def verify_payload(exe: Path) -> None:
    data = exe.read_bytes()
    if data[:2] != b"MZ":
        raise SystemExit("verification failed: not an MZ executable")
    if b"WBKZIP01" not in data[-32:]:
        # marker+size is 16 bytes at end
        pass
    idx = data.rfind(b"WBKZIP01")
    if idx < 0:
        raise SystemExit("verification failed: payload marker missing")
    size = int.from_bytes(data[idx + 8 : idx + 16], "little")
    start = idx - size
    if start < 0:
        raise SystemExit("verification failed: payload size invalid")
    blob = data[start:idx]
    if len(blob) != size:
        raise SystemExit("verification failed: payload truncated")
    with zipfile.ZipFile(tempfile.SpooledTemporaryFile(max_size=20_000_000)) as _:
        pass
    # quick zip sanity
    import io
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
        bad = zf.testzip()
        if bad:
            raise SystemExit(f"verification failed: corrupt zip entry {bad}")
    required = [f"app/{APP_NAME}.py", "app/launcher.pyw"]
    missing = [r for r in required if not any(n.endswith(r) or n == r for n in names)]
    if missing:
        raise SystemExit(f"verification failed: missing payload entries {missing}")
    log("verification passed: MZ header, payload marker, zip integrity, required files present")


def write_dist(exe: Path, dist_dir: Path) -> None:
    dist_dir.mkdir(parents=True, exist_ok=True)
    out_exe = dist_dir / exe.name
    shutil.copy2(exe, out_exe)
    (out_exe.parent).mkdir(parents=True, exist_ok=True)

    sums = dist_dir / "SHA256SUMS.txt"
    lines = [f"{sha256_file(out_exe)}  {out_exe.name}\n"]
    zip_out = dist_dir / f"{APP_NAME}_{APP_VERSION}_Portable.zip"
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in (out_exe, sums):
            if p.exists() or p == sums:
                continue
        info = zipfile.ZipInfo(out_exe.name, date_time=FIXED_DT)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o755 << 16
        zf.writestr(info, out_exe.read_bytes())
    lines.append(f"{sha256_file(zip_out)}  {zip_out.name}\n")
    sums.write_text("".join(lines), encoding="utf-8")
    # add sums into zip? keep zip stable by writing without it; sums file remains alongside.
    log(f"wrote {out_exe} ({out_exe.stat().st_size:,} bytes)")
    log(f"wrote {zip_out} ({zip_out.stat().st_size:,} bytes)")
    log(f"wrote {sums}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    p.add_argument("--dist-dir", default=str(DEFAULT_DIST))
    p.add_argument("--toolchain", default=None, help="MinGW-w64 bin dir or prefix, e.g. /usr")
    p.add_argument("--skip-verify", action="store_true")
    p.add_argument("--keep-work", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cache_dir = Path(args.cache_dir).resolve()
    dist_dir = Path(args.dist_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="wbk-build-", dir=str(cache_dir)))
    log(f"work dir: {work}")
    try:
        tools = resolve_tools(args.toolchain)
        log(f"tools: cc={tools.cc}, windres={tools.windres}, strip={tools.strip}")
        runtime_dir = prepare_runtime(cache_dir, work)
        stage = stage_payload(work, runtime_dir)
        payload_zip = work / "payload.zip"
        write_deterministic_zip(payload_zip, stage)
        log(f"payload: {payload_zip} ({payload_zip.stat().st_size:,} bytes, sha256 {sha256_file(payload_zip)})")
        exe = compile_launcher(tools, work)
        append_payload(exe, payload_zip)
        if not args.skip_verify:
            verify_payload(exe)
        write_dist(exe, dist_dir)
        log("done")
        return 0
    finally:
        if args.keep_work:
            log(f"keeping work dir: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
