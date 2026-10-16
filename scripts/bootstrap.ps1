param([switch]$SkipNode)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Node = Get-Command node -ErrorAction SilentlyContinue
$Npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $SkipNode -and (-not $Node -or -not $Npm)) {
    throw 'Node.js 20+ and npm are required. Install Node.js LTS, then rerun bootstrap.ps1.'
}
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    py -3.12 -m venv (Join-Path $ProjectRoot '.venv')
}
& $Python -m pip install --upgrade pip
$InstallSpec = "${ProjectRoot}[test]"
& $Python -m pip install -e $InstallSpec
if (-not $SkipNode) {
    Push-Location $ProjectRoot
    try { npm install } finally { Pop-Location }
}
Write-Output "安装完成。下一步：& '$Python' scripts/pipeline.py validate --category config/categories/bath_towel_set.json"
