[CmdletBinding()]
param([string]$InstallPath, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$packageRoot = Split-Path -Parent $PSScriptRoot
try {
    if (-not $InstallPath) {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = '请选择已经部署的选品工作台目录'
        $dialog.ShowNewFolderButton = $false
        if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { exit 2 }
        $InstallPath = $dialog.SelectedPath
        $dialog.Dispose()
    }
    $InstallPath = [IO.Path]::GetFullPath($InstallPath)
    $python = Join-Path $InstallPath '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $InstallPath 'BUILD_INFO.json') -PathType Leaf)) {
        throw '所选目录不是已经部署的选品工作台，请先完成首次部署。'
    }
    $updateArguments = @((Join-Path $PSScriptRoot 'apply_update.py'), '--package-root', $packageRoot, '--install-root', $InstallPath)
    if ($CheckOnly) { $updateArguments += '--check' }
    & $python @updateArguments
    exit $LASTEXITCODE
} catch {
    Write-Host ('升级未完成：' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
