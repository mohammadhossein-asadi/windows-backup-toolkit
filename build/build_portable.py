#!/usr/bin/env python3
"""Build the portable Windows .exe (and .zip) for Backup Restore Toolkit.

One command:

    python3 build/build_portable.py

What it does
------------
1. Downloads micromamba into build/.cache (once).
2. Creates a cached win-64 Python 3.12 + Tk environment (the runtime we ship).
3. Creates a cached Linux cross toolchain (mingw-w64 gcc + windres).
4. Compiles build/launcher/launcher.c (a tiny self-extracting launcher) with the
   icon and VERSIONINFO resources from build/assets + build/launcher/launcher.rc.
5. Assembles the portable folder (runtime + app + launcher.pyw + Run Toolkit.bat).
6. Zips it deterministically and appends it to the launcher with a WBKZIP01
   trailer, producing a single self-contained .exe.
7. Verifies the result: PE header, trailer, archive contents, app hash, resources.

Outputs (in dist/):
    Backup_Restore_Toolkit_<version>_Portable.exe   <- single file, no Python needed
    Backup_Restore_Toolkit_<version>_Portable.zip   <- same bundle, unzip + .bat

Nothing is downloaded twice: pass --cache-dir to share a cache, or reuse the
default build/.cache between runs. Requires only Python 3.8+ on the host.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = Path(__file__).resolve().parent
APP_SOURCE = ROOT / "files" / "Backup_Restore_Toolkit.py"
LAUNCHER_SRC = BUILD / "launcher" / "launcher.c"
LAUNCHER_RC = BUILD / "launcher" / "launcher.rc"
BUNDLE_ASSETS = BUILD / "bundle"
ASSETS = BUILD / "assets"

APP_NAME = "Backup_Restore_Toolkit"
TRAILER_MAGIC = b"WBKZIP01"
FIXED_ZIP_DATE = (2020, 1, 1, 0, 0, 0)

# Files that must be inside the payload for the app to start.
REQUIRED_ENTRIES = [
    "Backup_Restore_Toolkit.py",
    "launcher.pyw",
    "app_icon.png",
    "app_icon.ico",
    "Run Toolkit.bat",
    "runtime/pythonw.exe",
    "runtime/python312.dll",
    "runtime/DLLs/_tkinter.pyd",
    "runtime/Lib/tkinter/__init__.py",
    "runtime/Library/lib/tcl8.6/init.tcl",
    "runtime/Library/lib/tk8.6/tk.tcl",
    "runtime/Library/bin/tcl86t.dll",
    "runtime/Library/bin/tk86t.dll",
    "runtime/Library/bin/zlib1.dll",
    "runtime/vcruntime140.dll",
]

# Bulk that is not needed at runtime (saves ~18 MB compressed).
TRIM_DIRS = [
    "conda-meta",
    "Scripts",
    "include",
    "Library/include",
    "Lib/test",
    "Lib/idlelib",
    "Lib/ensurepip",
    "Lib/turtledemo",
    "Lib/lib2to3",
    "Lib/site-packages/pip",
    "Lib/site-packages/setuptools",
]
TRIM_GLOBS = ["*.lib", "*.a", "*.pdb"]


def log(msg: str) -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    log(f"\n== {msg}")


def run(cmd: list, cwd: Path | None = None, env: dict | None = None) -> None:
    shown = " ".join(str(c) for c in cmd)
    log(f"  $ {shown}")
    subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None, env=env, check=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def app_version() -> str:
    text = APP_SOURCE.read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise SystemExit(f"Could not find VERSION in {APP_SOURCE}")
    return match.group(1)


def host_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        return "linux-aarch64" if machine in ("aarch64", "arm64") else "linux-64"
    if system == "darwin":
        return "osx-arm64" if machine in ("arm64", "aarch64") else "osx-64"
    raise SystemExit(
        "This script cross-builds on Linux or macOS. On Windows, run it inside WSL "
        "(or build a one-file exe with PyInstaller - see build/README.md)."
    )


def ensure_micromamba(cache: Path) -> Path:
    exe = cache / "micromamba" / "bin" / "micromamba"
    if exe.exists():
        return exe
    plat = host_platform()
    url = f"https://micro.mamba.pm/api/micromamba/{plat}/latest"
    log(f"  downloading micromamba ({plat}) ...")
    with urllib.request.urlopen(url, timeout=180) as resp:
        blob = resp.read()
    exe.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:bz2") as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith("bin/micromamba"))
        member.name = "micromamba"
        tf.extract(member, path=str(exe.parent))
    exe.chmod(0o755)
    return exe


def ensure_env(mm: Path, prefix: Path, specs: list[str], marker: str, cache: Path,
               win64: bool = False) -> Path:
    """Create a conda env once, reusing it on later builds."""
    marker_path = prefix / marker
    if marker_path.exists():
        log(f"  reusing {prefix.name}")
        return prefix
    if prefix.exists():
        log(f"  removing incomplete {prefix.name}")
        shutil.rmtree(prefix, ignore_errors=True)
    env = dict(os.environ, MAMBA_ROOT_PREFIX=str(cache / "mm-root"))
    cmd = [mm, "create", "-y", "-p", prefix, "-c", "conda-forge", *specs]
    if win64:
        cmd[2:2] = ["--platform", "win-64"]
    run(cmd, env=env)
    if not marker_path.exists():
        raise SystemExit(f"Environment {prefix} was created but {marker} is missing")
    return prefix


def build_runtime(cache: Path, mm: Path) -> Path:
    step("Windows Python runtime (win-64, with Tk)")
    return ensure_env(
        mm, cache / "winpy", ["python=3.12", "tk"], "python.exe", cache, win64=True
    )


def build_toolchain(cache: Path, mm: Path) -> Path:
    step("Cross toolchain (mingw-w64)")
    return ensure_env(
        mm, cache / "toolchain", ["gcc_win-64", "binutils_win-64"],
        "bin/x86_64-w64-mingw32-gcc", cache,
    )


def trim_runtime(runtime: Path) -> None:
    for rel in TRIM_DIRS:
        target = runtime / rel
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    for pattern in TRIM_GLOBS:
        for path in runtime.rglob(pattern):
            try:
                path.unlink()
            except OSError:
                pass


def _tool(prefix: Path, name: str) -> Path:
    """Binary inside a toolchain prefix (works for /usr and for conda prefixes)."""
    candidate = prefix / "bin" / name
    if not candidate.exists():
        raise SystemExit(f"Toolchain is missing {name}: {candidate} does not exist")
    return candidate


def compile_launcher(toolchain: Path, work: Path, version: str) -> Path:
    step(f"Launcher executable (v{version})")
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ASSETS / "app_icon.ico", work / "app.ico")
    shutil.copy2(LAUNCHER_SRC, work / "launcher.c")
    shutil.copy2(BUILD / "launcher" / "miniz.c", work / "miniz.c")
    shutil.copy2(BUILD / "launcher" / "miniz.h", work / "miniz.h")

    version_comma = ",".join((version.split(".") + ["0", "0", "0", "0"])[:4])
    rc_text = LAUNCHER_RC.read_text(encoding="utf-8")
    rc_text = rc_text.replace("@VERSION@", version).replace("@VERSION_COMMA@", version_comma)
    (work / "launcher.rc").write_text(rc_text, encoding="utf-8")

    env = dict(os.environ, PATH=f"{toolchain / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    run([_tool(toolchain, "x86_64-w64-mingw32-windres"),
         "-c", "65001", "-I.", "launcher.rc", "launcher_res.o"],
        cwd=work, env=env)
    exe = work / "launcher.exe"
    run([
        _tool(toolchain, "x86_64-w64-mingw32-gcc"),
        "-O2", "-mwindows", "-o", str(exe), f'-DAPP_VERSION="{version}"',
        "launcher.c", "miniz.c", "launcher_res.o",
        "-static", "-static-libgcc", "-Wl,--no-insert-timestamp",
    ], cwd=work, env=env)
    if not exe.exists():
        raise SystemExit("Launcher compilation failed")
    return exe


def assemble_bundle(bundle: Path, runtime: Path, version: str) -> None:
    step("Portable bundle")
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    log("  copying runtime ...")
    shutil.copytree(runtime, bundle / "runtime", symlinks=True)
    trim_runtime(bundle / "runtime")

    shutil.copy2(APP_SOURCE, bundle / "Backup_Restore_Toolkit.py")
    shutil.copy2(BUNDLE_ASSETS / "launcher.pyw", bundle / "launcher.pyw")
    shutil.copy2(BUNDLE_ASSETS / "Run Toolkit.bat", bundle / "Run Toolkit.bat")
    shutil.copy2(ASSETS / "app_icon.png", bundle / "app_icon.png")
    shutil.copy2(ASSETS / "app_icon.ico", bundle / "app_icon.ico")

    readme = (BUNDLE_ASSETS / "README.txt").read_text(encoding="utf-8")
    (bundle / "README.txt").write_text(readme.replace("@VERSION@", version), encoding="utf-8")


def write_zip_deterministic(src: Path, out: Path) -> None:
    """Zip a folder with sorted entries and fixed timestamps (reproducible)."""
    files = sorted(p for p in src.rglob("*") if p.is_file())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            rel = path.relative_to(src).as_posix()
            info = zipfile.ZipInfo(rel, date_time=FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())


def make_exe(launcher: Path, payload: Path, out: Path) -> None:
    """Concatenate launcher + zip + 16-byte trailer (magic + payload offset)."""
    launcher_bytes = launcher.read_bytes()
    payload_bytes = payload.read_bytes()
    out.write_bytes(launcher_bytes + payload_bytes + TRAILER_MAGIC + struct.pack("<Q", len(launcher_bytes)))


def verify(exe: Path, payload: Path, version: str) -> None:
    step("Verification")
    data = exe.read_bytes()
    problems: list[str] = []

    if not data.startswith(b"MZ"):
        problems.append("not a Windows executable (missing MZ header)")
    if data[-16:-8] != TRAILER_MAGIC:
        problems.append("missing WBKZIP01 trailer")
    else:
        offset = struct.unpack("<Q", data[-8:])[0]
        if data[offset:offset + 4] != b"PK\x03\x04":
            problems.append("payload offset does not point at a ZIP archive")

    with zipfile.ZipFile(payload) as zf:
        names = set(zf.namelist())
        for entry in REQUIRED_ENTRIES:
            if entry not in names:
                problems.append(f"payload missing {entry}")
        packed_app = zf.read("Backup_Restore_Toolkit.py")
        if hashlib.sha256(packed_app).hexdigest() != sha256(APP_SOURCE):
            problems.append("packaged app differs from source")
        if any(n.lower().endswith(".pdb") for n in names):
            problems.append("payload contains debug symbols (*.pdb)")
        bad = zf.testzip()
        if bad:
            problems.append(f"corrupt entry in payload: {bad}")

    # VERSIONINFO strings are UTF-16LE; check they are present in the launcher part.
    launcher_bytes = data[:len(data) - len(payload.read_bytes()) - 16]
    for text in ("FileVersion", "ProductName", version):
        if text.encode("utf-16-le") not in launcher_bytes:
            problems.append(f"version resource missing {text!r}")

    if problems:
        for p in problems:
            log(f"  FAIL: {p}")
        raise SystemExit("Verification failed")

    log(f"  exe       {exe.stat().st_size:,} bytes  sha256 {sha256(exe)}")
    log(f"  payload   {payload.stat().st_size:,} bytes, {len(names)} files")
    log("  checks    PE header, trailer, archive, app hash, version resource: OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the portable Windows exe/zip")
    parser.add_argument("--cache-dir", default=str(BUILD / ".cache"),
                        help="where to keep micromamba, the win-64 runtime and toolchain")
    parser.add_argument("--dist-dir", default=str(ROOT / "dist"), help="output folder")
    parser.add_argument("--skip-verify", action="store_true", help="skip post-build checks")
    parser.add_argument("--toolchain", metavar="PREFIX",
                        help="use an existing mingw-w64 prefix (e.g. /usr on CI) instead of "
                             "downloading one; the prefix must contain bin/x86_64-w64-mingw32-gcc")
    args = parser.parse_args()

    if os.name == "nt":
        raise SystemExit(
            "Run this on Linux/macOS (or WSL). On plain Windows use PyInstaller - "
            "see build/README.md."
        )
    if not APP_SOURCE.exists():
        raise SystemExit(f"Missing source: {APP_SOURCE}")
    for needed in [LAUNCHER_SRC, LAUNCHER_RC, ASSETS / "app_icon.ico", ASSETS / "app_icon.png",
                   BUNDLE_ASSETS / "launcher.pyw", BUNDLE_ASSETS / "Run Toolkit.bat",
                   BUNDLE_ASSETS / "README.txt"]:
        if not needed.exists():
            raise SystemExit(f"Missing build input: {needed}")

    cache = Path(args.cache_dir).resolve()
    dist = Path(args.dist_dir).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    dist.mkdir(parents=True, exist_ok=True)
    work = cache / "work"

    version = app_version()
    log(f"Backup Restore Toolkit {version}")
    log(f"cache: {cache}")
    log(f"dist:  {dist}")

    mm = ensure_micromamba(cache)
    runtime = build_runtime(cache, mm)
    if args.toolchain:
        toolchain = Path(args.toolchain).resolve()
        _tool(toolchain, "x86_64-w64-mingw32-gcc")
        log(f"\n== Cross toolchain\n  using system toolchain {toolchain}")
    else:
        toolchain = build_toolchain(cache, mm)
    launcher = compile_launcher(toolchain, work, version)

    bundle = work / "bundle"
    assemble_bundle(bundle, runtime, version)

    step("Packaging")
    payload = work / "payload.zip"
    write_zip_deterministic(bundle, payload)
    log(f"  payload.zip {payload.stat().st_size:,} bytes")

    exe = dist / f"{APP_NAME}_{version}_Portable.exe"
    make_exe(launcher, payload, exe)
    zip_out = dist / f"{APP_NAME}_{version}_Portable.zip"
    shutil.copy2(payload, zip_out)

    step("Outputs")
    log(f"  {exe}  ({exe.stat().st_size:,} bytes)")
    log(f"  {zip_out}  ({zip_out.stat().st_size:,} bytes)")

    if not args.skip_verify:
        verify(exe, payload, version)

    log("\nDone.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Command failed ({exc.returncode}): {exc.cmd}")
