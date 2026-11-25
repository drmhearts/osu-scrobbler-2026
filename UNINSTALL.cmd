@echo off
setlocal EnableExtensions DisableDelayedExpansion
title osu! life hacks uninstall
set "APP_ROOT=%~dp0"
if exist "%APP_ROOT%app\runtime\python.exe" (
  set "APP_PYTHON=%APP_ROOT%app\runtime\python.exe"
  goto run
)
if exist "%APP_ROOT%.venv\Scripts\python.exe" (
  set "APP_PYTHON=%APP_ROOT%.venv\Scripts\python.exe"
  goto run
)
echo The bundled Python is missing. Extract the full Windows package and try again.
pause
exit /b 1

:run
"%APP_PYTHON%" -m osu_scrobbler uninstall
if errorlevel 1 (
  echo Uninstall did not finish. Check the message above, then try again.
  pause
  exit /b 1
)
echo.
echo To finish, delete this extracted program folder:
echo %APP_ROOT%
echo Your osu! libraries and saved app data were kept.
pause
exit /b 0
