@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

echo ==================================================
echo  QQ Bot - Start GUI directly (no packaging needed)
echo ==================================================
echo.

if not exist ".venv-qq\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv-qq
    if errorlevel 1 ( echo ERROR: venv creation failed & pause & exit /b 1 )
)

.venv-qq\Scripts\python.exe -c "import PyQt6, requests, fastapi" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies (first run takes 2-3 min)...
    .venv-qq\Scripts\python.exe -m pip install -r requirements.qq.txt --quiet --no-warn-script-location
    .venv-qq\Scripts\python.exe -m pip install PyQt6 requests --quiet --no-warn-script-location
    if errorlevel 1 ( echo ERROR: pip install failed & pause & exit /b 1 )
)

if not exist ".tmp" mkdir .tmp
set "TEMP=%~dp0.tmp"
set "TMP=%~dp0.tmp"

echo Starting QQ Bot GUI...
.venv-qq\Scripts\python.exe qq_gui.py
