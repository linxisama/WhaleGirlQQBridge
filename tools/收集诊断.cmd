@echo off
chcp 65001 >nul
setlocal
echo 鲸鱼娘 QQ 控制台 · 一键诊断收集
echo 正在收集，请稍候（不会结束任何进程，也不会修改配置）...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0收集诊断.ps1"
echo.
pause