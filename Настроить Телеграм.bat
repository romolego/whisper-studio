@echo off
rem Only ASCII inside this file on purpose: cmd re-reads a batch file by byte
rem offsets, so a codepage switch next to non-ASCII text breaks the parsing of
rem every line after it. All Russian text comes from Python, which prints to the
rem console through the Unicode API and needs no codepage tricks.
setlocal

set "PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo Python environment is missing. Install Whisper first.
  echo.
  pause
  exit /b 1
)

"%PYTHON%" -m telegram_bot --setup

echo.
pause
