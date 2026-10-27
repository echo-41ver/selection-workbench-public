$ErrorActionPreference = 'Stop'
$selectionRoot = Split-Path -Parent $PSScriptRoot
$selectionRecord = Join-Path $selectionRoot 'runtime\local-models\native-d1-setup.json'
$selectionNative = Get-NetTCPConnection -LocalPort 7863 -State Listen -ErrorAction SilentlyContinue
if (-not $selectionNative) { Write-Output '未运行本机 D1，保留现有分析方案。'; exit 0 }
try { $selectionHealth = Invoke-RestMethod 'http://127.0.0.1:7863/health' -TimeoutSec 4 }
catch { Write-Output '7863 未提供本机 D1 健康信息，未修改该服务。'; exit 0 }
if ($selectionHealth.ready -ne $true -or $selectionHealth.model -ne 'LiquidAI/d1-3B') {
    Write-Output '7863 不是支持的本机 D1，未修改该服务。'; exit 0
}
$selectionProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $selectionNative.OwningProcess)
$selectionMatch = [regex]::Match($selectionProcess.CommandLine, '(?:^|\s)"?([A-Za-z]:[\\/][^"\r\n]*[\\/]app\.py)"?\s*$')
if (-not $selectionMatch.Success) { throw '已连接 D1，但无法确认其启动脚本；保留原服务。' }
$selectionApi = [IO.Path]::GetFullPath($selectionMatch.Groups[1].Value.Trim())
$selectionD1Root = Split-Path -Parent $selectionApi
$selectionPython = Join-Path $selectionD1Root '.venv\Scripts\python.exe'
$selectionTemplate = Join-Path $PSScriptRoot 'local_d1_api.py'
$selectionConfig = Get-Content -LiteralPath (Join-Path $selectionD1Root 'model\config.json') -Raw | ConvertFrom-Json
if ($selectionConfig.auto_map.AutoModel -ne 'modeling_d1.D1Model' -or
    -not (Test-Path -LiteralPath $selectionPython -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $selectionD1Root 'runtime.py') -PathType Leaf)) {
    throw 'D1 安装布局不符，未修改服务。'
}
$selectionCurrent = [IO.File]::ReadAllText($selectionApi)
$selectionDesired = [IO.File]::ReadAllText($selectionTemplate)
if ($selectionCurrent -cne $selectionDesired -and
    (Get-FileHash -LiteralPath $selectionApi -Algorithm SHA256).Hash.ToLowerInvariant() -ne
    '12d5304fb3dae69095f3cc9a7174f460f60a7885c01b27f400af9958cb7722f8') {
    throw 'D1 脚本包含未识别修改，已保留，未覆盖。'
}
$selectionUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$selectionTaskName = 'SelectionWorkbench-LocalD1-7863-' + $selectionUser.User.Value.Split('-')[-1]
$selectionService = Join-Path $selectionD1Root 'selection-workbench-service.ps1'
$selectionTask = Get-ScheduledTask -TaskName $selectionTaskName -ErrorAction SilentlyContinue
if ($selectionTask -and $selectionTask.Actions.Arguments -notlike ('*"' + $selectionService + '"*')) {
    throw 'D1 后台任务名称已被其他程序使用，未覆盖。'
}
if ($selectionTask.State -eq 'Running' -and $selectionCurrent -ceq $selectionDesired -and $selectionHealth.api_version -eq 'swb-native-d1-concurrent-v1') {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $selectionRecord) | Out-Null
    @{stage='完成';root=$selectionD1Root;task=$selectionTaskName;health=$selectionHealth;independent_of_codex=$true} |
        ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $selectionRecord -Encoding UTF8
    Write-Output '本机 D1 并发服务已配置，保留当前进程。'; exit 0
}
if ($selectionHealth.concurrency.active -gt 0) { throw 'D1 正在分析，已保留任务。请在工作台暂停第三层后再次双击部署。' }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $selectionRecord) | Out-Null
@{stage='更新本机 D1';root=$selectionD1Root;old_pid=$selectionProcess.ProcessId;old_created=$selectionProcess.CreationDate.ToString('o');task=$selectionTaskName} |
    ConvertTo-Json | Set-Content -LiteralPath $selectionRecord -Encoding UTF8
