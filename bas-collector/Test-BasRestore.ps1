<#
.SYNOPSIS
    Actually restores a backup and checks it, then throws the copy away.

.DESCRIPTION
    A backup that has never been restored is a hypothesis, not a backup.

    Backup/restore fails quietly in ways a successful dump does not reveal:
    missing extensions, permission problems, a role that does not exist on the
    target, a schema-search-path assumption nobody wrote down. You find these
    out either now, deliberately, or later, in the middle of an actual incident.

    This restores the newest dump into a scratch database, compares row counts
    against the live one, and drops the scratch database again. The live
    database is never touched.

    Run it after setting up backups, and again whenever anything about the
    database changes meaningfully.

.PARAMETER BackupFile
    A specific dump to test. Defaults to the newest.

.PARAMETER Keep
    Leave the restored scratch database in place for inspection.
#>

param(
    [string]$BackupFile,
    [switch]$Keep
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

function Say($msg, $colour = 'Gray') { Write-Host "  $msg" -ForegroundColor $colour }

# --- Find the backup -------------------------------------------------------
if (-not $BackupFile) {
    $dir = $env:BAS_BACKUP_DIR
    if (-not $dir) {
        $oneDrive = $env:OneDriveCommercial
        if (-not $oneDrive) { $oneDrive = $env:OneDrive }
        if ($oneDrive) { $dir = Join-Path $oneDrive 'BAS-Backups' } else { $dir = 'C:\dev\bas-backups' }
    }
    $newest = Get-ChildItem $dir -Filter 'bas_*.dump' -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) {
        Write-Host ""
        Say "No backups found in $dir. Run Backup-BasDatabase.ps1 first." 'Yellow'
        Write-Host ""
        exit 1
    }
    $BackupFile = $newest.FullName
}

Write-Host ""
Say "Testing: $BackupFile"
Say "Size:    $([math]::Round((Get-Item $BackupFile).Length / 1MB, 2)) MB"
Write-Host ""

# --- Connection details ----------------------------------------------------
$dsn = $null
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^DATABASE_URL=(.+)$' | Select-Object -First 1
    if ($m) { $dsn = $m.Matches[0].Groups[1].Value.Trim() }
}
if (-not $dsn) { $dsn = $env:DATABASE_URL }
if (-not $dsn) { Say "No DATABASE_URL found." 'Red'; exit 1 }

# postgresql://user:pass@host:port/dbname
if ($dsn -notmatch '^postgresql://([^:]+):([^@]+)@([^:/]+):(\d+)/(.+)$') {
    Say "Could not parse DATABASE_URL." 'Red'; exit 1
}
$dbUser = $Matches[1]; $dbPass = $Matches[2]
$dbHost = $Matches[3]; $dbPort = $Matches[4]; $dbName = $Matches[5]

$scratch    = "bas_restoretest"
$scratchDsn = "postgresql://${dbUser}:${dbPass}@${dbHost}:${dbPort}/$scratch"
$adminDsn   = "postgresql://${dbUser}:${dbPass}@${dbHost}:${dbPort}/postgres"

# --- Tools -----------------------------------------------------------------
$psql = (Get-Command psql -ErrorAction SilentlyContinue).Source
if (-not $psql) {
    $f = Get-ChildItem 'C:\Program Files\PostgreSQL' -Recurse -Filter 'psql.exe' -ErrorAction SilentlyContinue |
         Sort-Object FullName -Descending | Select-Object -First 1
    if ($f) { $psql = $f.FullName }
}
$pgRestore = $psql -replace 'psql(\.exe)?$', 'pg_restore$1'
if (-not $psql -or -not (Test-Path $pgRestore)) { Say "Could not find psql / pg_restore." 'Red'; exit 1 }

$failed = 0
$compared = 0

