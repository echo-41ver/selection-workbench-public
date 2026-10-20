[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WorkerName,
    [ValidateRange(0, 65535)][int]$MarionettePort = 0,
    [string]$ProfileId,
    [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$WebDriverPort,
    [Parameter(Mandatory = $true)][string]$GeckoDriverPath,
    [ValidateRange(5, 120)][int]$WaitSeconds = 60,
    [switch]$RecoverOwnedDriver
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root "runtime\$WorkerName"
$sidFile = Join-Path $runtime 'flower_session_id.txt'
$bidiFile = Join-Path $runtime 'flower_bidi_url.txt'
$driverState = Join-Path $runtime 'driver.json'
$bindingState = Join-Path $runtime 'binding.json'
$base = "http://127.0.0.1:$WebDriverPort"
$script:bindingClock = [Diagnostics.Stopwatch]::StartNew()
$script:bindingLastElapsed = 0.0
function Write-BindingTiming([string]$Stage) {
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    $elapsed = $script:bindingClock.Elapsed.TotalSeconds
    @{ stage = $Stage; elapsed_seconds = [Math]::Round($elapsed - $script:bindingLastElapsed, 3);
       total_seconds = [Math]::Round($elapsed, 3); observed_at = (Get-Date).ToUniversalTime().ToString('o') } |
        ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $runtime 'binding-timing.jsonl') -Encoding utf8
    $script:bindingLastElapsed = $elapsed
}

. (Join-Path $PSScriptRoot 'windows_tcp_listener.ps1')

if (-not (Test-Path -LiteralPath $GeckoDriverPath -PathType Leaf)) {
    throw "geckodriver不存在：$GeckoDriverPath"
}
function Invoke-LocalDriverRest([string]$Uri, [string]$Method = 'GET', [string]$Body = '', [int]$TimeoutSeconds = 3) {
    if (([uri]$Uri).Host -ne '127.0.0.1') { throw 'Driver REST must stay on loopback.' }
    # Explicitly bypass Windows/system HTTP proxy in both PS5 and PS7.
    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
    try {
        $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::new($Method), $Uri)
        if ($Body) { $request.Content = [Net.Http.StringContent]::new($Body, [Text.Encoding]::UTF8, 'application/json') }
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) { throw "Local WebDriver HTTP $([int]$response.StatusCode): $text" }
        return ($text | ConvertFrom-Json)
    } finally { $client.Dispose(); $handler.Dispose() }
}
function Show-VerifiedProfileWindow([int]$RootPid) {
    if (-not ('OwnedFlowerForeground' -as [type])) {
        Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class OwnedFlowerForeground {
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr handle);
 [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr handle, int command);
 [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr handle);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr handle);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr handle, out uint pid);
 [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint from, uint to, bool attach);
 [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
}
"@
    }
    $tree = @(Get-ProfileBrowserTree $RootPid)
    $windows = @(foreach ($member in $tree) {
        $p = Get-Process -Id $member -ErrorAction SilentlyContinue
        if ($p -and $p.MainWindowHandle -ne 0) { $p }
    })
    if ($windows.Count -ne 1) { throw 'Selected profile must expose one visible browser window; no other window was activated.' }
    $selected = $windows[0]
    $handle = $selected.MainWindowHandle
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        [uint32]$windowProcessId = 0
        $targetThread = [OwnedFlowerForeground]::GetWindowThreadProcessId($handle, [ref]$windowProcessId)
        if ($windowProcessId -ne $selected.Id -or -not $targetThread) { throw 'Selected profile window ownership changed; activation stopped.' }
        $currentThread = [OwnedFlowerForeground]::GetCurrentThreadId()
        [uint32]$foregroundProcessId = 0
        $foregroundThread = [OwnedFlowerForeground]::GetWindowThreadProcessId([OwnedFlowerForeground]::GetForegroundWindow(), [ref]$foregroundProcessId)
        $attached = [Collections.Generic.List[uint32]]::new()
        try {
            foreach ($otherThread in @($foregroundThread, $targetThread) | Select-Object -Unique) {
                if ($otherThread -and $otherThread -ne $currentThread -and [OwnedFlowerForeground]::AttachThreadInput($currentThread, $otherThread, $true)) {
                    $attached.Add($otherThread)
                }
            }
            if ([OwnedFlowerForeground]::IsIconic($handle)) { $null = [OwnedFlowerForeground]::ShowWindowAsync($handle, 9) }
            $null = [OwnedFlowerForeground]::BringWindowToTop($handle)
            $null = [OwnedFlowerForeground]::SetForegroundWindow($handle)
        } finally {
            foreach ($otherThread in $attached) { $null = [OwnedFlowerForeground]::AttachThreadInput($currentThread, $otherThread, $false) }
        }
        if ([OwnedFlowerForeground]::GetForegroundWindow() -eq $handle) {
            return @{ window_pid = $selected.Id; window_handle = $handle.ToInt64(); activated = $true; attempts = $attempt }
        }
        Start-Sleep -Milliseconds 150
    }
    throw 'Selected profile window could not be activated after bounded retries; browser retained.'

}
function Resolve-ProfileMarionettePort([string]$Id) {
    $browsers = @(Get-CimInstance Win32_Process -Filter "Name = 'FlowerBrowser.exe'" |
        Where-Object { $_.CommandLine -match '(?i)(--marionette|user-data-dir|flower)' })
    $browserIds = @($browsers | ForEach-Object { [int]$_.ProcessId })
    $roots = @($browsers | Where-Object { $browserIds -notcontains [int]$_.ParentProcessId })
    $pattern = '(?i)(?:--user-data-dir=|[\\/])[^\s"]*' + [regex]::Escape($Id) + '[^\s"]*'
    $matched = @($roots | Where-Object { $_.CommandLine -match $pattern })
    if ($matched.Count -gt 1) { throw "Profile ID did not resolve to one unique FlowerBrowser root process." }
    if ($matched.Count -eq 0) {
        if ($script:expectedProfileBrowserPid) { throw 'Selected profile browser exited while waiting for Marionette.' }
        return 0
    }
    $script:profileBrowserPid = [int]$matched[0].ProcessId
    $createdTicks = $matched[0].CreationDate.ToUniversalTime().Ticks
    if ($script:expectedProfileBrowserPid -and
        ($script:profileBrowserPid -ne $script:expectedProfileBrowserPid -or $createdTicks -ne $script:expectedProfileBrowserTicks)) {
        throw 'Selected profile browser ownership changed while waiting for Marionette.'
    }
    $script:expectedProfileBrowserPid = $script:profileBrowserPid
    $script:expectedProfileBrowserTicks = $createdTicks
    $match = [regex]::Match([string]$matched[0].CommandLine, '(?i)(?:--user-data-dir|--profile(?:-directory)?)(?:=|\s+)\s*(?:"(?<quoted>[^"]+)"|(?<bare>[^"\s]+))')
    if (-not $match.Success) { throw 'Cannot locate the selected browser profile directory.' }
    $profileRoot = if ($match.Groups['quoted'].Success) { $match.Groups['quoted'].Value } else { $match.Groups['bare'].Value }
    $directPortFile = Join-Path $profileRoot 'MarionetteActivePort'
    $active = $null
    foreach ($candidate in @($directPortFile, $script:profileMarionetteFile) | Select-Object -Unique) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $candidateFile = Get-Item -LiteralPath $candidate
            if ($candidateFile.LastWriteTimeUtc -ge $matched[0].CreationDate.ToUniversalTime().AddSeconds(-2)) {
                $active = $candidateFile
                break
            }
        }
    }
    if (-not $active) {
        $active = Get-ChildItem -LiteralPath $profileRoot -Filter MarionetteActivePort -File -Recurse -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
    }
    if (-not $active) { return 0 }
    $script:profileMarionetteFile = $active.FullName
    $script:profileMarionetteFileUpdated = $active.LastWriteTimeUtc.ToString('o')
    # A leftover port file from an older incarnation must never choose a browser.
    if ($active.LastWriteTimeUtc -lt $matched[0].CreationDate.ToUniversalTime().AddSeconds(-2)) { return 0 }
    $resolved = [int]((Get-Content -LiteralPath $active.FullName -Raw).Trim() -split '\s+')[0]
    if ($resolved -lt 1 -or $resolved -gt 65535) { throw 'Invalid profile MarionetteActivePort.' }
    return $resolved
}
function Wait-ProfileMarionettePort([string]$Id, [double]$TimeoutSeconds) {
    $script:expectedProfileBrowserPid = $null
    $script:expectedProfileBrowserTicks = $null
    $clock = [Diagnostics.Stopwatch]::StartNew()
    do {
        $port = Resolve-ProfileMarionettePort $Id
        if ($port -gt 0) {
            $selected = @(Get-CimInstance Win32_Process -Filter "Name = 'FlowerBrowser.exe' AND ProcessId = $script:expectedProfileBrowserPid")
            if ($selected.Count -ne 1 -or $selected[0].CreationDate.ToUniversalTime().Ticks -ne $script:expectedProfileBrowserTicks) {
                throw 'Selected profile browser ownership changed before Marionette binding.'
            }
            $listeners = @(Get-LocalTcpListener -LocalPort $port)
            if ($listeners.Count -gt 0) {
                $tree = @(Get-ProfileBrowserTree $script:expectedProfileBrowserPid)
                if (@($listeners | Where-Object { $tree -notcontains [int]$_.OwningProcess }).Count -gt 0) {
                    throw 'Selected Marionette port belongs to another process; refusing to bind.'
                }
                return $port
            }
        }
        $remaining = $TimeoutSeconds - $clock.Elapsed.TotalSeconds
        if ($remaining -gt 0) { Start-Sleep -Milliseconds ([Math]::Min(250, [Math]::Ceiling($remaining * 1000))) }
    } while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds)
    throw 'Selected profile Marionette was not ready before the bounded deadline; browser retained.'
}
function Get-ProfileBrowserTree([int]$RootProcessId) {
    $browserProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'FlowerBrowser.exe'")
    $members = [Collections.Generic.HashSet[int]]::new()
    $null = $members.Add($RootProcessId)
    do {
        $changed = $false
        foreach ($browserProcess in $browserProcesses) {
            if ($members.Contains([int]$browserProcess.ParentProcessId)) {
                if ($members.Add([int]$browserProcess.ProcessId)) { $changed = $true }
            }
        }
    } while ($changed)
    return @($members)
}
$bindingDeadline = (Get-Date).AddSeconds($WaitSeconds)
if ($ProfileId) { $MarionettePort = Wait-ProfileMarionettePort $ProfileId $WaitSeconds }
if ($MarionettePort -lt 1) { throw 'A valid MarionettePort or resolvable ProfileId is required.' }
Write-BindingTiming 'resolve_profile_marionette'

