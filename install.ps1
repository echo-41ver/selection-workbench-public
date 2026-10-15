[CmdletBinding()]
param([string]$CompanyConfig, [switch]$SkipAdsPower, [switch]$SkipModelDownload, [switch]$SkipProxyClient, [switch]$PreflightOnly)
$ErrorActionPreference = 'Stop'
# Keep built-in cmdlets available when an agent inherits a PowerShell 7 module path.
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $env:PSModulePath
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$root = $PSScriptRoot
$localRuntime = Join-Path $root 'local-runtime.ps1'
if (Test-Path -LiteralPath $localRuntime -PathType Leaf) { . $localRuntime }
Set-Location -LiteralPath $root
function Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "安装命令失败: $Exe (exit $LASTEXITCODE)" }
}
function Refresh-Path {
    $appAliases = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
    $env:PATH = [Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH','User') + ';' + $appAliases + ';' + $env:PATH
    $localRuntime = Join-Path $root 'local-runtime.ps1'
    if (Test-Path -LiteralPath $localRuntime -PathType Leaf) { . $localRuntime }
}
function Find-WinGet {
    $command = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $alias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
    if (Test-Path -LiteralPath $alias -PathType Leaf) { return $alias }
    return $null
}
function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Ensure-WinGet {
    $winget = Find-WinGet
    if ($winget) { return $winget }
    Write-Host '未发现 WinGet，正在通过微软官方 PowerShell 模块自动准备…'
    try {
        # Use the current-user repair unless this process is already elevated.
        # https://learn.microsoft.com/windows/package-manager/winget/#install-winget-on-windows-sandbox
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Install-PackageProvider -Name NuGet -Force -Scope CurrentUser -ErrorAction Stop | Out-Null
        Install-Module -Name Microsoft.WinGet.Client -Force -Repository PSGallery -Scope CurrentUser -ErrorAction Stop | Out-Null
        Import-Module Microsoft.WinGet.Client -ErrorAction Stop
        if (Test-Administrator) {
            Repair-WinGetPackageManager -AllUsers -ErrorAction Stop | Out-Null
        } else {
            Repair-WinGetPackageManager -ErrorAction Stop | Out-Null
        }
        Refresh-Path
    } catch {
        throw "自动准备 WinGet 失败，安装已停止。原因: $($_.Exception.Message)"
    }
    $winget = Find-WinGet
    if (-not $winget) { throw 'WinGet 自动准备后仍找不到可执行文件，安装已停止。' }
    return $winget
}
function Install-Package([string]$Id, [string]$Version) {
    if ($Id -notin @('Python.Python.3.12','OpenJS.NodeJS.LTS','Microsoft.PowerShell','Microsoft.VCRedist.2015+.x64','ClashVergeRev.ClashVergeRev')) { throw '安装包不在允许清单。' }
    $winget = Ensure-WinGet
    if (-not $script:WinGetSourcePrepared) {
        Write-Host '正在准备 WinGet 官方软件源数据…'
        Checked $winget @('source','update','--name','winget','--disable-interactivity')
        $script:WinGetSourcePrepared = $true
    }
    $arguments = @('install','--id',$Id,'--exact','--source','winget','--silent','--accept-package-agreements','--accept-source-agreements','--disable-interactivity')
    if ($Version) { $arguments += @('--version',$Version) }
    Checked $winget $arguments
    Refresh-Path
}
function Install-FixedRuntime([ValidateSet('python','node','powershell')][string]$Runtime) {
    $downloads = Join-Path $root 'runtime\downloads'
    New-Item -ItemType Directory -Force -Path $downloads | Out-Null
    switch ($Runtime) {
        'python' {
            $url = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
            $installer = Join-Path $downloads 'python-3.12.10-amd64.exe'
            Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
            $signature = Get-AuthenticodeSignature -LiteralPath $installer
            if ($signature.Status -ne 'Valid') { throw 'Python 3.12.10 官方安装器签名无效。' }
            $target = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312'
            $process = Start-Process -FilePath $installer -ArgumentList @('/quiet','InstallAllUsers=0',"TargetDir=$target",'PrependPath=0','Include_launcher=1','Include_pip=1','Include_test=0','SimpleInstall=1') -WindowStyle Hidden -Wait -PassThru
            if ($process.ExitCode -notin @(0,3010)) { throw "Python 安装失败：$($process.ExitCode)" }
        }
        'node' {
            $installer = Join-Path $downloads 'node-v24.19.0-x64.msi'
            Invoke-WebRequest -Uri 'https://nodejs.org/dist/v24.19.0/node-v24.19.0-x64.msi' -OutFile $installer -UseBasicParsing
            $signature = Get-AuthenticodeSignature -LiteralPath $installer
            if ($signature.Status -ne 'Valid') { throw 'Node.js 24.19.0 官方安装器签名无效。' }
            $process = Start-Process -FilePath 'msiexec.exe' -Verb RunAs -ArgumentList @('/i',('"' + $installer + '"'),'/qn','/norestart') -WindowStyle Hidden -Wait -PassThru
            if ($process.ExitCode -notin @(0,3010)) { throw "Node.js 安装失败：$($process.ExitCode)" }
        }
        'powershell' {
            $installer = Join-Path $downloads 'PowerShell-7.6.5-win-x64.msi'
            Invoke-WebRequest -Uri 'https://github.com/PowerShell/PowerShell/releases/download/v7.6.5/PowerShell-7.6.5-win-x64.msi' -OutFile $installer -UseBasicParsing
            $signature = Get-AuthenticodeSignature -LiteralPath $installer
            if ($signature.Status -ne 'Valid') { throw 'PowerShell 7.6.5 官方安装器签名无效。' }
            $process = Start-Process -FilePath 'msiexec.exe' -Verb RunAs -ArgumentList @('/i',('"' + $installer + '"'),'/qn','/norestart') -WindowStyle Hidden -Wait -PassThru
            if ($process.ExitCode -notin @(0,3010)) { throw "PowerShell 安装失败：$($process.ExitCode)" }
        }
    }
    Refresh-Path
}
function Find-Python312 {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { return $null }
    foreach ($line in @(& py -0p 2>$null)) {
        if ($line -match '([A-Za-z]:\\[^\r\n]*\\python\.exe)\s*$') {
            $candidate = $Matches[1]
            if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
            & $candidate -c 'import sys; sys.exit(sys.version_info[:3] != (3,12,10))' 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
    }
    return $null
}
function Find-NodeToolchain {
    $candidates = @()
    foreach ($command in @(Get-Command node -CommandType Application -All -ErrorAction SilentlyContinue)) {
        $candidates += Split-Path -Parent $command.Source
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($base) { $candidates += Join-Path $base 'nodejs'; $candidates += Join-Path $base 'Programs\nodejs' }
    }
    foreach ($directory in @($candidates | Select-Object -Unique)) {
        $node = Join-Path $directory 'node.exe'
        $npm = Join-Path $directory 'npm.cmd'
        $npx = Join-Path $directory 'npx.cmd'
        if (-not ((Test-Path -LiteralPath $node -PathType Leaf) -and (Test-Path -LiteralPath $npm -PathType Leaf) -and (Test-Path -LiteralPath $npx -PathType Leaf))) { continue }
        try {
            $nodeVersion = & $node --version 2>$null
            if ($LASTEXITCODE -eq 0 -and $nodeVersion.Trim() -eq 'v24.19.0') {
                return [pscustomobject]@{ Directory = $directory; Node = $node; Npm = $npm; Npx = $npx }
            }
        } catch { continue }
    }
    return $null
}
function Has-Node20 { return $null -ne (Find-NodeToolchain) }
function Get-PowerShellVersion {
    param([string]$Path, [int]$TimeoutMilliseconds=10000)
    $probe = [Diagnostics.Process]::new()
    try {
        $probe.StartInfo = [Diagnostics.ProcessStartInfo]::new()
        $probe.StartInfo.FileName = $Path
        $probe.StartInfo.Arguments = '-NoProfile -NonInteractive -Command "$PSVersionTable.PSVersion.ToString()"'
        $probe.StartInfo.UseShellExecute = $false
        $probe.StartInfo.CreateNoWindow = $true
        $probe.StartInfo.RedirectStandardOutput = $true
        $probe.StartInfo.RedirectStandardError = $true
        if (-not $probe.Start()) { return $null }
        if (-not $probe.WaitForExit($TimeoutMilliseconds)) { $probe.Kill(); return $null }
        if ($probe.ExitCode -ne 0) { return $null }
        return [version]$probe.StandardOutput.ReadToEnd().Trim()
    } catch { return $null }
    finally { $probe.Dispose() }
}
function Has-SystemPowerShell7 {
    $candidates = @(Join-Path $root 'runtime\tools\powershell\pwsh.exe')
    if ($env:ProgramFiles) { $candidates += Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe' }
    if ($env:LOCALAPPDATA) { $candidates += Join-Path $env:LOCALAPPDATA 'Microsoft\PowerShell\7\pwsh.exe' }
    if ($env:LOCALAPPDATA) { $candidates += Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe' }
    $found = Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue
    if ($found) { $candidates += $found.Source }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $version = Get-PowerShellVersion $candidate
        if ($version -and $version.ToString() -eq '7.6.5') { return $true }
    }
    return $false
}
function Has-VCRuntime {
    $vc = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction SilentlyContinue
    return $null -ne $vc -and $vc.Installed -eq 1 -and $vc.Major -eq 14
}
function Get-ExistingNetworkEnvironment {
    # Presence only. The application's protocol probe later chooses the real endpoint.
    # Never import another computer's address, port, VPN subscription or IP.
    $names = @('clash','clash-meta','verge-mihomo','mihomo','clash-verge','sing-box','v2ray','xray','v2rayN','ss-local','shadowsocks','shadowsocksr','nekoray','hiddify','wireguard','openvpn','tailscaled','vpnip')
    $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -in $names })
    if ($running.Count) { return '检测到已有代理或 VPN 程序，保留现有客户端。' }
    $systemProxy = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    if (($systemProxy.ProxyEnable -eq 1 -and $systemProxy.ProxyServer) -or $systemProxy.AutoConfigURL -or $env:HTTPS_PROXY -or $env:HTTP_PROXY) {
        return '检测到已有系统代理配置，保留现有网络设置。'
    }
    $registryPaths = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*')
    foreach ($registryPath in $registryPaths) {
        $installed = @(Get-ItemProperty -Path $registryPath -ErrorAction SilentlyContinue | Where-Object {
            $_.DisplayName -match '(?i)clash|mihomo|sing.box|v2ray|xray|shadowsocks|nekoray|hiddify|wireguard|openvpn|tailscale|vpnip'
        })
        if ($installed.Count) { return '检测到已安装的代理或 VPN 客户端；请在本机完成启动及授权。' }
    }
    return $null
}
function Ensure-NetworkClient {
    $existing = Get-ExistingNetworkEnvironment
    if ($existing) { Write-Host $existing; return }
    # An explicitly authorized direct route does not require a new VPN client.
    if ($CompanyConfig) {
        $companyInput = Get-Content -LiteralPath $CompanyConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($companyInput.preproxy_mode -eq 'DIRECT') { Write-Host '公司配置明确使用直连，无需安装代理客户端。'; return }
    }
    Write-Host '未检测到已有代理或 VPN 客户端，正在自动安装 Clash Verge Rev。安装后请自行完成订阅授权；不会复制原电脑配置或修改系统代理。'
    Install-Package 'ClashVergeRev.ClashVergeRev'
    if (-not (Get-ExistingNetworkEnvironment)) { throw '代理客户端安装后未检测到，请查看 WinGet 安装日志。' }
}
. (Join-Path $root 'scripts\deployment_guard.ps1')
$preflight = Get-DeploymentPreflight $root
if ($PreflightOnly) { $preflight | ConvertTo-Json -Compress; exit 0 }
if (-not $CompanyConfig -and (Test-Path -LiteralPath (Join-Path $root 'company.local.json') -PathType Leaf)) {
    $CompanyConfig = Join-Path $root 'company.local.json'
}
# Prompt precedes any JSON read, AdsPower detection using this file, or persistence.
Confirm-CompanyImport -ConfigPath $CompanyConfig -Root $root
$ownedPrefix = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith(($ownedPrefix + '.venv\'), [StringComparison]::OrdinalIgnoreCase) -and $_.ProcessId -ne $PID
})
if ($running.Count) { throw '本项目程序正在运行；请先停止任务并关闭本地网页服务，再安装更新。未移动现有环境。' }
$transaction = $null
$stage = '准备系统工具'
try {
Refresh-Path
if (-not $SkipProxyClient) { $stage = '检测本机网络与准备代理客户端'; Ensure-NetworkClient }
$stage = '准备系统工具'
if (-not (Find-Python312)) { Install-FixedRuntime 'python' }
if (-not (Has-Node20)) { Install-FixedRuntime 'node' }
if (-not (Has-SystemPowerShell7)) { Install-FixedRuntime 'powershell' }
if (-not (Has-VCRuntime)) { Install-Package 'Microsoft.VCRedist.2015+.x64' }
if (-not (Find-Python312) -or -not (Has-Node20) -or -not (Has-SystemPowerShell7) -or -not (Has-VCRuntime)) { throw '固定版本 Python 3.12.10、Node.js 24.19.0、PowerShell 7.6.5 或 VC++ 运行库安装后仍不可用，请按安装器提示处理后重试 deploy.cmd。' }
$toolchain = Find-NodeToolchain
$env:PATH = $toolchain.Directory + ';' + $env:PATH
$stage = '备份项目环境'
$transaction = Start-ProjectDeployment -Root $root -ImportCompany ([bool]$CompanyConfig)
$stage = '安装项目依赖'
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { Checked (Find-Python312) @('-m','venv',(Join-Path $root '.venv')) }
$assetRoot = Join-Path $root 'runtime\deployment-assets'
$wheelhouse = Join-Path $assetRoot 'wheelhouse'
$env:TORCH_HOME = Join-Path $root 'runtime\models\torch'
if (Test-Path -LiteralPath (Join-Path $assetRoot 'manifest.json') -PathType Leaf) {
    $assetManifest = Get-Content -LiteralPath (Join-Path $assetRoot 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($entry in $assetManifest.files.PSObject.Properties) {
        $assetPath = Join-Path $assetRoot ($entry.Name.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) { throw "部署依赖资产缺失：$($entry.Name)" }
        if ((Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value.sha256) { throw "部署依赖资产校验失败：$($entry.Name)" }
    }
}
if (Test-Path -LiteralPath $wheelhouse -PathType Container) {
    $requirements = Get-Content -LiteralPath (Join-Path $root 'requirements.lock.txt') -Raw -Encoding UTF8
    $githubRequirement = 'geekbi-temu-research-mcp @ https://github.com/geekbi/geekbi-temu-research-mcp/archive/d3536f77db76b70ae9c6e69bf499f47ad972850e.zip'
    if (-not $requirements.Contains($githubRequirement)) { throw '离线依赖清单中的极鲸云客户端版本不符合部署源。' }
    $offlineRequirements = $requirements.Replace($githubRequirement, 'geekbi-temu-research-mcp==0.1.0')
    $offlineLock = Join-Path $env:TEMP ('selection-workbench-requirements-' + [guid]::NewGuid().ToString('N') + '.txt')
    try {
        [IO.File]::WriteAllText($offlineLock, $offlineRequirements, [Text.UTF8Encoding]::new($false))
        Checked $python @('-m','pip','install','--no-index','--find-links',$wheelhouse,'--upgrade','pip==25.0.1')
        Checked $python @('-m','pip','install','--no-index','--find-links',$wheelhouse,'-r',$offlineLock)
        Checked $python @('-m','pip','install','--no-index','--find-links',$wheelhouse,'--no-build-isolation','--no-deps','-e',"${root}[test]")
    } finally {
        if (Test-Path -LiteralPath $offlineLock -PathType Leaf) { Remove-Item -LiteralPath $offlineLock }
    }
} else {
    Checked $python @('-m','pip','install','--upgrade','pip==25.0.1')
    Checked $python @('-m','pip','install','-r',(Join-Path $root 'requirements.lock.txt'),'--extra-index-url','https://download.pytorch.org/whl/cpu')
    Checked $python @('-m','pip','install','--no-deps','-e',"${root}[test]")
}
Checked $python @('-c','import torch, torchvision, PIL, psutil, openpyxl, xlsxwriter; print("Python dependencies loaded")')
$bundledModules = Join-Path $assetRoot 'node_modules'
if (Test-Path -LiteralPath (Join-Path $bundledModules 'playwright\package.json') -PathType Leaf) {
    $targetModules = Join-Path $root 'node_modules'
    New-Item -ItemType Directory -Force -Path $targetModules | Out-Null
    foreach ($module in @('playwright','playwright-core')) {
        $sourceModule = Join-Path $bundledModules $module
        if (-not (Test-Path -LiteralPath (Join-Path $sourceModule 'package.json') -PathType Leaf)) { throw "随包 Node 模块缺失：$module" }
        Copy-Item -LiteralPath $sourceModule -Destination $targetModules -Recurse -Force
    }
} else {
    Checked $toolchain.Npm @('ci')
}
Checked $toolchain.Npx @('playwright','install','chromium')
Checked $python @((Join-Path $root 'scripts\ensure_node_runtime.py'))
$modelAsset = Join-Path $assetRoot 'models\resnet50-11ad3fa6.pth'
if (Test-Path -LiteralPath $modelAsset -PathType Leaf) {
    $modelCache = Join-Path $env:TORCH_HOME 'hub\checkpoints'
    New-Item -ItemType Directory -Force -Path $modelCache | Out-Null
    Copy-Item -LiteralPath $modelAsset -Destination (Join-Path $modelCache 'resnet50-11ad3fa6.pth') -Force
} elseif (-not $SkipModelDownload) {
    Checked $python @('-c','from torchvision.models import resnet50, ResNet50_Weights; resnet50(weights=ResNet50_Weights.IMAGENET1K_V2)')
}
$stage = '准备 AdsPower'
if (-not $SkipAdsPower) {
    $detectAdsPower = @('scripts/company_setup.py', '--find-adspower')
    if ($CompanyConfig) { $detectAdsPower += @('--company-config', $CompanyConfig) }
    $installed = & $python @detectAdsPower
    if ($LASTEXITCODE -ne 0) { throw 'AdsPower 已安装路径检测失败，未启动重复安装。' }
    if (-not $installed) {
        $downloads = Join-Path $root 'runtime\downloads'
        New-Item -ItemType Directory -Force -Path $downloads | Out-Null
        $html = (Invoke-WebRequest 'https://www.adspower.com/download' -UseBasicParsing).Content
        $match = [regex]::Match($html, 'https://version\.adspower\.net/software/win64-global/[^"\s<>]+\.exe')
        if (-not $match.Success) { throw '无法从 AdsPower 官方下载页解析 Windows 安装器，未执行未知下载地址。' }
        $installer = Join-Path $downloads 'AdsPower-setup.exe'
        Invoke-WebRequest $match.Value -OutFile $installer -UseBasicParsing
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne 'Valid') { throw 'AdsPower 安装器数字签名无效，停止安装。' }
        (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash | Set-Content (Join-Path $downloads 'AdsPower.sha256')
        $process = Start-Process -FilePath $installer -ArgumentList '/S' -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "AdsPower 安装失败: $($process.ExitCode)" }
        $installed = & $python @detectAdsPower
        if ($LASTEXITCODE -ne 0 -or -not $installed) { throw 'AdsPower 安装器结束后仍找不到程序，请检查安装目录。' }
    }
}
$stage = '导入已确认的公司配置'
if ($CompanyConfig) { Checked $python @('scripts/company_setup.py','--import-file',$CompanyConfig) }
$stage = '运行准备检查'
Checked $python @('scripts/company_setup.py','--prepare-kernel')
Checked $python @('scripts/generate_agent_config.py')
Checked $python @('scripts/selection.py','doctor')
Checked $python @('scripts/verify_deployment.py')
Write-DeploymentStatus -Root $root -Stage '完成' -Succeeded $true -RolledBack $false
Write-Output '完整工作流的本机准备检查已通过。双击 start-web.cmd 打开选品工作台；可选 Skill 位于 runtime/agent-config/skills/amazon-temu-automation，MCP 配置位于 runtime/agent-config。'
} catch {
    $restored = $false
    if ($transaction) {
        try { Restore-ProjectDeployment $transaction; $restored = $true }
        catch { Write-Warning '自动恢复未完成；备份仍保留在 .deployment-backup，请勿删除。' }
    }
    Write-DeploymentStatus -Root $root -Stage $stage -Succeeded $false -RolledBack $restored
    Write-Error "部署在【$stage】失败。项目环境恢复状态：$restored。原数据与断点保留；查看上方命令错误和 runtime/install-status.json。" -ErrorAction Continue
    exit 1
}
