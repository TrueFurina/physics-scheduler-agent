@echo off
chcp 65001 >nul 2>&1
title Physics Lab Reschedule System - Starter
echo ============================================
echo   Physics Lab Reschedule System
echo   One-click starter (backend + frontend)
echo ============================================
echo.

REM ============================================
REM  Unified launcher: tries schtasks first,
REM  falls back to PowerShell Start-Process.
REM  All logic in start.ps1 (ASCII-safe).
REM ============================================

echo [1/2] Checking services...
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\Program\physics-scheduler-agent\start.ps1"

echo.
echo [2/2] Verifying web access...
timeout /t 2 /nobreak >nul
curl -s http://localhost:9000/health >nul 2>nul && echo   [OK] Backend  http://localhost:9000  ready || echo   [WARN] Backend not responding
curl -s http://localhost:5173 >nul 2>nul && echo   [OK] Frontend http://localhost:5173 ready || echo   [WARN] Frontend not responding

echo.
echo ============================================
echo   Done! Open in browser:
echo     http://localhost:5173
echo ============================================
echo.
pause
