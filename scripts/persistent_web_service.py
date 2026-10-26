"""Run the local console from a manual Windows task, independent of its caller."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def launch_diagnostic(path, identity, state, **fields):
    """Persist fixed launch phases, never configurations, credentials or commands."""
    try:prior=json.loads(path.read_text('utf-8-sig'))
    except (OSError,ValueError):prior={}
    row={**identity,'phase':prior.get('phase','powershell_start'),'state':state,'updated_at':time.time()}
    for name in ('error_type','hresult'):
        if name in prior:row[name]=prior[name]
    row.update(fields)
    from selection_automation.merge_temu_snapshots import atomic_write
    atomic_write(path,json.dumps(row,ensure_ascii=False,indent=2))
    with path.with_suffix('.jsonl').open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(row,ensure_ascii=False)+'\n');stream.flush()
    return row


def start(port, root=ROOT, amazon_category=None,resume_category=None, *, paid=False, preserve_prepared=True):
    root = Path(root).resolve()
    environment_path = root / f'runtime/web-console/service-path-{port}.json'
    environment_path.parent.mkdir(parents=True, exist_ok=True)
    environment_path.write_text(json.dumps({'PATH': os.environ.get('PATH', '')}), encoding='utf-8')
    name = 'SelectionWorkbench-' + hashlib.sha256(str(root).lower().encode()).hexdigest()[:12] + f'-{port}'
    category=amazon_category or resume_category
    if category:
        category=Path(category).resolve()
        if not category.is_relative_to((root/'runs').resolve()) or category.name!='category.json':
            raise ValueError('Expected a frozen category within this installation')
        name+=('-amazon-' if amazon_category else '-collect-')+hashlib.sha256(str(category).encode()).hexdigest()[:12]
    configuration = {'name': name, 'python': str(root / '.venv/Scripts/python.exe'),
                     'script': str(root / 'scripts/persistent_web_service.py'),
                     'root': str(root), 'port': port,'resume':bool(resume_category),
                     'priority':7 if category else 4}
    configuration['arguments']='"'+configuration['script']+'" --host --port '+str(port)
    if amazon_category:
        configuration['arguments']='"'+str(root/'scripts/collect_amazon_until_complete.py')+'" --category "'+str(category)+'"'
    elif resume_category:
        configuration['arguments']='"'+configuration['script']+'" --collection-host --port '+str(port)+' --resume-category "'+str(category)+'"'
        if paid: configuration['arguments']+=' --paid'
        if not preserve_prepared: configuration['arguments']+=' --new-preparation'
    launch_id=uuid.uuid4().hex
    diagnostic_path=environment_path.parent/f'launch-{launch_id}.json'
    identity={'launch_id':launch_id,'kind':'collection' if resume_category else 'amazon' if amazon_category else 'web',
              'started_at':time.time()}
    configuration.update(launch_id=launch_id,launch_kind=identity['kind'],launch_started_at=identity['started_at'],
                         diagnostic_path=str(diagnostic_path),diagnostic_events_path=str(diagnostic_path.with_suffix('.jsonl')))
    launch_diagnostic(diagnostic_path,identity,'starting')
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$configuration = [Console]::In.ReadToEnd() | ConvertFrom-Json
function Record-LaunchPhase($phase,$state='starting',$errorType=$null,$hresult=$null) {
    $script:launchPhase=$phase
    $row=@{launch_id=$configuration.launch_id;kind=$configuration.launch_kind;started_at=$configuration.launch_started_at;
        phase=$phase;state=$state;updated_at=([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0)}
    if($errorType){$row.error_type=$errorType;$row.hresult=$hresult}
    $raw=$row|ConvertTo-Json -Compress
    $encoding=New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($configuration.diagnostic_path,$raw,$encoding)
    [IO.File]::AppendAllText($configuration.diagnostic_events_path,$raw+"`n",$encoding)
}
try {
Record-LaunchPhase 'create_scheduler'
$scheduler = New-Object -ComObject Schedule.Service
Record-LaunchPhase 'connect_scheduler'
$scheduler.Connect()
Record-LaunchPhase 'get_folder'
$folder = $scheduler.GetFolder('\')
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Record-LaunchPhase 'create_definition'
$definition = $scheduler.NewTask(0)
Record-LaunchPhase 'configure_definition'
$definition.RegistrationInfo.Description = 'Local selection console; manually started, independent of Codex. No automatic trigger.'
$definition.Principal.UserId = $user
$definition.Principal.LogonType = 3
$definition.Principal.RunLevel = 0
$definition.Settings.Enabled = $true
$definition.Settings.MultipleInstances = 2
$definition.Settings.ExecutionTimeLimit = 'PT0S'
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.AllowDemandStart = $true
$definition.Settings.Priority = $configuration.priority
$action = $definition.Actions.Create(0)
$action.Path = $configuration.python
$action.Arguments = $configuration.arguments
$action.WorkingDirectory = $configuration.root
Record-LaunchPhase 'register_task'
$registered = $folder.RegisterTaskDefinition($configuration.name, $definition, 6, $user, $null, 3, $null)
Record-LaunchPhase 'run_task'
$null = $registered.Run($null)
Record-LaunchPhase 'task_launched' 'launched'
@{task_name=$configuration.name; launch_mode='WINDOWS_TASK_SCHEDULER'; automatic_trigger=$false} | ConvertTo-Json -Compress
} catch {
    Record-LaunchPhase $script:launchPhase 'failed' $_.Exception.GetType().FullName $_.Exception.HResult
    [Console]::Error.WriteLine('Scheduler launch failed at '+$script:launchPhase+'; HRESULT='+$_.Exception.HResult)
    exit 1
}
'''
    import base64
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
               '-EncodedCommand', base64.b64encode(script.encode('utf-16-le')).decode()]
    try:
        result = subprocess.run(command, input=json.dumps(configuration), encoding='utf-8', errors='replace',
                                capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        row=launch_diagnostic(diagnostic_path,identity,'failed',error_code='SCHEDULER_TIMEOUT',timeout_seconds=30)
        raise RuntimeError(f"Windows 独立后台启动超时（阶段 {row['phase']}；记录 launch-{launch_id}.json）") from None
    except OSError as exc:
        row=launch_diagnostic(diagnostic_path,identity,'failed',error_code='SCHEDULER_PROCESS_FAILED',error_type=type(exc).__name__,
                              winerror=getattr(exc,'winerror',None),errno=exc.errno)
        raise RuntimeError(f"Windows 独立后台启动失败（阶段 {row['phase']}；记录 launch-{launch_id}.json）") from None
    if result.returncode:
        row=launch_diagnostic(diagnostic_path,identity,'failed',error_code='SCHEDULER_EXIT_FAILED',native_exit_code=result.returncode)
        raise RuntimeError(f"Windows 独立后台启动失败（阶段 {row['phase']}；退出码 {result.returncode}；记录 launch-{launch_id}.json）")
    try:launch=json.loads(result.stdout.strip())
    except ValueError:
        launch_diagnostic(diagnostic_path,identity,'failed',error_code='SCHEDULER_INVALID_RESPONSE',native_exit_code=result.returncode)
        raise RuntimeError(f'Windows 独立后台启动未返回有效结果（记录 launch-{launch_id}.json）') from None
    launch_diagnostic(diagnostic_path,identity,'launched',native_exit_code=result.returncode,phase='task_launched')
    return launch


def configure_environment(port):
    os.environ.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    environment_path = ROOT / f'runtime/web-console/service-path-{port}.json'
    if environment_path.is_file():
        os.environ['PATH'] = json.loads(environment_path.read_text('utf-8'))['PATH']
    # Task Scheduler need not inherit the caller's terminal PATH.
    import winreg
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
                      (winreg.HKEY_CURRENT_USER, r'Environment')):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value = winreg.QueryValueEx(handle, 'Path')[0]
                os.environ['PATH'] += ';' + os.path.expandvars(value)
        except OSError:
            pass


def collection_host(port, category, *, paid=False, preserve_prepared=True):
    configure_environment(port)
    category=Path(category).resolve()
    if category.name!='category.json' or not category.is_relative_to((ROOT/'runs').resolve()):
        raise ValueError('Expected frozen category in this installation')
    # A newly prepared task has Amazon/keyword data but no log directory yet.
    # The independent host must create it before launching the supervisor.
    (category.parent/'logs').mkdir(parents=True,exist_ok=True)
    command=[sys.executable,str(ROOT/'scripts/selection.py'),'run','--prepared-start','--category',str(category)]
    if preserve_prepared: command.append('--preserve-prepared')
    if paid: command.append('--paid')
    failures=0
    while True:
        with (category.parent/'logs/supervisor.log').open('ab') as stream:
            result=subprocess.run(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode==0 or (category.parent/'STOP').exists():return result.returncode
        try:state=json.loads((category.parent/'status.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):state={}
        if str(state.get('状态','')).startswith('完成'):return result.returncode
        if '--preserve-prepared' not in command:
            try:manifest=json.loads((ROOT/'config/sessions.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):manifest={}
            if manifest.get('task_key')==category.parent.name:command.append('--preserve-prepared')
        failures+=1
        delay=min(60,5*(2**min(failures-1,4)))
        (category.parent/'supervisor_recovery.json').write_text(json.dumps({
            'launch_mode':'WINDOWS_TASK_SCHEDULER','attempts':failures,'retry_seconds':delay if failures<3 else None,
            'last_exit_code':result.returncode,'updated_at':time.time(),'checkpoints_preserved':True}),encoding='utf-8')
        if failures >= 3:
            state.update(状态='自动恢复已停止，原断点已保留',需人工介入=True,
                建议动作='请在工作台查看当前失败原因；处理后点击“重试准备环境”或“开始 / 继续采集”。')
            (category.parent/'status.json').write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
            return result.returncode
        time.sleep(delay)


def healthy(port):
    """Probe the HTTP handler, not report generation or external Agents."""
    import http.client
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
    try:
        connection.request('GET','/api/health')
        response=connection.getresponse()
        value=json.loads(response.read(1024))
        return response.status==200 and value.get('ok') is True and value.get('service')=='selection-workbench'
    except (OSError,ValueError,http.client.HTTPException):
        return False
    finally:connection.close()


def host(port):
    from selection_automation.process_lock import ProcessLock
    from restart_web import listener, validate_console, native_process_active
    import psutil
    configure_environment(port)
    directory = ROOT / 'runtime/web-console'
    directory.mkdir(parents=True, exist_ok=True)
    with ProcessLock(directory / f'persistent-service-{port}.lock'):
        (directory / f'persistent-service-{port}.json').write_text(json.dumps({
            'pid': os.getpid(), 'created': psutil.Process().create_time(), 'root': str(ROOT),
            'port': port, 'launch_mode': 'WINDOWS_TASK_SCHEDULER'}, ensure_ascii=False), encoding='utf-8')
        while True:
            process = listener(port)
            if process:
                validate_console(process, ROOT)
            else:
                with (directory / f'service-{port}.log').open('ab') as stream:
                    child = subprocess.Popen([sys.executable, str(ROOT / 'scripts/web_console.py'), '--port', str(port)],
                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                process = psutil.Process(child.pid)
            # A Windows venv launcher can survive its real Python child. A
            # blocking wait on that shell used to leave the port offline forever.
            missing_since=None
            while True:
                try:process.wait(timeout=5);break
                except psutil.NoSuchProcess:break
                except psutil.TimeoutExpired:
                    try:
                        if not native_process_active(process.pid):break
                    except psutil.NoSuchProcess:break
                current=listener(port)
                if current:
                    validate_console(current,ROOT)
                    if healthy(port):
                        missing_since=None
                        continue
                if missing_since is None:missing_since=time.monotonic()
                if time.monotonic()-missing_since>=60:
                    from restart_web import stop_console
                    stop_console(process,ROOT)
                    break
            # Let the console's own release handoff finish before adopting it.
            time.sleep(2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', action='store_true')
    parser.add_argument('--collection-host',action='store_true')
    parser.add_argument('--paid',action='store_true')
    parser.add_argument('--new-preparation',action='store_true')
    parser.add_argument('--amazon-category',type=Path)
    parser.add_argument('--resume-category',type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1–65535')
    if args.collection_host:
        if not args.resume_category: parser.error('--collection-host requires --resume-category')
        raise SystemExit(collection_host(args.port,args.resume_category,paid=args.paid,preserve_prepared=not args.new_preparation))
    elif args.host:
        host(args.port)
    else:
        print(json.dumps(start(args.port,amazon_category=args.amazon_category,resume_category=args.resume_category,paid=args.paid,preserve_prepared=not args.new_preparation), ensure_ascii=False))
