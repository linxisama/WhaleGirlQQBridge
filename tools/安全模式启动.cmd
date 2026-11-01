@echo off
chcp 65001 >nul
setlocal
echo 正在以「兼容模式」启动鲸鱼娘 QQ 控制台...
echo 兼容模式会关闭无边框/半透明/云母/背景图，并使用软件渲染，
echo 适合远程桌面、多个虚拟显示适配器或程序双击打不开的机器。
echo.
start "" "%~dp0WhaleGirlQQBridge.exe" --safe-mode