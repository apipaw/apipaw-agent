@echo off
chcp 65001 >nul
rem 强制 Python 以 UTF-8 读写文件：Windows 默认 GBK 码页下，
rem 读取含中文的 requirements.txt / .py 源文件可能抛 UnicodeDecodeError。
set PYTHONUTF8=1
title APIPaw Agent - 环境初始化

cd /d "%~dp0"
echo ===================================================
echo   APIPaw Agent 正在初始化虚拟环境与安装依赖...
echo ===================================================

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 系统中未找到 Python，请先安装 Python 3.8+ 并勾选 "Add to PATH"！
    pause
    exit /b
)

if not exist ".\venv\Scripts\python.exe" (
    if not exist ".\.venv\Scripts\python.exe" (
        echo [1/2] 正在创建独立虚拟环境 ^(.venv^)...
        python -m venv .venv
    )
)

echo [2/2] 正在安装或补全 requirements.txt 中的依赖包...
if exist ".\.venv\Scripts\python.exe" (
    .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
    .\.venv\Scripts\pip.exe install -r requirements.txt
) else if exist ".\venv\Scripts\python.exe" (
    .\venv\Scripts\python.exe -m pip install --upgrade pip -q
    .\venv\Scripts\pip.exe install -r requirements.txt
)

echo ===================================================
echo   环境与依赖安装成功！
echo ===================================================
pause
