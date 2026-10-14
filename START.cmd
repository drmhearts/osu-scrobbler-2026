@echo off
setlocal
cd /d "%~dp0"
title osu! scrobbler - setup

if exist ".venv\Scripts\python.exe" goto ready

echo Preparing osu! scrobbler...
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
  echo Install a supported Python and extract the app into a fresh folder.
  goto failed
)
".venv\Scripts\python.exe" "%~dp0tools\start_windows.py" %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo Setup stopped. Fix the error above, then double-click START.cmd again.
pause
exit /b 1
