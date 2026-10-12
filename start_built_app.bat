@echo off
cd /d "%~dp0"
set UNIFIED_HOST=0.0.0.0
if exist "dist\CallCenterUnified\CallCenterUnified.exe" ("dist\CallCenterUnified\CallCenterUnified.exe") else (echo EXE not found. Run build_windows_no_python_required.bat first.)
pause
