[CmdletBinding()]
param(
    [string]$Username,
    [SecureString]$Password,
    [string]$ProxyHost = 'proxy.proxyshare.com',
    [ValidateRange(1, 65535)][int]$ProxyPort = 5959,
    [string]$PreProxyHost = '127.0.0.1',
    [ValidateRange(1, 65535)][int]$PreProxyPort = 7890,
    [string]$CredentialFile = 'runtime/secrets/proxyshare.credential.xml',
    [string]$SettingsFile = 'runtime/secrets/proxyshare.settings.json'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$credentialPath = Join-Path $root $CredentialFile
$settingsPath = Join-Path $root $SettingsFile

if (-not $Username) {
    $entered = Get-Credential -Message '输入 ProxyShare 住宅代理用户名和密码（仅加密保存在当前 Windows 用户）'
    $Username = $entered.UserName
    $Password = $entered.Password
} elseif (-not $Password) {
    $Password = Read-Host '输入 ProxyShare 住宅代理密码' -AsSecureString
}
if ($Username -notmatch '_session-[^_]+') {
    throw 'ProxyShare 用户名缺少 _session-... 段，无法为并行 worker 生成独立 session。'
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $credentialPath) | Out-Null
[PSCredential]::new($Username, $Password) | Export-Clixml -LiteralPath $credentialPath -Force
[ordered]@{
    schema_version = 'proxyshare-chain-settings-v1'
    proxy_host = $ProxyHost
    proxy_port = $ProxyPort
    preproxy_host = $PreProxyHost
    preproxy_port = $PreProxyPort
    chain_host = '127.0.0.1'
    chain_base_port = 17891
    worker_count = 4
    updated_at = (Get-Date).ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath $settingsPath -Encoding utf8

[pscustomobject]@{
    initialized = $true
    credential_file = $credentialPath
    settings_file = $settingsPath
    password_persisted_in_plaintext = $false
} | ConvertTo-Json -Compress
