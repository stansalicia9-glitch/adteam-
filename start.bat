@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Adobe Firefly Team Console
echo   Open browser: http://127.0.0.1:5005
echo   (Ctrl+C to stop)
echo ============================================
start "" http://127.0.0.1:5005
python\python.exe app.py
echo.
echo [server stopped]
pause
