Windows Backup & Restore Toolkit @VERSION@ - Portable
=====================================================

No Python installation is required: the runtime is included in "runtime".

Start it by double-clicking "Run Toolkit.bat" (or use the single-file .exe).
Right-click -> Run as administrator for drivers, the hosts file and system settings.

Where things are stored
-----------------------
Settings:  %APPDATA%\WindowsBackupToolkit\settings.json
Backups:   the folder you pick in the app (Desktop by default)
Logs:      <backup folder>\logs\*.log

Backup modes
------------
Full        copy everything selected
Incremental complete backup; unchanged files are hardlinked from the previous one
Compact     changed files only; restore merges missing files from the base backup

Tools > Verify backup also checks a stored hash sample (integrity.json) so silent
changes that size and modification time miss are still detected.

Uninstalling: just delete this folder (and %APPDATA%\WindowsBackupToolkit if you
also want to drop your settings).
