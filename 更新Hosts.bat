@echo off
chcp 65001 > nul
title GitHub Hosts 更新工具

:: 自动请求管理员权限
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo 正在请求管理员权限...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

if exist "dist\GitHubHostsUpdater.exe" (
    set EXE=dist\GitHubHostsUpdater.exe
) else if exist "GitHubHostsUpdater.exe" (
    set EXE=GitHubHostsUpdater.exe
) else (
    echo [X] 未找到 GitHubHostsUpdater.exe
    echo     请先运行 build.bat 打包, 或把本文件放到 exe 同目录。
    pause
    exit /b 1
)

if "%~1"=="" (
    "%EXE%" --check
    echo.
    echo ============================================================
    set /p CONFIRM=确认写入 hosts? (y/N):
    if /i "%CONFIRM%"=="y" (
        "%EXE%" --yes
    ) else (
        echo 已取消。
    )
) else (
    "%EXE%" %*
)

echo.
pause
