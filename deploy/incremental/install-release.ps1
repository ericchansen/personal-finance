[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory)][string]$ReleaseRoot,
    [string]$RepositoryRoot = (Join-Path $PSScriptRoot '..\..'),
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$repository = [IO.Path]::GetFullPath($RepositoryRoot)
$target = [IO.Path]::GetFullPath($ReleaseRoot)
$pythonExe = (Get-Command $Python -ErrorAction Stop).Source
if ($PSCmdlet.ShouldProcess($target, 'Install a separate incremental archive; no marker or task activation')) {
    & $pythonExe -I -B (Join-Path $repository 'deploy\incremental\entrypoint.py') `
        install --repository $repository --release-root $target --python $pythonExe
    exit $LASTEXITCODE
}
