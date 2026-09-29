@echo off
chcp 936 >nul
title 清除 共济生图 本地数据
set TARGET=%APPDATA%\maizi

echo ============================================================
echo   共济生图 · 清除本地数据
echo ============================================================
echo.
echo 将要删除的目录：
echo   %TARGET%
echo.
echo 里面有这些东西：
echo   config.json       API 密钥 + 模型选择
echo   history.json      生成记录
echo   history\          生成图片的本地缓存
echo   uploads.json      参考图上传缓存
echo   model_cache.json  模型规格缓存
echo   auth_cache.json   密钥校验记录
echo   app.log           运行日志
echo.

if not exist "%TARGET%" (
  echo 没有找到该目录 —— 说明本机还没运行过，或者已经清过了。
  echo.
  pause
  exit /b 0
)

set /p CONFIRM=确认全部删除？输入 Y 再回车，其它键取消：
if /i not "%CONFIRM%"=="Y" (
  echo.
  echo 已取消，什么都没删。
  pause
  exit /b 0
)

rd /s /q "%TARGET%"

rem 顺手清掉单文件版可能残留的临时解压目录
for /d %%d in ("%TEMP%\_MEI*") do rd /s /q "%%d" 2>nul

echo.
echo 已删除。
echo 下次打开 共济生图 会要求重新填写 API 密钥，默认模型为标准版。
echo.
pause
