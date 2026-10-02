@echo off
chcp 65001 > nul
title 打包 GitHubHostsUpdater.exe

echo ============================================================
echo   打包 GitHub Hosts 更新工具为独立 exe
echo ============================================================
echo.

cd /d "%~dp0"

:: 查找可用的 python
set PY=
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\pyinstaller.exe" (
    set PYI=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\pyinstaller.exe
) else (
    where pyinstaller >nul 2>&1 && set PYI=pyinstaller
)

if not defined PYI (
    echo [X] 未找到 pyinstaller。
    echo     安装: pip install pyinstaller
    pause
    exit /b 1
)

echo 使用: %PYI%
echo 打包中, 请稍候 (首次约需 1-2 分钟)...
echo.

"%PYI%" --onefile --console --name GitHubHostsUpdater ^
    --distpath "%~dp0dist" ^
    --workpath "%TEMP%\pyi-build" ^
    --specpath "%TEMP%\pyi-spec" ^
    --noconfirm ghhosts.py

if %errorLevel% neq 0 (
    echo.
    echo [X] 打包失败。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   打包完成!
echo   产物: %~dp0dist\GitHubHostsUpdater.exe
echo ============================================================
echo.
dir /b "%~dp0dist"
echo.
pause
