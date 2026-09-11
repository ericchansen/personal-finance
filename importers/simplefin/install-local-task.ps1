[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$DataDir,
    [string]$TaskName = 'Personal Finance - SimpleFIN local sync',
    [string]$At = '06:00',
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$data = (Resolve-Path -LiteralPath $DataDir).Path
$pythonExe = (Get-Command $Python -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $pythonExe `
    -Argument "-m importers.simplefin.local_sync --data-dir `"$data`"" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Fetch SimpleFIN and update the local Wealthfolio app.' `
    -Force | Out-Null
Write-Host "Installed '$TaskName' at $At. Disable any old SimpleFIN collectors or writers."
