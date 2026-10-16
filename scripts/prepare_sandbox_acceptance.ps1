[CmdletBinding()]
param([string]$ManifestPath)
$ErrorActionPreference = 'Stop'
# An agent or PowerShell 7 parent may omit Windows PowerShell's built-in modules.
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $env:PSModulePath
$root = Split-Path -Parent $PSScriptRoot
if (-not $ManifestPath) { $ManifestPath = Join-Path $root 'dist\最新部署包.json' }
$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $manifest.internal_package) { throw '完整沙盒安装验收需要公司内部分发包；公开包不能代替公司授权配置。' }
if ($manifest.commit -notmatch '^[0-9a-f]{40}$') { throw '发布清单的提交编号无效' }
$archive = Join-Path (Split-Path -Parent $ManifestPath) $manifest.internal_package
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $manifest.internal_sha256) { throw '部署包校验失败，未生成验收入口' }
$base = Join-Path $root ('runtime\sandbox-acceptance\' + $manifest.commit.Substring(0,7))
$inputDir = Join-Path $base 'input'
$results = Join-Path $base 'results'
New-Item -ItemType Directory -Path $inputDir,$results -Force | Out-Null
Copy-Item -LiteralPath $archive -Destination (Join-Path $inputDir 'release.zip') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'run_sandbox_acceptance.ps1') -Destination (Join-Path $inputDir 'run-acceptance.ps1') -Force
$manifest.internal_sha256 | Set-Content -LiteralPath (Join-Path $inputDir 'release.sha256') -Encoding ASCII
$manifest.commit | Set-Content -LiteralPath (Join-Path $inputDir 'release.commit') -Encoding ASCII
$escapedInput = [Security.SecurityElement]::Escape($inputDir)
$escapedResults = [Security.SecurityElement]::Escape($results)
$configuration = @"
<Configuration>
  <VGpu>Disable</VGpu>
  <Networking>Enable</Networking>
  <MemoryInMB>8192</MemoryInMB>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <MappedFolders>
    <MappedFolder><HostFolder>$escapedInput</HostFolder><SandboxFolder>C:\SelectionInput</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$escapedResults</HostFolder><SandboxFolder>C:\SelectionResults</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\SelectionInput\run-acceptance.ps1</Command></LogonCommand>
</Configuration>
"@
$launch = Join-Path $root 'dist\验收最新部署包.wsb'
[IO.File]::WriteAllText($launch,$configuration,[Text.UTF8Encoding]::new($false))
@{commit=$manifest.commit; sandbox=$launch; results=$results; scope='完整依赖与公司连接准备、离线回归、本地网页接口；不采集、不执行付费视觉分析'} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'dist\沙盒验收.json') -Encoding UTF8
Write-Output "沙盒验收入口已同步：$launch"
