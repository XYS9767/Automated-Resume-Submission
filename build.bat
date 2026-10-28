@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call setup.bat
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -r requirements-release.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" build_release.py %*
if errorlevel 1 goto fail
echo Build and offline executable checks passed. See dist.
pause
exit /b 0
:fail
echo Build failed. See the error above.
pause
exit /b 1
