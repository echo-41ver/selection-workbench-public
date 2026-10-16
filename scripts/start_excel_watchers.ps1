param(
    [ValidatePattern('^[A-Za-z0-9_-]+$')][string]$CategoryKey = 'bath_towel_set',
    [int]$TemuIntervalSeconds = 60,
    [int]$Layer12IntervalSeconds = 300,
    [int]$Layer123IntervalSeconds = 300
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw '请先运行 install.ps1 安装本项目环境'
}
$WatcherScript = Join-Path $Root 'scripts\watch_excel_output.py'
$StateRoot = Join-Path $Root "runs\$CategoryKey\excel_watch"
New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null

function Test-WatcherIdentity($Process, [string]$Kind, [bool]$AllowRelative = $false) {
    if (-not $Process -or $Process.Name -notmatch '^python(?:w)?\.exe$') { return $false }
    $command = [string]$Process.CommandLine
    $scriptMatches = $command.IndexOf($WatcherScript, [StringComparison]::OrdinalIgnoreCase) -ge 0
    if ($AllowRelative -and -not $scriptMatches) {
        $scriptMatches = $command -match '(?:^|\s)"?scripts[\\/]watch_excel_output\.py"?(?:\s|$)'
    }
    return $scriptMatches -and
        $command -match ('--kind\s+"?' + [regex]::Escape($Kind) + '"?(?:\s|$)') -and
        $command -match ('--category-key\s+"?' + [regex]::Escape($CategoryKey) + '"?(?:\s|$)')
}

# Serialise launchers as well as checking the running watcher identity. A
# repeated launch must not truncate logs before the Python instance lock runs.
$launchLock = $null
try {
    $launchLock = [IO.File]::Open((Join-Path $StateRoot 'launcher.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
} catch [IO.IOException] {
    Write-Output '另一个Excel启动器正在运行，本次跳过。'
    exit 75
}
try {
$specs = @(
    @{ Kind='temu'; Interval=$TemuIntervalSeconds },
    @{ Kind='layer12'; Interval=$Layer12IntervalSeconds },
    @{ Kind='layer123'; Interval=$Layer123IntervalSeconds }
)
foreach ($spec in $specs) {
    $processes = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'")
    $pidPath = Join-Path $StateRoot ($spec.Kind + '.pid')
    $watcherPid = 0
    if (Test-Path -LiteralPath $pidPath) {
        [int]::TryParse(([string](Get-Content -LiteralPath $pidPath -Raw)).Trim(), [ref]$watcherPid) | Out-Null
    }
    $existing = @($processes | Where-Object {
        ($_.ProcessId -eq $watcherPid -and (Test-WatcherIdentity $_ $spec.Kind $true)) -or
        (Test-WatcherIdentity $_ $spec.Kind)
    })
    if ($existing.Count -gt 0) {
        Write-Output ($spec.Kind + ' Excel watcher 已运行，保留现有进程和日志。PID=' + ($existing.ProcessId -join ','))
        continue
    }
    $log = Join-Path $StateRoot ($spec.Kind + '.log')
    $err = Join-Path $StateRoot ($spec.Kind + '.err.log')
    $arguments = @(
        ('"' + $WatcherScript + '"'),
        '--kind', $spec.Kind,
        '--category-key', $CategoryKey,
        '--interval', [string]$spec.Interval
    )
    Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err | Out-Null
}
Start-Sleep -Seconds 2
Get-ChildItem -LiteralPath $StateRoot -Filter '*.json' -ErrorAction SilentlyContinue | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw }
} finally {
    if ($launchLock) { $launchLock.Dispose() }
}
