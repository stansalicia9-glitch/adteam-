@echo off
chcp 65001 >nul
title FF_protocol 5006 控制台 (这个窗口别关,关了服务就停)
cd /d "%~dp0"
echo ================================================
echo   FF_protocol 团队工具 5006 启动中...
echo   网址: http://127.0.0.1:5006
echo   ★这个黑窗口别关,关了 5006 就停了
echo   崩了会自动重启,要彻底停就关窗口
echo ================================================
:loop
"%~dp0python\python.exe" app.py
echo.
echo [!] 5006 进程退出了,3秒后自动重启... (要彻底停就关这个窗口)
timeout /t 3 /nobreak >nul
goto loop
