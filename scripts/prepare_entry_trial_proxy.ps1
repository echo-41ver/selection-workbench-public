param([Parameter(Mandatory=$true)][string]$OutputDirectory,
      [ValidateRange(1024,65535)][int]$Port=17941)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$settings=Get-Content -LiteralPath (Join-Path $taskRoot 'runtime/secrets/proxyshare.settings.json') -Raw | ConvertFrom-Json
$credential=Import-Clixml -LiteralPath (Join-Path $taskRoot 'runtime/secrets/proxyshare.credential.xml')
$token=[guid]::NewGuid().ToString('N').Substring(0,12)
$taskUser=[regex]::Replace($credential.UserName,'_session-[^_]+',"_session-$token")
if($taskUser -eq $credential.UserName){throw 'Credential has no replaceable session segment'}
if(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue){throw 'Isolated proxy port is occupied'}
$env:TEMU_CHAIN_LISTEN_HOST='127.0.0.1'
$env:TEMU_CHAIN_LISTEN_PORT=[string]$Port
$env:TEMU_CHAIN_WORKER='32'
$env:TEMU_CHAIN_ACTIVITY_DIR=Join-Path $taskRoot 'runtime/proxyshare-chains/activity/worker-32'
$env:TEMU_PREPROXY_HOST=[string]$settings.preproxy_host
$env:TEMU_PREPROXY_PORT=[string]$settings.preproxy_port
$env:TEMU_PREPROXY_PROTOCOL=[string]$settings.preproxy_protocol
if(!$env:TEMU_PREPROXY_PROTOCOL){$env:TEMU_PREPROXY_PROTOCOL='SOCKS5'}
$env:PROXYSHARE_PROXY_HOST=[string]$settings.proxy_host
$env:PROXYSHARE_PROXY_PORT=[string]$settings.proxy_port
$env:PROXYSHARE_PROXY_USER=$taskUser
$env:PROXYSHARE_PROXY_PASSWORD=$credential.GetNetworkCredential().Password
try{
  $scriptPath=Join-Path $taskRoot 'scripts/proxyshare_chain_proxy.mjs'
  $p=Start-Process -FilePath 'node' -ArgumentList @(('"{0}"' -f $scriptPath)) -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $OutputDirectory 'proxy.stdout.log') -RedirectStandardError (Join-Path $OutputDirectory 'proxy.stderr.log')
  @{pid=$p.Id;port=$Port;session_token=$token;worker=32}|ConvertTo-Json -Compress
}finally{
  Remove-Item Env:PROXYSHARE_PROXY_PASSWORD,Env:PROXYSHARE_PROXY_USER -ErrorAction SilentlyContinue
}
