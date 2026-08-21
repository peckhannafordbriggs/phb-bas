<#
.SYNOPSIS
    Runs the health check and makes noise if something is wrong.

.DESCRIPTION
    Wraps healthcheck.py and turns its exit code into something a human will
    actually notice.

    Four delivery routes, deliberately in this order:

      1. logs\healthcheck.log   always written, first
      2. Windows Event Log      persists across reboots and weekends
      3. email                  optional, only if SMTP is configured
      4. desktop notification   cosmetic, Windows only, always last

    The order matters. A desktop notification is the least reliable route: it
    needs someone logged in and looking, and it depends on GUI assemblies being
    loadable. It runs last so a failure there cannot prevent the log entry, the
    event log entry, or the email.

    The event log entry matters more than it looks. A toast nobody is present to
    see is useless on a Sunday; the event log still has the timestamp on Monday.
    That is the difference between "collection stopped at some point" and
    "collection stopped Friday 18:40".

    Silent when healthy, so it can run every 30 minutes without becoming noise
    people learn to ignore.

    Pure ASCII with a BOM: Windows PowerShell 5.1 reads .ps1 as ANSI without
    one, and a stray non-ASCII byte becomes a parse error pointing at the
    wrong line.

.PARAMETER Force
    Notify even when healthy. For testing that alerts actually reach you.
#>

param([switch]$Force)

# 'Continue', not 'Stop'. With 'Stop', PowerShell turns any stderr output from a
# native command into a terminating error, so a harmless notice from python or
# psql aborts the script. Native tool success is $LASTEXITCODE, checked below.
$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir 'healthcheck.log'

# Resolve Python the same way the scheduled task does, so behaviour matches.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python -or $python -like '*WindowsApps*') {
    $python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
}

$script   = Join-Path $root 'healthcheck.py'
$output   = (& $python $script 2>&1) -join [Environment]::NewLine
$exitCode = $LASTEXITCODE

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
switch ($exitCode) {
    0       { $level = 'OK' }
    1       { $level = 'WARNING' }
    2       { $level = 'CRITICAL' }
    default { $level = 'ERROR' }
}

# --- 1. Log file, always ---------------------------------------------------
Add-Content -Path $logFile -Value "[$stamp] $level"
Add-Content -Path $logFile -Value $output

if ((Get-Item $logFile).Length -gt 2MB) {
    Move-Item $logFile "$logFile.1" -Force
}

if ($exitCode -eq 0 -and -not $Force) {
    Write-Host "[$stamp] OK"
    exit 0
}

Write-Host $output

# --- 2. Windows Event Log --------------------------------------------------
try {
    if (-not [System.Diagnostics.EventLog]::SourceExists('BAS Collector')) {
        New-EventLog -LogName Application -Source 'BAS Collector' -ErrorAction Stop
    }
    if ($exitCode -ge 2) { $entryType = 'Error' } else { $entryType = 'Warning' }
    Write-EventLog -LogName Application -Source 'BAS Collector' -EventId 1 -EntryType $entryType -Message $output
} catch {
    # Registering the source needs admin once. Not fatal.
    Add-Content -Path $logFile -Value "  (event log unavailable: $_)"
}

