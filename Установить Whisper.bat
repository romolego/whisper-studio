@echo off
rem Only ASCII inside this file on purpose: cmd re-reads a batch file by byte
rem offsets, so a codepage switch next to non-ASCII text breaks the parsing of
rem every line after it. All Russian text comes from PowerShell and Python,
rem which print to the console through the Unicode API.
rem
rem powershell.exe, not pwsh.exe: PowerShell 7 is one of the things this script
rem installs, so it cannot be required to run it.
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*

echo.
pause
