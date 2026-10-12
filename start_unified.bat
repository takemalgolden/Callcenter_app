@echo off
cd /d "%~dp0"
set UNIFIED_HOST=0.0.0.0
py -3 -m pip install -r shift_planner_app\requirements.txt
if errorlevel 1 (echo Python and dependencies are required on the server PC.& pause & exit /b 1)
py -3 unified.py
pause
