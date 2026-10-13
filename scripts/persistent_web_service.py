"""Run the local console from a manual Windows task, independent of its caller."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def start(port, root=ROOT, amazon_category=None,resume_category=None):
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
                     'root': str(root), 'port': port,'resume':bool(resume_category)}
    configuration['arguments']='"'+configuration['script']+'" --host --port '+str(port)
    if amazon_category:
        configuration['arguments']='"'+str(root/'scripts/collect_amazon_until_complete.py')+'" --category "'+str(category)+'"'
    elif resume_category:
        configuration['arguments']='"'+str(root/'scripts/selection.py')+'" run --prepared-start --preserve-prepared --category "'+str(category)+'"'
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$configuration = [Console]::In.ReadToEnd() | ConvertFrom-Json
$scheduler = New-Object -ComObject Schedule.Service
$scheduler.Connect()
$folder = $scheduler.GetFolder('\')
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$definition = $scheduler.NewTask(0)
$definition.RegistrationInfo.Description = 'Local selection console; manually started, independent of Codex. No automatic trigger.'
$definition.Principal.UserId = $user
$definition.Principal.LogonType = 3
$definition.Principal.RunLevel = 0
$definition.Settings.Enabled = $true
if ($configuration.resume) { $definition.Settings.MultipleInstances = 0 } else { $definition.Settings.MultipleInstances = 2 }
$definition.Settings.ExecutionTimeLimit = 'PT0S'
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.AllowDemandStart = $true
$action = $definition.Actions.Create(0)
$action.Path = $configuration.python
$action.Arguments = $configuration.arguments
$action.WorkingDirectory = $configuration.root
$registered = $folder.RegisterTaskDefinition($configuration.name, $definition, 6, $user, $null, 3, $null)
$null = $registered.Run($null)
@{task_name=$configuration.name; launch_mode='WINDOWS_TASK_SCHEDULER'; automatic_trigger=$false} | ConvertTo-Json -Compress
'''
    import base64
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
               '-EncodedCommand', base64.b64encode(script.encode('utf-16-le')).decode()]
    result = subprocess.run(command, input=json.dumps(configuration), encoding='utf-8', errors='replace',
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Windows 独立后台启动失败：' + result.stderr.strip())
    return json.loads(result.stdout.strip())


def host(port):
    from selection_automation.process_lock import ProcessLock
    from restart_web import listener, validate_console
    import psutil
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
            try:
                process.wait()
            except psutil.NoSuchProcess:
                pass
            # Let the console's own release handoff finish before adopting it.
            time.sleep(2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', action='store_true')
    parser.add_argument('--amazon-category',type=Path)
    parser.add_argument('--resume-category',type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1–65535')
    if args.host:
        host(args.port)
    else:
        print(json.dumps(start(args.port,amazon_category=args.amazon_category,resume_category=args.resume_category), ensure_ascii=False))
