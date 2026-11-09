@echo off
rem Portable launcher: no Python installation needed.
cd /d "%~dp0"
set "PATH=%~dp0runtime;%~dp0runtime\Library\bin;%PATH%"
set "TCL_LIBRARY=%~dp0runtime\Library\lib\tcl8.6"
set "TK_LIBRARY=%~dp0runtime\Library\lib\tk8.6"
start "" "%~dp0runtime\pythonw.exe" "%~dp0launcher.pyw"
