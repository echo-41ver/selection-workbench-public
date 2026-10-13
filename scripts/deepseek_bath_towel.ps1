[CmdletBinding()]
param(
    [ValidateSet('Start','Sample','Status','Stop')]
    [string]$Action = 'Status',
    [ValidateRange(1,4)]
    [int]$Workers = 4,
    [switch]$SkipSampleGate,
    [switch]$RefreshSnapshot
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Base = Join-Path $Root 'runs\bath_towel_set'
$RunDir = Join-Path $Base 'stage123\deepseek_bath_towel_200'
$BatchRunDir = Join-Path $Base 'stage123\deepseek_batch12_remaining'
$SampleDir = Join-Path $BatchRunDir 'sample_13'
$PidFile = Join-Path $BatchRunDir 'runner.pid'
$LogOut = Join-Path $BatchRunDir 'runner.stdout.log'
$LogErr = Join-Path $BatchRunDir 'runner.stderr.log'
$Candidate = Join-Path $Base 'stage12_v2\layer3_candidates_bath_towel_set.jsonl'
$Config = Join-Path $Base 'stage12_config.json'
$Results = Join-Path $Base 'stage123\results.jsonl'
$FinalExcel = Join-Path $Base 'excel\浴巾套装_第一二三层.xlsx'
$FrozenCandidates = Join-Path $RunDir 'frozen\layer3_candidates_bath_towel_set_with_revision.jsonl'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { $Python = $command.Source }
}

function Require-Python {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "找不到项目Python：请先运行 scripts\bootstrap.ps1。"
    }
}

New-Item -ItemType Directory -Force -Path $RunDir,$BatchRunDir | Out-Null

function Set-DeepSeekEnvironment {
    param([switch]$AllowMissingKey)
    $userKey = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
    if (-not $AllowMissingKey -and [string]::IsNullOrWhiteSpace($userKey)) {
        throw '没有找到DEEPSEEK_API_KEY；没有启动任何API请求。'
    }
    # Always replace a stale key inherited from Codex/PowerShell.  The
    # persisted User-scope key is the single source of truth for each launch.
    $env:DEEPSEEK_API_KEY = $userKey
    # The SDK uses the OpenAI-compatible variable, but this category is locked
    # to DeepSeek.  Always override a stale OpenAI key in this child process.
    $env:OPENAI_API_KEY = $env:DEEPSEEK_API_KEY
    $env:OPENAI_BASE_URL = 'https://api.deepseek.com'
    $env:OPENAI_VISION_MODEL = 'deepseek-flash'
    $env:DEEPSEEK_REASONING_EFFORT = 'low'
    $env:LAYER3_WORKERS = [string]$Workers
}

function Read-JsonIfExists([string]$Path) {
    if (Test-Path -LiteralPath $Path) { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    return $null
}

function Existing-RunnerProcess {
    # Verify command identity as well as PID: Windows can reuse a stopped PID.
    $runnerScript = Join-Path $Root 'scripts\run_deepseek_bath_towel_batch12_remaining.py'
    $processes = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($runnerScript) })
    foreach ($process in $processes) {
        try { return Get-Process -Id ([int]$process.ProcessId) -ErrorAction Stop } catch { }
    }
    return $null
}

function Get-RemainingPlan {
    $output = & $Python (Join-Path $Root 'scripts\run_deepseek_bath_towel_batch12_remaining.py') --candidates $FrozenCandidates --results $Results --plan-only
    if ($LASTEXITCODE -ne 0) { throw '共享缓存检查失败；未启动API请求。' }
    return ($output | ConvertFrom-Json)
}

function Ensure-FrozenSnapshot {
    $snapshotArgs = @(
        (Join-Path $Root 'scripts\run_deepseek_bath_towel_layer3.py'),
        '--candidate-file', $Candidate, '--stage12-config', $Config,
        '--run-dir', $RunDir, '--results', $Results, '--final-excel', $FinalExcel,
        '--workers', '1', '--validate-only', '--no-excel'
    )
    if ($RefreshSnapshot) { $snapshotArgs += '--refresh-snapshot' }
    & $Python @snapshotArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $FrozenCandidates)) {
        throw '第三层冻结快照失败；没有启动API请求。'
    }
}

