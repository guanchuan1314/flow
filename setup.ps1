<#
.SYNOPSIS
  Sets up Flow (broker CRM with the MT5 Manager API) on Windows, from a fresh copy of the project:
  installs what is missing (the Python in .python-version, Docker Desktop), creates the PostgreSQL database in Docker, the Python
  environment and .env (with generated secrets), migrates the database and, in production, creates the
  first admin and the Windows services. Safe to run again: finished steps are skipped and existing
  settings in .env are never overwritten (except the ones you pass as parameters).

.DESCRIPTION
  -Mode dev   (default) Local development / demo. Start the demo afterwards with .\run-demo.ps1.
  -Mode prod  Production on Windows Server / Windows 10+: writes APP_ENV=prod and the settings production
              requires, checks the configuration and the MT5 connection, creates the first admin
              (-AdminEmail) and, with -InstallServices, the Windows services (API, worker, HTTPS proxy).

  The database runs in Docker (container "flow-db-1", data in the "flow_pgdata" volume) unless you pass
  -DatabaseUrl to use a PostgreSQL server of your own. The CRM itself runs on Windows: the MT5 Manager API
  is a Windows-only library.

.EXAMPLE
  .\setup.ps1                                               # development / demo
.EXAMPLE
  .\setup.ps1 -BrokerName "Northwind Markets"               # same, with your broker's name
