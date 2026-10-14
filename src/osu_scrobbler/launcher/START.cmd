@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "SCROBBLER_DIR=%~dp0"
if exist "%~dp0app\pyproject.toml" set "SCROBBLER_DIR=%~dp0app\"
if exist "%~dp0app\runtime\pythonw.exe" set "SCROBBLER_DIR=%~dp0app\"
cd /d "%SCROBBLER_DIR%"
title osu! life hacks

if exist "%SCROBBLER_DIR%runtime\pythonw.exe" (
  start "" /b "%SCROBBLER_DIR%runtime\pythonw.exe" "%SCROBBLER_DIR%tools\start_windows.py" %*
  exit /b 0
)
if exist "%LOCALAPPDATA%\osu-scrobbler\launch.ps1" (
  start "" /b powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\osu-scrobbler\launch.ps1"
  exit /b 0
)

if exist ".venv\Scripts\python.exe" goto ready

echo Setting up...
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto create_with_launcher
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto create_with_python
echo Python 3.11 or newer is needed. Install it from:
echo https://www.python.org/downloads/windows/
echo Then double-click START.cmd again.
goto failed

:create_with_launcher
py -3 -m venv .venv
if errorlevel 1 goto failed
goto ready

:create_with_python
python -m venv .venv
if errorlevel 1 goto failed

:ready
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
  echo This folder's environment needs Python 3.11 or newer.
  echo Install Python 3.11+ and reopen START.cmd.
  goto failed
)
start "" /b ".venv\Scripts\pythonw.exe" "%SCROBBLER_DIR%tools\start_windows.py" %*
exit /b 0

:failed
echo.
echo Could not start. Check the message above and reopen START.cmd.
pause
exit /b 1
