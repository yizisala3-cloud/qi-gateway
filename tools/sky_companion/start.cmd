@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -X utf8 tools\sky_companion\bridge.py run
if errorlevel 1 pause
