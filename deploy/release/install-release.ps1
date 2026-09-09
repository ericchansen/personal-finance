[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [string]$ReleaseRoot = 'D:\documents\finance-runtime\personal-finance',
    [string]$RepositoryRoot = (Join-Path $PSScriptRoot '..\..'),
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'

function Test-PathWithin {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Parent
    )
    $candidate = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($Parent).TrimEnd([char[]]"\/")
    return $candidate.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $candidate.StartsWith(
            $root + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )
}

$repository = [IO.Path]::GetFullPath($RepositoryRoot)
$releaseRootPath = [IO.Path]::GetFullPath($ReleaseRoot)
if (Test-PathWithin -Path $releaseRootPath -Parent $repository) {
    throw 'The release root must be outside the repository.'
}
$dirty = @(& git -C $repository status --porcelain --untracked-files=all)
if ($LASTEXITCODE -ne 0 -or $dirty.Count -ne 0) {
    throw 'A stable release can only be installed from a clean committed worktree.'
}
$commit = (& git -C $repository rev-parse HEAD).Trim()
$tree = (& git -C $repository rev-parse 'HEAD^{tree}').Trim()
if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') {
    throw 'The release commit could not be resolved.'
}
$pythonExe = (Get-Command $Python -ErrorAction Stop).Source
$releases = Join-Path $releaseRootPath 'releases'
$target = Join-Path $releases $commit
$staging = Join-Path $releases ".staging-$([guid]::NewGuid().ToString('N'))"
$archive = Join-Path $releaseRootPath ".release-$([guid]::NewGuid().ToString('N')).zip"

if (-not $PSCmdlet.ShouldProcess($target, "Install immutable release $commit")) {
    return
}

New-Item -ItemType Directory -Force -Path $releases | Out-Null
try {
    if (-not (Test-Path -LiteralPath $target)) {
        & git -C $repository archive --format=zip --output=$archive $commit
        if ($LASTEXITCODE -ne 0) {
            throw 'git archive failed.'
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $staging
        Push-Location $staging
        try {
            & $pythonExe -B -c 'import finance_store.identity; import importers.simplefin.cli'
            if ($LASTEXITCODE -ne 0) {
                throw 'The staged release failed its import smoke test.'
            }
        }
        finally {
            Pop-Location
        }
        $fileCount = @(
            Get-ChildItem -LiteralPath $staging -File -Recurse
        ).Count
        $manifest = [ordered]@{
            schemaVersion = 1
            kind = 'personal-finance-release'
            commit = $commit
            tree = $tree
            archiveSha256 = (
                Get-FileHash -LiteralPath $archive -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            trackedFileCount = $fileCount
            installedAt = [DateTimeOffset]::UtcNow.ToString('o')
        }
        [IO.File]::WriteAllText(
            (Join-Path $staging 'release-manifest.json'),
            ($manifest | ConvertTo-Json -Depth 5) + "`n",
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $staging -Destination $target
    }
    $manifestPath = Join-Path $target 'release-manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.commit -ne $commit -or $manifest.tree -ne $tree) {
        throw 'An existing release directory does not match the current commit.'
    }
    $launcherSource = Join-Path $target 'deploy\release\run-source-collector.ps1'
    $launcher = Join-Path $releaseRootPath 'run-source-collector.ps1'
    Copy-Item -LiteralPath $launcherSource -Destination "$launcher.tmp" -Force
    Move-Item -LiteralPath "$launcher.tmp" -Destination $launcher -Force
    $pointer = [ordered]@{
        schemaVersion = 1
        kind = 'personal-finance-release-pointer'
        commit = $commit
        tree = $tree
        releasePath = $target
        manifestSha256 = (
            Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        pythonExecutable = $pythonExe
    }
    $pointerPath = Join-Path $releaseRootPath 'current.json'
    [IO.File]::WriteAllText(
        "$pointerPath.tmp",
        ($pointer | ConvertTo-Json -Depth 5) + "`n",
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath "$pointerPath.tmp" -Destination $pointerPath -Force
    Write-Output ($pointer | ConvertTo-Json -Compress)
}
finally {
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
}
