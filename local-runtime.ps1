# Process-local paths for this installation. Never changes the machine PATH.
$selectionRuntimeRoot = $PSScriptRoot
$selectionToolPaths = @()
foreach ($selectionToolsRoot in @('runtime\tools','deployment-assets\tools')) {
    foreach ($selectionToolName in @('node','powershell','codex')) {
        $selectionToolPath = Join-Path $selectionRuntimeRoot (Join-Path $selectionToolsRoot $selectionToolName)
        if (Test-Path -LiteralPath $selectionToolPath -PathType Container) { $selectionToolPaths += $selectionToolPath }
    }
}
$env:PATH = (($selectionToolPaths + @($env:PATH -split ';')) | Where-Object { $_ } | Select-Object -Unique) -join ';'
$env:TORCH_HOME = Join-Path $selectionRuntimeRoot 'runtime\models\torch'
$selectionBrowserCache = Join-Path $selectionRuntimeRoot 'deployment-assets\browsers'
if (Test-Path -LiteralPath $selectionBrowserCache -PathType Container) { $env:PLAYWRIGHT_BROWSERS_PATH = $selectionBrowserCache }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
