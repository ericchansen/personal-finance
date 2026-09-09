[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [string]$TaskName = 'Personal Finance - SimpleFIN source collection',
    [string]$DataDir = 'D:\documents\finance-data',
    [string]$ReleaseRoot = 'D:\documents\finance-runtime\personal-finance',
    [string]$At = '06:00',
    [string]$PowerShell = 'powershell.exe',
    [ValidateRange(1, 90)]
    [int]$Days = 90
)

$ErrorActionPreference = 'Stop'
$releaseRootPath = [IO.Path]::GetFullPath($ReleaseRoot)
$launcher = Join-Path $releaseRootPath 'run-source-collector.ps1'
$pointer = Join-Path $releaseRootPath 'current.json'
if (
    -not (Test-Path -LiteralPath $launcher -PathType Leaf) -or
    -not (Test-Path -LiteralPath $pointer -PathType Leaf)
) {
    throw 'Install a stable release before registering the collector task.'
}
$powerShellExe = (Get-Command $PowerShell -ErrorAction Stop).Source
$arguments = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', "`"$launcher`"",
    '-ReleaseRoot', "`"$releaseRootPath`"",
    '-DataDir', "`"$([IO.Path]::GetFullPath($DataDir))`"",
    '-Days', $Days
)
$action = New-ScheduledTaskAction -Execute $powerShellExe `
    -Argument ($arguments -join ' ') -WorkingDirectory $releaseRootPath
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew

if ($PSCmdlet.ShouldProcess($TaskName, 'Install daily source-only SimpleFIN task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description 'Fetch immutable SimpleFIN source evidence for PostgreSQL authority ingestion.' `
        -Force | Out-Null
    Write-Host "Installed scheduled task '$TaskName'."
}
