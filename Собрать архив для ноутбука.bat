@echo off
rem Only ASCII inside this file on purpose: see the note in the installer.
rem
rem Runs on the machine that already works and builds a copy for another one.
rem Pass --with-data to take recordings along, --no-zip to keep just a folder.
setlocal

set "PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo Python environment is missing. Run the installer first.
  echo.
  pause
  exit /b 1
)

pushd "%~dp0"
"%PYTHON%" -m scripts.pack_for_laptop %*
popd

echo.
pause
