@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
call setup.bat
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install "pyinstaller>=6,<7"
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --console --name BOSS-Auto-Apply --collect-all DrissionPage --hidden-import docx --hidden-import fitz --hidden-import pdfplumber --hidden-import jieba --hidden-import filelock main.py
if errorlevel 1 goto fail
copy /Y "config.yaml" "dist\config.yaml" >nul
if not exist "dist\resumes" mkdir "dist\resumes"
echo Built: dist\BOSS-Auto-Apply.exe
pause
exit /b 0
:fail
echo Build failed. See the error above.
pause
exit /b 1
