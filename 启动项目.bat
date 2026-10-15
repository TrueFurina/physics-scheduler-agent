@echo off
chcp 65001 >nul
title Physics Scheduler - Start
echo ============================================
echo   Physics Lab Reschedule System - Starter
echo ============================================
echo.

echo [1/3] Check backend...
tasklist /FI "IMAGENAME eq uvicorn.exe" 2>nul | findstr /i "uvicorn" >nul
if %errorlevel%==0 (
    echo   [OK] Backend already running
) else (
    echo   [..] Starting backend (FastAPI :8000)...
    start "backend" /min cmd /c "cd /d E:\Program\physics-scheduler-agent\backend && D:\miniconda3_new\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
    timeout /t 4 /nobreak >nul
    echo   [OK] Backend started
)

echo.
echo [2/3] Check frontend...
netstat -ano | findstr ":5173" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo   [OK] Frontend already running
) else (
    echo   [..] Starting frontend (Vite :5173)...
    start "frontend" /min cmd /c "cd /d E:\Program\physics-scheduler-agent\frontend && D:\Node.js\npm.cmd run dev"
    timeout /t 8 /nobreak >nul
    echo   [OK] Frontend started
)

echo.
echo [3/3] Verify services...
timeout /t 2 /nobreak >nul
curl -s http://localhost:8000/health >nul 2>nul && echo   [OK] Backend  http://localhost:8000  ready || echo   [WARN] Backend not responding
curl -s http://localhost:5173 >nul 2>nul && echo   [OK] Frontend http://localhost:5173 ready || echo   [WARN] Frontend not responding

echo.
echo ============================================
echo   Done! Open in browser:
echo     http://localhost:5173
echo ============================================
echo.
pause
