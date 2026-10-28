@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
call run.bat --doctor %*
pause
