@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 前半部分：登录控制台 -> 删非管理员 -> 添加 registered_accounts.txt 小号
echo 如需添加后直接导 cookie，把下面命令加上 --then-extract
python\python.exe admin_console_manage.py --reset-added
pause
