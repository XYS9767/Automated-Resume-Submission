@echo off
setlocal
cd /d "%~dp0"
set "CHECK=import sys; assert sys.version_info >= (3, 10); import DrissionPage, yaml, docx, fitz, pdfplumber, requests, jieba, filelock; from importlib.metadata import version; assert version('DrissionPage') == '4.1.1.4'"
if not exist ".venv\Scripts\python.exe" goto check_py
".venv\Scripts\python.exe" -c "%CHECK%" >nul 2>&1
if not errorlevel 1 goto run_venv
:check_py
py -3 -c "%CHECK%" >nul 2>&1
if not errorlevel 1 goto run_py
python -c "%CHECK%" >nul 2>&1
if not errorlevel 1 goto run_python
call setup.bat
if errorlevel 1 exit /b 1
:run_venv
".venv\Scripts\python.exe" main.py %*
exit /b %errorlevel%
:run_py
py -3 main.py %*
exit /b %errorlevel%
:run_python
python main.py %*
exit /b %errorlevel%
