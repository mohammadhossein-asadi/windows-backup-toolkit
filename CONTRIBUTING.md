# Contributing

Thanks for taking the time to improve the Backup & Restore Toolkit.

## What this project is

A portable, **Windows-only** tkinter app packaged as a single-file exe. The
source of truth for the shipped payload is
[`files/Backup_Restore_Toolkit.py`](files/Backup_Restore_Toolkit.py).

## Before you start

- Keep changes focused. This is a safety-oriented backup tool; prefer small,
  reviewable diffs over large refactors.
- **Do not weaken safety checks** to make a run "pass" (integrity sampling,
  resume markers, skip/exclusion logging, preflight, error reporting). If a
  behavior must change, explain why and update the docs/tests that cover it.
- Avoid adding third-party runtime dependencies to the app — it is intentionally
  close to stdlib-only so the portable exe stays predictable.
- Never commit secrets, real Wi-Fi passwords/keys, personal backup data, or
  machine-specific paths.

## Development setup

You only need Python to edit the app source:

```bash
git clone https://github.com/mohammadhossein-asadi/windows-backup-toolkit.git
cd windows-backup-toolkit
python files/Backup_Restore_Toolkit.py   # on Windows
```

For a quick syntax check on any platform:

```bash
python -m py_compile files/Backup_Restore_Toolkit.py
```

## Building the portable exe

The build runs on Linux and cross-compiles a Windows x64 exe:

```bash
python3 build/build_portable.py --toolchain /usr --dist-dir dist
```

It downloads a pinned Windows embeddable Python runtime on first run (cached in
`build/.cache/`) and needs MinGW-w64 (`x86_64-w64-mingw32-gcc`, `windres`,
`strip`). The build is deterministic: identical inputs produce the same exe
hash.

Validate the workflow definition before opening a PR:

```bash
python3 build/check_workflow.py
```

## Pull requests

1. Create a branch from `main`.
2. Make the smallest change that fully addresses the issue.
3. Update [`README.md`](README.md) / [`docs/HINTS.md`](docs/HINTS.md) and add an
   entry under **Unreleased** in [`CHANGELOG.md`](CHANGELOG.md) for user-visible
   changes.
4. Bump `APP_VERSION` in `files/Backup_Restore_Toolkit.py` (and keep the build
   script in sync) only when preparing a release — don't bump it in unrelated PRs
   unless asked.
5. Run the checks you touched (`py_compile`, `build/check_workflow.py`, and/or a
   local build) and report exactly what you ran.

## Reporting bugs & requesting features

- **Bugs:** include your Windows version, what you were backing up/restoring, the
  steps to reproduce, and the relevant log lines or the `backup_summary.json` /
  exclusion report (redact personal paths if needed).
- **Features:** describe the problem you're trying to solve, not just the
  solution. Backup/restore changes should call out safety, performance and
  resume/rollback implications.

## Code style

- Match the existing style in the file you edit (this codebase favors clear,
  explicit logic over clever abstractions).
- Keep UI text short and actionable; log technical detail rather than showing it
  in dialogs.
- Prefer standard library APIs; guard Windows-specific calls so the module can
  still be imported/checked on non-Windows platforms where practical.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
