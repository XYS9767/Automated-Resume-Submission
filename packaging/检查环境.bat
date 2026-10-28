@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
"BOSS-Auto-Apply.exe" --doctor %*
pause
