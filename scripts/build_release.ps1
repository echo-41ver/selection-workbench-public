[CmdletBinding()]
param([string]$DistPath)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '构建需要本机项目 Python，请先安装依赖。' }
$arguments = @((Join-Path $PSScriptRoot 'build_delivery_packages.py'))
if ($DistPath) { $arguments += @('--dist', $DistPath) }
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw '部署包未通过构建检查，旧包保留。' }