.EXAMPLE
  .\setup.ps1 -Mode prod -BrokerName "Northwind Markets" -PortalHost portal.northwind.com `
              -BackofficeHost backoffice.northwind.com -SmtpHost smtp.northwind.com -AdminEmail you@northwind.com
.EXAMPLE
  .\setup.ps1 -Mode prod -InstallServices                   # elevated PowerShell, once .env is complete
#>
param(
    [ValidateSet("dev", "prod")] [string]$Mode = "dev",
    [string]$BrokerName = "",             # shown everywhere (default Flow); also editable in Administration > Branding
    [string]$DatabaseUrl = "",            # your own PostgreSQL instead of the Docker one
    [string]$PortalHost = "",             # prod: client portal + website host name, e.g. portal.yourbroker.com
    [string]$BackofficeHost = "",         # prod: back-office host name, e.g. backoffice.yourbroker.com
    [string]$PublicUrl = "",              # default: https://<PortalHost>
    [string]$AllowedHosts = "",           # default: <PortalHost>,<BackofficeHost>
    [string]$SmtpHost = "",               # prod: required; set SMTP_USER / SMTP_PASSWORD / SMTP_FROM in .env
    [string]$AdminEmail = "",             # creates the first admin (asks for the password)
    [switch]$InstallServices,             # prod: FlowCRM-API, FlowCRM-Worker and FlowCRM-Proxy (needs elevation)
    [string]$Nssm = "",                   # path to nssm.exe (default: on PATH, else installed with winget)
    [switch]$NoInstall,                   # never install software; stop and say what is missing instead
    [switch]$SkipChecks                   # prod: skip check-config (e.g. MT5 not reachable yet)
)
# Native commands (python, pip, docker) are checked through $LASTEXITCODE: Windows PowerShell would turn
# their stderr output (progress, log lines) into fatal errors under "Stop".
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Done($text) { Write-Host "    $text" -ForegroundColor Green }
function Note($text) { Write-Host "    $text" -ForegroundColor Yellow }

if ($env:OS -ne "Windows_NT") { throw "Flow runs on Windows (the MT5 Manager API is a Windows-only library)." }

function Test-Command([scriptblock]$command) {
    # Runs a native command only to see whether it succeeds. (Windows PowerShell turns a native command's
    # stderr into an error, which "Stop" would make fatal.)
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & $command *> $null; $ok = $LASTEXITCODE -eq 0 } catch { $ok = $false }
    $ErrorActionPreference = $old
    return $ok
}
function Get-Output([scriptblock]$command) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $out = & $command 2>$null } catch { $out = $null }
    $ErrorActionPreference = $old
    return $out
}

function Update-Path {
    # Programs installed by winget are not on this session's PATH until it is re-read.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Install-Package($id, $what) {
    if ($NoInstall) { throw "$what is not installed. Install it (winget install --id $id) and run this script again." }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "$what is not installed and winget is not available. Install $what yourself and run this script again."
    }
    Note "Installing $what with winget (Windows may ask for permission)..."
    winget install --id $id --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Installing $what failed (winget exit code $LASTEXITCODE)." }
    Update-Path
}

# ---------------------------------------------------------------- 1. Python
# .python-version names the Python Flow is built and tested on (e.g. 3.12). It is installed when missing (winget,
# or the official installer from python.org where winget is not available, e.g. Windows Server), and .venv is
# created again when it runs another version (after an update that moves to a newer Python).
Step "Python"
$pythonVersion = (Get-Content (Join-Path $PSScriptRoot ".python-version") -TotalCount 1 -ErrorAction Stop).Trim()
if ($pythonVersion -notmatch '^3\.\d+$') { throw ".python-version must hold a version like 3.12 (found '$pythonVersion')." }
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Get-PythonVersion([string[]]$python) {
    $extra = @($python | Select-Object -Skip 1)
    return Get-Output { & $python[0] @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" }
}
function Find-Python {
    # The py launcher first, then the folders the installer uses (machine-wide, then this user), then "python"
    # on PATH. Each must run exactly this version (the Microsoft Store placeholder runs nothing).
    $folder = "Python" + $pythonVersion.Replace(".", "")
    $candidates = @(
        @("py", "-$pythonVersion"),
        @(Join-Path $env:ProgramFiles "$folder\python.exe"),
        @(Join-Path $env:LOCALAPPDATA "Programs\Python\$folder\python.exe"),
        @("python"))
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
        if ((Get-PythonVersion $candidate) -eq $pythonVersion) { return , $candidate }
    }
    return $null
}
function Install-Python {
    if ($NoInstall) { throw "Python $pythonVersion is not installed. Install it from https://www.python.org and run this script again." }
    # Production installs for all users: the services run under another account than the one running setup.
    $scope = if ($Mode -eq "prod") { "machine" } else { "user" }
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Note "Installing Python $pythonVersion with winget (Windows may ask for permission)..."
        winget install --id "Python.Python.$pythonVersion" --exact --silent --scope $scope --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) { Update-Path; return }
        Note "winget could not install Python (exit code $LASTEXITCODE); downloading it from python.org instead."
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $base = "https://www.python.org/ftp/python/"
    # The newest release of this version that has a Windows installer (the last ones of an old version are
    # source-only security releases).
    $releases = (Invoke-WebRequest $base -UseBasicParsing).Links.href |
        Where-Object { $_ -match ("^" + [regex]::Escape($pythonVersion) + "\.\d+/$") } |
        ForEach-Object { [version]$_.TrimEnd("/") } | Sort-Object -Descending
    $url = $null
    foreach ($v in $releases) {
        $candidate = "$base$v/python-$v-amd64.exe"
        try { Invoke-WebRequest $candidate -Method Head -UseBasicParsing | Out-Null; $url = $candidate; break } catch { }
    }
    if (-not $url) { throw "No Windows installer for Python $pythonVersion found on python.org. Install it yourself and run this script again." }
    $installer = Join-Path $env:TEMP (Split-Path $url -Leaf)
    Note "Downloading $url ..."
    Invoke-WebRequest $url -OutFile $installer -UseBasicParsing
    $allUsers = if ($scope -eq "machine") { 1 } else { 0 }
    Note "Installing Python $pythonVersion (Windows may ask for permission)..."
    $p = Start-Process $installer -Wait -PassThru -ArgumentList "/quiet", "InstallAllUsers=$allUsers", "PrependPath=1",
        "Include_launcher=1", "Include_test=0"
    Remove-Item $installer -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { throw "The Python installer failed (exit code $($p.ExitCode))." }
    Update-Path
}

$current = if (Test-Path $venvPython) { Get-PythonVersion @($venvPython) } else { $null }
if ($current -eq $pythonVersion) {
    Done ".venv already runs Python $pythonVersion"
} else {
    $python = Find-Python
    if (-not $python) {
        Install-Python
        $python = Find-Python
        if (-not $python) { throw "Python $pythonVersion was installed but cannot be found yet. Open a new PowerShell and run this script again." }
    }
    if (Test-Path (Join-Path $PSScriptRoot ".venv")) {
        Note $(if ($current) { "Python changed from $current to ${pythonVersion}: creating .venv again" } else { "Creating .venv again (it is broken)" })
        Remove-Item (Join-Path $PSScriptRoot ".venv") -Recurse -Force -ErrorAction Stop  # fails while Flow is running
    }
    $extra = @($python | Select-Object -Skip 1)
    & $python[0] @extra -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment (.venv)." }
    Done "Created .venv with Python $pythonVersion"
}

# ---------------------------------------------------------------- 2. Dependencies
Step "Python packages"
if (-not (Test-Command { & $venvPython -m pip --version })) { & $venvPython -m ensurepip --upgrade | Out-Null }
& $venvPython -m pip install --quiet --disable-pip-version-check --upgrade pip
$extras = if ($Mode -eq "prod") { "mt5" } else { "dev,mt5" }
& $venvPython -m pip install --quiet --disable-pip-version-check -e ".[$extras]"
if ($LASTEXITCODE -ne 0) { throw "Installing the Python packages failed (see above)." }
Done "Installed Flow with [$extras]"

# ---------------------------------------------------------------- 3. Settings (.env)
Step "Settings (.env)"
$envFile = Join-Path $PSScriptRoot ".env"
$newEnv = -not (Test-Path $envFile)
if ($newEnv) { Copy-Item (Join-Path $PSScriptRoot ".env.example") $envFile -ErrorAction Stop; Done "Created .env from .env.example" }
$lines = [System.Collections.Generic.List[string]](Get-Content $envFile -Encoding UTF8 -ErrorAction Stop)

function Get-Setting($name) {
    foreach ($l in $lines) { if ($l -match "^\s*$name\s*=\s*(.*?)\s*(#.*)?$") { return $Matches[1] } }
    return $null
}
function Set-Setting($name, $value) {
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*#?\s*$name\s*=") { $lines[$i] = "$name=$value"; return }
    }
    $lines.Add("$name=$value")
}
function New-Secret([string]$kind = "urlsafe") {
    if ($kind -eq "hex") { return & $venvPython -c "import secrets; print(secrets.token_hex(24))" }
    return & $venvPython -c "import secrets; print(secrets.token_urlsafe(48))"
}
function Test-PortFree([int]$port) {
    -not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

if ($BrokerName) { Set-Setting "APP_NAME" $BrokerName; Done "Broker name: $BrokerName" }
elseif ($newEnv -or -not (Get-Setting "APP_NAME")) { Set-Setting "APP_NAME" "Flow" }
$secret = Get-Setting "SECRET_KEY"
if (-not $secret -or $secret -eq "change-me" -or $secret.Length -lt 32) { Set-Setting "SECRET_KEY" (New-Secret); Done "Generated SECRET_KEY" }
if (-not (Get-Setting "CREDENTIALS_KEY")) {
    Set-Setting "CREDENTIALS_KEY" (New-Secret)
    Done "Generated CREDENTIALS_KEY (encrypts MT5 and 2FA secrets; back it up together with the database)"
}

$useDocker = -not $DatabaseUrl
$current = Get-Setting "DATABASE_URL"
if ($DatabaseUrl) {
    Set-Setting "DATABASE_URL" $DatabaseUrl
} elseif ($current -and $current -notmatch "STRONG_PASSWORD" -and $current -notmatch "@127\.0\.0\.1:$(Get-Setting 'POSTGRES_PORT')/") {
    $useDocker = $false  # .env already points at a PostgreSQL of your own
    Done "Using the database in DATABASE_URL"
}
if ($useDocker) {
    if (-not (Get-Setting "POSTGRES_PASSWORD")) {
        Set-Setting "POSTGRES_USER" "flow"
        Set-Setting "POSTGRES_DB" "flow"
        Set-Setting "POSTGRES_PASSWORD" (New-Secret "hex")
        $port = 5432
        while (-not (Test-PortFree $port)) { $port = if ($port -eq 5432) { 55432 } else { $port + 1 } }
        Set-Setting "POSTGRES_PORT" $port
        Done "Generated the database password (PostgreSQL in Docker on 127.0.0.1:$port)"
    }
    $pgPort = Get-Setting "POSTGRES_PORT"
    Set-Setting "DATABASE_URL" ("postgresql+psycopg://{0}:{1}@127.0.0.1:{2}/{3}" -f (Get-Setting "POSTGRES_USER"),
        (Get-Setting "POSTGRES_PASSWORD"), $pgPort, (Get-Setting "POSTGRES_DB"))
}

if ($Mode -eq "prod") {
    # What production requires (the app refuses to start otherwise); see the production block in .env.example.
    Set-Setting "APP_ENV" "prod"
    Set-Setting "AUTO_MIGRATE" "false"
    Set-Setting "MT5_GATEWAY" "manager"
    Set-Setting "EMBEDDED_WORKER" "false"
    Set-Setting "STAFF_2FA_REQUIRED" "true"
    if ($PortalHost) { Set-Setting "PORTAL_HOST" $PortalHost }
    if ($BackofficeHost) { Set-Setting "BACKOFFICE_HOST" $BackofficeHost }
    $ph, $bh = (Get-Setting "PORTAL_HOST"), (Get-Setting "BACKOFFICE_HOST")
    if (-not $PublicUrl -and $ph) { $PublicUrl = "https://$ph" }
    if (-not $AllowedHosts -and $ph -and $bh) { $AllowedHosts = "$ph,$bh" }
    if ($SmtpHost) { Set-Setting "SMTP_HOST" $SmtpHost }
    if ($PublicUrl -and -not $PublicUrl.StartsWith("https://")) { throw "-PublicUrl must start with https:// in production." }
}
if ($PublicUrl) { Set-Setting "PUBLIC_URL" $PublicUrl }
if ($AllowedHosts) { Set-Setting "ALLOWED_HOSTS" $AllowedHosts }
[System.IO.File]::WriteAllLines($envFile, $lines, (New-Object System.Text.UTF8Encoding($false)))  # UTF-8, no BOM
Done ".env saved"

# ---------------------------------------------------------------- 4. Database (Docker)
if ($useDocker) {
    Step "Database (PostgreSQL in Docker)"
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Install-Package "Docker.DockerDesktop" "Docker Desktop"
        Note "Docker Desktop was installed. Windows may need a restart (WSL 2). Restart if asked, start Docker Desktop"
        Note "once, accept its terms, then run this script again."
        exit 2
    }
    if (-not (Test-Command { docker info })) {
        $desktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
        if (Test-Path $desktop) { Note "Starting Docker Desktop..."; Start-Process $desktop }
        $deadline = (Get-Date).AddMinutes(4)
        do { Start-Sleep 3; $up = Test-Command { docker info } } while (-not $up -and (Get-Date) -lt $deadline)
        if (-not $up) { throw "Docker is not running. Start Docker Desktop (accept its terms the first time) and run this script again." }
    }
    if (-not (Test-Command { docker compose up -d --wait db })) {
        throw "The database container did not start or become ready: docker compose logs db"
    }
    Done "PostgreSQL is running (container flow-db-1, data in volume flow_pgdata)"
    if ($Mode -eq "prod") {
        Note "Production: in Docker Desktop > Settings > General, tick 'Start Docker Desktop when you sign in',"
        Note "and have this server sign in automatically after a restart, so the database starts with Windows."
    }
}

# ---------------------------------------------------------------- 5. Production requirements
if ($Mode -eq "prod") {
    $missing = @()
    if (-not (Get-Setting "PORTAL_HOST") -or -not (Get-Setting "BACKOFFICE_HOST")) {
        $missing += "PORTAL_HOST and BACKOFFICE_HOST (or -PortalHost / -BackofficeHost): your two domain names"
    }
    $hosts = Get-Setting "ALLOWED_HOSTS"
    if (-not $hosts -or $hosts -eq "*") { $missing += "ALLOWED_HOSTS (or -AllowedHosts): your portal and back-office domain names" }
    if (-not ((Get-Setting "PUBLIC_URL") -like "https://*")) { $missing += "PUBLIC_URL (or -PublicUrl): https://your portal address" }
    if (-not (Get-Setting "SMTP_HOST")) { $missing += "SMTP_HOST (or -SmtpHost), with SMTP_USER, SMTP_PASSWORD and SMTP_FROM" }
    if ($missing) {
        Note "Fill these in .env (or pass them as parameters), then run this script again:"
        $missing | ForEach-Object { Note "  - $_" }
        exit 1
    }
}

# ---------------------------------------------------------------- 6. Migrate, check, first admin
Step "Database migration"
& $venvPython -m app.cli migrate
if ($LASTEXITCODE -ne 0) { throw "Database migration failed." }
Done "Database is up to date"

if ($Mode -eq "dev") {
    Step "Ready"
    Write-Host "    Start the demo:      .\run-demo.ps1        (MT5 simulator + demo data, opens the browser)"
    Write-Host "    Run the tests:       .venv\Scripts\python -m pytest -q"
    Write-Host "    Broker name/colour:  Administration -> Branding (or APP_NAME / ACCENT_COLOR in .env)"
    Write-Host "    Connect a real MT5:  sign in, then MT5 -> Configuration"
    return
}

if (-not $SkipChecks) {
    Step "Checking settings, database and MT5"
    & $venvPython -m app.cli check-config
    if ($LASTEXITCODE -ne 0) { throw "check-config reported problems (see above). Fix .env and run again, or use -SkipChecks." }
}
if ($AdminEmail) {
    Step "First admin"
    & $venvPython -m app.cli create-admin --email $AdminEmail
    if ($LASTEXITCODE -ne 0) { throw "Creating the admin failed." }
}

# ---------------------------------------------------------------- 7. Windows services
if ($InstallServices) {
    Step "Windows services"
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Installing services needs an elevated PowerShell (Run as administrator)."
    }
    if (-not $Nssm) {
        if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) { Install-Package "NSSM.NSSM" "NSSM (service manager)" }
        $Nssm = (Get-Command nssm -ErrorAction Stop).Source
    }
    if (-not (Get-Command caddy -ErrorAction SilentlyContinue)) { Install-Package "CaddyServer.Caddy" "Caddy (HTTPS)" }
    $caddy = (Get-Command caddy -ErrorAction Stop).Source
    $pgService = if ($useDocker) { "" } else { "postgresql-x64-16" }
    & (Join-Path $PSScriptRoot "deploy\windows\install-services.ps1") -Nssm $Nssm -Caddy $caddy -PostgresService $pgService
}

Step "Ready"
Write-Host "    Point $(Get-Setting 'PORTAL_HOST') and $(Get-Setting 'BACKOFFICE_HOST') at this server and open ports 80/443."
Write-Host "    Sign in at https://$(Get-Setting 'BACKOFFICE_HOST')/admin, then set up the MT5 server under MT5 -> Configuration."
if (-not $InstallServices) { Write-Host "    Install the services: .\setup.ps1 -Mode prod -InstallServices (elevated PowerShell)" }
