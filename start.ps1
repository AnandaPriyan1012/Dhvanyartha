# Dhvanyartha launcher.
#
# Starts the backend (port 8000) and the parent dashboard (port 5500) together.
# Port 5500 is not optional: the extension only links itself to the signed-in
# parent account when it sees the dashboard on that exact port.
#
# Run from this folder:   powershell -ExecutionPolicy Bypass -File start.ps1
#
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads .ps1 files using
# the system ANSI codepage, so a UTF-8 character such as an em dash is decoded as
# cp1252 bytes - one of which is a curly quote that PowerShell treats as a string
# delimiter, breaking the parse with a confusing error far from the real line.

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
    Write-Host "  No backend\.env file yet. Scanning will not work without one." -ForegroundColor Red
    Write-Host ""
    Write-Host "  1. Get a free Gemini API key:  https://aistudio.google.com/apikey"
    Write-Host "  2. Copy backend\.env.example to backend\.env"
    Write-Host "  3. Put your key in it, then run this script again."
    Write-Host ""
    $answer = Read-Host "  Create backend\.env from the example now? (y/n)"
    if ($answer -eq "y") {
        Copy-Item (Join-Path $backend ".env.example") $envFile
        Write-Host "  Created backend\.env. Add your key to it, then re-run." -ForegroundColor Yellow
    }
    exit 1
}

# --- 4. start both servers --------------------------------------------------
# Everything below talks to 127.0.0.1, never "localhost". On Windows, localhost
# resolves to the IPv6 ::1 first, and uvicorn binds IPv4 only - so the readiness
# probe kept reporting a perfectly healthy backend as "still starting".

function Test-Port($port) {
  try {
    $c = New-Object Net.Sockets.TcpClient
    $c.Connect("127.0.0.1", $port); $c.Close(); return $true
  } catch { return $false }
}

if (Test-Port 8000) {
  Write-Host "  Backend already running on port 8000, leaving it alone."
} else {
  Write-Host "  Starting backend on http://127.0.0.1:8000 ..."
  Start-Process -FilePath $venvPy -WorkingDirectory $backend -ArgumentList "-m", "uvicorn", "main:app", "--port", "8000"
}

if (Test-Port 5500) {
  Write-Host "  Dashboard already running on port 5500, leaving it alone."
} else {
  Write-Host "  Starting dashboard on http://127.0.0.1:5500 ..."
  Start-Process -FilePath $venvPy -WorkingDirectory $frontend -ArgumentList "-m", "http.server", "5500"
}

# --- 5. confirm the backend is actually usable ------------------------------
# Importing pandas and matplotlib makes a cold start take a good 15-30 seconds on
# Windows, so allow a generous window here. Ten seconds was not enough and made a
# perfectly healthy backend look like a failure.
Write-Host "  Waiting for the backend (first start imports pandas/matplotlib, so this can take ~30s)..."
$health = $null
foreach ($attempt in 1..90) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        break
    } catch {
        if ($attempt % 5 -eq 0) { Write-Host "    still starting... ($attempt s)" }
    }
}

Write-Host ""
if ($null -eq $health) {
    Write-Host "  Backend did not come up. Check the backend window for the error." -ForegroundColor Red
    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 1
}

if ($health.gemini_configured) {
    Write-Host "  Backend ready, Gemini configured." -ForegroundColor Green
} else {
    Write-Host "  Backend is running but Gemini is NOT configured:" -ForegroundColor Red
    Write-Host "  $($health.detail)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Dashboard : http://127.0.0.1:5500" -ForegroundColor Cyan
Write-Host "  API docs  : http://127.0.0.1:8000/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Next: load the extension at chrome://extensions"
Write-Host "        (Developer mode -> Load unpacked -> the 'extension' folder)"
Write-Host "        then sign in on the dashboard to link it."
Write-Host ""
Write-Host "  Close the two new windows to stop the servers."
Write-Host ""

Start-Process "http://127.0.0.1:5500"
