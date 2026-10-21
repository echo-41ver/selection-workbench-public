import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
import psutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import persistent_web_service as persistent
import selection


def test_web_guard_recovers_a_live_wrapper_with_no_listening_server(tmp_path,monkeypatch):
 import restart_web
 monkeypatch.setattr(persistent,'ROOT',tmp_path)
 monkeypatch.setattr(persistent,'configure_environment',lambda *a:None)
 clock=[0];stopped=[]
 monkeypatch.setattr(persistent.time,'monotonic',lambda:clock[0])
 def pause(n):raise InterruptedError('stop after recovery')
 monkeypatch.setattr(persistent.time,'sleep',pause)
 def wait(timeout):
  clock[0]+=30
  raise psutil.TimeoutExpired(timeout)
 wrapper=SimpleNamespace(pid=123,wait=wait)
 monkeypatch.setattr(restart_web,'native_process_active',lambda *a:True)
 listeners=iter([wrapper,None,None,None])
 monkeypatch.setattr(restart_web,'listener',lambda *a:next(listeners))
 monkeypatch.setattr(restart_web,'validate_console',lambda *a:None)
 monkeypatch.setattr(restart_web,'stop_console',lambda process,root:stopped.append(process))
 with pytest.raises(InterruptedError):persistent.host(8765)
 assert stopped==[wrapper]


def test_web_guard_recovers_unresponsive_http_even_when_port_listens(tmp_path,monkeypatch):
 import restart_web
 monkeypatch.setattr(persistent,'ROOT',tmp_path)
 monkeypatch.setattr(persistent,'configure_environment',lambda *a:None)
 clock=[0];stopped=[]
 monkeypatch.setattr(persistent.time,'monotonic',lambda:clock[0])
 def pause(n):raise InterruptedError('stop after recovery')
 monkeypatch.setattr(persistent.time,'sleep',pause)
 def wait(timeout):clock[0]+=30;raise psutil.TimeoutExpired(timeout)
 process=SimpleNamespace(pid=123,wait=wait)
 monkeypatch.setattr(restart_web,'native_process_active',lambda *a:True)
 monkeypatch.setattr(restart_web,'listener',lambda *a:process)
 monkeypatch.setattr(restart_web,'validate_console',lambda *a:None)
 monkeypatch.setattr(persistent,'healthy',lambda *a:False)
 monkeypatch.setattr(restart_web,'stop_console',lambda value,root:stopped.append(value))
 with pytest.raises(InterruptedError):persistent.host(8765)
 assert stopped==[process]

@pytest.mark.parametrize('paid,preserve',[(False,True),(False,False),(True,True)])
def test_collection_task_is_independent_and_preserves_explicit_flags(tmp_path,monkeypatch,paid,preserve):
 category=tmp_path/'runs/task/category.json';category.parent.mkdir(parents=True);category.write_text('{}')
 captured={}
 def run(command,**kwargs):
  captured.update(json.loads(kwargs['input']));captured['scheduler']=command
  return SimpleNamespace(returncode=0,stdout=json.dumps({'launch_mode':'WINDOWS_TASK_SCHEDULER','task_name':'probe'}),stderr='')
 monkeypatch.setattr(persistent.subprocess,'run',run)
 result=persistent.start(8765,root=tmp_path,resume_category=category,paid=paid,preserve_prepared=preserve)
 assert result['launch_mode']=='WINDOWS_TASK_SCHEDULER'
 assert '--collection-host' in captured['arguments']
 assert ('--paid' in captured['arguments']) is paid
 assert ('--new-preparation' in captured['arguments']) is (not preserve)
 assert captured['scheduler'][0]=='powershell.exe'

@pytest.mark.skipif(os.name!='nt',reason='Windows independent launcher')
def test_normal_start_uses_scheduler_without_spawning_caller_child(tmp_path,monkeypatch):
 base=tmp_path/'runs/task';base.mkdir(parents=True);category=base/'category.json';category.write_text('{}');cat={'category_key':'task','category_name_zh':'keyboard'}
 monkeypatch.setattr(selection,'ROOT',tmp_path)
 owner={'pid':os.getpid(),'created':psutil.Process().create_time(),'category_key':'task'}
 calls=[]
 def start(*args,**kwargs):
  calls.append(kwargs)
  return {'launch_mode':'WINDOWS_TASK_SCHEDULER','task_name':'probe'}
 monkeypatch.setattr(persistent,'start',start)
 answers=iter([None,owner]);monkeypatch.setattr(selection,'live_owner',lambda:next(answers))
 monkeypatch.setattr(selection,'spawn',lambda *a,**k:pytest.fail('caller-owned child must not start the supervisor'))
 result=selection.request_start(category,cat,base,False,preserve_prepared=True)
 assert result['PID']==owner['pid']
 assert result['launch_mode']=='WINDOWS_TASK_SCHEDULER'
 assert calls[0]['preserve_prepared'] is True and calls[0]['paid'] is False


