@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".tmp" mkdir .tmp
set "TEMP=%~dp0.tmp"
set "TMP=%~dp0.tmp"
if not exist ".venv-qq\Scripts\python.exe" (
    python -m venv .venv-qq
    if errorlevel 1 goto failed
)
".venv-qq\Scripts\python.exe" -c "import fastapi,uvicorn,httpx,yaml,cryptography,loguru,websockets" >nul 2>&1
if errorlevel 1 (
    python -m pip --python .venv-qq\Scripts\python.exe install -r requirements.qq.txt
    if errorlevel 1 goto failed
)
echo 注意：网页版功能比桌面版少，功能完整的桌面控制台请双击 start.bat。
echo QQ console: http://127.0.0.1:8090
".venv-qq\Scripts\python.exe" qq_main.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo 启动失败。请确认已安装 Python 3，并检查 requirements.qq.txt 后重试。
pause
exit /b 1
