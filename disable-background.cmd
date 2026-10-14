@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Use the project folder containing .venv.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m osu_scrobbler tray --uninstall
pause
