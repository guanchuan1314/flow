<#
.SYNOPSIS
  Updates Flow to the latest code of the branch this copy is on (git pull from GitHub), then brings everything
  up to date: Python (when the new code moves to another version, see .python-version), the Python packages,
  the database migration and, in production, the Windows services.

.DESCRIPTION
  1. Checks this is a git copy with no local changes, on a branch (the one you checked out; no need to name it).
  2. Fetches that branch from origin; stops here when there is nothing new (unless -Force).
  3. Production: makes a backup first when -BackupDir is given, and stops FlowCRM-API and FlowCRM-Worker.
  4. git pull (fast-forward only).
  5. Runs setup.ps1 in the mode .env says (APP_ENV=prod -> prod): installs the Python in .python-version and
     creates .venv again when the version changed, installs the packages, migrates the database, checks the
     configuration.
  6. Production: starts the services again and checks that the API reports ready.

  Production needs an elevated PowerShell (Run as administrator) to stop and start the services.

.EXAMPLE
  .\deploy.ps1                                   # update to the latest code of the current branch
.EXAMPLE
  .\deploy.ps1 -BackupDir D:\backups\flow        # production: back up first
.EXAMPLE
  git checkout release; .\deploy.ps1             # follow another branch from now on
#>
param(
    [string]$BackupDir = "",   # run deploy\windows\backup.ps1 into this folder before updating
    [switch]$Force,            # run every step even when the branch has nothing new
    [int]$Port = 8000          # the API port the services listen on (for the ready check)
)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Done($text) { Write-Host "    $text" -ForegroundColor Green }
function Note($text) { Write-Host "    $text" -ForegroundColor Yellow }
function Invoke-Git {
    $out = & git.exe @args 2>&1
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed: $out" }
    return $out
}

$services = "FlowCRM-Worker", "FlowCRM-API", "FlowCRM-Proxy"  # start order (the proxy depends on the API)

# ---------------------------------------------------------------- 1. Where we are
Step "Code"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is not installed (winget install --id Git.Git)." }
if (-not (Test-Path (Join-Path $PSScriptRoot ".git"))) { throw "This folder is not a git copy of Flow: clone it from GitHub to deploy with this script." }
$branch = (Invoke-Git rev-parse --abbrev-ref HEAD | Out-String).Trim()
if ($branch -eq "HEAD") { throw "No branch is checked out (detached HEAD). Check out the branch to follow: git checkout master" }
$changes = Invoke-Git status --porcelain --untracked-files=no
if ($changes) {
    Note "These files were changed on this machine:"
    $changes | ForEach-Object { Note "  $_" }
    throw "Local changes would be overwritten. Commit or undo them (git stash), then run this script again."
}
Invoke-Git fetch --quiet origin $branch | Out-Null
$before = (Invoke-Git rev-parse HEAD | Out-String).Trim()
$after = (Invoke-Git rev-parse "origin/$branch" | Out-String).Trim()
if ($before -eq $after -and -not $Force) { Done "Branch $branch is already up to date ($($before.Substring(0, 7)))."; return }
$behind = (Invoke-Git rev-list --count "HEAD..origin/$branch" | Out-String).Trim()
Done "Branch $branch : $behind new commit(s)"
Invoke-Git log --oneline "HEAD..origin/$branch" | Select-Object -First 15 | ForEach-Object { Write-Host "      $_" }

$envFile = Join-Path $PSScriptRoot ".env"
$mode = "dev"
if ((Test-Path $envFile) -and (Get-Content $envFile -Encoding UTF8 | Where-Object { $_ -match '^\s*APP_ENV\s*=\s*prod\b' })) { $mode = "prod" }
$installed = @($services | Where-Object { Get-Service -Name $_ -ErrorAction SilentlyContinue })

# ---------------------------------------------------------------- 2. Backup, stop
if ($installed) {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Stopping the Flow services needs an elevated PowerShell (Run as administrator)."
    }
}
if ($BackupDir) {
    Step "Backup"
    & (Join-Path $PSScriptRoot "deploy\windows\backup.ps1") -BackupDir $BackupDir
    if (-not $?) { throw "The backup failed; nothing was changed." }
    Done "Backed up to $BackupDir"
}
if ($installed) {
    Step "Stopping the services"
    foreach ($name in $services[($services.Count - 1)..0]) {
        if ($installed -contains $name) { Stop-Service -Name $name -Force -ErrorAction Stop; Done "Stopped $name" }
    }
} elseif ($mode -eq "dev") {
    Note "Stop Flow first if it is running (run-demo.ps1): its files are about to change."
}

# ---------------------------------------------------------------- 3. Pull, update
Step "Pulling $branch"
Invoke-Git pull --ff-only --quiet origin $branch | Out-Null
Done "Now at $((Invoke-Git log -1 --format='%h %s' | Out-String).Trim())"

Step "Updating Python, packages and the database (setup.ps1 -Mode $mode)"
# A new PowerShell: the setup.ps1 just pulled, not a copy of the old one.
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "setup.ps1") -Mode $mode
$setupOk = $LASTEXITCODE -eq 0

# ---------------------------------------------------------------- 4. Start
if ($installed) {
    if (-not $setupOk) {
        Note "setup.ps1 failed (see above): the services stay stopped. Fix the problem and run .\deploy.ps1 -Force,"
        Note "or go back to the previous version: git reset --hard $($before.Substring(0, 7)), then .\deploy.ps1 -Force."
        exit 1
    }
    Step "Starting the services"
    foreach ($name in $services) {
        if ($installed -contains $name) { Start-Service -Name $name -ErrorAction Stop; Done "Started $name" }
    }
    $ready = $null
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep 3
        try { $ready = Invoke-RestMethod "http://127.0.0.1:$Port/ready" -TimeoutSec 5 } catch { $ready = $null }
    } while (-not $ready -and (Get-Date) -lt $deadline)
    if (-not $ready) { Note "The API did not report ready within a minute: check logs\crm\api.log"; exit 1 }
    Done "API ready: $($ready.status)"
} elseif (-not $setupOk) {
    exit 1
}

Step "Done"
Write-Host "    Flow is at $((Invoke-Git log -1 --format='%h %s' | Out-String).Trim()) on branch $branch."
if (-not $installed) { Write-Host "    Start it again: .\run-demo.ps1" }
