"""Entry point used by the portable build.

Runs with the bundled pythonw.exe so no console window appears. It points Tcl/Tk
at the runtime that ships next to this file, then runs the toolkit as __main__.
"""
import os
import runpy
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
runtime = here / "runtime"

os.environ.setdefault("TCL_LIBRARY", str(runtime / "Library" / "lib" / "tcl8.6"))
os.environ.setdefault("TK_LIBRARY", str(runtime / "Library" / "lib" / "tk8.6"))
os.environ["PATH"] = os.pathsep.join(
    [str(runtime), str(runtime / "Library" / "bin"), os.environ.get("PATH", "")]
)

try:
    os.chdir(here)
except OSError:
    pass

sys.argv = [str(here / "Backup_Restore_Toolkit.py")]
runpy.run_path(str(here / "Backup_Restore_Toolkit.py"), run_name="__main__")
