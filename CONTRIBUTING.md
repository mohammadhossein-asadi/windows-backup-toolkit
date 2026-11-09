# Contributing

Short version: the app is one stdlib-only Python file, the exe is built cross-platform
with one command, and a pull request should keep both working.

## Run it from source

You need Windows 10/11 and **Python 3.10+ with Tk** (the python.org installer includes it
by default). There are no dependencies to install — the app imports only the standard
library and Tk.

```bat
python -m tkinter                        :: sanity check: should open a test window
python Backup_Restore_Toolkit.py         :: runs with a console for log output
pythonw Backup_Restore_Toolkit.py        :: runs without a console
```

Start it **as administrator** when touching driver, registry, `hosts` or Wi-Fi code;
those paths are skipped otherwise. `app_icon.png` next to the script gives the window its
custom icon.

## Where things live

All application code is in `files/Backup_Restore_Toolkit.py` (~5800 lines, in sections):

- **Settings and defaults** — `DEFAULT_SETTINGS`, skip rules, ignore lists near the top.
- **`BackupToolkit`** — backup/restore engine: stage methods, manifest, integrity,
  hardlinks, retention.
- **`App`** — the Tk interface: Dashboard, Backup, Restore, Settings pages and dialogs.
- **`main()`** at the bottom — the only entry point.

Keep the existing widget and method contracts intact (`self.txt`, `append_log`, the named
buttons, settings keys). Other parts of the UI and the portable launcher depend on them.

## Rebuild the portable exe

The build cross-compiles, so run it on **Linux, macOS or WSL**. On plain Windows use
PyInstaller instead — see [build/README.md](build/README.md).

```bash
python3 build/build_portable.py
```

That pulls a Windows Python runtime and a mingw-w64 cross compiler into
`build/.cache` on first use, then writes `dist/Backup_Restore_Toolkit_<version>_Portable.exe`
and `.zip`. Later runs take about ten seconds. `py build/build_portable.py --skip-verify`
skips the self-checks; `--toolchain <prefix>` reuses an existing compiler.

The build always re-reads `VERSION` from the source, so bumping it is all that's needed
for a new file name, a new Windows version resource, and a new GitHub release.

## Before opening a pull request

1. **Bump `VERSION`** in `files/Backup_Restore_Toolkit.py`.
2. Run the app and do a quick manual pass:
   - Full backup of a small folder, then **Verify** it.
   - Compact backup on top of it, then **Preview** and restore to a scratch folder.
   - Cancel a backup mid-run and confirm it **resumes** rather than restarts.
   - Settings: add a skip folder/extension and confirm it is honoured.
3. Build the exe on Linux/macOS/WSL and launch it: `python3 build/build_portable.py`.
4. If you touched the workflow: `python3 build/check_workflow.py`.

Tests are manual today — describe what you ran in the pull request. Say explicitly if
something was not checked (for example "not tested on a second machine").

## Style

- Standard library only. No new third-party dependencies, no network calls at runtime.
- Windows-first, but don't crash on other platforms — the app already tolerates Linux for
  layout work.
- User-facing text: plain words, no jargon, say what will happen before doing it.
- Destructive actions (delete, overwrite, restore) need a confirmation or a preview.
- Log meaningful stages and warnings; errors should say what to do next.

## Releases

Pushing a version bump to `main` makes GitHub Actions build and publish a release named
`v<version>`. Pushing tag `v<version>` works too; a tag that disagrees with `VERSION`
fails the build on purpose.