$selectionRecheck = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $selectionProcess.ProcessId)
if ($selectionRecheck.CreationDate -ne $selectionProcess.CreationDate -or $selectionRecheck.CommandLine -ne $selectionProcess.CommandLine) {
    throw 'D1 进程身份已变化，未停止或覆盖。'
}
Stop-Process -Id $selectionProcess.ProcessId -Force
if ($selectionTask) { Stop-ScheduledTask -TaskName $selectionTaskName }
$selectionDeadline = [DateTime]::UtcNow.AddSeconds(15)
while (Get-NetTCPConnection -LocalPort 7863 -State Listen -ErrorAction SilentlyContinue) {
    if ([DateTime]::UtcNow -gt $selectionDeadline) { throw 'D1 端口尚未释放，未覆盖脚本。' }
    Start-Sleep -Milliseconds 250
}
if (-not (Test-Path -LiteralPath (Join-Path $selectionD1Root 'app-before-selection-workbench.py'))) {
    Copy-Item -LiteralPath $selectionApi -Destination (Join-Path $selectionD1Root 'app-before-selection-workbench.py')
}
Copy-Item -LiteralPath $selectionTemplate -Destination $selectionApi -Force
@'
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:HF_HOME = Join-Path $PSScriptRoot 'cache\huggingface'
$env:HF_HUB_OFFLINE = '1'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:TEMP = Join-Path $PSScriptRoot 'tmp'
$env:TMP = $env:TEMP
$selectionLogs = Join-Path $PSScriptRoot 'service-logs'
New-Item -ItemType Directory -Force -Path $selectionLogs | Out-Null
$selectionLog = Join-Path $selectionLogs ('native-d1-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
$selectionNativeProcess = Start-Process -FilePath (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -ArgumentList @('-u',('"' + (Join-Path $PSScriptRoot 'app.py') + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $selectionLog -RedirectStandardError ($selectionLog + '.stderr') -Wait -PassThru
exit $selectionNativeProcess.ExitCode
'@ | Set-Content -LiteralPath $selectionService -Encoding UTF8
$selectionAction = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $selectionService + '"') -WorkingDirectory $selectionD1Root
$selectionPrincipal = New-ScheduledTaskPrincipal -UserId $selectionUser.Name -LogonType Interactive -RunLevel Limited
$selectionTrigger = New-ScheduledTaskTrigger -AtLogOn -User $selectionUser.Name
$selectionSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $selectionTaskName -Action $selectionAction -Principal $selectionPrincipal -Trigger $selectionTrigger -Settings $selectionSettings -Force | Out-Null
while ((Get-ScheduledTask -TaskName $selectionTaskName).State -eq 'Running') {
    if ([DateTime]::UtcNow -gt $selectionDeadline) { throw 'D1 旧启动进程尚未结束，保留已保存状态。' }
    Start-Sleep -Milliseconds 250
}
Start-ScheduledTask -TaskName $selectionTaskName
$selectionDeadline = [DateTime]::UtcNow.AddSeconds(180)
do {
    Start-Sleep -Seconds 2
    try { $selectionHealth = Invoke-RestMethod 'http://127.0.0.1:7863/health' -TimeoutSec 2 } catch { $selectionHealth = $null }
} until ($selectionHealth.api_version -eq 'swb-native-d1-concurrent-v1' -or [DateTime]::UtcNow -gt $selectionDeadline)
if ($selectionHealth.api_version -ne 'swb-native-d1-concurrent-v1') { throw 'D1 更新后未就绪，诊断记录见程序的一键导出入口。' }
@{stage='完成';root=$selectionD1Root;task=$selectionTaskName;api_version=$selectionHealth.api_version;health=$selectionHealth;independent_of_codex=$true} |
    ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $selectionRecord -Encoding UTF8
Write-Output '本机 D1 并发服务已更新，Windows 登录后自动启动。'
exit 0
