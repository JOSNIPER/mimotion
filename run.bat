@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   mimotion - Zepp Life 刷步数
echo ================================
echo.
venv\Scripts\python.exe main.py
echo.
echo ================================
echo   执行完毕，按任意键关闭窗口
echo ================================
pause >nul
