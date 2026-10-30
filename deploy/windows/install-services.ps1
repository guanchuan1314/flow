<#
.SYNOPSIS
  Installs Flow (broker CRM) as Windows services using NSSM (https://nssm.cc):
    FlowCRM-API     web UI + REST API (uvicorn, bound to localhost; Caddy in front for HTTPS)
    FlowCRM-Worker  deal/position sync, copy-trading engine, fee scheduler (one worker for the CRM's MT5 server)
    FlowCRM-Proxy   Caddy with deploy\Caddyfile: HTTPS for PORTAL_HOST and BACKOFFICE_HOST (only with -Caddy)

  Applies the database migrations, then checks the configuration, installs both services (depending on the
  PostgreSQL service when it runs on this machine) and checks that the API is ready and the worker is running.

  Logs: the app writes daily files kept 30 days under logs\crm (LOG_DIR). NSSM only captures the console as a
  fallback for start-up crashes, in logs\services (rotated at 10 MB; backup.ps1 prunes old files).

.PARAMETER ServiceUser
  Windows account the services run as, e.g. ".\mt5crm" or "DOMAIN\svc-mt5crm". It needs read access to the
  project folder and write access to logs\, storage\ and the MT5 work folder. Default: LocalSystem (not
  recommended: it has full control of the machine).

.PARAMETER Caddy
  Path to caddy.exe. When given, also installs the FlowCRM-Proxy service (host names from PORTAL_HOST and
  BACKOFFICE_HOST in .env; certificates are kept in storage\caddy).

.PARAMETER PostgresService
  Name of the local PostgreSQL Windows service the CRM services start after. Skipped when not installed
  (database on another machine).

.EXAMPLE
  # From an elevated PowerShell, in the project folder:
  .\deploy\windows\install-services.ps1 -Nssm C:\tools\nssm.exe -ServiceUser ".\mt5crm" -ServicePassword (Read-Host -AsSecureString)
#>
param(
    [string]$AppDir = (Resolve-Path "$PSScriptRoot\..\..").Path,
    [string]$Nssm = "nssm.exe",
    [int]$Port = 8000,
    [string]$ServiceUser = "",
    [System.Security.SecureString]$ServicePassword = $null,
    [string]$PostgresService = "postgresql-x64-16",
    [string]$Caddy = ""
)
$ErrorActionPreference = "Stop"

$python = Join-Path $AppDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python venv not found at $python. Create it first (see README)." }
if (-not (Test-Path (Join-Path $AppDir ".env"))) { throw ".env not found in $AppDir. Copy .env.example and fill it in." }
$serviceLogDir = Join-Path $AppDir "logs\services"
$appLogDir = Join-Path $AppDir "logs\crm"
New-Item -ItemType Directory -Force $serviceLogDir, $appLogDir | Out-Null

# Migrate first: check-config verifies the database schema is current.
Write-Host "Applying database migrations..."
& $python -m app.cli migrate
if ($LASTEXITCODE -ne 0) { throw "Database migration failed." }
Write-Host "Checking configuration, database and MT5 connectivity..."
& $python -m app.cli check-config
if ($LASTEXITCODE -ne 0) { throw "check-config failed; fix the problems above before installing." }

$dependsOn = $null
if ($PostgresService) {
    if (Get-Service -Name $PostgresService -ErrorAction SilentlyContinue) {
        $dependsOn = $PostgresService
    } else {
        Write-Warning "PostgreSQL service '$PostgresService' not found on this machine; services will not depend on it. Pass -PostgresService <name> if it has another name."
    }
}

$plainPassword = $null
if ($ServiceUser) {
    if (-not $ServicePassword) { throw "-ServiceUser needs -ServicePassword (use Read-Host -AsSecureString)." }
    $plainPassword = [System.Net.NetworkCredential]::new("", $ServicePassword).Password
} else {
    Write-Warning "Services will run as LocalSystem, which has full control of this machine. Prefer a dedicated account: -ServiceUser .\mt5crm -ServicePassword (Read-Host -AsSecureString)."
}

function Install-CrmService($name, $arguments, $component) {
    & $Nssm stop $name 2>$null | Out-Null
    & $Nssm remove $name confirm 2>$null | Out-Null
    & $Nssm install $name $python $arguments
    & $Nssm set $name AppDirectory $AppDir
    # UTF-8 everywhere regardless of the Windows locale; the app keeps its own daily logs (30 days) in LOG_DIR.
    & $Nssm set $name AppEnvironmentExtra "PYTHONUTF8=1" "PYTHONUNBUFFERED=1" "LOG_DIR=$appLogDir" "AUTO_MIGRATE=false" "CRM_COMPONENT=$component"
    # Console capture: only a fallback for crashes before logging starts.
    & $Nssm set $name AppStdout (Join-Path $serviceLogDir "$component-console.log")
    & $Nssm set $name AppStderr (Join-Path $serviceLogDir "$component-console.log")
    & $Nssm set $name AppRotateFiles 1
    & $Nssm set $name AppRotateOnline 1
    & $Nssm set $name AppRotateBytes 10485760
    # The worker exits with code 1 when one of its threads dies: always restart.
    & $Nssm set $name AppExit Default Restart
    & $Nssm set $name AppRestartDelay 5000
    & $Nssm set $name AppStopMethodConsole 15000
    & $Nssm set $name Start SERVICE_AUTO_START
    & $Nssm set $name Description "Flow CRM $component"
    if ($dependsOn) { & $Nssm set $name DependOnService $dependsOn }
    if ($ServiceUser) { & $Nssm set $name ObjectName $ServiceUser $plainPassword }
}

# One API process: requests run on a thread pool, the in-process login rate limiter stays exact,
# and only one Manager API connection is held. Scale out behind a load balancer if ever needed.
Install-CrmService "FlowCRM-API" "-m uvicorn app.main:app --host 127.0.0.1 --port $Port --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log --timeout-graceful-shutdown 20" "api"
Install-CrmService "FlowCRM-Worker" "-m app.workers" "worker"
$plainPassword = $null

& $Nssm start "FlowCRM-Worker"
& $Nssm start "FlowCRM-API"
Start-Sleep 8

if ($Caddy) {
    $envValues = @{}
    Get-Content (Join-Path $AppDir ".env") | ForEach-Object {
        if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*(#.*)?$') { $envValues[$Matches[1]] = $Matches[2] }
    }
    foreach ($key in "PORTAL_HOST", "BACKOFFICE_HOST") {
        if (-not $envValues[$key]) { throw "$key is not set in .env (the HTTPS proxy needs both host names)." }
    }
    $name = "FlowCRM-Proxy"
    $caddyData = Join-Path $AppDir "storage\caddy"
    New-Item -ItemType Directory -Force $caddyData, (Join-Path $AppDir "logs\caddy") | Out-Null
    & $Nssm stop $name 2>$null | Out-Null
    & $Nssm remove $name confirm 2>$null | Out-Null
    & $Nssm install $name $Caddy "run --config `"$(Join-Path $AppDir 'deploy\Caddyfile')`" --adapter caddyfile"
    & $Nssm set $name AppDirectory $AppDir
    & $Nssm set $name AppEnvironmentExtra "PORTAL_HOST=$($envValues.PORTAL_HOST)" "BACKOFFICE_HOST=$($envValues.BACKOFFICE_HOST)" "XDG_DATA_HOME=$caddyData"
    & $Nssm set $name AppStdout (Join-Path $serviceLogDir "proxy-console.log")
    & $Nssm set $name AppStderr (Join-Path $serviceLogDir "proxy-console.log")
    & $Nssm set $name AppExit Default Restart
    & $Nssm set $name Start SERVICE_AUTO_START
    & $Nssm set $name DependOnService "FlowCRM-API"
    & $Nssm set $name Description "Flow CRM HTTPS proxy (Caddy)"
    & $Nssm start $name
    Write-Host "HTTPS proxy installed for $($envValues.PORTAL_HOST) and $($envValues.BACKOFFICE_HOST)."
}

$healthy = $true
try {
    $ready = Invoke-RestMethod "http://127.0.0.1:$Port/ready" -TimeoutSec 15
    Write-Host "API ready: $($ready | ConvertTo-Json -Compress)"
} catch {
    $healthy = $false
    Write-Warning "API did not report ready yet; check $appLogDir\api.log and $serviceLogDir\api-console.log"
}
$worker = Get-Service -Name "FlowCRM-Worker" -ErrorAction SilentlyContinue
if ($worker -and $worker.Status -eq "Running") {
    Write-Host "Worker service running."
} else {
    $healthy = $false
    Write-Warning "Worker service is not running (status: $($worker.Status)); check $appLogDir\worker.log and $serviceLogDir\worker-console.log"
}
Write-Host "Services installed. App logs: $appLogDir  Console capture: $serviceLogDir"
if (-not $healthy) { exit 1 }
