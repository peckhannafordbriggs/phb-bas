<#
.SYNOPSIS
    Tests the dump-verification rule inside Backup-BasDatabase.ps1.

.DESCRIPTION
    Backup-BasDatabase.ps1 decides whether a dump is good by searching the
    pg_restore table of contents for core tables. That rule had two faults:

      1. It looked only for the standalone schema's names ("TABLE DATA bas
         reading"), so against the platform database - where the TOC says
         "TABLE DATA public bas_readings" - it reported failure every night
         while writing perfectly good dumps, and skipped rotation.

      2. "TABLE DATA bas point" is a substring of "TABLE DATA bas point_link",
         so a dump missing bas.point verified clean.

    Both are fixed by Test-TocHasTables. This script proves it, using table of
    contents text captured from real pg_dump output.

    It extracts the function from Backup-BasDatabase.ps1 rather than copying
    it, so it tests the shipped code and fails if someone edits the rule.

    Exits 0 if every case behaves correctly, 1 otherwise.
#>

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- Extract the real function from the real script ------------------------
$scriptPath = Join-Path $root 'Backup-BasDatabase.ps1'
if (-not (Test-Path $scriptPath)) {
    Write-Host "FAILED: cannot find $scriptPath"
    exit 1
}
$src = Get-Content $scriptPath -Raw
$m = [regex]::Match($src, '(?ms)^function Test-TocHasTables.*?^\}')
if (-not $m.Success) {
    Write-Host "FAILED: Test-TocHasTables not found in Backup-BasDatabase.ps1."
    Write-Host "        The verification rule has been changed or removed - re-check it by hand."
    exit 1
}
Invoke-Expression $m.Value

# The layout decision, mirroring the script. Kept here so the test states the
# expected outcome explicitly rather than re-running the whole backup.
$standaloneTables = @('bas reading', 'bas point', 'bas site', 'bas sync_checkpoint')
$platformTables   = @('public bas_readings', 'public bas_points',
                      'public bas_sites', 'public bas_sync_checkpoints')

function Get-Layout($toc) {
    if (Test-TocHasTables $toc $standaloneTables) { return 'standalone bas schema' }
    if (Test-TocHasTables $toc $platformTables)   { return 'platform public.bas_* tables' }
    return 'REFUSED'
}

# --- Fixtures: real pg_restore --list output, 24 August 2026 ---------------
# Trailing owner column ("bas") is what pg_restore emits; the rule's trailing
# \s relies on a whitespace character following the table name, and this is it.
$platformToc = @'
;
; Archive created at 2026-08-24 10:46:00 EDT
;     dbname: phb_platform
;
; Selected TOC Entries:
;
2783; 0 33076 TABLE DATA public _prisma_migrations bas
2780; 0 33058 TABLE DATA public audit_events bas
2776; 0 24690 TABLE DATA public bas_data_gaps bas
2767; 0 24626 TABLE DATA public bas_equipment bas
2765; 0 24610 TABLE DATA public bas_equipment_types bas
2778; 0 24700 TABLE DATA public bas_ingest_runs bas
2769; 0 24640 TABLE DATA public bas_orgs bas
2774; 0 24678 TABLE DATA public bas_point_links bas
2766; 0 24618 TABLE DATA public bas_point_roles bas
2772; 0 24660 TABLE DATA public bas_points bas
2773; 0 24670 TABLE DATA public bas_readings bas
2770; 0 24648 TABLE DATA public bas_sites bas
2771; 0 24654 TABLE DATA public bas_stations bas
2775; 0 24684 TABLE DATA public bas_sync_checkpoints bas
2781; 0 33064 TABLE DATA public employees bas
2782; 0 33070 TABLE DATA public module_grants bas
'@

$standaloneToc = @'
;
; Archive created at 2026-08-24 10:46:00 EDT
;     dbname: bas
;
; Selected TOC Entries:
;
2741; 0 32980 TABLE DATA bas data_gap bas
2732; 0 32847 TABLE DATA bas equipment bas
2723; 0 32769 TABLE DATA bas equipment_type bas
2739; 0 32960 TABLE DATA bas ingest_run bas
2726; 0 32790 TABLE DATA bas org bas
2734; 0 32880 TABLE DATA bas point bas
2736; 0 32922 TABLE DATA bas point_link bas
2724; 0 32777 TABLE DATA bas point_role bas
2735; 0 32908 TABLE DATA bas reading bas
2728; 0 32808 TABLE DATA bas site bas
2730; 0 32825 TABLE DATA bas station bas
2737; 0 32944 TABLE DATA bas sync_checkpoint bas
'@

# A dump that contains none of the BAS core tables.
$neitherToc = @'
;
; Selected TOC Entries:
;
2781; 0 33064 TABLE DATA public employees bas
'@

# The substring trap: bas.point_link is present, bas.point is NOT.
# The rule as originally shipped accepted this.
$trapToc = @'
;
; Selected TOC Entries:
;
2634; 0 32922 TABLE DATA bas point_link bas
2633; 0 32908 TABLE DATA bas reading bas
2632; 0 32808 TABLE DATA bas site bas
2635; 0 32944 TABLE DATA bas sync_checkpoint bas
'@

# A platform dump truncated so bas_points is missing - must be refused, and is
# the platform-side equivalent of the trap above.
$platformIncompleteToc = @'
;
; Selected TOC Entries:
;
2773; 0 24670 TABLE DATA public bas_readings bas
2774; 0 24678 TABLE DATA public bas_point_links bas
2770; 0 24648 TABLE DATA public bas_sites bas
2775; 0 24684 TABLE DATA public bas_sync_checkpoints bas
'@

$cases = @(
    @{ Name = 'platform database dump';                      Toc = $platformToc;           Expect = 'platform public.bas_* tables' },
    @{ Name = 'standalone bas database dump (no regression)'; Toc = $standaloneToc;         Expect = 'standalone bas schema' },
    @{ Name = 'dump with neither layout';                     Toc = $neitherToc;            Expect = 'REFUSED' },
    @{ Name = 'bas point_link present, bas point absent';     Toc = $trapToc;               Expect = 'REFUSED' },
    @{ Name = 'platform dump missing bas_points';             Toc = $platformIncompleteToc; Expect = 'REFUSED' }
)

$failed = 0
Write-Host ''
Write-Host 'Dump-verification rule'
Write-Host '----------------------'
foreach ($c in $cases) {
    $got = Get-Layout $c.Toc
    if ($got -eq $c.Expect) {
        Write-Host ("  PASS  {0}" -f $c.Name)
        Write-Host ("          -> {0}" -f $got)
    } else {
        $failed++
        Write-Host ("  FAIL  {0}" -f $c.Name)
        Write-Host ("          expected {0}, got {1}" -f $c.Expect, $got)
    }
}

Write-Host ''
if ($failed) {
    Write-Host ("{0} case(s) failed. Do not trust the nightly backup verification." -f $failed)
    exit 1
}
Write-Host ("All {0} cases behave correctly." -f $cases.Count)
Write-Host 'A platform dump verifies, a standalone dump still verifies, and a'
Write-Host 'dump missing a core table is refused rather than rubber-stamped.'
exit 0
