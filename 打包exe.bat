@echo off
chcp 936 >nul
cd /d "%~dp0"
title 打包 麦子生图.exe

where python >nul 2>nul
if not %errorlevel%==0 (
  echo 没有检测到 Python，请先安装 Python 3（勾选 Add Python to PATH）后再运行本脚本。
  pause & exit /b 1
)

echo [1/3] 安装打包工具 PyInstaller …
python -m pip install --upgrade pip pyinstaller || (echo 安装失败，检查网络后重试 & pause & exit /b 1)

echo.
echo [2/3] 正在打包（约 1-2 分钟，请勿关闭窗口）…
python -m PyInstaller --onefile --noconsole --clean --name 麦子生图 --add-data "index.html;." app.py || (echo 打包失败 & pause & exit /b 1)

echo.
echo [3/3] 完成！
echo   exe 位置： %~dp0dist\麦子生图.exe
echo   把这个 exe 拷到任何地方双击即可使用（首次会让你填 API 密钥）。
echo.
pause
