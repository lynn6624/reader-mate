@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 已经在跑、而且真的能应答？
netstat -ano | findstr "LISTENING" | findstr ":8831 " >nul
if errorlevel 1 goto start
curl -s -m 3 -o nul http://127.0.0.1:8831/ >nul 2>&1
if errorlevel 1 goto start
echo 读伴已经在跑，直接开浏览器。
start "" http://127.0.0.1:8831
exit /b

:start
echo 正在启动 读伴 ...
start "" pythonw reader_server.py

set /a n=0
:wait
ping -n 2 127.0.0.1 >nul
curl -s -m 3 -o nul http://127.0.0.1:8831/ >nul 2>&1
if not errorlevel 1 goto ready
set /a n+=1
if %n% lss 15 goto wait
echo.
echo [x] 服务没能起来。把 data\server.err.log 发给开发者看看。
pause
exit /b

:ready
start "" http://127.0.0.1:8831
echo 已启动：http://127.0.0.1:8831
echo 要停就双击「stop-windows.cmd」
ping -n 5 127.0.0.1 >nul
