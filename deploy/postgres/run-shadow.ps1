[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$DataDir,
    [string]$EnvironmentMarker = 'shadow-authority-v1',
    [string]$Python = 'python',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$arguments = @(
    '-m', 'finance_store.cli',
    '--data-dir', $DataDir,
    '--environment', $EnvironmentMarker,
    'scheduled-run'
)
$backupBasename = $null
$lockDirectory = Join-Path $DataDir 'postgres-shadow'
New-Item -ItemType Directory -Force $lockDirectory | Out-Null
$lockPath = Join-Path $lockDirectory 'scheduler.lock'
$lock = $null
$locationPushed = $false

try {
    try {
        $lock = [IO.File]::Open(
            $lockPath,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    } catch [IO.IOException] {
        throw 'Another PostgreSQL shadow scheduler run is active.'
    }

    if ($Apply) {
        $backupOutput = & docker compose `
            --project-directory $PSScriptRoot `
            -f (Join-Path $PSScriptRoot 'compose.yml') `
            --profile ops run --rm backup
        if ($LASTEXITCODE -ne 0) {
            throw 'Shadow backup failed; scheduled apply was not attempted.'
        }
        $created = @($backupOutput) |
            Where-Object { $_ -match '^Backup created: ([A-Za-z0-9._-]+)$' } |
            Select-Object -Last 1
        if (-not $created) {
            throw 'Shadow backup did not return a verified backup basename.'
        }
        $backupBasename = [regex]::Match(
            $created, '^Backup created: ([A-Za-z0-9._-]+)$'
        ).Groups[1].Value
        $arguments += @('--apply', '--backup-basename', $backupBasename)
        $env:FINANCE_SHADOW_ENVIRONMENT = $EnvironmentMarker
        $env:FINANCE_SHADOW_MUTATIONS_ENABLED = 'apply-reviewed-plan'
    }

    Push-Location $repoRoot
    $locationPushed = $true
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Shadow scheduled run exited with code $LASTEXITCODE."
    }
} finally {
    if ($locationPushed) {
        Pop-Location
    }
    if ($lock) {
        $lock.Dispose()
    }
    Remove-Item Env:FINANCE_SHADOW_MUTATIONS_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:FINANCE_SHADOW_ENVIRONMENT -ErrorAction SilentlyContinue
}
