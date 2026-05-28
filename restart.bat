@echo off
:: Brain Memory v3.0 — 开源版启动脚本
:: %~dp0 自动定位到脚本所在目录，无需修改路径
cd /d "%~dp0"

:: 杀掉旧进程
taskkill /F /FI "WINDOWTITLE eq Brain Memory" 2>nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /F /PID %%a 2>nul
timeout /t 1 /nobreak >nul

:: 启动服务（需要 python 在 PATH 中）
start "Brain Memory" python -m uvicorn main:app --host 127.0.0.1 --port 8000
timeout /t 2 /nobreak >nul
start http://127.0.0.1:8000
echo Brain Memory v3.0 — http://127.0.0.1:8000