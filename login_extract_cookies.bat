@echo off
cd /d "%~dp0"
python firefly_login_extract_cookies.py --accounts missing_cookie_accounts.txt --workers 1
pause
