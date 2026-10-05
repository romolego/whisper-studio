@echo off
rem Only ASCII inside this file on purpose: see the note in the installer.
setlocal

set "PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo Python environment is missing. Run the installer first.
  echo.
  pause
  exit /b 1
)

pushd "%~dp0"
"%PYTHON%" -m scripts.doctor
popd

echo.
pause
