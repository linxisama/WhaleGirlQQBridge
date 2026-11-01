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
".venv-qq\Scripts\python.exe" -c "import fastapi,uvicorn,httpx,yaml,cryptography,loguru,websockets,PyQt6" >nul 2>&1
if errorlevel 1 (
    python -m pip --python .venv-qq\Scripts\python.exe install -r requirements.qq.txt
    if errorlevel 1 goto failed
)
echo 正在启动鲸鱼娘桌面控制台……
echo 启动后会自动打开桌面控制台窗口（桌面版功能完整）。
echo 控制台端口默认 27499，可用 --port 参数、环境变量 WHALEGIRL_PORT 或 config.qq.yaml 的 qq.console_port 修改。
echo 想用浏览器打开网页版（功能比桌面版少），请双击 start-web.bat。
".venv-qq\Scripts\python.exe" gui_launcher.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo 启动失败。请确认已安装 Python 3，并检查 requirements.qq.txt 后重试。
pause
exit /b 1
