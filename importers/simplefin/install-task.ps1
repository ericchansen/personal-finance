[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [string]$TaskName = 'Personal Finance - SimpleFIN plan',
    [string]$DataDir = 'D:\documents\finance-data',
    [string]$At = '06:00',
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$cli = Join-Path $PSScriptRoot 'cli.py'
$pythonExe = (Get-Command $Python -ErrorAction Stop).Source
$arguments = "`"$cli`" pull-plan --data-dir `"$DataDir`""
$action = New-ScheduledTaskAction -Execute $pythonExe -Argument $arguments `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew

if ($PSCmdlet.ShouldProcess($TaskName, 'Install daily SimpleFIN plan-only task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description 'Fetch immutable SimpleFIN snapshot and write a dry-run import/drift plan.' `
        -Force | Out-Null
    Write-Host "Installed scheduled task '$TaskName'."
}
