$ErrorActionPreference = 'Stop'
$restartArguments = @($args)
. (Join-Path $PSScriptRoot 'scripts\resolve_web_launch.ps1')
$root = Resolve-WebLaunchRoot -SourceRoot $PSScriptRoot -EntryName 'restart-web.ps1'
if (-not [StringComparer]::OrdinalIgnoreCase.Equals($root, $PSScriptRoot)) {
    & (Join-Path $root 'restart-web.ps1') @restartArguments
    exit $LASTEXITCODE
}
Set-Location -LiteralPath $root
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Please run deploy.cmd first.' }
& $python (Join-Path $root 'scripts\restart_web.py') @restartArguments
exit $LASTEXITCODE
