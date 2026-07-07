@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  播种登录：在弹出的浏览器里手动把管理员登进控制台一次
echo  （邮箱 -> 验证码 -> 密码 -> 选企业 Bush ^& Perez L.P.）
echo  看到产品 users 页(有 Add users 按钮)即自动保存退出
echo ============================================================
python\python.exe admin_seed_login.py
pause
