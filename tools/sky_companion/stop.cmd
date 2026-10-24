@echo off
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -X utf8 tools\sky_companion\bridge.py stop
