# Starts the local demo (MT5 simulator + demo data) and opens the browser. Run .\setup.ps1 once first.
#   .\run-demo.ps1             start
#   .\run-demo.ps1 -Reset      delete the demo data (database, uploads, simulator state) and start over
param([switch]$Reset, [int]$Port = 8000)
$ErrorActionPreference = "Continue"  # native commands are checked through $LASTEXITCODE
Set-Location $PSScriptRoot
if (-not (Test-Path .\.venv\Scripts\python.exe) -or -not (Test-Path .\.env)) { throw "Run .\setup.ps1 first." }
$settings = @{}
Get-Content .env | ForEach-Object { if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*(#.*)?$') { $settings[$Matches[1]] = $Matches[2] } }
if ($settings.APP_ENV -in "prod", "production") { throw "This .env is for production (APP_ENV=prod); the demo only runs with APP_ENV=dev." }
$docker = [bool]$settings.POSTGRES_PASSWORD  # setup.ps1 put the database in Docker

if ($Reset) {
    if ($docker) {
        docker compose down -v
    } else {
        # The bundled PostgreSQL keeps its data in pgdata\: stop it if it is still running.
        $pgCtl = ".\.venv\Lib\site-packages\pgserver\pginstall\bin\pg_ctl.exe"
        if ((Test-Path pgdata\postmaster.pid) -and (Test-Path $pgCtl)) { & $pgCtl -D pgdata -m fast -w stop 2>$null | Out-Null }
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue pgdata
    }
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue storage, mt5data
    Write-Host "Demo data reset."
}
if ($docker) {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw "Docker is not running: start Docker Desktop, then run this again." }
    docker compose up -d --wait db
    if ($LASTEXITCODE -ne 0) { throw "The database container did not start: docker compose logs db" }
}

$env:PYTHONUTF8 = "1"
$env:PORT = "$Port"
# Open the browser once the server answers (the first start seeds the demo data, ~10 s).
Start-Job -ArgumentList $Port {
    param($p)
    for ($i = 0; $i -lt 90; $i++) {
        try { Invoke-WebRequest "http://127.0.0.1:$p/health" -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process "http://127.0.0.1:$p/admin"; return }
        catch { Start-Sleep 1 }
    }
} | Out-Null
& .\.venv\Scripts\python.exe -m app.devserver
