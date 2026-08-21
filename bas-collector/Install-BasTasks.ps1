<#
.SYNOPSIS
    Runs collection in the background, permanently. No PowerShell window.

.DESCRIPTION
    Creates three scheduled tasks:

      BAS Collector Sync    every 15 minutes, one "collector sync" per run
      BAS Health Check      every 30 minutes, alerts if something is wrong
      BAS Database Backup   nightly at 02:15, verified pg_dump

    All run hidden, survive reboot, and run whether or not you are logged in.

    WHY SCHEDULED RUNS RATHER THAN THE "collector run" LOOP

    "collector run" works, but it is a long-lived process, and long-lived
    processes die: a network hiccup, a memory issue, an unhandled edge case at
    3am. When it dies it stays dead until a human notices, and with a 42-hour
    roll horizon that silence costs real data.

    A scheduled "sync" has no such failure mode. Each run is independent and
    short. If one fails, the next runs fifteen minutes later and catches up,
    because the checkpoint only advances on committed data. Windows handles
    scheduling, restarts after reboot, and keeps its own execution history.

    Fewer moving parts, and a failure mode that self-heals.

    NOTE ON ENCODING: this file is deliberately pure ASCII with a BOM. Windows
    PowerShell 5.1 reads .ps1 files as ANSI unless a BOM is present, so any
    non-ASCII character (an em-dash, a curly quote) gets mangled into bytes that
    can break string parsing in ways the error message does not explain.

.PARAMETER Uninstall
    Remove all three tasks.

.NOTES
    Run this in an ELEVATED PowerShell (right-click, Run as Administrator).
#>

param([switch]$Uninstall)

$ErrorActionPreference = 'Continue'
# 'Continue', not 'Stop': with 'Stop', PowerShell turns any stderr output from a
# native command into a terminating error, so a harmless warning from python or
# psql aborts the script. Native tool success is $LASTEXITCODE, which this
# script checks explicitly.

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host ""
    Write-Host "  This needs an elevated PowerShell." -ForegroundColor Yellow
    Write-Host "  Close this, right-click PowerShell, choose 'Run as administrator', re-run."
    Write-Host ""
    exit 1
}

$root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$syncTask   = 'BAS Collector Sync'
$backupTask = 'BAS Database Backup'
$healthTask = 'BAS Health Check'

if ($Uninstall) {
    foreach ($t in @($syncTask, $healthTask, $backupTask)) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Write-Host "  Removed: $t"
        }
    }
    Write-Host ""
    Write-Host "  Done. Collection is no longer running in the background."
    Write-Host ""
    exit 0
}

# Locate Python by full path. A scheduled task does not inherit your
# interactive PATH, and "works in my terminal, fails as a task" is the most
# common way this goes wrong.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python -or $python -like '*WindowsApps*') {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
    )
    $python = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $python) {
    Write-Host ""
    Write-Host "  Could not find python.exe. Edit this script to set it explicitly." -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "  Python      $python"
Write-Host "  Project     $root"

# Pre-flight. Scheduling a broken collector just means it fails silently every
# fifteen minutes.
Push-Location $root
$check = & $python -m collector check 2>&1 | Out-String
Pop-Location

if ($check -notmatch 'OK') {
    Write-Host ""
    Write-Host "  'collector check' is not passing. Fix that first." -ForegroundColor Yellow
    Write-Host ""
    Write-Host $check
    exit 1
}
Write-Host "  Pre-flight  collector check passed"
Write-Host ""

$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# S4U runs without storing your password and without needing you logged in.
# Outbound TCP (the JACE, and Postgres on localhost) works fine under it.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

# --- Task 1: collection ---------------------------------------------------

$syncAction = New-ScheduledTaskAction -Execute $python -Argument '-m collector sync' -WorkingDirectory $root

$triggerBoot = New-ScheduledTaskTrigger -AtStartup
$triggerLoop = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $syncTask -Action $syncAction -Trigger @($triggerBoot, $triggerLoop) -Principal $principal -Settings $settings -Force -Description 'Collects Niagara trend history into PostgreSQL every 15 minutes. Read-only against the station.' | Out-Null

Write-Host "  Created: $syncTask          every 15 min, and at startup"

# --- Task 2: health check -------------------------------------------------

$healthScript = Join-Path $root 'Invoke-BasHealthCheck.ps1'
$healthArgs   = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $healthScript + '"'

$healthAction  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $healthArgs -WorkingDirectory $root
$healthTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $healthTask -Action $healthAction -Trigger $healthTrigger -Principal $principal -Settings $settings -Force -Description 'Alerts if BAS collection has stalled or data is about to be overwritten on the station.' | Out-Null

Write-Host "  Created: $healthTask            every 30 min"

# --- Task 3: nightly backup -----------------------------------------------
# 02:15 rather than a round hour: everything else in the world runs at 02:00,
# and a backup competing with whatever else the machine decided to do at
# exactly 2am is a backup that sometimes does not finish.
$backupScript = Join-Path $root 'Backup-BasDatabase.ps1'
$backupArgs   = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $backupScript + '"'

$backupAction  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $backupArgs -WorkingDirectory $root
$backupTrigger = New-ScheduledTaskTrigger -Daily -At '02:15'

$backupSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $backupTask -Action $backupAction -Trigger $backupTrigger -Principal $principal -Settings $backupSettings -Force -Description 'Nightly pg_dump of the BAS database. This data cannot be re-collected: the JACE overwrites its own history every ~42 hours.' | Out-Null

Write-Host "  Created: $backupTask        daily at 02:15"

Write-Host ""
Write-Host "  All three tasks are running in the background now." -ForegroundColor Green
Write-Host ""
Write-Host "  You can close every PowerShell window. Collection continues."
Write-Host "  It survives reboots and does not need you logged in."
Write-Host ""
Write-Host "  Check on it:"
Write-Host "    Get-ScheduledTask 'BAS*' | Get-ScheduledTaskInfo | Format-Table TaskName, LastRunTime, LastTaskResult"
Write-Host "    python -m collector status"
Write-Host "    Get-Content logs\healthcheck.log -Tail 30"
Write-Host ""
Write-Host "  Test that alerts actually reach you:"
Write-Host "    powershell -ExecutionPolicy Bypass -File .\Invoke-BasHealthCheck.ps1 -Force"
Write-Host ""
Write-Host "  Back up now, and prove the backup can actually be restored:"
Write-Host "    powershell -ExecutionPolicy Bypass -File .\Backup-BasDatabase.ps1"
Write-Host "    powershell -ExecutionPolicy Bypass -File .\Test-BasRestore.ps1"
Write-Host ""
Write-Host "  Stop it all:"
Write-Host "    powershell -ExecutionPolicy Bypass -File .\Install-BasTasks.ps1 -Uninstall"
Write-Host ""