try {
    # --- Restore into a scratch database -----------------------------------
    Say "Creating scratch database '$scratch'..."
    # DROP DATABASE must be the ONLY statement in the -c argument. psql wraps
    # multiple statements in a single transaction, and DROP DATABASE cannot run
    # inside a transaction block - so combining it with anything else makes it
    # fail silently, leaving the scratch database behind to break the next run.
    & $psql $adminDsn -q -c "DROP DATABASE IF EXISTS $scratch" 2>&1 | Out-Null
    & $psql $adminDsn -q -c "CREATE DATABASE $scratch" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Say "Could not create scratch database '$scratch'." 'Red'
        Say "If it already exists from an interrupted run, remove it with:" 'Yellow'
        Say "  psql `"$adminDsn`" -c `"DROP DATABASE $scratch`"" 'Yellow'
        exit 1
    }

    Say "Restoring..."
    # --no-owner / --no-privileges so the restore does not depend on roles
    # existing on the target. That is what makes this dump portable to a
    # rebuilt machine, which is the scenario that matters.
    $out = & $pgRestore --dbname=$scratchDsn --no-owner --no-privileges $BackupFile 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Say "pg_restore reported problems:" 'Yellow'
        Write-Host $out
    }

    # --- Compare against live ----------------------------------------------
    Write-Host ""
    Say "Comparing restored copy against live database:" 'White'
    Write-Host ""
    Write-Host ("    {0,-22} {1,12} {2,12}   {3}" -f 'table', 'live', 'restored', '')

    foreach ($t in @('org','site','station','equipment','point','reading',
                     'sync_checkpoint','ingest_run','data_gap','point_role')) {
        $live = (& $psql $dsn        -tA -c "SELECT count(*) FROM bas.$t" 2>$null) -as [int]
        $rest = (& $psql $scratchDsn -tA -c "SELECT count(*) FROM bas.$t" 2>$null) -as [int]

        if ($null -eq $rest) {
            $verdict = 'MISSING'; $colour = 'Red'; $failed++
        } elseif ($live -eq $rest) {
            $verdict = 'match'; $colour = 'Green'
        } else {
            # A backup is a point-in-time snapshot, and the collector keeps
            # running while this test does. Every table the collector writes to
            # can therefore legitimately be AHEAD in the live database - not
            # just readings. The first version of this check only allowed drift
            # on 'reading' and reported a false failure the moment an
            # ingest_run row appeared between backup and test.
            #
            # Forward drift on a collector-written table is expected. Drift the
            # other way (restored ahead of live) means rows vanished from live
            # since the backup, which is always worth looking at.
            $drift = $live - $rest
            $growing = @('reading','ingest_run','sync_checkpoint','data_gap',
                         'point','station','site')

            if ($drift -gt 0 -and $growing -contains $t) {
                $verdict = "ok (+$drift since backup)"; $colour = 'Green'
            } elseif ($drift -lt 0) {
                $verdict = "RESTORED AHEAD OF LIVE ($drift)"; $colour = 'Red'; $failed++
            } else {
                $verdict = "MISMATCH ($drift)"; $colour = 'Red'; $failed++
            }
        }
        Write-Host ("    {0,-22} {1,12} {2,12}   " -f $t, $live, $rest) -NoNewline
        Write-Host $verdict -ForegroundColor $colour
        $compared++
    }

    # --- Views work too? ----------------------------------------------------
    Write-Host ""
    $views = (& $psql $scratchDsn -tA -c "SELECT count(*) FROM information_schema.views WHERE table_schema='bas'" 2>$null) -as [int]
    if ($views -ge 6) {
        Say "Views restored: $views" 'Green'
    } else {
        Say "Views restored: $views (expected at least 6)" 'Red'; $failed++
    }

    # A view that exists but does not run is not restored in any useful sense.
    $probe = (& $psql $scratchDsn -tA -c "SELECT count(*) FROM bas.v_reading" 2>&1)
    if ($LASTEXITCODE -eq 0) {
        Say "v_reading queryable in the restored copy" 'Green'
    } else {
        Say "v_reading FAILED in the restored copy: $probe" 'Red'; $failed++
    }

} finally {
    if (-not $Keep) {
        # DROP DATABASE must be the ONLY statement in the -c argument. psql wraps
    # multiple statements in a single transaction, and DROP DATABASE cannot run
    # inside a transaction block - so combining it with anything else makes it
    # fail silently, leaving the scratch database behind to break the next run.
    & $psql $adminDsn -q -c "DROP DATABASE IF EXISTS $scratch" 2>&1 | Out-Null
        Write-Host ""
        Say "Scratch database dropped. Live database untouched."
    } else {
        Write-Host ""
        Say "Scratch database '$scratch' left in place (-Keep)."
    }
}

# A verification that did not actually verify anything must not report success.
# The first version of this script threw on a bad format string, skipped the
# entire comparison loop, and still printed "RESTORE VERIFIED" - which is worse
# than having no check at all.
if ($compared -lt 10) {
    Write-Host ""
    Write-Host "  INCONCLUSIVE - only $compared of 10 tables were compared." -ForegroundColor Red
    Write-Host "  Something interrupted the comparison. Do not treat this backup as verified." -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host ""
if ($failed -eq 0) {
    Write-Host "  RESTORE VERIFIED - this backup can actually be recovered from." -ForegroundColor Green
    Write-Host ""
    exit 0
} else {
    Write-Host "  $failed PROBLEM(S) - do not rely on this backup." -ForegroundColor Red
    Write-Host ""
    exit 1
}
