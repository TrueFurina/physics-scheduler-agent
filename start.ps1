# Physics Lab Reschedule System - One-click starter (PowerShell)
# Usage: powershell -ExecutionPolicy Bypass -File start.ps1
# Note: Keep this file ASCII-only to avoid encoding issues in Windows PowerShell 5.1

$ErrorActionPreference = "SilentlyContinue"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Physics Lab Reschedule System - Starter" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# 0. Resolve project root and toolchain (avoid hard-coded paths that go stale)
$RepoRoot = $PSScriptRoot
if (-not $RepoRoot) { $RepoRoot = "E:\Program\physics-scheduler-agent" }

# numpy/OpenBLAS 在机器紧时会导入失败 —— 跑 Python 一律单线程（实测教训，见项目记忆）
$env:OPENBLAS_NUM_THREADS = "1"

# Demo mode: below, the launcher injects DEMO_MODE=1 + the demo password so
# admin/admin123 stays consistent with the talk script. security.py only
# accepts the default demo password when DEMO_MODE=1; production
# (APP_ENV=production) refuses to start without a strong ADMIN_PASS (>= 16 chars).
# Set ADMIN_PASS yourself if you need a stable non-demo value.

$PythonExe = $env:PYTHON_EXE
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    $pyCandidates = @(
        "D:\miniconda3_new\python.exe",
        "C:\Users\Lenovo\miniconda3\python.exe",
        "C:\Users\Lenovo\anaconda3\python.exe"
    )
    $PythonExe = $pyCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $PythonExe) {
    $pyCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pyCmd) { $PythonExe = $pyCmd.Source }
}
if ($PythonExe) {
    Write-Host "[OK] Python: $PythonExe" -ForegroundColor Green
} else {
    Write-Host "[ERR] Python not found. Set PYTHON_EXE env var or install Miniconda." -ForegroundColor Red
}

$NpmCmd = $env:NPM_CMD
if (-not $NpmCmd -or -not (Test-Path $NpmCmd)) {
    $npmCandidates = @(
        "D:\Node.js\npm.cmd",
        "C:\Program Files\nodejs\npm.cmd"
    )
    $NpmCmd = $npmCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $NpmCmd) {
    $npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($npmCmd) { $NpmCmd = $npmCmd.Source }
}
if ($NpmCmd) {
    Write-Host "[OK] npm: $NpmCmd" -ForegroundColor Green
} else {
    Write-Host "[ERR] npm not found. Set NPM_CMD env var or install Node.js." -ForegroundColor Red
}

# 1. Check backend
$backendRunning = Get-NetTCPConnection -LocalPort 9000 -State Listen -ErrorAction SilentlyContinue
if ($backendRunning) {
    Write-Host "[OK] Backend already running (9000)" -ForegroundColor Green
} else {
    Write-Host "[..] Starting backend (FastAPI :9000)..." -ForegroundColor Yellow
    # Demo credentials: env vars take precedence over .env (load_dotenv override=False),
    # keeping admin/admin123 working for local demo (pitch + prepare_demo convention).
    $env:DEMO_MODE = "1"
    $env:ADMIN_PASS = "admin123"
    if ($PythonExe) {
        Start-Process -FilePath $PythonExe `
            -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000" `
            -WorkingDirectory "$RepoRoot\backend" -WindowStyle Hidden
        Start-Sleep -Seconds 5
        Write-Host "[OK] Backend started" -ForegroundColor Green
    } else {
        Write-Host "[ERR] Backend not started: Python interpreter missing" -ForegroundColor Red
    }
}

# 2. Check frontend
$frontendRunning = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
if ($frontendRunning) {
    Write-Host "[OK] Frontend already running (5173)" -ForegroundColor Green
} else {
    Write-Host "[..] Starting frontend (Vite :5173)..." -ForegroundColor Yellow
    if ($NpmCmd) {
        Start-Process -FilePath $NpmCmd `
            -ArgumentList "run", "dev" `
            -WorkingDirectory "$RepoRoot\frontend" -WindowStyle Hidden
        Start-Sleep -Seconds 8
        Write-Host "[OK] Frontend started" -ForegroundColor Green
    } else {
        Write-Host "[ERR] Frontend not started: npm missing" -ForegroundColor Red
    }
}

# 3. Verify
Start-Sleep -Seconds 2
try {
    $backendOk = Invoke-WebRequest -Uri "http://localhost:9000/health" -TimeoutSec 3 -UseBasicParsing
    if ($backendOk.StatusCode -eq 200) { Write-Host "[OK] Backend http://localhost:9000 ready" -ForegroundColor Green }
} catch { Write-Host "[WARN] Backend not responding" -ForegroundColor Red }

try {
    $frontendOk = Invoke-WebRequest -Uri "http://localhost:5173" -TimeoutSec 3 -UseBasicParsing
    if ($frontendOk.StatusCode -eq 200) { Write-Host "[OK] Frontend http://localhost:5173 ready" -ForegroundColor Green }
} catch { Write-Host "[WARN] Frontend not responding" -ForegroundColor Red }

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Done! Open in browser: http://localhost:5173" -ForegroundColor White
Write-Host "============================================" -ForegroundColor Cyan
