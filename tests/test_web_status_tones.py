"""Status colours must describe real tasks and put human-action waits in red."""
import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import web_console as web

@pytest.mark.parametrize('worker,expected',[
 ({'status':'RUNNING'},'green'),
 ({'status':'WAITING_CAPTCHA','manual_action_required':True},'red'),
 ({'status':'STOPPED_LOGIN_REQUIRED','manual_action_required':True},'green'),
 ({'status':'BINDING_FAILED'},'red'),
 ({'status':'REPLACING_LOGIN_ENVIRONMENT','manual_action_required':True},'green'),
 ({'status':'RUNNING','live_snapshot':{'状态':'等待首屏商品','采集状态':'WAITING_INITIAL_PRODUCTS','阻断':True}},'green'),
])
def test_running_worker_severity(tmp_path,monkeypatch,worker,expected):
 console=web.Console(tmp_path);console.key='new_task'
 monkeypatch.setattr(console,'owner',lambda:{'category_key':'new_task'})
 base=tmp_path/'runs/new_task';(base/'temu/supervisors').mkdir(parents=True)
 (base/'status.json').write_text(json.dumps({'状态':'运行中'}),'utf8')
 (base/'temu/supervisors/worker-01-new.json').write_text(json.dumps(worker),'utf8')
 if worker.get('live_snapshot'):monkeypatch.setattr(web.selection,'worker_snapshot',lambda *a:({},worker['live_snapshot'],None))
 else:monkeypatch.setattr(web.selection,'worker_snapshot',lambda *a:({},None,None))
 assert console.status()['severity']==expected

def test_stage_failure_is_not_masked_by_running_supervisor(tmp_path,monkeypatch):
 console=web.Console(tmp_path);console.key='new_task';monkeypatch.setattr(console,'owner',lambda:{'category_key':'new_task'})
 base=tmp_path/'runs/new_task';base.mkdir(parents=True)
 (base/'status.json').write_text(json.dumps({'状态':'运行中','阶段':{'stage2':{'状态':'失败'}}}),'utf8')
 assert console.status()['severity']=='red'

def test_lost_supervisor_warns_without_pretending_normal_progress(tmp_path,monkeypatch):
 console=web.Console(tmp_path);console.key='new_task';monkeypatch.setattr(console,'owner',lambda:None)
 base=tmp_path/'runs/new_task';base.mkdir(parents=True)
 (base/'status.json').write_text(json.dumps({'状态':'运行中'}),'utf8')
 state=console.status();assert state['active'] is False and state['severity']=='yellow'
 assert '监督进程已退出' in state['phase']
