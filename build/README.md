# Portable Windows build

One command rebuilds the single-file portable `.exe` (and the equivalent `.zip`)
for the toolkit:

```bash
python3 build/build_portable.py
```

Outputs land in `dist/`:

| File | What it is |
| --- | --- |
| `Backup_Restore_Toolkit_<version>_Portable.exe` | Single self-extracting app, no Python needed |
| `Backup_Restore_Toolkit_<version>_Portable.zip` | Same bundle, unzip and double-click `Run Toolkit.bat` |

## How it works

1. **Toolchain** – downloads `micromamba` once into `build/.cache`, then creates two
   cached environments:
   - `build/.cache/winpy` – win-64 Python 3.12 + Tk (the runtime that gets shipped)
   - `build/.cache/toolchain` – `gcc_win-64` + `binutils_win-64` for cross-compiling
2. **Launcher** – compiles `build/launcher/launcher.c` (with `miniz` for unzipping)
   plus `build/launcher/launcher.rc`, which embeds the icon and Windows version info.
3. **Bundle** – copies the runtime, trims docs/tests/headers, adds the app,
   `launcher.pyw`, `Run Toolkit.bat`, the readme and both icon files.
4. **Package** – writes a deterministic ZIP, then concatenates
   `launcher.exe + payload.zip + "WBKZIP01" + offset`.
5. **Verify** – checks the PE header, trailer and payload offset, opens the embedded
   archive, confirms every required file, confirms the packaged `Backup_Restore_Toolkit.py`
   is byte-identical to `files/Backup_Restore_Toolkit.py`, and checks the version strings.

At runtime the exe unpacks once to `%LOCALAPPDATA%\WindowsBackupToolkit\app-<version>`
and starts `runtime\pythonw.exe launcher.pyw`, so there is no console window and no
Python installation on the target machine.

## Requirements

- Linux or macOS host (or WSL). Python 3.8+ and network access on the first run.
- After the first run everything is cached: later builds work offline and take
  well under a minute for packaging (the ~130 MB runtime copy dominates).

## Options

```
--cache-dir PATH   where micromamba/runtime/toolchain are kept (default build/.cache)
--dist-dir PATH    output folder (default dist/)
--toolchain PREFIX use an existing mingw-w64 prefix (e.g. /usr) instead of downloading one
--skip-verify      skip the post-build checks
```

## Changing the icon or version

- Icon: replace `build/assets/app_icon.png` (256×256) and `build/assets/app_icon.ico`
  (multi-size). The app loads `app_icon.png` for its window icon, the exe embeds the `.ico`.
- Version: edit `VERSION` in `files/Backup_Restore_Toolkit.py`. The exe name and the
  Windows version resource follow it automatically.

## Automatic builds on GitHub

`.github/workflows/build-portable.yml` builds the exe on every push that touches the
source or the build system, and publishes a GitHub Release named after the version:

- Bump `VERSION` in `files/Backup_Restore_Toolkit.py` and push to `main`.
- The workflow installs Ubuntu's mingw-w64 cross compiler (fast, no 1.5 GB cache),
  restores the cached Windows runtime, and runs `build_portable.py --toolchain /usr`.
- It creates release `v<version>` (or replaces the files if that release already exists)
  with the `.exe`, the `.zip` and `SHA256SUMS.txt`.
- Pushing a tag like `v3.2.0` also works; a tag that disagrees with `VERSION` fails the
  build instead of publishing a misleading download.
- Pull requests only run the build and keep the result as a workflow artifact.

Requirements: the repository default token needs *Contents: write*, which the workflow
requests explicitly. No secrets are involved.

Check the workflow locally after editing it (needs PyYAML):

```bash
python3 build/check_workflow.py
```

It parses the YAML and runs `bash -n` on every `run:` block, so shell typos are caught
before pushing.

## On native Windows

Cross-compiling from Windows is not covered here. Two options:

```bat
py -m pip install pyinstaller
py -m PyInstaller --onefile --windowed --icon build\assets\app_icon.ico ^
    --name Backup_Restore_Toolkit files\Backup_Restore_Toolkit.py
```

(`--add-data "app_icon.png;."` if you also want the window icon), or run this script
inside WSL, which produces the exact same artifact as CI/Linux builds.

## Third-party code

`build/launcher/miniz.c` / `miniz.h` are [miniz](https://github.com/richgel999/miniz)
by Rich Geldreich, MIT licensed, used only to unpack the embedded archive.
