[CmdletBinding()]
param([string]$CompanyConfig,[switch]$AcceptCompanyConfig,[switch]$SkipAdsPower,[switch]$SkipModelDownload,[switch]$SkipProxyClient,[switch]$PreflightOnly,[string]$WorkerTicket)
$ErrorActionPreference='Stop'
$env:PSModulePath=(Join-Path $PSHOME 'Modules')+';'+$env:PSModulePath
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$utf8=New-Object Text.UTF8Encoding($false)
$root=[IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$entryPath=[IO.Path]::GetFullPath($PSCommandPath)
$runtime=Join-Path $root 'runtime\deployment-entry'
$activePath=Join-Path $runtime 'active.json'
$systemPS=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$sessionId=[Diagnostics.Process]::GetCurrentProcess().SessionId

Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Diagnostics;
using System.Threading.Tasks;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
public sealed class SelectionEntryCapture {
 public Process Process; public Task Output, Error; public string CaptureError;
 private static readonly Encoding Utf8=new UTF8Encoding(false);
 public static string Clean(string value) {
  value=Regex.Replace(value,@"(?i)\b(Bearer|Basic)\s+[^\s\x22'<>]+","$1 [REDACTED]");
  value=Regex.Replace(value,@"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@","$1[REDACTED]@");
  value=Regex.Replace(value,@"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-(?:proj-)?[A-Za-z0-9_-]+)\b","[REDACTED TOKEN]");
  value=Regex.Replace(value,@"(?i)([\w.-]*(?:token|secret|password|passwd|pwd|api.?key|access.?key|private.?key|authorization|cookie|credential|session)[\w.-]*[\x22']?\s*[:=]\s*)(?:\x22[^\x22]*\x22|'[^']*'|[^\s,;}]+)","$1[REDACTED]");
  value=Regex.Replace(value,@"(?i)(--(?:api[_-]?key|token|secret|password|passwd|pwd|cookie)[=\s]+)(?:\x22[^\x22]*\x22|'[^']*'|\S+)","$1[REDACTED]");
  value=Regex.Replace(value,@"\?[^\s\x22'<>]+","?[REDACTED QUERY]");
  value=Regex.Replace(value,@"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{40,}(?![A-Za-z0-9])","[REDACTED OPAQUE VALUE]");
  return Regex.IsMatch(value,@"(?i)(company[ _-]?config|公司配置)\s*[:=]")?"[REDACTED COMPANY CONFIGURATION]":value;
 }
 private void Pump(StreamReader reader,string path) {
  bool privateBlock=false,oversized=false;var line=new StringBuilder();
  try { int value;while((value=reader.Read())>=0){
   char c=(char)value;if(c=='\n'){WriteLine(path,line.ToString(),ref privateBlock,oversized);line.Clear();oversized=false;}
   else if(c!='\r'&&!oversized){if(line.Length>=65536){line.Clear();oversized=true;}else line.Append(c);}
  }if(line.Length>0||oversized)WriteLine(path,line.ToString(),ref privateBlock,oversized);
  } catch(Exception e){CaptureError=Clean(e.Message);}
 }
 private void WriteLine(string path,string line,ref bool block,bool oversized) {
  if(Regex.IsMatch(line,@"-----BEGIN [^-]*(PRIVATE KEY|CERTIFICATE)-----"))block=true;
  string text=block?"[REDACTED SECRET BLOCK]":oversized?"[REDACTED OVERSIZED OUTPUT LINE]":Clean(line);
  if(Regex.IsMatch(line,@"-----END [^-]*(PRIVATE KEY|CERTIFICATE)-----"))block=false;
  File.AppendAllText(path,DateTimeOffset.Now.ToString("o")+" "+text+Environment.NewLine,Utf8);
 }
 public void Start(string exe,string arguments,string cwd,string stdout,string stderr) {
  Process=new Process();Process.StartInfo=new ProcessStartInfo(exe,arguments);
  Process.StartInfo.WorkingDirectory=cwd;Process.StartInfo.UseShellExecute=false;Process.StartInfo.CreateNoWindow=true;
  Process.StartInfo.RedirectStandardInput=true;Process.StartInfo.RedirectStandardOutput=true;Process.StartInfo.RedirectStandardError=true;
  Process.StartInfo.StandardOutputEncoding=Utf8;Process.StartInfo.StandardErrorEncoding=Utf8;
  Process.Start();Output=Task.Run(()=>Pump(Process.StandardOutput,stdout));Error=Task.Run(()=>Pump(Process.StandardError,stderr));
 }
 [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] private struct StartupInfo {
  public int cb; public string reserved,desktop,title; public int x,y,xSize,ySize,xChars,yChars,fill,flags;
  public short showWindow,reservedLength; public IntPtr reservedBytes,input,output,error;
 }
 [StructLayout(LayoutKind.Sequential)] private struct StartupInfoEx {public StartupInfo info;public IntPtr attributes;}
 [StructLayout(LayoutKind.Sequential)] private struct ProcessInfo {public IntPtr process,thread;public int pid,threadId;}
 [DllImport("kernel32.dll",SetLastError=true)] private static extern bool InitializeProcThreadAttributeList(IntPtr list,int count,int flags,ref IntPtr bytes);
 [DllImport("kernel32.dll",SetLastError=true)] private static extern bool UpdateProcThreadAttribute(IntPtr list,uint flags,IntPtr attribute,IntPtr value,IntPtr bytes,IntPtr previous,IntPtr returnedBytes);
 [DllImport("kernel32.dll")] private static extern void DeleteProcThreadAttributeList(IntPtr list);
 [DllImport("kernel32.dll",SetLastError=true)] private static extern bool SetHandleInformation(IntPtr handle,uint mask,uint flags);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern bool CreateProcessW(string application,StringBuilder command,IntPtr processSecurity,IntPtr threadSecurity,bool inherit,uint flags,IntPtr environment,string cwd,ref StartupInfoEx startup,out ProcessInfo process);
 [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
 public static Process StartDetached(string exe,string arguments,string cwd,string stdin,string stdout,string stderr) {
  // A redirected caller may own inheritable pipe handles unrelated to the three
  // standard handles. Only an explicit handle list keeps a detached worker from
  // retaining those pipes after the observer returns its real timeout code.
  using(var input=new FileStream(stdin,FileMode.CreateNew,FileAccess.ReadWrite,FileShare.Read))
  using(var output=new FileStream(stdout,FileMode.CreateNew,FileAccess.Write,FileShare.Read))
  using(var error=new FileStream(stderr,FileMode.CreateNew,FileAccess.Write,FileShare.Read)) {
   var handles=new[]{input.SafeFileHandle.DangerousGetHandle(),output.SafeFileHandle.DangerousGetHandle(),error.SafeFileHandle.DangerousGetHandle()};
   IntPtr list=IntPtr.Zero,values=IntPtr.Zero,bytes=IntPtr.Zero;bool initialized=false;
   try {
    foreach(var handle in handles)if(!SetHandleInformation(handle,1,1))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    InitializeProcThreadAttributeList(IntPtr.Zero,1,0,ref bytes);
    int sizingError=Marshal.GetLastWin32Error();
    if(bytes==IntPtr.Zero||sizingError!=122)throw new System.ComponentModel.Win32Exception(sizingError);
    list=Marshal.AllocHGlobal(bytes);
    if(!InitializeProcThreadAttributeList(list,1,0,ref bytes))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    initialized=true;values=Marshal.AllocHGlobal(IntPtr.Size*handles.Length);Marshal.Copy(handles,0,values,handles.Length);
    if(!UpdateProcThreadAttribute(list,0,new IntPtr(0x20002),values,new IntPtr(IntPtr.Size*handles.Length),IntPtr.Zero,IntPtr.Zero))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    var startup=new StartupInfoEx();startup.info.cb=Marshal.SizeOf(typeof(StartupInfoEx));startup.info.flags=0x100;
    startup.info.input=handles[0];startup.info.output=handles[1];startup.info.error=handles[2];startup.attributes=list;
    ProcessInfo created;
    if(!CreateProcessW(exe,new StringBuilder("\""+exe+"\" "+arguments),IntPtr.Zero,IntPtr.Zero,true,0x08080000,IntPtr.Zero,cwd,ref startup,out created))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    try{return Process.GetProcessById(created.pid);}finally{CloseHandle(created.thread);CloseHandle(created.process);}
   }finally{if(initialized)DeleteProcThreadAttributeList(list);if(list!=IntPtr.Zero)Marshal.FreeHGlobal(list);if(values!=IntPtr.Zero)Marshal.FreeHGlobal(values);}
  }
 }
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern uint GetFinalPathNameByHandle(IntPtr handle,StringBuilder path,uint length,uint flags);
 public static string FinalPath(IntPtr handle){var b=new StringBuilder(32768);if(GetFinalPathNameByHandle(handle,b,(uint)b.Capacity,0)==0)throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());return b.ToString();}
 public static void ReplaceRecord(string source,string destination){File.Replace(source,destination,null);}
}
'@

function Write-Record([string]$Path,$Value){
 $temp=$Path+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
 [IO.File]::WriteAllText($temp,($Value|ConvertTo-Json -Depth 15),$utf8)
 if([IO.File]::Exists($Path)){[SelectionEntryCapture]::ReplaceRecord($temp,$Path)}else{[IO.File]::Move($temp,$Path)}
}
function Read-Record([string]$Path){
 $deadline=[DateTime]::UtcNow.AddSeconds(3)
 while($true){
  if(-not [IO.File]::Exists($Path)){return $null}
  $stream=$null;$reader=$null
  try{
   # Records are atomically published, but Windows readers/scanners may still
   # hold a write/delete handle. Cooperate without blocking the next publication.
   $share=[IO.FileShare]([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
   $stream=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,$share)
   $reader=[IO.StreamReader]::new($stream,$utf8,$true)
   return ($reader.ReadToEnd()|ConvertFrom-Json)
  }catch{
   $errorBase=$_.Exception.GetBaseException();$native=$errorBase.HResult -band 65535
   if(-not ($errorBase -is [IO.IOException]) -or $native -notin @(32,33) -or [DateTime]::UtcNow -ge $deadline){throw}
  }finally{if($reader){$reader.Dispose()};if($stream){$stream.Dispose()}}
  Start-Sleep -Milliseconds 50
 }
}
function File-Sha([string]$Path){$hash=[Security.Cryptography.SHA256]::Create();$hashStream=[IO.File]::OpenRead($Path);try{return ([BitConverter]::ToString($hash.ComputeHash($hashStream))).Replace('-','').ToLowerInvariant()}finally{$hashStream.Dispose();$hash.Dispose()}}
function Same-Path([string]$A,[string]$B){return [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath($A),[IO.Path]::GetFullPath($B))}
function Quote-Native([string]$Value){return '"'+($Value -replace '(\\*)"','$1$1\"' -replace '(\\+)$','$1$1')+'"'}
function Get-Limit([string]$Name,[int]$Default,[int]$Maximum){$v=[Environment]::GetEnvironmentVariable($Name);$n=0;if($v -and [int]::TryParse($v,[ref]$n) -and $n -ge 1 -and $n -le $Maximum){return $n};return $Default}
function Get-ContextView {
 $path=Join-Path $env:LOCALAPPDATA ('SelectionWorkbenchDeploymentProbe-'+[Guid]::NewGuid().ToString('N')+'.tmp')
 $file=$null;$physical=$null
 try{$file=New-Object IO.FileStream($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read);$physical=[SelectionEntryCapture]::FinalPath($file.SafeFileHandle.DangerousGetHandle());return [ordered]@{logical_path=$path;final_path=$physical;mapped_to_msix_localcache=($physical -match '(?i)\\Packages\\[^\\]+\\LocalCache\\')}}
 finally{if($file){$file.Dispose();[IO.File]::Delete($path)}}
}
function Get-Identity {return [ordered]@{pid=$PID;start_time_utc=[Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().ToString('o');sid=$sid;session_id=$sessionId}}
function Test-Live($Identity){try{$p=[Diagnostics.Process]::GetProcessById([int]$Identity.pid);return [math]::Abs(($p.StartTime.ToUniversalTime()-([datetime]$Identity.start_time_utc).ToUniversalTime()).TotalMilliseconds) -lt 10}catch{return $false}}
function Get-TaskFolder {$service=New-Object -ComObject 'Schedule.Service';$service.Connect();return $service.GetFolder('\')}
function Task-Arguments($Request){return '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File '+(Quote-Native $entryPath)+' -WorkerTicket '+(Quote-Native (Join-Path $Request.attempt_dir 'request.json'))}
function Assert-OwnTask($Task,$Request){
 $d=$Task.Definition;$user=$d.Principal.UserId
 if($user -notmatch '^S-1-'){$user=(New-Object Security.Principal.NTAccount($user)).Translate([Security.Principal.SecurityIdentifier]).Value}
 if($Task.Name -ne $Request.task_name -or $d.RegistrationInfo.Description -ne ('Selection deployment entry '+$Request.nonce) -or $user -ne $Request.sid -or $d.Principal.LogonType -ne 3 -or $d.Principal.RunLevel -ne 0 -or $d.Triggers.Count -ne 0 -or $d.Actions.Count -ne 1 -or -not (Same-Path $d.Actions.Item(1).Path $systemPS) -or $d.Actions.Item(1).Arguments -cne (Task-Arguments $Request) -or -not (Same-Path $d.Actions.Item(1).WorkingDirectory $root)){throw 'Deployment task identity/action does not match this request.'}
}
function Cleanup-Task($Request){
 if($Request.mode -ne 'scheduled'){return $true}
 try{
  $folder=Get-TaskFolder;$task=$folder.GetTask($Request.task_name);Assert-OwnTask $task $Request
  for($n=0;$n -lt 10 -and [int]$task.State -in @(2,4);$n++){Start-Sleep -Milliseconds 100;$task=$folder.GetTask($Request.task_name);Assert-OwnTask $task $Request}
  if([int]$task.State -in @(2,4)){Write-Record (Join-Path $Request.attempt_dir 'task-cleanup.json') @{task_name=$Request.task_name;deleted=$false;reason='task_still_running';at=[DateTimeOffset]::Now.ToString('o')};return $false}
  $folder.DeleteTask($Request.task_name,0);Write-Record (Join-Path $Request.attempt_dir 'task-cleanup.json') @{task_name=$Request.task_name;deleted=$true;at=[DateTimeOffset]::Now.ToString('o')};return $true
 }catch{if(($_.Exception.GetBaseException().HResult -band 65535) -eq 2){return $true};Write-Record (Join-Path $Request.attempt_dir 'task-cleanup.json') @{task_name=$Request.task_name;deleted=$false;error=[SelectionEntryCapture]::Clean($_.Exception.Message)};return $false}
}
function Relay-Logs($Request,$State){
 foreach($name in @('stdout','stderr')){
  $path=Join-Path $Request.attempt_dir ($name+'.log');if(-not [IO.File]::Exists($path)){continue}
  $stream=$null
  try{$stream=New-Object IO.FileStream($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite);[void]$stream.Seek($State[$name].offset,[IO.SeekOrigin]::Begin);$available=[math]::Min(262144,$stream.Length-$State[$name].offset);if($available -le 0){continue};$bytes=New-Object byte[] ([int]$available);$count=$stream.Read($bytes,0,$bytes.Length);$State[$name].offset+=$count;$chars=New-Object char[] ($count+2);$n=$State[$name].decoder.GetChars($bytes,0,$count,$chars,0,$false);$text=New-Object string($chars,0,$n);if($name -eq 'stderr'){[Console]::Error.Write($text)}else{[Console]::Out.Write($text)}}finally{if($stream){$stream.Dispose()}}
 }
}
function Invoke-NativePhase($Request,[string]$Phase,[string]$Script,[string[]]$Arguments){
 $argumentsText='-NoProfile -ExecutionPolicy Bypass -File '+(Quote-Native $Script)
 foreach($arg in $Arguments){$argumentsText+=' '+(Quote-Native $arg)}
 $capture=New-Object SelectionEntryCapture
 $capture.Start($systemPS,$argumentsText,$root,(Join-Path $Request.attempt_dir 'stdout.log'),(Join-Path $Request.attempt_dir 'stderr.log'))
 $identity=@{pid=$capture.Process.Id;start_time_utc=$capture.Process.StartTime.ToUniversalTime().ToString('o')}
 Write-Record (Join-Path $Request.attempt_dir 'phase.json') @{nonce=$Request.nonce;phase=$Phase;process=$identity;at=[DateTimeOffset]::Now.ToString('o')}
 while(-not $capture.Process.HasExited){Start-Sleep -Milliseconds 200}
 $code=$capture.Process.ExitCode
 [void][Threading.Tasks.Task]::WaitAll(@($capture.Output,$capture.Error),2000)
 if($capture.CaptureError){throw ('Output capture failed: '+$capture.CaptureError)}
 return [ordered]@{phase=$Phase;native_exit_code=$code;process=$identity;stdout_closed=$capture.Output.IsCompleted;stderr_closed=$capture.Error.IsCompleted;ended_at=[DateTimeOffset]::Now.ToString('o')}
}
function Invoke-Worker([string]$Ticket){
 $workerLock=$null;$request=$null;$result=$null;$claimed=$false
 try{
  $ticketFull=[IO.Path]::GetFullPath($Ticket)
  if(-not $ticketFull.StartsWith($runtime+'\',[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($ticketFull) -ne 'request.json'){throw 'Worker ticket is outside this deployment root.'}
  $request=Read-Record $ticketFull
  if(-not $request -or $request.nonce -notmatch '^[a-f0-9]{32}$' -or -not (Same-Path $request.root $root) -or -not (Same-Path $request.attempt_dir (Join-Path $runtime $request.nonce)) -or -not (Same-Path $ticketFull (Join-Path $request.attempt_dir 'request.json')) -or -not (Same-Path $request.entry_path $entryPath) -or $request.entry_sha256 -ne (File-Sha $entryPath) -or $request.sid -ne $sid -or [int]$request.session_id -ne $sessionId){throw 'Worker root, version, nonce, SID or session validation failed.'}
  $active=Read-Record $activePath
  if(-not $active -or $active.nonce -ne $request.nonce -or (Test-Path -LiteralPath (Join-Path $request.attempt_dir 'result.json'))){throw 'Worker request is stale or already completed.'}
  $claim=New-Object IO.FileStream((Join-Path $request.attempt_dir 'worker-claim.json'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
  try{$claimBytes=$utf8.GetBytes(((Get-Identity)|ConvertTo-Json));$claim.Write($claimBytes,0,$claimBytes.Length);$claimed=$true}finally{$claim.Dispose()}
  $workerLock=New-Object IO.FileStream((Join-Path $runtime 'worker.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
  if($request.mode -eq 'scheduled'){$folder=Get-TaskFolder;Assert-OwnTask ($folder.GetTask($request.task_name)) $request}elseif($request.mode -ne 'direct'){throw 'Unknown worker mode.'}
  $view=Get-ContextView
  if($view.mapped_to_msix_localcache){throw 'Worker still has an MSIX LocalCache file view; no installation was started.'}
  $identity=Get-Identity
  Write-Record (Join-Path $request.attempt_dir 'worker-started.json') @{nonce=$request.nonce;identity=$identity;view=$view;entry_sha256=$request.entry_sha256;started_at=[DateTimeOffset]::Now.ToString('o')}
  $deadline=[DateTime]::UtcNow.AddSeconds([int]$request.handshake_seconds)
  do{$ack=Read-Record (Join-Path $request.attempt_dir 'parent-ack.json');if($ack -and $ack.nonce -eq $request.nonce -and $ack.parent_pid -eq $request.parent.pid){break};if(-not (Test-Live $request.parent)){throw 'Parent ended before the installation handshake; no installation was started.'};Start-Sleep -Milliseconds 200}while([DateTime]::UtcNow -lt $deadline)
  if(-not $ack -or $ack.nonce -ne $request.nonce -or $ack.parent_pid -ne $request.parent.pid){throw 'Parent handshake timed out; no installation was started.'}
  $nativeArgs=@();if($request.parameters.CompanyConfig){$nativeArgs+=@('-CompanyConfig',[string]$request.parameters.CompanyConfig)}
  foreach($name in @('AcceptCompanyConfig','SkipAdsPower','SkipModelDownload','SkipProxyClient','PreflightOnly')){if($request.parameters.$name){$nativeArgs+='-'+$name}}
  $install=Invoke-NativePhase $request 'install' (Join-Path $root 'install.ps1') $nativeArgs
  $code=[int]$install.native_exit_code;$web=$null
  if($code -eq 0 -and -not $request.parameters.PreflightOnly){$web=Invoke-NativePhase $request 'start-web' (Join-Path $root 'start-web.ps1') @();$code=[int]$web.native_exit_code}
  $result=@{nonce=$request.nonce;completed=$true;native_exit_code=$code;worker=$identity;install=$install;web=$web;preflight_only=[bool]$request.parameters.PreflightOnly;deployment_checks='owned_by_install_ps1';ended_at=[DateTimeOffset]::Now.ToString('o')}
 }catch{
  if($claimed){$result=@{nonce=$request.nonce;completed=$true;native_exit_code=70;worker=(Get-Identity);error=[SelectionEntryCapture]::Clean($_.Exception.Message);ended_at=[DateTimeOffset]::Now.ToString('o')}}
  else{[Console]::Error.WriteLine([SelectionEntryCapture]::Clean($_.Exception.Message))}
 }finally{if($result){Write-Record (Join-Path $request.attempt_dir 'result.json') $result};if($workerLock){$workerLock.Dispose()}}
 if($result){return [int]$result.native_exit_code};return 70
}

if($WorkerTicket){exit (Invoke-Worker $WorkerTicket)}
$entryLock=$null;$request=$null;$finished=$false;$exitCode=70;$dispatched=$false;$taskCreated=$false
try{
 [void][IO.Directory]::CreateDirectory($runtime)
 try{$entryLock=New-Object IO.FileStream((Join-Path $runtime 'entry.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)}catch{throw 'Another deployment entry is observing this root; no new worker was dispatched.'}
 $previous=Read-Record $activePath
 if($previous){
  if($previous.nonce -notmatch '^[a-f0-9]{32}$' -or -not (Same-Path $previous.attempt_dir (Join-Path $runtime $previous.nonce))){throw 'The active deployment record does not belong to this root; no worker was dispatched.'}
  $old=Read-Record (Join-Path $previous.attempt_dir 'request.json');$oldResult=Read-Record (Join-Path $previous.attempt_dir 'result.json')
  if(-not $oldResult -or -not $oldResult.completed -or $oldResult.nonce -ne $previous.nonce -or (Test-Live $oldResult.worker)){throw ('A previous worker is running or its completion is unconfirmed. No duplicate was dispatched. Evidence: '+$previous.attempt_dir)}
  if(-not (Cleanup-Task $old)){throw 'The previous deployment task is still running or its identity is unconfirmed; no worker was dispatched.'};[IO.File]::Delete($activePath)
 }
 $view=Get-ContextView
 $nonce=[Guid]::NewGuid().ToString('N');$attempt=Join-Path $runtime $nonce;[void][IO.Directory]::CreateDirectory($attempt)
 $parameters=@{CompanyConfig=$CompanyConfig;AcceptCompanyConfig=[bool]$AcceptCompanyConfig;SkipAdsPower=[bool]$SkipAdsPower;SkipModelDownload=[bool]$SkipModelDownload;SkipProxyClient=[bool]$SkipProxyClient;PreflightOnly=[bool]$PreflightOnly}
 if($CompanyConfig -and -not [IO.Path]::IsPathRooted($CompanyConfig)){$parameters.CompanyConfig=[IO.Path]::GetFullPath((Join-Path $root $CompanyConfig))}
 $request=[pscustomobject]@{nonce=$nonce;root=$root;entry_path=$entryPath;entry_sha256=(File-Sha $entryPath);attempt_dir=$attempt;sid=$sid;session_id=$sessionId;parent=(Get-Identity);parameters=$parameters;mode=$(if($view.mapped_to_msix_localcache){'scheduled'}else{'direct'});task_name=('SelectionDeployment-'+$nonce);handshake_seconds=(Get-Limit 'SELECTION_DEPLOYMENT_HANDSHAKE_SECONDS' 60 300);wait_seconds=(Get-Limit 'SELECTION_DEPLOYMENT_WAIT_SECONDS' 14400 86400);created_at=[DateTimeOffset]::Now.ToString('o');parent_view=$view}
 Write-Record (Join-Path $attempt 'request.json') $request
 Write-Record $activePath @{nonce=$nonce;attempt_dir=$attempt;mode=$request.mode;task_name=$request.task_name;created_at=$request.created_at}
 [Console]::Out.WriteLine('Deployment entry evidence: '+$attempt)
 if($request.mode -eq 'scheduled'){
  if($sessionId -le 0){throw 'An interactive Windows session is required for deployment.'}
  foreach($proxyName in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY')){if([Environment]::GetEnvironmentVariable($proxyName) -and [Environment]::GetEnvironmentVariable($proxyName) -ne [Environment]::GetEnvironmentVariable($proxyName,'User') -and [Environment]::GetEnvironmentVariable($proxyName) -ne [Environment]::GetEnvironmentVariable($proxyName,'Machine')){throw 'Process-only proxy settings cannot be securely transferred. Launch deploy.cmd from your normal configured terminal; no proxy setting was changed.'}}
  $service=New-Object -ComObject 'Schedule.Service';$service.Connect();$folder=$service.GetFolder('\');$definition=$service.NewTask(0)
  $definition.RegistrationInfo.Description='Selection deployment entry '+$nonce;$definition.Principal.UserId=$sid;$definition.Principal.LogonType=3;$definition.Principal.RunLevel=0
  $definition.Settings.Enabled=$true;$definition.Settings.AllowDemandStart=$true;$definition.Settings.StartWhenAvailable=$false;$definition.Settings.ExecutionTimeLimit='PT0S';$definition.Settings.DisallowStartIfOnBatteries=$false;$definition.Settings.StopIfGoingOnBatteries=$false
  $action=$definition.Actions.Create(0);$action.Path=$systemPS;$action.Arguments=Task-Arguments $request;$action.WorkingDirectory=$root
  $task=$folder.RegisterTaskDefinition($request.task_name,$definition,2,$sid,$null,3,$null);$taskCreated=$true;Assert-OwnTask $task $request
  $running=$task.RunEx($null,4,$sessionId,$null)
  $dispatched=$true
  Write-Record (Join-Path $attempt 'dispatch.json') @{task_name=$request.task_name;instance_guid=$running.InstanceGuid;sid=$sid;session_id=$sessionId;logon_type=3;run_level=0;at=[DateTimeOffset]::Now.ToString('o')}
 }else{
  $workerInput=Join-Path $attempt 'worker.stdin.txt';$workerOutput=Join-Path $attempt 'worker.stdout.log';$workerError=Join-Path $attempt 'worker.stderr.log'
  $process=[SelectionEntryCapture]::StartDetached($systemPS,(Task-Arguments $request),$root,$workerInput,$workerOutput,$workerError)
  $dispatched=$true
  Write-Record (Join-Path $attempt 'dispatch.json') @{process=@{pid=$process.Id;start_time_utc=$process.StartTime.ToUniversalTime().ToString('o')};stdio=@{mode='attempt_files_handle_allowlist';stdout=$workerOutput;stderr=$workerError};at=[DateTimeOffset]::Now.ToString('o')}
 }
 $state=@{stdout=@{offset=[long]0;decoder=$utf8.GetDecoder()};stderr=@{offset=[long]0;decoder=$utf8.GetDecoder()}}
 $handshakeDeadline=[DateTime]::UtcNow.AddSeconds($request.handshake_seconds);$completionDeadline=$null;$acknowledged=$false
 while($true){
  Relay-Logs $request $state
  $hello=Read-Record (Join-Path $attempt 'worker-started.json')
  if($hello -and -not $acknowledged){
   if($hello.nonce -ne $nonce -or $hello.identity.sid -ne $sid -or [int]$hello.identity.session_id -ne $sessionId -or $hello.entry_sha256 -ne $request.entry_sha256 -or $hello.view.mapped_to_msix_localcache -or -not (Test-Live $hello.identity)){throw 'Worker handshake identity/context mismatch; installation was not acknowledged.'}
   Write-Record (Join-Path $attempt 'parent-ack.json') @{nonce=$nonce;parent_pid=$PID;at=[DateTimeOffset]::Now.ToString('o')};$acknowledged=$true;$completionDeadline=[DateTime]::UtcNow.AddSeconds($request.wait_seconds)
  }
  $result=Read-Record (Join-Path $attempt 'result.json')
  if($result -and $result.nonce -eq $nonce -and $result.completed){$parsedCode=0;if($null -eq $result.native_exit_code -or -not [int]::TryParse([string]$result.native_exit_code,[ref]$parsedCode) -or ($parsedCode -eq 0 -and -not $acknowledged)){throw 'Worker result lacks a verified handshake or a real native exit code.'};$exitCode=$parsedCode;$finished=$true;break}
  if(( -not $acknowledged -and [DateTime]::UtcNow -ge $handshakeDeadline) -or ($acknowledged -and [DateTime]::UtcNow -ge $completionDeadline)){
   Write-Record (Join-Path $attempt 'parent-timeout.json') @{nonce=$nonce;phase=$(if($acknowledged){'waiting_for_worker_completion'}else{'waiting_for_handshake'});worker=$hello.identity;at=[DateTimeOffset]::Now.ToString('o');processes_killed=$false}
   [Console]::Error.WriteLine('Deployment observation timed out. Worker/task may still be running; no process was stopped. Do not start another deployment. Evidence: '+$attempt);$exitCode=124;break
  }
  Start-Sleep -Milliseconds 200
 }
 if($finished){
  for($i=0;$i -lt 10;$i++){Relay-Logs $request $state;if(-not (Test-Live $result.worker)){break};Start-Sleep -Milliseconds 100}
  $taskCleaned=Cleanup-Task $request
  if(-not (Test-Live $result.worker) -and $taskCleaned){[IO.File]::Delete($activePath)}
  if($exitCode -ne 0 -and $result.error){[Console]::Error.WriteLine($result.error)}
 }
}catch{
 [Console]::Error.WriteLine([SelectionEntryCapture]::Clean($_.Exception.Message))
 if($request){
  $released=$false
  if(-not $dispatched){
   $safeToRelease=$true
   if($taskCreated){try{$ownTask=(Get-TaskFolder).GetTask($request.task_name);Assert-OwnTask $ownTask $request;if([int]$ownTask.State -in @(2,4)){$safeToRelease=$false}else{$safeToRelease=Cleanup-Task $request}}catch{$safeToRelease=$false}}
   if($safeToRelease){$active=Read-Record $activePath;if($active -and $active.nonce -eq $request.nonce){[IO.File]::Delete($activePath);$released=$true}}
  }
  Write-Record (Join-Path $request.attempt_dir 'parent-error.json') @{nonce=$request.nonce;error=[SelectionEntryCapture]::Clean($_.Exception.Message);at=[DateTimeOffset]::Now.ToString('o');dispatched=$dispatched;active_released_before_dispatch=$released;processes_killed=$false}
 }
 $exitCode=70
}finally{if($entryLock){$entryLock.Dispose()}}
if($exitCode -ne 0){
 $support=Join-Path $PSScriptRoot 'deployment_support.ps1'
 if(Test-Path -LiteralPath $support){
  try{
   . $support
   $failedStage=if($result -and $result.web -and [int]$result.web.native_exit_code -ne 0){'启动工作台'}else{''}
   $message=if($result -and $result.error){[string]$result.error}elseif($request){$record=Read-Record (Join-Path $request.attempt_dir 'parent-error.json');[string]$record.error}else{''}
   Show-DeploymentFailure -Root $root -NativeExitCode $exitCode -Stage $failedStage -Message $message
  }catch{[Console]::Error.WriteLine('部署未完成。请双击程序目录中的导出诊断.cmd，将诊断包交给维护者。')}
 }else{[Console]::Error.WriteLine('部署文件不完整，请重新下载完整公司包并完整解压后运行 deploy.cmd。')}
}
exit $exitCode
