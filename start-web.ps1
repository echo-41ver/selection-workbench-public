[CmdletBinding()]
param([ValidateRange(0,65535)][int]$Port = 8765, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts/resolve_web_launch.ps1')
$root = Resolve-WebLaunchRoot -SourceRoot $PSScriptRoot -EntryName 'start-web.ps1'
if (-not [StringComparer]::OrdinalIgnoreCase.Equals($root, $PSScriptRoot)) {
    & (Join-Path $root 'start-web.ps1') @PSBoundParameters
    exit $LASTEXITCODE
}
Set-Location -LiteralPath $root
$appAliases = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
$env:PATH = [Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH','User') + ';' + $appAliases + ';' + $env:PATH
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:TORCH_HOME = Join-Path $root 'runtime\models\torch'
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Python 环境缺失；请先运行 deploy.cmd' }
& $python (Join-Path $root 'scripts\ensure_node_runtime.py')
if ($LASTEXITCODE -ne 0) { throw '网页采集依赖未就绪；请按上方提示修复后重试' }
& $python (Join-Path $root 'scripts\verify_deployment.py')
if ($LASTEXITCODE -ne 0) { throw '完整选品流程尚未就绪；请按上方提示修复后重试' }
$arguments = @((Join-Path $root 'scripts\restart_web.py'),'--ensure-running','--port',[string]$Port)
if (-not $NoBrowser) { $arguments += '--open-browser' }
& $python @arguments
exit $LASTEXITCODE
