# Building the portable Windows exe

This folder contains the deterministic build system that packages
`files/Backup_Restore_Toolkit.py` into a single Windows x64 executable with a
bundled Python runtime.

## What it produces

`python3 build/build_portable.py` writes to `dist/` by default:

- `Backup_Restore_Toolkit_3.1.0_Portable.exe` — the single-file launcher
- `Backup_Restore_Toolkit_3.1.0_Portable.zip` — the exe plus checksums
- `SHA256SUMS.txt` — SHA-256 hashes for verification

The exe is a native MinGW-w64 launcher that:

1. extracts a private, versioned runtime to `%LOCALAPPDATA%\BackupRestoreToolkit\…`
   (a stable hash of the embedded payload, so it does not re-extract every run),
2. runs the bundled `launcher.pyw`, which prefers the packaged toolkit copy and
   falls back to a `Backup_Restore_Toolkit*.py` file next to the exe, and
3. starts the tkinter app with the bundled stdlib/tkinter runtime.

## Prerequisites (Linux, cross-compiling)

```bash
sudo apt-get update
sudo apt-get install -y mingw-w64 ca-certificates python3
```

The script needs `x86_64-w64-mingw32-gcc`, `x86_64-w64-mingw32-windres`, and
`x86_64-w64-mingw32-strip` (or a toolchain prefix passed via `--toolchain`).

## Build

```bash
# From the repo root
python3 build/build_portable.py --toolchain /usr --dist-dir dist
```

On first run it downloads and verifies the pinned Windows embeddable Python
distribution (SHA-256 checked) and caches it in `build/.cache/`.

Useful flags:

- `--cache-dir PATH` — where the runtime and work files are cached
  (default `build/.cache`)
- `--toolchain /usr` or `--toolchain /path/to/prefix` — MinGW-w64 location
- `--dist-dir PATH` — output directory (default `dist`)
- `--skip-verify` — skip the static payload/zip verification step (faster local
  iteration; CI and releases should keep verification on)
- `--keep-work` — keep the staging directory for inspection

## Determinism

Identical inputs produce the same `Backup_Restore_Toolkit_3.1.0_Portable.exe`
hash. The script normalizes timestamps inside the embedded runtime (`.pyc`,
`.pyd`, `.dll`, `.exe`, zip entries) and passes `-Wl,--no-insert-timestamp` to
the MinGW linker.

Reproduce locally:

```bash
python3 build/build_portable.py --toolchain /usr --dist-dir dist-a
python3 build/build_portable.py --toolchain /usr --dist-dir dist-b
sha256sum dist-a/Backup_Restore_Toolkit_3.1.0_Portable.exe \
          dist-b/Backup_Restore_Toolkit_3.1.0_Portable.exe
```

The hashes must match (assuming the same source, script, and cached runtime).

## Payload layout (inside the exe's embedded zip)

```
python/        # Windows embeddable runtime + stdlib + tkinter
app/
  Backup_Restore_Toolkit.py
  launcher.pyw
  README.txt
  build-provenance.json
  version.txt
Run Toolkit.bat
```

## Versioning & releases

`APP_VERSION` in `build/build_portable.py` must match the version the toolkit
reports. The GitHub Actions workflow builds on push/PR and, on non-PR events,
publishes/updates a GitHub Release tagged `v<version>` with the exe, zip and
checksums.

## CI workflow check

```bash
python3 build/check_workflow.py
```

This validates the workflow YAML and does a lightweight sanity pass (required
jobs/steps present, scripts parse).
