# Hints for everyday use

Short, practical notes. Read the [README](../README.md) for the full picture.

## First run

1. Right-click the exe → **Run as administrator** the first time, so drivers, `hosts`,
   registry and Wi-Fi profiles are included. Later runs can be normal.
2. On the **Backup** page pick a destination parent folder — an external drive is ideal.
3. Leave all stages ticked and press **Start** for a full backup. It's the one you'll
   want on a bad day.
4. When it finishes, open **Tools → Verify** once to confirm the backup is healthy.

## Choosing a mode

- **Full** — archiving, external drives, or "I reinstall Windows twice a year".
- **Incremental** — regular backups kept on the *same NTFS drive* as the last one.
  Each snapshot looks complete; unchanged files cost nothing.
- **Compact** — most frequent, smallest. Only changed files are stored, and a restore
  pulls the missing pieces from the base backup.

Rule of thumb: if the previous backup lives on another drive, use Full or Compact.
Hardlinks can't cross drives, and the app will warn you before starting.

## Don't delete the base backup

Compact and incremental backups depend on their parent. If you clean up old backups, keep
the newest full/base one. The app shows the chain when you select a folder to restore.

## Retire what you don't need

Big folders that rarely belong in a profile backup:

```
node_modules  .git  obj  bin  build  dist  venv  .venv
Videos\Captures  Downloads  SteamLibrary  *.iso  *.vmdk  *.mp4
```

Put them under **Settings → Custom skip rules** — folders, files and extensions each
have their own list. Build and cache folders (`node_modules`, `__pycache__`, `Temp`,
browser caches) are already skipped automatically.

## Before a reinstall

1. Run a **full** backup as administrator.
2. **Verify** it.
3. Copy the backup folder to a different physical disk — a backup on the drive you're
   about to format is not a backup.
4. On the fresh system, install nothing but this app, then restore **Drivers** first and
   reboot if Windows asks.

## Restoring without fear

- Use **Preview** — it lists new / overwrite / identical counts per stage.
- Restore into a **scratch folder** first if you're unsure. You can copy the files
  yourself afterwards.
- Close browsers, Outlook and OneDrive before restoring their data; locked files are the
  one common failure.
- Restoring `Settings` can change system behaviour. Do it last, and reboot afterwards.

## Speed

- More than one thread helps on SSDs: set `BKUP_THREADS=8` before launching.
- Antivirus scanning each copied file is usually the real bottleneck — exclude the
  destination folder from real-time scanning if you trust it.
- Cancelling is safe: the partial backup can be resumed, not restarted.

## Housekeeping

- Keep the last 3–5 backups with **retention** rather than deleting by hand; it previews
  before removing anything and refuses to delete the only full backup.
- Backups are plain folders — a `.zip` next to them is enough for archiving.
- Nothing is encrypted. Use BitLocker on the backup drive if the data matters.

## Reading the log

`<backup folder>\logs\<date>.log` records every stage, warning and failure. When
something looks wrong, that file explains what was skipped and why — usually
"access denied" (needs admin) or "file in use" (close the app).
