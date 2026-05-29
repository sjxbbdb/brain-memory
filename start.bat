@echo off
chcp 65001 >nul
title Brain Memory

echo.
echo   Brain Memory v4.1
echo   ==================
echo.
echo   Copy .env.example to .env and add your API keys first!
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Python 3.12+ required
    pause
    exit /b 1
)

echo   Installing dependencies...
pip install fastapi uvicorn aiohttp pydantic 2>nul

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8001 ^| findstr LISTENING 2^>nul') do (
    taskkill /F /PID %%a 2>nul
)

echo   Starting...
cd /d "%~dp0"
python -m uvicorn api.main:app --host 127.0.0.1 --port 8001

pause
