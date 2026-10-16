[CmdletBinding()]
param(
    [ValidateSet('start', 'prepare-replacement', 'status', 'stop')][string]$Action = 'start',
    [ValidateRange(0, 32)][int]$Worker = 0,
    [string]$CredentialFile = 'runtime/secrets/proxyshare.credential.xml',
    [string]$SettingsFile = 'runtime/secrets/proxyshare.settings.json',
    [string]$StateFile = 'runtime/proxyshare-chains/state.json'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$credentialPath = Join-Path $root $CredentialFile
$settingsPath = Join-Path $root $SettingsFile
$statePath = Join-Path $root $StateFile
$chainScript = Join-Path $PSScriptRoot 'proxyshare_chain_proxy.mjs'
$runtime = Split-Path -Parent $statePath

function New-SessionToken {
    -join ((48..57) + (97..122) | Get-Random -Count 12 | ForEach-Object { [char]$_ })
}

function Read-State {
    if (Test-Path -LiteralPath $statePath) {
        return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json -AsHashtable
    }
    return @{ schema_version = 'proxyshare-chain-state-v1'; workers = @{} }
}

function Write-State([hashtable]$state) {
    $state.updated_at = (Get-Date).ToString('o')
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    $temp = "$statePath.$PID.tmp"
    $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temp -Encoding utf8
    Move-Item -LiteralPath $temp -Destination $statePath -Force
}

function Stop-WorkerChain([hashtable]$entry) {
    $processIds = @([int]($entry.pid ?? 0))
    # A previous launcher can leave a stale PID. Reconcile only this lane's
    # actual listener, and only when its executable belongs to this checkout.
    if ([int]($entry.port ?? 0) -gt 0) {
        $processIds += @(Get-NetTCPConnection -LocalPort ([int]$entry.port) -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess)
    }
    foreach ($processId in ($processIds | Sort-Object -Unique)) {
        if ($processId -le 0) { continue }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
        if ($process -and $process.Name -eq 'node.exe' -and $process.CommandLine -and $process.CommandLine.Contains(('"{0}"' -f $chainScript))) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            try { Wait-Process -Id $processId -Timeout 5 -ErrorAction SilentlyContinue } catch { }
        }
    }
}

$settings = if (Test-Path -LiteralPath $settingsPath) {
    Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json -AsHashtable
} else { $null }
$state = Read-State
$workerCount = 4
$policyPath = Join-Path $root 'runtime/proxy_policy.json'
if (Test-Path -LiteralPath $policyPath) {
    $runtimePolicy = Get-Content -LiteralPath $policyPath -Raw | ConvertFrom-Json -AsHashtable
    $configuredCount = @($runtimePolicy.worker_chain_ports).Count
    if ($configuredCount -ge 1 -and $configuredCount -le 32) { $workerCount = $configuredCount }
}
if ($Action -ne 'stop' -and $Worker -gt $workerCount) { throw "当前安装只有 $workerCount 路环境，worker $Worker 不存在" }
$targets = if ($Worker -gt 0) { @($Worker) } elseif ($Action -eq 'stop') { @(@(1..$workerCount) + @(Get-ChildItem -LiteralPath $runtime -Filter 'worker-*.json' -ErrorAction SilentlyContinue | ForEach-Object { if ($_.BaseName -match '^worker-(\d+)$' -and [int]$Matches[1] -le 32) { [int]$Matches[1] } })) | Sort-Object -Unique } else { @(1..$workerCount) }
# Canonical per-worker state prevents concurrent rotations overwriting each other.
function Worker-StatePath([int]$number) { Join-Path $root "runtime/proxyshare-chains/worker-$number.json" }
function Worker-State([int]$number) {
    $path = Worker-StatePath $number
    if (Test-Path -LiteralPath $path) { return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json -AsHashtable }
    if ($state.workers.ContainsKey([string]$number)) { return $state.workers[[string]$number] }
    return @{}
}
function Save-WorkerState([int]$number, [hashtable]$entry) {
    $path = Worker-StatePath $number
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
    $temp = "$path.$PID.tmp"
    $entry | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temp -Encoding utf8
    Move-Item -LiteralPath $temp -Destination $path -Force
}

if ($Action -eq 'status') {
    $result = foreach ($number in $targets) {
        $entry = Worker-State $number
        $port = if ($entry) { [int]$entry.port } elseif ($settings) { [int]$settings.chain_base_port + $number - 1 } else { 17890 + $number }
        $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        [pscustomobject]@{ worker = $number; port = $port; listening = [bool]$listener; pid = if ($listener) { ($listener | Select-Object -First 1 -ExpandProperty OwningProcess) } else { $null } }
    }
    $result | ConvertTo-Json -Compress
    exit 0
}