$listener = @(Get-LocalTcpListener -LocalPort $WebDriverPort)
$previous = if (Test-Path -LiteralPath $driverState) { Get-Content -LiteralPath $driverState -Raw | ConvertFrom-Json } else { $null }
$owned = if ($previous) { Get-Process -Id $previous.pid -ErrorAction SilentlyContinue } else { $null }
if ($listener -or $owned) {
    if (-not $owned -or @($listener | Where-Object { $_.OwningProcess -ne $owned.Id }).Count -gt 0 -or $owned.Path -ne $GeckoDriverPath -or $owned.StartTime.ToUniversalTime().Ticks -ne $previous.started_ticks) {
        throw "WebDriver端口$WebDriverPort被非本任务进程占用；未终止其他进程。"
    }
    if ($previous.marionette_port -eq $MarionettePort -and (Test-Path -LiteralPath $sidFile) -and (Test-Path -LiteralPath $bidiFile)) {
        $savedSession = (Get-Content -LiteralPath $sidFile -Raw).Trim()
        try {
            $current = Invoke-LocalDriverRest "$base/session/$savedSession/url"
            if ($current.value -is [string]) {
                Write-BindingTiming 'existing_driver_already_bound'
                [pscustomobject]@{ worker = $WorkerName; status = 'ALREADY_BOUND'; webdriver_url = $base } | ConvertTo-Json -Compress
                exit 0
            }
        } catch { }
    }
    if ($previous.marionette_port -eq $MarionettePort) {
        $bindingRecord = if (Test-Path -LiteralPath $bindingState) { Get-Content -LiteralPath $bindingState -Raw | ConvertFrom-Json } else { $null }
        $sameOwnedProfile = $ProfileId -and $previous.profile_id -eq $ProfileId -and
            $previous.browser_pid -eq $script:expectedProfileBrowserPid -and
            $previous.browser_started_ticks -eq $script:expectedProfileBrowserTicks
        $incompleteOwnedBinding = $sameOwnedProfile -and
            (-not $bindingRecord -or $bindingRecord.geckodriver_pid -ne $owned.Id -or
             $bindingRecord.phase -in @('REQUESTING_SESSION', 'SESSION_BIND_FAILED'))
        if (-not $incompleteOwnedBinding -and -not ($RecoverOwnedDriver -and $sameOwnedProfile)) {
            throw 'Existing selected-profile driver has no proven recoverable session; retained rather than creating a second session.'
        }
        # A fenced recovery may disconnect a hung driver after the collector lease
        # is released. Browser, profile and saved collection data remain intact.
    }
    Stop-Process -Id $owned.Id -Force
    if (-not $owned.WaitForExit(5000)) { throw 'Owned local driver did not exit; refusing a second driver session.' }
}
Write-BindingTiming 'existing_driver_ownership_and_recovery'

