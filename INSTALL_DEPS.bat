@echo off
setlocal
cd /d "%~dp0"

echo [INFO] Installing local Python dependencies for this folder.
echo [INFO] This step needs internet access and may take several minutes.
echo.

set "BASEPY="
where py >nul 2>nul
if not errorlevel 1 set "BASEPY=py -3"
if not defined BASEPY (
  where python >nul 2>nul
  if not errorlevel 1 set "BASEPY=python"
)

if not defined BASEPY (
  echo [ERROR] Python was not found.
  echo Install Python 3.10 or newer first, and enable "Add Python to PATH".
  pause
  exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [INFO] Creating local virtual environment: .venv
  %BASEPY% -m venv "%~dp0.venv"
  if errorlevel 1 (
    echo [ERROR] Failed to create .venv.
    pause
    exit /b 1
  )
)

set "PY=%~dp0.venv\Scripts\python.exe"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0ms-playwright"

echo [INFO] Upgrading pip...
"%PY%" -m pip install --upgrade pip
if errorlevel 1 goto failed

echo [INFO] Installing Python packages from requirements.txt...
"%PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto failed

echo [INFO] Installing Playwright Chromium into this folder...
"%PY%" -m playwright install chromium
if errorlevel 1 goto failed

echo.
echo [OK] Dependencies are ready.
echo Run START_PANEL.bat to open the web panel.
if not defined AUTO_START_AFTER_INSTALL pause
exit /b 0

:failed
echo.
echo [ERROR] Installation failed. Check the network connection and retry.
pause
exit /b 1
