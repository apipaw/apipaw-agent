@echo off
chcp 65001 >nul
title APIPaw Agent - DePIN 算力节点

cd /d "%~dp0"

echo ===================================================
echo   APIPaw Agent 算力节点启动中...
echo   工作目录: %CD%
echo ===================================================

if exist ".\venv\Scripts\python.exe" (
    set "PYTHON_EXE=.\venv\Scripts\python.exe"
) else if exist ".\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=.\.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

%PYTHON_EXE% main.py

if %errorlevel% neq 0 (
    echo [错误] 程序退出异常。
)
pause
