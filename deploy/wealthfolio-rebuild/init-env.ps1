# Generate external configuration for the isolated rebuild candidate.

[CmdletBinding()]
param(
    [string]$DataDir = (Join-Path $env:TEMP 'personal-finance\synthetic-rebuild\wealthfolio'),
    [string]$EnvFile,
    [string]$BindAddr = '127.0.0.1',
    [int]$Port = 18091
)

$ErrorActionPreference = 'Stop'

function Test-PathWithin {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [string]$Parent
    )

    $comparison = if (
        [System.Environment]::OSVersion.Platform -eq
        [System.PlatformID]::Win32NT
    ) {
        [System.StringComparison]::OrdinalIgnoreCase
    }
    else {
        [System.StringComparison]::Ordinal
    }
    $trimCharacters = [char[]]"\/"
    $normalizedPath = [System.IO.Path]::GetFullPath($Path)
    $normalizedParent = [System.IO.Path]::GetFullPath($Parent).TrimEnd(
        $trimCharacters
    )
    $prefix = $normalizedParent + [System.IO.Path]::DirectorySeparatorChar
    return (
        $normalizedPath.Equals($normalizedParent, $comparison) -or
        $normalizedPath.StartsWith($prefix, $comparison)
    )
}

function Write-NewPrivateFile {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [string]$Content,
        [Parameter(Mandatory)]
        [System.Text.Encoding]$Encoding
    )

    $stream = $null
    $created = $false
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $created = $true
        $bytes = $Encoding.GetBytes($Content)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    catch {
        if ($null -ne $stream) {
            $stream.Dispose()
            $stream = $null
        }
        if ($created) {
            Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        }
        throw
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

if ($Port -eq 8088) {
    throw 'The rebuild candidate refuses production port 8088.'
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$dataPath = [System.IO.Path]::GetFullPath($DataDir)
$envPath = if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    Join-Path $dataPath 'wealthfolio-rebuild.env'
}
else {
    [System.IO.Path]::GetFullPath($EnvFile)
}

if (Test-PathWithin -Path $dataPath -Parent $repoRoot) {
    throw 'The rebuild data directory must be outside the public checkout.'
}
if (Test-PathWithin -Path $envPath -Parent $repoRoot) {
    throw 'The rebuild environment file must be outside the public checkout.'
}

$passwordFile = Join-Path $dataPath 'ADMIN-PASSWORD.txt'
foreach ($path in ($envPath, $passwordFile)) {
    if (Test-Path -LiteralPath $path) {
        throw "Private configuration already exists at $path; refusing to overwrite it."
    }
}

$passwordBytes = [byte[]]::new(18)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($passwordBytes)
$password = [Convert]::ToBase64String($passwordBytes).TrimEnd('=')

$keyBytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($keyBytes)
$secretKey = [Convert]::ToBase64String($keyBytes)

$hash = $password | python -c @"
import sys
from argon2 import PasswordHasher
sys.stdout.write(PasswordHasher().hash(sys.stdin.read().rstrip('\n')))
"@
if (-not $hash -or $hash -notlike '`$argon2id`$*') {
    throw 'Failed to generate Argon2id hash; install argon2-cffi.'
}

New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
New-Item -ItemType Directory -Path (
    [System.IO.Path]::GetDirectoryName($envPath)
) -Force | Out-Null

$envContent = @"
# Generated clean-rebuild candidate configuration. Keep outside the checkout.
WF_REBUILD_DATA_DIR=$dataPath
WF_REBUILD_BIND_ADDR=$BindAddr
WF_REBUILD_PORT=$Port
WF_REBUILD_SECRET_KEY=$secretKey
WF_REBUILD_AUTH_PASSWORD_HASH='$hash'
WF_REBUILD_CORS_ALLOW_ORIGINS=http://localhost:$Port,http://127.0.0.1:$Port
"@

$passwordCreated = $false
try {
    Write-NewPrivateFile -Path $passwordFile -Content "$password`n" `
        -Encoding ([System.Text.Encoding]::ASCII)
    $passwordCreated = $true
    Write-NewPrivateFile -Path $envPath -Content $envContent `
        -Encoding ([System.Text.UTF8Encoding]::new($false))
}
catch {
    if ($passwordCreated) {
        Remove-Item -LiteralPath $passwordFile -Force -ErrorAction SilentlyContinue
    }
    throw
}

$composePath = Join-Path $PSScriptRoot 'compose.yml'
Write-Host "External candidate environment: $envPath"
Write-Host "External password file: $passwordFile"
Write-Host (
    'Start with: docker compose --env-file "{0}" -f "{1}" up -d' -f
    $envPath, $composePath
)
