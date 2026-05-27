@echo off
title Brain Memory System
cd /d E:\brain-memory

echo ========================================
echo   Brain Memory System
echo ========================================
echo.
echo   Dashboard:  http://127.0.0.1:8765
echo   API Docs:   http://127.0.0.1:8765/docs
echo.
echo   Press Ctrl+C to stop.
echo ========================================
echo.

C:\Users\24763\AppData\Local\Programs\Python\Python312\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8765

pause
