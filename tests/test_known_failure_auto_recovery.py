import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import persistent_web_service as persistent
import company_setup as company
import resume_worker_binding as binding
from selection_automation.local_proxy_prepare import refresh_saved_owner

@pytest.mark.parametrize('changed,protocol,expected',[(False,'HTTP',True),(True,'HTTP',False),(False,None,False)])
def test_only_same_verified_proxy_endpoint_can_refresh_pid(tmp_path,changed,protocol,expected):
 saved={'proxy_host':'residential.example','proxy_port':5959,'preproxy_host':'127.0.0.1','preproxy_port':7890,'preproxy_protocol':'HTTP','local_proxy_owner':{'pid':1,'created':1,'name':'clash-meta.exe'}}
 evidence={'processes':[{'pid':2,'created':2,'name':'clash-meta.exe','listeners':[{'host':'127.0.0.1','port':7891 if changed else 7890}]}]}
 result=refresh_saved_owner(tmp_path,saved,evidence=evidence,protocol_probe=lambda _: {'protocol':protocol},check_owner=lambda _:True)
 assert (result['local_proxy_owner']['pid']==2) is expected
 assert result['proxy_host']==saved['proxy_host'] and result['preproxy_port']==7890
 assert (tmp_path/'runtime/secrets/proxyshare.settings.json').exists() is expected


def test_restart_refresh_uses_system_http_declaration_like_discovery(tmp_path):
 saved={'preproxy_host':'127.0.0.1','preproxy_port':7890,'preproxy_protocol':'HTTP','local_proxy_owner':{'pid':1,'created':1,'name':'clash-meta.exe'}}
 evidence={'processes':[{'pid':2,'created':2,'name':'clash-meta.exe','listeners':[{'host':'127.0.0.1','port':7890,'declared_protocol':None}]}],
           'system':{'endpoints':[{'host':'127.0.0.1','port':7890,'declared_protocol':'HTTP'}]}}
 result=refresh_saved_owner(tmp_path,saved,evidence=evidence,check_owner=lambda _:True,
     protocol_probe=lambda candidate:{'protocol':'MIXED' if candidate['declared_protocol']=='HTTP' else 'SOCKS5'})
 assert result['local_proxy_owner']['pid']==2 and result['preproxy_protocol']=='HTTP'

@pytest.mark.parametrize('stop',[False,True])
def test_independent_host_retries_crash_but_honors_user_stop(tmp_path,monkeypatch,stop):
 monkeypatch.setattr(persistent,'ROOT',tmp_path);monkeypatch.setattr(persistent,'configure_environment',lambda _:None)
 category=tmp_path/'runs/task/category.json';category.parent.mkdir(parents=True);category.write_text('{}');(category.parent/'logs').mkdir()
 calls=[];delays=[]
 def run(command,**kwargs):
  calls.append(command)
  if stop:(category.parent/'STOP').write_text('stop')
  return SimpleNamespace(returncode=1 if len(calls)==1 else 0)
 monkeypatch.setattr(persistent.subprocess,'run',run);monkeypatch.setattr(persistent.time,'sleep',delays.append)
 assert persistent.collection_host(8765,category)==(1 if stop else 0)
 assert len(calls)==(1 if stop else 2)
 assert delays==([] if stop else [5])


def test_closed_task_replaces_only_closed_lane_and_preserves_data(tmp_path,monkeypatch):
 import selection_automation.profile_single_use as single
 monkeypatch.setattr(company,'ROOT',tmp_path)
 workers=[{'name':'worker-01-new','profile_id':'live'},{'name':'worker-02-new','profile_id':'closed'}]
 manifest=tmp_path/'config/sessions.json';manifest.parent.mkdir();manifest.write_text(json.dumps({'workers':workers}))
 checkpoint=tmp_path/'retained.jsonl';checkpoint.write_text('retained')
 monkeypatch.setattr(single,'closed_used_profile',lambda root,profile:profile=='closed')
 calls=[]
 def run(command,**kwargs):
  calls.append(command);manifest.write_text(json.dumps({'workers':[workers[0],{'name':'worker-02-new','profile_id':'new'}]}))
 monkeypatch.setattr(company.subprocess,'run',run)
 updated=company.restore_closed_task_workers('task',workers)
 assert len(calls)==1 and calls[0][calls[0].index('--worker')+1]=='2'
 assert updated[0]['profile_id']=='live' and updated[1]['profile_id']=='new'
 assert checkpoint.read_text()=='retained'