if ($Action -eq 'stop') {
    foreach ($number in $targets) {
        $key = [string]$number
        $entry = Worker-State $number
        Stop-WorkerChain $entry
        $entry.pid = $null
        Save-WorkerState $number $entry
    }
    Write-State $state
    [pscustomobject]@{ stopped = $targets } | ConvertTo-Json -Compress
    exit 0
}

# Same byte 0 as Python RouteLock/WorkerLease; no setting commit or collector
# admission may race actual adoption/restart of a different local route.
New-Item -ItemType Directory -Force -Path (Join-Path $root 'runtime') | Out-Null
$routeGuard = [System.IO.File]::Open((Join-Path $root 'runtime\local-proxy-route.lock'), 'OpenOrCreate', 'ReadWrite', 'ReadWrite')
$routeLocked = $false
try {
    if ($routeGuard.Length -eq 0) { $routeGuard.WriteByte(48); $routeGuard.Flush() }
    $routeDeadline = (Get-Date).AddSeconds(120)
    while (-not $routeLocked) {
        try { $routeGuard.Lock(0, 1); $routeLocked = $true }
        catch [System.IO.IOException] {
            if ((Get-Date) -ge $routeDeadline) { throw '出口路线正在提交或任务正在启动；未采用新路线。' }
            Start-Sleep -Milliseconds 50
        }
    }
    # Reload after locking; the earlier snapshot cannot authorize adoption.
    $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json -AsHashtable
    $state = Read-State

# Ownership validation is local-only and happens before any existing chain stop.
$python = Join-Path $root '.venv\Scripts\python.exe'
& $python (Join-Path $root 'scripts\local_proxy_preflight.py') --validate-saved | Out-Null
if ($LASTEXITCODE -ne 0) { throw '本机出口进程或端口已变化；请安全停止任务后重新准备，未切换出口。' }

if (-not (Test-Path -LiteralPath $credentialPath)) { throw "缺少加密凭据：$credentialPath；先运行 initialize_proxyshare_credential.ps1" }
if (-not $settings) { throw "缺少代理设置：$settingsPath；先运行 initialize_proxyshare_credential.ps1" }
if ([int]$settings.preproxy_port -gt 0 -and -not (Get-NetTCPConnection -LocalPort ([int]$settings.preproxy_port) -State Listen -ErrorAction SilentlyContinue)) {
    throw "本机 SOCKS5 预代理 $($settings.preproxy_host):$($settings.preproxy_port) 未监听；ProxyShare 在当前网络不能直连。"
}

$credential = Import-Clixml -LiteralPath $credentialPath
$plainPassword = $credential.GetNetworkCredential().Password
$baseUser = $credential.UserName
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

try {
    foreach ($number in $targets) {
        # Per-lane lifecycle lock: a health check and replacement must not launch
        # competing listeners. Different workers retain independent locks.
        $lockPath = Join-Path $runtime "worker-$number.lifecycle.lock"
        $lockStream = $null
        $lockDeadline = (Get-Date).AddSeconds(30)
        while (-not $lockStream) {
            try { $lockStream = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
            catch [System.IO.IOException] {
                if ((Get-Date) -ge $lockDeadline) { throw "worker $number 代理生命周期锁等待超时" }
                Start-Sleep -Milliseconds 150
            }
        }
        try {
        $key = [string]$number
        $entry = Worker-State $number
        $port = [int]$settings.chain_base_port + $number - 1
        $existingListener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        $routeStamp = '{0}|{1}|{2}|{3}|{4}' -f $settings.preproxy_host,$settings.preproxy_port,($settings.preproxy_protocol ?? 'SOCKS5'),$settings.local_proxy_owner.pid,$settings.local_proxy_owner.created
        $sameEndpoint = $entry.route_stamp -and ((($entry.route_stamp -split '\|')[0..2] -join '|') -eq (($routeStamp -split '\|')[0..2] -join '|'))
        if ($sameEndpoint -and $entry.route_stamp -ne $routeStamp) {
            & $python (Join-Path $root 'scripts\local_proxy_preflight.py') --validate-saved | Out-Null
            if ($LASTEXITCODE -ne 0) { throw '同一代理端口的新进程尚未核实；保留原链路。' }
            $entry.route_stamp = $routeStamp
            Save-WorkerState $number $entry
            $state.workers[$key] = $entry
        }
        if ($Action -eq 'start' -and $entry.route_stamp -eq $routeStamp -and $entry.traffic_policy -eq 'temu-activity-lease-v1' -and $entry.pid -and $existingListener -and $existingListener.OwningProcess -contains [int]$entry.pid) {
            $owned = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.pid)" -ErrorAction SilentlyContinue
            if ($owned -and $owned.CommandLine -and $owned.CommandLine.Contains($chainScript)) { continue }
        }
        if ($entry.route_stamp -ne $routeStamp) {
            & $python (Join-Path $root 'scripts\local_proxy_preflight.py') --route-change-check | Out-Null
            if ($LASTEXITCODE -ne 0) { throw '任务或人工验证正在使用原出口；未改变或重启不同路线。' }
        }
        $entry.port = $port
        Stop-WorkerChain $entry
        # Wait for actual port release, not just an old process handle. Never
        # overwrite the last good owner when an unrelated listener occupies it.
        $releaseDeadline = (Get-Date).AddSeconds(8)
        do {
            $occupied = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
            if (-not $occupied) { break }
            Start-Sleep -Milliseconds 200
        } while ((Get-Date) -lt $releaseDeadline)
        if ($occupied) { throw "worker $number 代理端口 $port 尚未释放；保留原状态，未启动重复代理" }
        # prepare-replacement is deliberately only the proxy half of a switch.
        # The public replacement entrypoint must create and bind a NEW AdsPower
        # profile after the old one has been deleted. Reusing the old profile is a
        # forbidden recovery mode for this project.
        $token = if ($Action -eq 'prepare-replacement' -or -not $entry.session_token) { New-SessionToken } else { [string]$entry.session_token }
        $workerUser = [regex]::Replace($baseUser, '_session-[^_]+', "_session-$token")
        $port = [int]$settings.chain_base_port + $number - 1

        $env:TEMU_CHAIN_LISTEN_HOST = [string]$settings.chain_host
        $env:TEMU_CHAIN_LISTEN_PORT = [string]$port
        $env:TEMU_CHAIN_WORKER = [string]$number
        $env:TEMU_CHAIN_ACTIVITY_DIR = Join-Path $root "runtime/proxyshare-chains/activity/worker-$number"
        $env:TEMU_PREPROXY_HOST = [string]$settings.preproxy_host
        $env:TEMU_PREPROXY_PORT = [string]$settings.preproxy_port
        $env:TEMU_PREPROXY_PROTOCOL = [string]($settings.preproxy_protocol ?? 'SOCKS5')
        $env:PROXYSHARE_PROXY_HOST = [string]$settings.proxy_host
        $env:PROXYSHARE_PROXY_PORT = [string]$settings.proxy_port
        $env:PROXYSHARE_PROXY_USER = $workerUser
        $env:PROXYSHARE_PROXY_PASSWORD = $plainPassword
        $stdout = Join-Path $runtime "chain-$port.out.log"
        $stderr = Join-Path $runtime "chain-$port.err.log"
        $process = Start-Process -FilePath 'node' -ArgumentList @(('"{0}"' -f $chainScript)) -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $entry = @{ worker = $number; port = $port; pid = $process.Id; session_token = $token; started_at = (Get-Date).ToString('o'); status = 'STARTING'; route_stamp = $routeStamp; traffic_policy = 'temu-activity-lease-v1' }

        $deadline = (Get-Date).AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 200
            $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        } while (-not $listener -and (Get-Date) -lt $deadline)
        if (-not $listener) { throw "worker $number 的本地代理链未监听 $port；查看 $stderr" }
        if ($listener.OwningProcess -notcontains $process.Id) { throw "代理端口 $port 被其他进程占用；未把旧监听误认为新session" }

        # Listener ownership is local-only. Actual TEMU readiness belongs to the
        # collector's active lease; setup must never consume residential traffic.
        $entry.status = 'LISTENING_REQUIRES_COLLECTOR'
        Save-WorkerState $number $entry
        $state.workers[$key] = $entry
        } finally { if ($lockStream) { $lockStream.Dispose() } }
    }
    Write-State $state
} finally {
    Remove-Item Env:PROXYSHARE_PROXY_PASSWORD, Env:PROXYSHARE_PROXY_USER -ErrorAction SilentlyContinue
    $plainPassword = $null
}

# Keep browser network permission alive even if a collector pauses or exits.
# The helper owns no browser and never stops or restarts a proxy chain.
Start-Process -FilePath $python -ArgumentList @(('"{0}"' -f (Join-Path $root 'scripts/retain_proxy_connections.py'))) -WorkingDirectory $root -WindowStyle Hidden | Out-Null

[pscustomobject]@{
    action = $Action
    workers = $targets
    ready = $true
    requires_new_adspower_environment = ($Action -eq 'prepare-replacement')
    state_file = $statePath
} | ConvertTo-Json -Compress

} finally {
    if ($routeLocked) { $routeGuard.Unlock(0, 1) }
    $routeGuard.Dispose()
}
