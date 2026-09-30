@echo off
chcp 65001 >nul
set found=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":8831 .*LISTENING"') do (
  echo 停止进程 %%a
  taskkill /F /PID %%a >nul 2>&1
  set found=1
)
if "%found%"=="1" (echo 读伴已停止) else (echo 读伴本来就没在跑)
ping -n 4 127.0.0.1 >nul