@pytest.mark.parametrize('refused',[True,False])
def test_refused_control_connection_recovers_but_uncertain_timeout_does_not(tmp_path,monkeypatch,refused):
 import urllib.error
 monkeypatch.setattr(binding.r,'ROOT',tmp_path);(tmp_path/'sid.txt').write_text('sid')
 def open(*a,**k):raise urllib.error.URLError(ConnectionRefusedError() if refused else TimeoutError())
 monkeypatch.setattr(binding.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=open))
 assert binding.invalid_session({'session_id_file':'sid.txt','webdriver_url':'http://127.0.0.1:4451'}) is refused


def test_fresh_start_crash_reuses_task_profiles_on_retry(tmp_path,monkeypatch):
 monkeypatch.setattr(persistent,'ROOT',tmp_path);monkeypatch.setattr(persistent,'configure_environment',lambda _:None)
 category=tmp_path/'runs/task/category.json';category.parent.mkdir(parents=True);category.write_text('{}');(category.parent/'logs').mkdir();(tmp_path/'config').mkdir()
 calls=[]
 def run(command,**kwargs):
  calls.append(list(command));(tmp_path/'config/sessions.json').write_text(json.dumps({'task_key':'task'}))
  return SimpleNamespace(returncode=1 if len(calls)==1 else 0)
 monkeypatch.setattr(persistent.subprocess,'run',run);monkeypatch.setattr(persistent.time,'sleep',lambda _:None)
 assert persistent.collection_host(8765,category,preserve_prepared=False)==0
 assert '--preserve-prepared' not in calls[0] and '--preserve-prepared' in calls[1]


def test_independent_collection_host_stops_identical_failures_and_keeps_checkpoint(tmp_path, monkeypatch):
 monkeypatch.setattr(persistent,'ROOT',tmp_path);monkeypatch.setattr(persistent,'configure_environment',lambda _:None)
 category=tmp_path/'runs/task/category.json';category.parent.mkdir(parents=True);category.write_text('{}')
 checkpoint=category.parent/'checkpoint.json';checkpoint.write_text('{"goods":243}')
 (category.parent/'status.json').write_text(json.dumps({'状态':'失败','原因':'准备环境失败','阶段':{'bootstrap':{'状态':'启动准备失败'}}}))
 calls=[];waits=[]
 monkeypatch.setattr(persistent.subprocess,'run',lambda command,**kwargs:calls.append(command) or SimpleNamespace(returncode=1))
 monkeypatch.setattr(persistent.time,'sleep',lambda seconds:waits.append(seconds))
 assert persistent.collection_host(8765,category)==1
 assert len(calls)==3 and waits==[5,10]
 state=json.loads((category.parent/'status.json').read_text(encoding='utf-8'))
 assert state['需人工介入'] and state['原因']=='准备环境失败' and '已停止' in state['状态']
 assert state['阶段']['bootstrap']['状态']=='启动准备失败'
 assert json.loads((category.parent/'supervisor_recovery.json').read_text(encoding='utf-8'))['retry_seconds'] is None
 assert checkpoint.read_text()=='{"goods":243}'


def test_repaired_binding_clears_only_its_lane_and_preserves_goods(tmp_path,monkeypatch):
 monkeypatch.setattr(binding.r,'ROOT',tmp_path);manifest=tmp_path/'config/sessions.json';manifest.parent.mkdir();manifest.write_text(json.dumps({'task_key':'task'}));monkeypatch.setattr(binding.r,'SESSIONS',manifest)
 folder=tmp_path/'runs/task/temu/supervisors';folder.mkdir(parents=True);path=folder/'worker-02-new.json';path.write_text(json.dumps({'status':'BINDING_FAILED','unique_goods':82,'current_query':'keyboard'}));other=folder/'worker-01-new.json';other.write_text('{"status":"RUNNING"}');before=other.read_bytes()
 binding.mark_rebound_worker({'name':'worker-02-new'})
 result=json.loads(path.read_text());assert result['status']=='RESUMING' and result['unique_goods']==82
 assert other.read_bytes()==before
