[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ReleaseRoot,
    [Parameter(Mandatory)]
    [string]$DataDir,
    [ValidateRange(1, 90)]
    [int]$Days = 90
)

$ErrorActionPreference = 'Stop'
$releaseRootPath = [IO.Path]::GetFullPath($ReleaseRoot)
$pointerPath = Join-Path $releaseRootPath 'current.json'
if (-not (Test-Path -LiteralPath $pointerPath -PathType Leaf)) {
    throw 'The installed release pointer is unavailable.'
}
$pointer = Get-Content -LiteralPath $pointerPath -Raw | ConvertFrom-Json
if (
    $pointer.schemaVersion -ne 1 -or
    [string]::IsNullOrWhiteSpace($pointer.commit) -or
    [string]::IsNullOrWhiteSpace($pointer.releasePath) -or
    [string]::IsNullOrWhiteSpace($pointer.manifestSha256) -or
    [string]::IsNullOrWhiteSpace($pointer.pythonExecutable)
) {
    throw 'The installed release pointer is invalid.'
}
$releasePath = [IO.Path]::GetFullPath([string]$pointer.releasePath)
$releasesPath = [IO.Path]::GetFullPath((Join-Path $releaseRootPath 'releases'))
if (-not $releasePath.StartsWith(
    $releasesPath + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw 'The installed release path escapes the release root.'
}
$manifestPath = Join-Path $releasePath 'release-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw 'The installed release manifest is unavailable.'
}
$manifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($manifestHash -ne [string]$pointer.manifestSha256) {
    throw 'The installed release manifest hash changed.'
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if (
    $manifest.commit -ne $pointer.commit -or
    $manifest.tree -ne $pointer.tree
) {
    throw 'The installed release pointer and manifest disagree.'
}

Remove-Item Env:\WEALTHFOLIO_MUTATIONS_ENABLED -ErrorAction SilentlyContinue
Remove-Item Env:\WEALTHFOLIO_WRITER_MODE -ErrorAction SilentlyContinue
Remove-Item Env:\WEALTHFOLIO_WRITER_ENVIRONMENT_ID -ErrorAction SilentlyContinue
$env:FINANCE_RELEASE_COMMIT = [string]$pointer.commit
$env:PYTHONDONTWRITEBYTECODE = '1'
Push-Location $releasePath
try {
    & ([string]$pointer.pythonExecutable) -B -m importers.simplefin.cli `
        --data-dir ([IO.Path]::GetFullPath($DataDir)) `
        pull-snapshot --days $Days
    if ($LASTEXITCODE -ne 0) {
        throw "Source collection failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
