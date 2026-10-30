<#
.SYNOPSIS
  Nightly backup: PostgreSQL dump + KYC document store + .env, with retention.
  Schedule with Task Scheduler (daily, "Run whether user is logged on or not").

  With the database in Docker (setup.ps1's default, POSTGRES_PASSWORD in .env), pg_dump runs inside the
  container and needs nothing installed. Otherwise the database password is passed to pg_dump through a
  temporary PGPASSFILE (never on the command line,
  where other users of the machine could see it). The copy of .env holds secrets (SECRET_KEY and CREDENTIALS_KEY, which
  decrypts the stored MT5 manager password, and the database password): keep the backup folder restricted
  to administrators and encrypt it if it leaves the machine. Without that .env the encrypted secrets in the
  dump cannot be read back.

.EXAMPLE
  .\deploy\windows\backup.ps1 -BackupDir D:\backups\flow                     # database in Docker
.EXAMPLE
  .\deploy\windows\backup.ps1 -BackupDir D:\backups\flow -PgDump "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe"
#>
param(
    [string]$AppDir = (Resolve-Path "$PSScriptRoot\..\..").Path,
    [string]$BackupDir = "D:\backups\flow",
    [string]$PgDump = "pg_dump.exe",
    [int]$KeepDays = 30
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Web

# Read DATABASE_URL / STORAGE_DIR from .env (postgresql+psycopg://user:pass@host:port/db)
$envPath = Join-Path $AppDir ".env"
$envFile = Get-Content $envPath -Encoding UTF8 | Where-Object { $_ -match "^\s*[A-Z_]+=" }
$cfg = @{}
foreach ($line in $envFile) { $k, $v = $line -split "=", 2; $cfg[$k.Trim()] = ($v -replace "\s+#.*$", "").Trim().Trim('"') }
$dbUrl = $cfg["DATABASE_URL"] -replace "^postgresql\+psycopg://", "postgresql://"
if ($dbUrl -notmatch "^postgresql://") { throw "DATABASE_URL is not PostgreSQL; nothing to back up with pg_dump." }
$uri = [System.Uri]$dbUrl
$user, $password = $uri.UserInfo -split ":", 2
$user = [System.Web.HttpUtility]::UrlDecode($user)
$password = if ($password) { [System.Web.HttpUtility]::UrlDecode($password) } else { "" }
$dbHost = if ($uri.Host) { $uri.Host } else { "localhost" }
$dbPort = if ($uri.Port -gt 0) { $uri.Port } else { 5432 }
$dbName = $uri.AbsolutePath.TrimStart("/")
$storage = if ($cfg["STORAGE_DIR"]) { $cfg["STORAGE_DIR"] } else { "./storage" }
if (-not [System.IO.Path]::IsPathRooted($storage)) { $storage = Join-Path $AppDir $storage }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force $BackupDir | Out-Null
$dump = Join-Path $BackupDir "crm-db-$stamp.dump"

if ($cfg["POSTGRES_PASSWORD"]) {
    # The database runs in Docker (docker-compose.yml): dump inside the container, copy the file out.
    docker exec flow-db-1 pg_dump --format=custom --no-owner --username $user --file /tmp/backup.dump $dbName
    if ($LASTEXITCODE -ne 0) { throw "pg_dump in the flow-db-1 container failed (is Docker running?)" }
    docker cp flow-db-1:/tmp/backup.dump $dump
    if ($LASTEXITCODE -ne 0) { throw "Copying the dump out of the container failed" }
    docker exec flow-db-1 rm -f /tmp/backup.dump | Out-Null
    $PgDump = $null
}

# pgpass line: host:port:db:user:password, with ':' and '\' escaped
$esc = { param($s) ($s -replace '\\', '\\' -replace ':', '\:') }
$passFile = Join-Path ([System.IO.Path]::GetTempPath()) ("crm-pgpass-" + [guid]::NewGuid() + ".conf")
if ($PgDump) { try {
    Set-Content -Path $passFile -Encoding ascii -Value ("{0}:{1}:{2}:{3}:{4}" -f (& $esc $dbHost), $dbPort, (& $esc $dbName), (& $esc $user), (& $esc $password))
    $env:PGPASSFILE = $passFile
    & $PgDump --format=custom --no-owner --file $dump --host $dbHost --port $dbPort --username $user --no-password $dbName
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
} finally {
    Remove-Item $passFile -Force -ErrorAction SilentlyContinue
    Remove-Item Env:\PGPASSFILE -ErrorAction SilentlyContinue
} }

if (Test-Path $storage) {
    Compress-Archive -Path (Join-Path $storage "*") -DestinationPath (Join-Path $BackupDir "crm-storage-$stamp.zip") -CompressionLevel Optimal
}
Copy-Item $envPath (Join-Path $BackupDir "crm-env-$stamp.env")
Write-Warning "crm-env-$stamp.env holds the secrets (SECRET_KEY, CREDENTIALS_KEY, database password): keep $BackupDir restricted."

Get-ChildItem $BackupDir -File | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } | Remove-Item -Force
# NSSM rotates the services' console capture but never deletes old files.
$serviceLogs = Join-Path $AppDir "logs\services"
if (Test-Path $serviceLogs) {
    Get-ChildItem $serviceLogs -File | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } | Remove-Item -Force
}
Write-Host "Backup written to $BackupDir ($stamp). Restore with: pg_restore --clean --no-owner -d <db-url> $dump (database in Docker: docker cp the file into flow-db-1 and run pg_restore there)"
