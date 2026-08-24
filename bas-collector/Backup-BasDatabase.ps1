<#
.SYNOPSIS
    Backs up the BAS database. Verifies the dump. Rotates old ones.

.DESCRIPTION
    This matters more here than for a typical application database.

    Most databases can be rebuilt from somewhere: a source system, an export, a
    replica. This one cannot. The JACE overwrites its own trend history roughly
    every 42 hours, so anything older than that exists in exactly one place -
    this database. There is no re-import, no vendor copy, no "pull it again".

    So: dump daily, verify every dump is actually readable, keep enough history
    that a problem noticed late is still recoverable, and test the restore path
    for real rather than assuming it works.

    DESTINATION
    Defaults to OneDrive. That is deliberate, and it is the opposite of the
    advice for the code folders: OneDrive's Files On-Demand causes file-locking
    problems for things tools open and write constantly, which is why C:\dev
    exists. A backup file is written once and never touched again, so none of
    that applies - and it gets the one property a backup on the same laptop
    cannot have, which is being somewhere other than the laptop.

    Override with BAS_BACKUP_DIR if there is a proper file share or NAS.

.PARAMETER Destination
    Where to write. Overrides BAS_BACKUP_DIR.

.PARAMETER KeepDays
    Daily backups to retain. Default 14. Backups dated the 1st of a month are
    kept regardless, so long-term coverage does not depend on noticing early.
#>

param(
    [string]$Destination,
    [int]$KeepDays = 14
)

$ErrorActionPreference = 'Continue'
# Deliberately 'Continue', not 'Stop'.
#
# With 'Stop', PowerShell turns ANY stderr output from a native command into a
# terminating error - including psql's harmless "NOTICE: database does not
# exist, skipping". The script would abort on a non-problem, while a real
# failure (a non-zero exit code) could go unnoticed.
#
# Native tool success is $LASTEXITCODE, not the absence of stderr. This script
# checks that explicitly instead.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir 'backup.log'

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $logFile -Value $line
}

# --- Where to write --------------------------------------------------------
if (-not $Destination) { $Destination = $env:BAS_BACKUP_DIR }
if (-not $Destination) {
    $oneDrive = $env:OneDriveCommercial
    if (-not $oneDrive) { $oneDrive = $env:OneDrive }
    if ($oneDrive) {
        $Destination = Join-Path $oneDrive 'BAS-Backups'
    } else {
        $Destination = 'C:\dev\bas-backups'
        Write-Log "WARNING: no OneDrive found, writing to $Destination - this is on the same disk as the database, which is not really a backup."
    }
}
if (-not (Test-Path $Destination)) { New-Item -ItemType Directory -Path $Destination -Force | Out-Null }

# --- Connection string, read from .env so it is defined in exactly one place -
$dsn = $null
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    $match = Select-String -Path $envFile -Pattern '^DATABASE_URL=(.+)$' | Select-Object -First 1
    if ($match) { $dsn = $match.Matches[0].Groups[1].Value.Trim() }
}
if (-not $dsn) { $dsn = $env:DATABASE_URL }
if (-not $dsn) {
    Write-Log "FAILED: no DATABASE_URL in .env or environment."
    exit 1
}

# --- Locate pg_dump --------------------------------------------------------
$pgDump = (Get-Command pg_dump -ErrorAction SilentlyContinue).Source
if (-not $pgDump) {
    $found = Get-ChildItem 'C:\Program Files\PostgreSQL' -Recurse -Filter 'pg_dump.exe' -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { $pgDump = $found.FullName }
}
if (-not $pgDump) {
    Write-Log "FAILED: could not find pg_dump.exe."
    exit 1
}

# --- Dump ------------------------------------------------------------------
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmm'
$file  = Join-Path $Destination "bas_$stamp.dump"

Write-Log "Backing up to $file"

# -Fc is the custom format: compressed, and pg_restore can read it selectively
# and verify its own table of contents. A plain SQL dump cannot be verified
# without actually running it.
& $pgDump $dsn -Fc --no-owner --no-privileges -f $file 2>&1 | ForEach-Object { Write-Log "  $_" }

