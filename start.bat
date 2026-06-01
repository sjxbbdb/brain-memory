@echo off
chcp 65001 >nul
title Brain Memory v5.0

echo.
echo   ========================================
echo     Brain Memory v5.0 — Self-Aware Agent
echo   ========================================
echo.

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Python not found. Install Python 3.12+
    pause
    exit /b 1
)

:: Get Python version
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   Python: %PYVER%

:: Check/fix dependencies
echo   Checking dependencies...
pip install fastapi uvicorn aiohttp pydantic python-multipart 2>nul
if %errorlevel% neq 0 (
    echo   [WARN] Some packages may need manual install:
    echo     pip install fastapi uvicorn aiohttp pydantic
)

:: Kill existing brain on port 8001
echo   Checking port 8001...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8001 ^| findstr LISTENING 2^>nul') do (
    echo   Killing old process on port 8001 (PID %%a)...
    taskkill /F /PID %%a 2>nul
)

:: Start brain
echo.
echo   Starting Brain Memory v5.0...
echo   Dashboard: http://127.0.0.1:8001/dashboard
echo   API Docs:  http://127.0.0.1:8001/docs
echo   Press Ctrl+C to stop
echo   ========================================
echo.

cd /d "%~dp0"
python -m uvicorn api.main:app --host 127.0.0.1 --port 8001 --reload

pause
