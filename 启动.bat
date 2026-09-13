@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist "stop_requested.flag" del /q "stop_requested.flag"
if not exist ".venv\Scripts\python.exe" (
    call setup.bat
    if errorlevel 1 goto end
)
".venv\Scripts\python.exe" main.py %*
:end
pause
