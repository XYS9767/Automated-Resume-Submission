@echo off
chcp 65001 >nul
cd /d "%~dp0"
>"stop_requested.flag" echo stop
echo 已请求停止，当前操作结束后保存进度。再次双击启动.bat 可继续。
pause
