# Changelog

All notable user-facing changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
toolkit uses semantic-style versioning.

## [3.1.0] - 2026-11-09

### Added

- **Integrity sampling mode** with a configurable file percentage (1–100%) to
  verify a representative subset during backup and restore.
- **Resume** for interrupted operations via per-output folder markers
  (`.backup-in-progress.json`, `.restore-in-progress.json`), so completed files
  and the current index are reused on the next run.
- **Skip rules** (glob patterns, default `*.tmp;~$*;Thumbs.db`) and an
  **exclusion history** panel with reasons and the last 20 skipped entries.
- **Retention policy** (keep last N / keep days) that cleans up old backup
  folders after a successful run.
- **Incremental-link backups**: unchanged files (same relative path and size,
  with sampled hash confirmation) reuse bytes from the previous backup via
  NTFS hard links; a dashboard counter shows how much data was reused.
- **Compact mode** to reduce RAM/CPU usage on slower machines, with safe
  adaptive chunk sizing and cooperative yields during scans.
- **Real-time dashboard** (progress, ETA, files copied, bytes written, current
  file) and detailed logs for backup/restore and validation steps.
- **Polished portable Windows build**: a native launcher with a custom icon and
  embedded version information, a one-command reproducible build script, and
  build provenance embedded in the payload.
- **Automated release workflow**: GitHub Actions builds the portable exe/zip and
  publishes a release with checksums for the checked-in version.

### Changed

- The bundled toolkit is now treated as a self-contained portable app: no
  external Python install is required to run the exe.
- Driver export/report and installed-programs export are written as structured,
  bounded artifacts alongside the backup for easier review.
- Wi-Fi profile export stores **names only** by default; password export is an
  explicit opt-in.

### Fixed

- Deterministic portable builds: identical inputs now produce the same exe hash
  across runs (timestamp-normalized runtime files and PE link stamp).
- Safer backup hygiene: lock/temp files, junction targets, and explicitly
  excluded patterns are recorded rather than copied blindly.
- More resilient backup/restore flows: per-file failures are logged without
  abandoning the whole run, and preflight checks catch common destination and
  permission problems early.

### Notes

- Targets **Windows 10/11 (64-bit)**. A Windows SmartScreen prompt is expected
  for unsigned builds; verify the published checksums before running.
- `files/Backup_Restore_Toolkit.py` is the source of truth for the portable
  payload.

## [3.0.0] - 2025-??-??

### Added

- Single-file tkinter backup & restore toolkit with full backups, selectable
  categories, driver export, Wi-Fi profile name export, environment report,
  restore from a backup folder, and a configurable `settings.json` next to the
  app.

[3.1.0]: https://github.com/mohammadhossein-asadi/windows-backup-toolkit/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/mohammadhossein-asadi/windows-backup-toolkit/releases/tag/v3.0.0
