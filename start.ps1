# Dhvanyartha launcher.
#
# Starts the backend (port 8000) and the parent dashboard (port 5500) together.
# Port 5500 is not optional: the extension only links itself to the signed-in
# parent account when it sees the dashboard on that exact port.
#
# Run from this folder:   powershell -ExecutionPolicy Bypass -File start.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venvPy = Join-Path $root "venv\Scripts\python.exe"
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"

Write-Host ""
Write-Host "  Dhvanyartha" -ForegroundColor Cyan
Write-Host "  ===========" -ForegroundColor Cyan
Write-Host ""

# --- 1. virtual environment -------------------------------------------------
if (-not (Test-Path $venvPy)) {
    Write-Host "  Creating virtual environment..." -ForegroundColor Yellow
    python -m venv (Join-Path $root "venv")
    if (-not (Test-Path $venvPy)) {
        Write-Host "  Could not create the virtual environment. Is Python installed?" -ForegroundColor Red
        exit 1
    }
}

# --- 2. dependencies --------------------------------------------------------
Write-Host "  Checking dependencies..."
& $venvPy -m pip install -q -r (Join-Path $backend "requirements.txt")

# --- 3. credentials ---------------------------------------------------------
$envFile = Join-Path $backend ".env"
if (-not (Test-Path $envFile)) {
    Write-Host ""
    Write-Host "  No backend\.env file yet — scanning will not work without one." -ForegroundColor Red
    Write-Host ""
    Write-Host "  1. Get a free Gemini API key:  https://aistudio.google.com/apikey"
    Write-Host "  2. Copy backend\.env.example to backend\.env"
    Write-Host "  3. Put your key in it, then run this script again."
    Write-Host ""
    $answer = Read-Host "  Create backend\.env from the example now? (y/n)"
    if ($answer -eq "y") {
        Copy-Item (Join-Path $backend ".env.example") $envFile
        Write-Host "  Created backend\.env — add your key to it, then re-run." -ForegroundColor Yellow
    }
    exit 1
}

# --- 4. start both servers --------------------------------------------------
Write-Host "  Starting backend on http://localhost:8000 ..."
Start-Process -FilePath $venvPy `
    -ArgumentList "-m", "uvicorn", "main:app", "--port", "8000" `
    -WorkingDirectory $backend

Write-Host "  Starting dashboard on http://localhost:5500 ..."
Start-Process -FilePath $venvPy `
    -ArgumentList "-m", "http.server", "5500" `
    -WorkingDirectory $frontend

# --- 5. confirm the backend is actually usable ------------------------------
Write-Host "  Waiting for the backend..."
$ready = $false
foreach ($attempt in 1..20) {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 2
        $ready = $true
        break
    } catch {
        # not up yet, keep waiting
    }
}

Write-Host ""
if (-not $ready) {
    Write-Host "  Backend did not come up. Check the backend window for the error." -ForegroundColor Red
    exit 1
}

if ($health.gemini_configured) {
    Write-Host "  Backend ready, Gemini configured." -ForegroundColor Green
} else {
    Write-Host "  Backend is running but Gemini is NOT configured:" -ForegroundColor Red
    Write-Host "  $($health.detail)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Dashboard : http://localhost:5500" -ForegroundColor Cyan
Write-Host "  API docs  : http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Load the extension at chrome://extensions (Developer mode -> Load unpacked -> extension folder)."
Write-Host "  Sign in on the dashboard to link the extension to your account."
Write-Host ""
Write-Host "  Close the two new windows to stop the servers."
Write-Host ""

Start-Process "http://localhost:5500"
