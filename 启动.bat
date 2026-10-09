@echo off
rem ============================================================
rem  BiliBili video downloader - launcher
rem  Double-click this file to start. Uses pythonw so no console
rem  window stays open. Content is ASCII on purpose: a .bat with
rem  Chinese text gets mangled by the OEM codepage.
rem ============================================================
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0bili_dl_gui.py"
    exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
    echo pythonw not found - starting with python instead.
    python "%~dp0bili_dl_gui.py"
    pause
    exit /b 0
)

echo Python not found on PATH. Install Python 3.10+ and try again.
pause
