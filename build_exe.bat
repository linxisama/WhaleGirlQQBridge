@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

echo ==================================================
echo  QQ Bot - Build EXE
echo ==================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ first.
    pause
    exit /b 1
)
python --version

if not exist ".venv-build\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv-build
    if errorlevel 1 ( echo ERROR: venv creation failed & pause & exit /b 1 )
)

echo Installing dependencies (first run takes 3-5 min)...
.venv-build\Scripts\python.exe -m pip install --upgrade pip --quiet --no-warn-script-location
.venv-build\Scripts\python.exe -m pip install -r requirements.qq.txt --quiet --no-warn-script-location
.venv-build\Scripts\python.exe -m pip install PyQt6 requests pyinstaller --quiet --no-warn-script-location
if errorlevel 1 ( echo ERROR: pip install failed & pause & exit /b 1 )

echo.
echo Building EXE (takes 2-5 min)...
.venv-build\Scripts\python.exe build_exe.py
if errorlevel 1 ( echo ERROR: build failed & pause & exit /b 1 )

echo.
echo ==================================================
echo  SUCCESS! Run: dist\qq_bot.exe
echo ==================================================
echo.
pause
