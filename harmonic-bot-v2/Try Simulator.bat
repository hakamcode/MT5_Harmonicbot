@echo off
cd /d "%~dp0"
python app.py --sim
if errorlevel 1 pause
