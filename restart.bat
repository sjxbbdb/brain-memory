@echo off
cd /d %~dp0
taskkill /F /FI "WINDOWTITLE eq Brain Memory" 2>nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /F /PID %%a 2>nul
timeout /t 1 /nobreak >nul
start "Brain Memory" python -m uvicorn main:app --host 127.0.0.1 --port 8000
timeout /t 2 /nobreak >nul
start http://127.0.0.1:8000
echo Brain Memory v3.1 started on http://127.0.0.1:8000
