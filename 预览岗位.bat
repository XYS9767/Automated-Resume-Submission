@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call setup.bat
    if errorlevel 1 goto end
)
".venv\Scripts\python.exe" main.py --search-only
:end
pause
