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
