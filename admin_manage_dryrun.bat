@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [DRY-RUN] 只识别并打印拟删/拟加，不实际操作。先确认识别正确再跑 admin_manage.bat
python\python.exe admin_console_manage.py --dry-run
pause