function Ensure-Layer123Watcher {
    $watchRoot = Join-Path $Base 'excel_watch'
    $watchPid = Join-Path $watchRoot 'layer123.pid'
    if (Test-Path -LiteralPath $watchPid) {
        try { $existingPid = [int](Get-Content -LiteralPath $watchPid -Raw).Trim() } catch { $existingPid = 0 }
        if ($existingPid -gt 0) {
            try { Get-Process -Id $existingPid -ErrorAction Stop | Out-Null; return } catch { }
        }
        Remove-Item -LiteralPath $watchPid -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Force -Path $watchRoot | Out-Null
    Start-Process -FilePath $Python -ArgumentList @(
        (Join-Path $Root 'scripts\watch_excel_output.py'), '--kind', 'layer123',
        '--category-key', 'bath_towel_set', '--interval', '300'
    ) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $watchRoot 'layer123.log') -RedirectStandardError (Join-Path $watchRoot 'layer123.err.log') | Out-Null
}

$launchMutex = $null
$lockTaken = $false
if ($Action -in @('Start','Sample')) {
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try { $lockName = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant()))).Replace('-','') } finally { $hash.Dispose() }
    $launchMutex = [Threading.Mutex]::new($false, "Local\BathTowelLaunch-$lockName")
    try { $lockTaken = $launchMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $lockTaken = $true }
    if (-not $lockTaken) { $launchMutex.Dispose(); throw '另一个启动、样本或停止操作正在执行，请勿重复启动。' }
}
try {
switch ($Action) {
    'Status' {
        $state = Read-JsonIfExists (Join-Path $BatchRunDir 'summary.json')
        $preflight = Read-JsonIfExists (Join-Path $RunDir 'preflight.json')
        $runner = Existing-RunnerProcess
        [pscustomobject]@{
            runner_running = [bool]$runner
            runner_pid = if ($runner) { $runner.Id } else { $null }
            state = if ($state) { if ([int]$state.failed_pairs -eq 0) { 'COMPLETE' } else { 'PARTIAL' } } else { 'NOT_STARTED' }
            preflight = if ($preflight) { $preflight.status } else { 'NOT_RUN' }
            results = Test-Path -LiteralPath $Results
            excel = Test-Path -LiteralPath $FinalExcel
            sample_gate = (Test-Path -LiteralPath (Join-Path $SampleDir 'sample_report.json'))
        } | ConvertTo-Json -Depth 5
        break
    }
    'Stop' {
        $runner = Existing-RunnerProcess
        if ($runner) {
            Stop-Process -Id $runner.Id -Force
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
            Write-Output "已停止浴巾套装DeepSeek运行器 PID=$($runner.Id)"
        } else {
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
            Write-Output '没有正在运行的浴巾套装DeepSeek运行器'
        }
        break
    }
    'Sample' {
        Require-Python
        $runner = Existing-RunnerProcess
        if ($runner) { Write-Output "运行器已经在后台运行 PID=$($runner.Id)"; break }
        Set-DeepSeekEnvironment -AllowMissingKey
        Ensure-FrozenSnapshot
        $plan = Get-RemainingPlan
        if ([int]$plan.remaining_pairs -eq 0) {
            [pscustomobject]@{ status='COMPLETE'; cached_success=$plan.cached_success; paid_api_requests=0 } | ConvertTo-Json
            break
        }
        Set-DeepSeekEnvironment
        & $Python (Join-Path $Root 'scripts\preflight_deepseek_bath_towel.py') --candidate-file $Candidate --run-dir $RunDir --workers $Workers
        if ($LASTEXITCODE -ne 0) { throw '本地预检未通过；没有启动样本请求。' }
        New-Item -ItemType Directory -Force -Path $SampleDir | Out-Null
        $sampleSize = [Math]::Min(13, [int]$plan.remaining_pairs)
        & $Python (Join-Path $Root 'scripts\run_deepseek_bath_towel_batch12_remaining.py') --candidates $FrozenCandidates --results $Results --run-dir $SampleDir --batch-size 12 --workers $Workers --limit-pairs $sampleSize
        if ($LASTEXITCODE -ne 0) { throw '13对样本请求未完成，未生成通过门禁。' }
        $summary = Read-JsonIfExists (Join-Path $SampleDir 'summary.json')
        if (-not $summary) { throw '样本运行缺少summary.json。' }
        $pass = ([int]$summary.success_pairs -eq $sampleSize -and [int]$summary.failed_pairs -eq 0)
        $report = [ordered]@{ schema_version='deepseek-bath-towel-batch-sample-gate-v3'; status=if($pass){'PASS'}else{'FAIL'}; candidate_set_key=$plan.candidate_set_key; sample_size=$sampleSize; success=[int]$summary.success_pairs; failed=[int]$summary.failed_pairs; completed_at=(Get-Date).ToUniversalTime().ToString('o'); requests=[int]$summary.provider_requests }
        $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $SampleDir 'sample_report.json') -Encoding UTF8
        $report | ConvertTo-Json -Depth 5
        if (-not $pass) { throw '样本门禁未通过；正式运行未启动。' }
        break
    }
    'Start' {
        Require-Python
        $runner = Existing-RunnerProcess
        if ($runner) { Write-Output "运行器已经在后台运行 PID=$($runner.Id)"; break }
        Set-DeepSeekEnvironment -AllowMissingKey
        Ensure-FrozenSnapshot
        $plan = Get-RemainingPlan
        if ([int]$plan.remaining_pairs -eq 0) {
            [pscustomobject]@{ status='COMPLETE'; cached_success=$plan.cached_success; paid_api_requests=0 } | ConvertTo-Json
            break
        }
        Set-DeepSeekEnvironment
        & $Python (Join-Path $Root 'scripts\preflight_deepseek_bath_towel.py') --candidate-file $Candidate --run-dir $RunDir --workers $Workers
        if ($LASTEXITCODE -ne 0) { throw '本地预检未通过；没有启动正式请求。' }
        $sampleReport = Read-JsonIfExists (Join-Path $SampleDir 'sample_report.json')
        if (-not $SkipSampleGate -and (-not $sampleReport -or $sampleReport.status -ne 'PASS' -or $sampleReport.candidate_set_key -ne $plan.candidate_set_key)) {
            Write-Output '尚无通过的13对样本门禁；Start将自动先运行样本，通过后紧接着启动全量。'
            & $PSCommandPath -Action Sample -Workers $Workers
            if ($LASTEXITCODE -ne 0) { throw '自动样本门禁未通过，为保证准确性未启动全量。' }
            $sampleReport = Read-JsonIfExists (Join-Path $SampleDir 'sample_report.json')
            if (-not $sampleReport -or $sampleReport.status -ne 'PASS') {
                throw '自动样本门禁缺少PASS结果，未启动全量。'
            }
        }
        $plan = Get-RemainingPlan
        if ([int]$plan.remaining_pairs -eq 0) {
            Write-Output '样本已写入共享结果，全部候选均已完成；不再启动正式请求。'
            break
        }
        Ensure-Layer123Watcher
        $args = @(
            (Join-Path $Root 'scripts\run_deepseek_bath_towel_batch12_remaining.py'),
            '--candidates', $FrozenCandidates, '--results', $Results,
            '--run-dir', $BatchRunDir, '--batch-size', '12', '--workers', ([string]$Workers)
        )
        $proc = Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $Root -RedirectStandardOutput $LogOut -RedirectStandardError $LogErr -WindowStyle Hidden -PassThru
        Set-Content -LiteralPath $PidFile -Value ([string]$proc.Id) -Encoding ASCII
        Write-Output "已在后台启动浴巾套装DeepSeek第三层，PID=$($proc.Id)，并发=$Workers；同一Amazon每批最多12个TEMU，Low首判，失败自动拆批，极少数硬冲突单候选High复判。"
        break
    }
}
} finally {
    if ($lockTaken) { $launchMutex.ReleaseMutex() }
    if ($launchMutex) { $launchMutex.Dispose() }
}