New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$process = Start-Process -FilePath $GeckoDriverPath `
    -ArgumentList @('--connect-existing', '--marionette-port', $MarionettePort, '--port', $WebDriverPort, '--log', 'warn') `
    -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'geckodriver.stdout.log') -RedirectStandardError (Join-Path $runtime 'geckodriver.stderr.log')
@{ pid = $process.Id; started_ticks = $process.StartTime.ToUniversalTime().Ticks; marionette_port = $MarionettePort; port = $WebDriverPort; profile_id = $ProfileId; browser_pid = $script:expectedProfileBrowserPid; browser_started_ticks = $script:expectedProfileBrowserTicks } |
    ConvertTo-Json | Set-Content -LiteralPath $driverState -Encoding utf8

try {
$deadline = $bindingDeadline
$ready = $false
do {
    Start-Sleep -Milliseconds 250
    try { $null = Invoke-LocalDriverRest "$base/status" -TimeoutSeconds 2; $ready = $true } catch { }
} while (-not $ready -and (Get-Date) -lt $deadline)
if (-not $ready) { throw "geckodriver未在$WaitSeconds秒内监听$WebDriverPort（PID=$($process.Id)）" }
Write-BindingTiming 'local_driver_ready'

# First binding must visibly activate ONLY the selected profile's process tree.
# Port ownership remains the separate authority check above.
$foreground = if ($ProfileId) { Show-VerifiedProfileWindow $script:expectedProfileBrowserPid } else { $null }
Write-BindingTiming 'activate_selected_profile_window'
$caps = @{ capabilities = @{ alwaysMatch = @{ webSocketUrl = $true; pageLoadStrategy = 'none' } } } | ConvertTo-Json -Depth 6 -Compress
$sessionTimeout = [Math]::Max(1, [Math]::Min(60, [Math]::Ceiling(($bindingDeadline - (Get-Date)).TotalSeconds)))
# Only record sanitized driver ownership; never browser arguments or credentials.
$matchingDrivers = @(Get-CimInstance Win32_Process -Filter "Name = 'geckodriver.exe'" | ForEach-Object {
    $m = [regex]::Match([string]$_.CommandLine, '--marionette-port\s+(\d+)')
    $p = [regex]::Match([string]$_.CommandLine, '--port\s+(\d+)')
    if ($m.Success -and [int]$m.Groups[1].Value -eq $MarionettePort) {
        @{ pid = [int]$_.ProcessId; created_at = $_.CreationDate.ToUniversalTime().ToString('o');
           marionette_port = [int]$m.Groups[1].Value; webdriver_port = if ($p.Success) { [int]$p.Groups[1].Value } else { $null } }
    }
})
Write-BindingTiming 'inspect_matching_drivers'
# Record ownership BEFORE requesting a session, including failed bindings.
@{ phase = 'REQUESTING_SESSION'; profile_id = $ProfileId; browser_pid = $script:expectedProfileBrowserPid;
    browser_started_ticks = $script:expectedProfileBrowserTicks; marionette_port = $MarionettePort;
    webdriver_port = $WebDriverPort; geckodriver_pid = $process.Id; geckodriver_started_ticks = $process.StartTime.ToUniversalTime().Ticks;
    marionette_port_file = $script:profileMarionetteFile; marionette_file_updated_at = $script:profileMarionetteFileUpdated;
    matching_geckodrivers = $matchingDrivers; foreground = $foreground; local_rest_proxy = 'DISABLED'; session_id_file = $sidFile; observed_at = (Get-Date).ToString('o') } |
    ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $bindingState -Encoding utf8
$session = Invoke-LocalDriverRest "$base/session" -Method POST -Body $caps -TimeoutSeconds $sessionTimeout
Write-BindingTiming 'create_existing_browser_session'
$sid = [string]$session.value.sessionId
$bidi = [string]$session.value.capabilities.webSocketUrl
if (-not $sid -or -not $bidi) { throw 'WebDriver未返回完整session id和BiDi地址。' }

[IO.File]::WriteAllText($sidFile, $sid, [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText($bidiFile, $bidi, [Text.UTF8Encoding]::new($false))
$boundState = Get-Content -LiteralPath $bindingState -Raw | ConvertFrom-Json
$boundState.phase = 'BOUND'
$boundState | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $bindingState -Encoding utf8

[pscustomobject]@{
    worker = $WorkerName
    webdriver_url = $base
    marionette_port = $MarionettePort
    webdriver_port = $WebDriverPort
    geckodriver_pid = $process.Id
    session_id_file = $sidFile
    bidi_url_file = $bidiFile
} | ConvertTo-Json -Compress
} catch {
    if (Test-Path -LiteralPath $bindingState) {
        $failureState = Get-Content -LiteralPath $bindingState -Raw | ConvertFrom-Json
        $failureState.phase = 'SESSION_BIND_FAILED'
        $failureState | Add-Member -NotePropertyName failure_kind -NotePropertyValue $(if ($_.Exception.Message -match 'Maximum number of active sessions') { 'ACTIVE_SESSION_ALREADY_EXISTS' } else { 'LOCAL_DRIVER_BIND_FAILED' }) -Force
        $failureState | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $bindingState -Encoding utf8
    }
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    throw
}
