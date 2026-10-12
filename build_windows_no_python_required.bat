@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "LOGDIR=%~dp0build_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
set "LOG=%LOGDIR%\build_latest.log"
>"%LOG%" echo Build launcher started: %DATE% %TIME%
>>"%LOG%" echo Current directory: %CD%
>>"%LOG%" echo Windows version:
ver >>"%LOG%" 2>&1
>>"%LOG%" echo Starting PowerShell...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows_no_python_required.ps1" >>"%LOG%" 2>&1
set "RESULT=%ERRORLEVEL%"
>>"%LOG%" echo PowerShell exit code: %RESULT%
echo.
if not "%RESULT%"=="0" (
  echo BUILD FAILED. Error code: %RESULT%
  echo Log: "%LOG%"
  echo.
  echo --- Last 60 lines of log ---
  powershell.exe -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 60" 2>nul
  echo.
  echo If the log is missing, please take a screenshot of this window.
  pause
  exit /b %RESULT%
)
echo Build script finished. Check dist\CallCenterUnified\CallCenterUnified.exe
echo Log: "%LOG%"
pause
