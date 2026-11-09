# Changelog

Notable changes, newest first. The canonical version is `VERSION` in
`files/Backup_Restore_Toolkit.py`; the portable exe's file name and Windows version
resource follow it automatically.

Earlier history predates this repository, so the 3.0.0 entry summarises what the v3
baseline already provided rather than recording every commit.

## 3.1.0 — 9 November 2026

Cheaper repeat backups, verification you can run on demand, and a real portable build.

### Added — backup modes

- **Compact (changed-only) backups.** Only new or modified files are written, so a run
  costs a fraction of a full backup. Restores merge the parent backup automatically —
  you pick the compact folder and the missing files come from its base.
- **Incremental hardlink mode.** Unchanged files are hardlinked from the previous
  backup, so each snapshot looks complete but only changed data uses disk space.
- **Hardlink preflight warning.** Before an incremental run the destination is probed;
  if hardlinks cannot work (different volume, non-NTFS filesystem, permissions) the app
  says so up front, with the reason and the alternatives, instead of failing partway.

### Added — trust and safety

- **Integrity hash sampling.** `integrity.json` stores SHA-256 samples of the backup so
  silent corruption — which size and modification time cannot catch — is detectable.
- **Verify backup** tool (Tools menu), checking manifest, stage folders and optionally
  the hash sample, with a readable per-check report.
- **Resume interrupted backup.** A lightweight in-progress manifest is written as the
  backup runs, so a cancelled or crashed run continues where it stopped instead of
  starting over.
- **Retention policy.** Keep the last *N* backups and/or remove backups older than *M*
  days, with a preview before anything is deleted, an optional automatic run after each
  backup, and a guard that never removes the only good full backup.

### Added — control and visibility

- **Custom skip rules** for folder names, file names and extensions, alongside the
  built-in ignore list of build, cache and temporary folders.
- **Dashboard "Data written / reused"** card, showing bytes actually copied versus
  bytes reused, so the savings from incremental and compact runs are visible.

### Added — packaging

- Custom application icon (16–256 px) used by the exe, Explorer and the app window.
- Windows VERSIONINFO resource: product name, version 3.1.0, file description and
  original filename, so Properties and task manager show something meaningful.
- Single-file portable exe that carries its own Python runtime and starts with no
  console window, plus an equivalent zip for anyone who prefers unpacking.
- Reproducible cross-build (`build/build_portable.py`) and a GitHub Actions workflow
  that publishes a release whenever `VERSION` changes.

### Fixed

- Opening the integrity check crashed: the dialog handler was referenced but never
  defined.

## 3.0.0 — earlier

The v3 baseline this version builds on: parallel backup stages, deferred blob restore,
resume tokens, decorated progress and detail logging, cancellation safety, ZIP/7-Zip
archiving, cross-version manifest plumbing, structured state, and a pluggable logger.

## Repository

Project scaffolding added around 3.1.0 rather than in the app itself: README, hints for
users, contributor guide, MIT license, build documentation and the release pipeline.