if ($LASTEXITCODE -ne 0 -or -not (Test-Path $file)) {
    Write-Log "FAILED: pg_dump exited $LASTEXITCODE"
    exit 1
}

$sizeMB = [math]::Round((Get-Item $file).Length / 1MB, 2)
Write-Log "Wrote $sizeMB MB"

# --- Verify ----------------------------------------------------------------
# A dump that was never checked is a file, not a backup. pg_restore --list
# reads the archive's table of contents, which catches truncation and
# corruption without needing a database to restore into.
# Derive pg_restore from pg_dump's path. Extension-agnostic so this does not
# silently resolve to the wrong binary if the executable has no .exe suffix.
$pgRestore = $pgDump -replace 'pg_dump(\.exe)?$', 'pg_restore$1'
if (-not (Test-Path $pgRestore)) {
    Write-Log "FAILED: derived pg_restore path does not exist: $pgRestore"
    exit 1
}
$toc = & $pgRestore --list $file 2>&1 | Out-String

if ($LASTEXITCODE -ne 0) {
    Write-Log "FAILED VERIFICATION: pg_restore could not read the dump. Treat this backup as worthless."
    exit 1
}

# The same data lives under two different names, and which one a dump uses
# depends entirely on which database .env points at:
#
#   standalone bas database   TABLE DATA bas reading
#   platform database         TABLE DATA public bas_readings
#
# The trailing \s matters: without it 'bas point' also matches 'bas point_link',
# so a dump missing bas.point could pass. $toc is a single string (Out-String),
# so \s also matches the newline when the table name ends the line.
function Test-TocHasTables($toc, [string[]]$tables) {
    foreach ($t in $tables) {
        $pattern = [regex]::Escape("TABLE DATA $t") + '\s'
        if ($toc -notmatch $pattern) { return $false }
    }
    return $true
}

$standaloneTables = @('bas reading', 'bas point', 'bas site', 'bas sync_checkpoint')
$platformTables   = @('public bas_readings', 'public bas_points',
                      'public bas_sites', 'public bas_sync_checkpoints')

if (Test-TocHasTables $toc $standaloneTables) {
    $layout = 'standalone bas schema'
} elseif (Test-TocHasTables $toc $platformTables) {
    $layout = 'platform public.bas_* tables'
} else {
    Write-Log "FAILED VERIFICATION: the dump has neither the standalone bas schema nor the platform bas_* tables. Core tables are missing."
    exit 1
}
Write-Log "Verified: archive readable, core tables present ($layout)"

# --- Rotate ----------------------------------------------------------------
# Keep the last N days, plus anything dated the 1st of a month. That way
# long-term coverage does not depend on someone noticing a problem quickly.
$cutoff  = (Get-Date).AddDays(-$KeepDays)
$removed = 0
Get-ChildItem $Destination -Filter 'bas_*.dump' | ForEach-Object {
    if ($_.Name -match 'bas_(\d{4})-(\d{2})-(\d{2})_') {
        $date = Get-Date -Year $Matches[1] -Month $Matches[2] -Day $Matches[3]
        $isMonthly = ($Matches[3] -eq '01')
        if ($date -lt $cutoff -and -not $isMonthly) {
            Remove-Item $_.FullName -Force
            $removed++
        }
    }
}
if ($removed) { Write-Log "Rotated out $removed old backup(s)" }

$kept = @(Get-ChildItem $Destination -Filter 'bas_*.dump')
$totalMB = [math]::Round(($kept | Measure-Object Length -Sum).Sum / 1MB, 2)
Write-Log "OK - $($kept.Count) backup(s) retained, $totalMB MB total"

# Full dumps stop being sensible somewhere past a few GB. At 500 points on
# 5-minute logging that is roughly a year. When it gets there, the answer is
# WAL archiving with point-in-time recovery rather than bigger daily files.
if ($totalMB -gt 5000) {
    Write-Log "NOTE: backups now exceed 5 GB. Consider WAL archiving instead of full daily dumps."
}

exit 0
