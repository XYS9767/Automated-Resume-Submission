@echo off
chcp 65001 >nul
cd /d "%~dp0"
>"stop_requested.flag" echo stop
echo 已请求停止。当前操作完成后保存进度，浏览器保持打开。
echo 再次双击启动.bat 可继续。
pause
