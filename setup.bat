@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
        if errorlevel 1 goto fail
        python -m venv .venv
    )
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
exit /b 0
:fail
echo Setup failed. Please install Python 3.10 or newer, then retry.
exit /b 1