def test_independent_host_creates_logs_for_brand_new_prepared_task(tmp_path,monkeypatch):
 from types import SimpleNamespace
 monkeypatch.setattr(persistent,'ROOT',tmp_path)
 monkeypatch.setattr(persistent,'configure_environment',lambda port:None)
 category=tmp_path/'runs/new_task/category.json';category.parent.mkdir(parents=True);category.write_text('{}')
 assert not (category.parent/'logs').exists()
 calls=[]
 def launch(command,**kwargs):
  calls.append(command);kwargs['stdout'].write(b'cold supervisor launch\n');return SimpleNamespace(returncode=0)
 monkeypatch.setattr(persistent.subprocess,'run',launch)
 assert persistent.collection_host(8765,category,preserve_prepared=False)==0
 assert len(calls)==1 and (category.parent/'logs/supervisor.log').read_bytes()==b'cold supervisor launch\n'


@pytest.mark.parametrize('failure',['timeout','native_error','invalid_response'])
def test_launch_failure_preserves_safe_last_phase_without_leaking_process_output(tmp_path,monkeypatch,failure):
 import subprocess
 def run(command,**kwargs):
  config=json.loads(kwargs['input']);path=Path(config['diagnostic_path'])
  path.write_text(json.dumps({'phase':'register_task','hresult':-2147024891,'error_type':'System.UnauthorizedAccessException'}),encoding='utf-8')
  if failure=='timeout':raise subprocess.TimeoutExpired(command,30,output='do-not-record-raw-configuration')
  return SimpleNamespace(returncode=5 if failure=='native_error' else 0,stdout='do-not-record-raw-configuration',stderr='secret-that-must-not-be-logged')
 monkeypatch.setattr(persistent.subprocess,'run',run)
 with pytest.raises(RuntimeError):persistent.start(8765,root=tmp_path)
 paths=list((tmp_path/'runtime/web-console').glob('launch-*.json'))
 assert len(paths)==1
 state=json.loads(paths[0].read_text('utf-8'))
 assert state['state']=='failed' and state['phase']=='register_task'
 assert state['hresult']==-2147024891
 if failure=='timeout':assert state['error_code']=='SCHEDULER_TIMEOUT' and state['timeout_seconds']==30
 evidence=paths[0].read_text('utf-8')+paths[0].with_suffix('.jsonl').read_text('utf-8')
 assert 'do-not-record-raw-configuration' not in evidence and 'secret-that-must-not-be-logged' not in evidence
 assert 'PATH' not in evidence and str(tmp_path) not in evidence


@pytest.mark.skipif(os.name!='nt',reason='parse the actual Windows PowerShell launcher without executing it')
def test_actual_encoded_powershell_diagnostic_script_parses_without_registering_task(tmp_path,monkeypatch):
 import base64
 native_run=persistent.subprocess.run;captured={}
 def capture(command,**kwargs):
  captured['encoded']=command[-1]
  return SimpleNamespace(returncode=0,stdout='{"launch_mode":"WINDOWS_TASK_SCHEDULER","task_name":"probe"}',stderr='')
 monkeypatch.setattr(persistent.subprocess,'run',capture)
 persistent.start(8765,root=tmp_path)
 script=("$source=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('"+captured['encoded']+"'));"
         "$tokens=$null;$errors=$null;[void][System.Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors);"
         "if($errors.Count){$errors.Message|ConvertTo-Json;exit 1};'syntax valid; launcher not executed'")
 result=native_run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script.encode('utf-16-le')).decode()],
                   capture_output=True,encoding='utf-8',errors='replace',timeout=15,creationflags=0)
 assert result.returncode==0,result.stdout+result.stderr
 state=json.loads(next((tmp_path/'runtime/web-console').glob('launch-*.json')).read_text('utf-8'))
 assert state['state']=='launched' and state['phase']=='task_launched' and state['native_exit_code']==0
