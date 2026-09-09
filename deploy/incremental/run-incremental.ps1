[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ReleaseRoot,
    [Parameter(Mandatory)][string]$Configuration,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$ConfigurationSha256,
    [ValidateSet('run', 'plan', 'status')][string]$Command = 'status',
    [switch]$EnableMutations
)
$ErrorActionPreference = 'Stop'

function Assert-RegularPath([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    while ($null -ne $item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse path refused.' }
        if ($item -is [IO.FileInfo]) { $item = $item.Directory } else { $item = $item.Parent }
    }
}
function File-Hash([string]$Path) {
    Assert-RegularPath $Path
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

try {
    $root = [IO.Path]::GetFullPath($ReleaseRoot).TrimEnd([char[]]"\/")
    Assert-RegularPath $root
    $pointerPath = Join-Path $root 'incremental-current.json'
    Assert-RegularPath $pointerPath
    $pointer = Get-Content -LiteralPath $pointerPath -Raw | ConvertFrom-Json
    if ($pointer.schemaVersion -ne 1 -or $pointer.kind -ne 'personal-finance-incremental-pointer' `
        -or $pointer.commit -cnotmatch '^[0-9a-f]{40}([0-9a-f]{24})?$') { throw 'Invalid pointer.' }
    $release = Join-Path (Join-Path $root 'incremental-releases') $pointer.commit
    if ([IO.Path]::GetFullPath($pointer.releasePath) -cne $release) { throw 'Invalid release path.' }
    $manifestPath = Join-Path $release 'incremental-release-manifest.json'
    if ((File-Hash $manifestPath) -cne $pointer.manifestSha256) { throw 'Manifest drift.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.schemaVersion -ne 1 -or $manifest.kind -ne 'personal-finance-incremental-release' `
        -or $manifest.commit -cne $pointer.commit -or $manifest.tree -cne $pointer.tree) { throw 'Manifest mismatch.' }
    $expected = @{}
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $name = $entry.Name
        if ($name -match '[:\\]' -or $name.StartsWith('/') -or $name -match '(^|/)(\.\.?|\.git)(/|$)' `
            -or $name -match '[. ](/|$)' -or $name.Contains('//') -or $expected.ContainsKey($name)) {
            throw 'Invalid manifest path.'
        }
        $expected[$name] = $entry.Value
    }
    if ($expected.Count -eq 0) { throw 'Empty manifest.' }
    $actual = @{}
    foreach ($item in Get-ChildItem -LiteralPath $release -Recurse -Force) {
        Assert-RegularPath $item.FullName
        if ($item.PSIsContainer) { continue }
        $name = $item.FullName.Substring($release.Length + 1).Replace('\', '/')
        if ($name -eq 'incremental-release-manifest.json') { continue }
        if (-not $expected.ContainsKey($name) -or (File-Hash $item.FullName) -cne $expected[$name]) {
            throw 'Release file drift.'
        }
        $actual[$name] = $true
    }
    if ($actual.Count -ne $expected.Count) { throw 'Missing release file.' }
    $launcherHash = File-Hash (Join-Path $root 'run-incremental.ps1')
    if ($launcherHash -cne $pointer.launcherSha256 `
        -or $launcherHash -cne $expected['deploy/incremental/run-incremental.ps1']) { throw 'Launcher drift.' }
    Assert-RegularPath $pointer.pythonExecutable
    if (-not [IO.Path]::IsPathRooted($pointer.pythonExecutable)) { throw 'Invalid interpreter.' }
    if ((File-Hash ([IO.Path]::GetFullPath($Configuration))) -cne $ConfigurationSha256) { throw 'Configuration drift.' }
    $arguments = @(
        '-I', '-B', (Join-Path $release 'deploy\incremental\entrypoint.py'),
        '--release-root', $root, '--configuration', ([IO.Path]::GetFullPath($Configuration)),
        '--configuration-sha256', $ConfigurationSha256, '--command', $Command
    )
    if ($EnableMutations) { $arguments += '--enable-mutations' }
    & ([string]$pointer.pythonExecutable) @arguments
    exit $LASTEXITCODE
}
catch {
    # Never emit exception text: filesystem/configuration values may be private.
    Write-Output '{"state":"held","reason":"incremental-launcher-preflight-failed"}'
    exit 1
}
