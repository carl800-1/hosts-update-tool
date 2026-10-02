@echo off
chcp 65001 > nul
title 打包 GitHub Hosts GUI 版本

echo ============================================================
echo   打包 GitHub Hosts 工具 (图形界面版)
echo ============================================================
echo.

cd /d "%~dp0"

:: 优先使用带 tkinter 的完整 Python
set PYI=
set PYBASE=
if exist "%LOCALAPPDATA%\Programs\Python\Python313\Scripts\pyinstaller.exe" (
    set PYBASE=%LOCALAPPDATA%\Programs\Python\Python313
) else if exist "%USERPROFILE%\py313-full\Scripts\pyinstaller.exe" (
    set PYBASE=%USERPROFILE%\py313-full
)

if defined PYBASE (
    set PYI=%PYBASE%\Scripts\pyinstaller.exe
) else (
    where pyinstaller >nul 2>&1 && set PYI=pyinstaller
)

if not defined PYI (
    echo [X] 未找到 pyinstaller。
    echo     安装: pip install pyinstaller
    pause
    exit /b 1
)

:: 检查 tkinter
"%PYBASE%\python.exe" -c "import tkinter" 2>nul
if %errorLevel% neq 0 (
    echo [!] 警告: 当前 Python 不含 tkinter, 打包出的 exe 将无法运行 GUI。
    echo     请先安装完整 Python, 或改用控制台版打包脚本 build.bat。
    echo.
    pause
)

echo 使用: %PYI%
echo.
echo [1/2] 打包 GUI 版 (无控制台窗口) ...
"%PYI%" --onefile --windowed --name GitHubHosts --distpath "%~dp0dist" ^
    --workpath "%TEMP%\pyi-build-gui" --specpath "%TEMP%\pyi-spec-gui" ^
    --noconfirm --add-data "%~dp0ghhosts.py;." "%~dp0ghhosts_gui.py"
if %errorLevel% neq 0 (
    echo [X] GUI 版打包失败。
    pause
    exit /b 1
)

echo.
echo [2/2] 打包控制台版 (保留命令行能力) ...
"%PYI%" --onefile --console --name GitHubHostsUpdater --distpath "%~dp0dist" ^
    --workpath "%TEMP%\pyi-build" --specpath "%TEMP%\pyi-spec" ^
    --noconfirm "%~dp0ghhosts.py"
if %errorLevel% neq 0 (
    echo [!] 控制台版打包失败 (GUI 版已成功)。
)

echo.
echo ============================================================
echo   打包完成
echo ============================================================
echo.
dir /b "%~dp0dist"
echo.
echo   GitHubHosts.exe         <- 双击运行, 图形界面
echo   GitHubHostsUpdater.exe  <- 命令行/计划任务用
echo.
pause
