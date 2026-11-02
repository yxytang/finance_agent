@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 没有找到项目的 Python 环境，请先按 README 安装依赖。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 "scripts\open_local.py"
if errorlevel 1 (
    pause
    exit /b 1
)
