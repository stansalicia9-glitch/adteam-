@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  Adobe Firefly 团队批量 Web 控制台
echo  启动后用浏览器打开： http://127.0.0.1:5005
echo  关闭：直接关掉这个黑窗口
echo ============================================================
start "" http://127.0.0.1:5005
python\python.exe app.py
pause
