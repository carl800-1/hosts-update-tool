@echo off
chcp 65001 > nul
title GitHub Hosts 自动更新

cd /d "%~dp0"

if exist "dist\GitHubHosts.exe" (
    set EXE=dist\GitHubHosts.exe
) else if exist "GitHubHosts.exe" (
    set EXE=GitHubHosts.exe
) else (
    echo [X] 未找到 GitHubHosts.exe
    echo     请先运行 build-gui.bat 打包。
    pause
    exit /b 1
)

:: 检测管理员权限, 有则以管理员启动
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo 正在请求管理员权限...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:: 以管理员身份启动 GUI
start "" "%EXE%"
exit /b
