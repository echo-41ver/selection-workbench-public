$ErrorActionPreference = 'Stop'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $env:PSModulePath
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$results = 'C:\SelectionResults'
$project = 'C:\SelectionAcceptance'
$stage = 'VERIFY_RELEASE'
$commit = ''
$web = $null
function Status([string]$State,[string]$Detail='') {
    @{status=$State;stage=$stage;detail=$Detail;commit=$commit;time=(Get-Date).ToString('o');paid_vision_started=$false} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $results 'status.json') -Encoding UTF8
}
function Checked([string]$Exe,[string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "验收命令失败：$Exe (exit $LASTEXITCODE)" }
}
Start-Transcript -Path (Join-Path $results 'transcript.log') -Force | Out-Null
try {
    Status 'RUNNING'
    $archive = Join-Path $PSScriptRoot 'release.zip'
    $expected = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release.sha256') -Raw).Trim()
    $commit = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release.commit') -Raw).Trim()
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) { throw '部署包哈希不匹配' }
    Expand-Archive -LiteralPath $archive -DestinationPath $project -Force
    $info = Get-Content -LiteralPath (Join-Path $project 'BUILD_INFO.json') -Raw | ConvertFrom-Json
    if ($info.commit -ne $commit) { throw '验收入口与部署包版本不一致' }
    if (-not (Test-Path -LiteralPath (Join-Path $project 'company.local.json'))) { throw '完整安装验收缺少公司配置' }
    Set-Location -LiteralPath $project
    $stage='INSTALL'; Status 'RUNNING'
    Checked 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $project 'install.ps1'))
    # Child installer PATH changes do not propagate to this process.
    $env:PATH=[Environment]::GetEnvironmentVariable('PATH','Machine')+';'+[Environment]::GetEnvironmentVariable('PATH','User')+';'+$env:PATH
    $python=Join-Path $project '.venv\Scripts\python.exe'
    $stage='OFFLINE_TESTS'; Status 'RUNNING'
    $env:DEEPSEEK_API_KEY=''; $env:ADSPOWER_API_KEY=''
    Checked $python @('-m','pip','check')
    Checked $python @('-m','pytest','-q')
    Checked 'npm.cmd' @('run','check')
    $stage='COMPATIBILITY'; Status 'RUNNING'
    Checked $python @('scripts/verify_deployment.py')
    Copy-Item -LiteralPath (Join-Path $project 'runtime/deployment-compatibility.json') -Destination (Join-Path $results 'deployment-compatibility.json') -Force
    $stage='WEB_ENTRY'; Status 'RUNNING'
    $web=Start-Process -FilePath $python -ArgumentList 'scripts/web_console.py --port 8765' -WorkingDirectory $project -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $results 'web.out.log') -RedirectStandardError (Join-Path $results 'web.err.log')
    $state=$null
    for ($attempt=0;$attempt -lt 30;$attempt++) {
        try { $state=Invoke-RestMethod 'http://127.0.0.1:8765/api/status'; break } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $state -or -not $state.ok -or $state.stage3_control.enabled -or $state.stage3_control.running) { throw '网页状态或第三层付费保护检查失败' }
    if (-not ($state.PSObject.Properties.Name -contains 'commercial') -or -not ($state.PSObject.Properties.Name -contains 'market_status')) { throw '网页没有加载商业选品接口' }
    $page=Invoke-WebRequest 'http://127.0.0.1:8765/' -UseBasicParsing
    if ($page.Content -notmatch 'refresh-market' -or $page.Content -notmatch 'biz-same_limit') { throw '网页缺少商业选品控件' }
    $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $results 'web-status.json') -Encoding UTF8
    $stage='DONE'
    Status 'INSTALL_AND_LOCAL_WEB_PASSED' '干净系统完整安装、公司连接准备、离线测试与网页接口通过；未执行真实采集、极鲸云首次授权、界面点击或付费视觉分析。'
} catch {
    Status 'FAILED' $_.Exception.Message
    Write-Error $_.Exception.Message -ErrorAction Continue
} finally {
    if ($web -and -not $web.HasExited) { Stop-Process -Id $web.Id -ErrorAction SilentlyContinue }
    Stop-Transcript | Out-Null
}
