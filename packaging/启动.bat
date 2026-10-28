@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist "stop_requested.flag" del /q "stop_requested.flag"
"BOSS-Auto-Apply.exe" %*
pause
