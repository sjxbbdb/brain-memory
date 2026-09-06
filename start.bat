@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Brain Memory v0.1

set "ROOT=%~dp0"
cd /d "%ROOT%"

echo.
echo   ========================================
echo     Brain Memory v0.1 - Self-Aware Agent
echo   ========================================
echo.

:: Check the system Python only as the venv bootstrap runtime.
where python >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python not found. Install Python 3.12+.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "SYSTEM_PYVER=%%v"
echo   Bootstrap Python: %SYSTEM_PYVER%

:: Keep all project packages inside the repository-local virtual environment.
if not exist ".venv\Scripts\python.exe" (
    echo   Creating project virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo   [ERROR] Could not create .venv.
        pause
        exit /b 1
    )
)
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "LOCKFILE=%ROOT%requirements.lock"
if not exist "%LOCKFILE%" set "LOCKFILE=%ROOT%requirements.txt"

echo   Checking project dependencies in .venv...
"%PYTHON%" -m pip install --disable-pip-version-check --no-input -r "%LOCKFILE%"
if errorlevel 1 (
    echo   [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

"%PYTHON%" -c "import fastapi, uvicorn, aiohttp, pydantic"
if errorlevel 1 (
    echo   [ERROR] Runtime dependency check failed.
    pause
    exit /b 1
)

:: Never terminate an unrelated process that already owns the API port.
netstat -ano | findstr ":8001" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo   [ERROR] Port 8001 is already in use. Stop the owning process or choose another port.
    pause
    exit /b 1
)

echo.
echo   Starting Brain Memory on http://127.0.0.1:8001
echo   Dashboard: http://127.0.0.1:8001/dashboard
echo   API Docs:  http://127.0.0.1:8001/docs
echo   Press Ctrl+C to stop
echo   ========================================
echo.

"%PYTHON%" -m uvicorn api.main:app --host 127.0.0.1 --port 8001
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo   Brain Memory stopped (exit code %EXIT_CODE%).
pause
exit /b %EXIT_CODE%
