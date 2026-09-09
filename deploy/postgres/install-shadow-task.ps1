[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [string]$TaskName = 'Personal Finance - PostgreSQL shadow authority',
    [string]$DataDir = 'D:\finance-data',
    [ValidateRange(0, 23)]
    [int]$Hour = 4,
    [ValidateRange(0, 59)]
    [int]$Minute = 17,
    [string]$PowerShell = 'powershell.exe',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run-shadow.ps1'
$powerShellExe = (Get-Command $PowerShell -ErrorAction Stop).Source
$arguments = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', "`"$runner`"",
    '-DataDir', "`"$DataDir`""
)
if ($Apply) {
    $arguments += '-Apply'
}
$action = New-ScheduledTaskAction `
    -Execute $powerShellExe `
    -Argument ($arguments -join ' ') `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger `
    -Daily `
    -At ([datetime]::Today.AddHours($Hour).AddMinutes($Minute))
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
$description = if ($Apply) {
    'Back up, seal, and apply immutable evidence to the isolated PostgreSQL shadow database.'
} else {
    'Verify and seal a read-only PostgreSQL shadow ingest plan.'
}

if ($PSCmdlet.ShouldProcess($TaskName, 'Install PostgreSQL shadow task')) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description $description `
        -Force | Out-Null
    Write-Host "Installed scheduled task '$TaskName' at $Hour`:$('{0:D2}' -f $Minute)."
}
