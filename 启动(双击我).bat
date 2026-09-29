@echo off
chcp 936 >nul
cd /d "%~dp0"
title 麦子 AI 生图

rem 优先用 pythonw（不弹黑窗）
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0app.py"
  exit /b 0
)
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3 "%~dp0app.py"
  exit /b 0
)
where python >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0app.py"
  exit /b 0
)

echo.
echo   没有检测到 Python。
echo   请任选一种方式：
echo     1) 安装 Python 3（安装时务必勾选 "Add Python to PATH"）：https://www.python.org/downloads/
echo     2) 或者运行 打包exe.bat 生成独立 exe（同样需要先装 Python，只需装一次）
echo.
pause