# --- 3. Email, if configured -----------------------------------------------
# Deliberately opt-in. PH+B is on Microsoft 365, which usually blocks basic SMTP
# auth, and a half-configured mail path that fails silently is worse than none.
if ($env:BAS_ALERT_TO -and $env:BAS_SMTP_SERVER) {
    try {
        if ($env:BAS_ALERT_FROM) { $from = $env:BAS_ALERT_FROM } else { $from = $env:BAS_ALERT_TO }
        if ($env:BAS_SMTP_PORT)  { $port = [int]$env:BAS_SMTP_PORT } else { $port = 587 }

        $mail = @{
            To         = $env:BAS_ALERT_TO
            From       = $from
            Subject    = "BAS collection: $level on $env:COMPUTERNAME"
            Body       = $output
            SmtpServer = $env:BAS_SMTP_SERVER
            Port       = $port
            UseSsl     = $true
        }
        if ($env:BAS_SMTP_USER) {
            $sec = ConvertTo-SecureString $env:BAS_SMTP_PASS -AsPlainText -Force
            $mail.Credential = New-Object System.Management.Automation.PSCredential($env:BAS_SMTP_USER, $sec)
        }
        Send-MailMessage @mail
        Add-Content -Path $logFile -Value "  (alert emailed to $env:BAS_ALERT_TO)"
    } catch {
        Add-Content -Path $logFile -Value "  (email failed: $_)"
    }
}

# --- 4. Desktop notification, Windows only, last ---------------------------
#
# Work out the headline first, in ordinary code. Only the GUI part is special.
$headline = $null
foreach ($line in ($output -split "`r?`n")) {
    if ($line -match '\[(WARNING|CRITICAL)\]') {
        $headline = $line -replace '^\s*\[\w+\]\s*', ''
        break
    }
}
if (-not $headline) { $headline = 'Check logs\healthcheck.log' }

# WHY THIS BLOCK IS A STRING AND NOT ORDINARY CODE
#
# A type literal like [System.Drawing.SystemIcons] is resolved when PowerShell
# COMPILES the script, not when that line runs. Compilation happens before the
# first statement executes. So if the assembly cannot load, the script dies at
# load time with "An error occurred while creating the pipeline" - before any
# output, before the log file is written, and crucially before the enclosing
# 'if' or 'try' has any chance to protect it. An unreachable line inside
# 'if ($false) { }' is enough to kill the script.
#
# On Windows that never happens, so the bug is invisible there. Anywhere the
# GUI assemblies are absent it takes down the whole health check, including the
# log file and the event log entry - the two routes that actually matter.
#
# [scriptblock]::Create compiles at RUNTIME. On a host that cannot load these
# types, the string is simply never compiled, and the failure is a caught
# exception rather than a dead script.
$notifyScript = @'
    param($exitCode, $level, $headline)
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    Add-Type -AssemblyName System.Drawing -ErrorAction Stop

    $icon = New-Object System.Windows.Forms.NotifyIcon
    if ($exitCode -ge 2) {
        $icon.Icon = [System.Drawing.SystemIcons]::Error
        $tipIcon   = [System.Windows.Forms.ToolTipIcon]::Error
    } else {
        $icon.Icon = [System.Drawing.SystemIcons]::Warning
        $tipIcon   = [System.Windows.Forms.ToolTipIcon]::Warning
    }
    $icon.Visible = $true

    # First finding only. A balloon tip truncates anything longer, and the full
    # detail is already in the log file.
    $icon.ShowBalloonTip(30000, "BAS collection: $level", $headline, $tipIcon)
    Start-Sleep -Seconds 12
    $icon.Dispose()
'@

# $IsWindows does not exist in Windows PowerShell 5.1, so it is $null there and
# this evaluates true. In PowerShell 7 on Linux it is $false and the block is
# skipped entirely.
if ($IsWindows -ne $false) {
    try {
        $notify = [scriptblock]::Create($notifyScript)
        & $notify $exitCode $level $headline
    } catch {
        Add-Content -Path $logFile -Value "  (notification unavailable: $_)"
    }
}

# Exit 0 whenever the CHECK ITSELF ran, regardless of what it found.
#
# Task Scheduler surfaces the exit code as LastTaskResult, so returning 1 for a
# warning makes the task list report the health check as failed when it did
# exactly its job. That trains you to ignore a red LastTaskResult, which is the
# wrong instinct for the one task whose silent failure means data loss.
#
# Non-zero here is reserved for the check being unable to run at all.
if ($exitCode -le 2) { exit 0 } else { exit $exitCode }
