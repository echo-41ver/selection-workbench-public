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


@pytest.mark.parametrize('progress_status',['WAITING_SEARCH_VERIFICATION','RUNNING',None])
@pytest.mark.parametrize('owner_active',[True,False])
def test_terminal_supervisor_failure_overrides_only_web_display_and_preserves_progress(tmp_path,monkeypatch,progress_status,owner_active):
 console=web.Console(tmp_path);console.key='new_task'
 monkeypatch.setattr(console,'owner',lambda:{'category_key':'new_task'} if owner_active else None)
 monkeypatch.setattr(console,'matching_snapshot',lambda *a,**kw:{})
 monkeypatch.setattr(web.selection,'ROOT',tmp_path)
 base=tmp_path/'runs/new_task';supervisor=base/'temu/supervisors/worker-01-new.json'
 output=base/'temu/workers/worker-01-new/curtain';output.mkdir(parents=True)
 supervisor.parent.mkdir(parents=True)
 category=base/'category.json';category.write_text(json.dumps({'category_key':'new_task','category_name_zh':'curtain',
                                                            'execution_mode':'collection','temu':{},'amazon':{}}),encoding='utf8')
 worker={'worker':'worker-01-new','status':'FAILED_AFTER_3_ATTEMPTS','query_status':'NO_FRESH_PROGRESS',
         'returncode':1,'current_query':'curtain','output':str(output),'completed_queries':['retained'],
         'updated_at':'2026-10-22T04:19:44+0800'}
 supervisor.write_text(json.dumps(worker),encoding='utf8')
 status_file=base/'status.json';status_file.write_text(json.dumps({'状态':'运行中'}),encoding='utf8')
 progress=output/'progress.json'
 if progress_status:
  progress.write_text(json.dumps({'status':progress_status,'unique_goods':7,'raw_collected_count':9,
                                 'updated_at':'2026-10-21T16:55:41Z','manual_action_required':True}),encoding='utf8')
 products=output/'products.jsonl';products.write_text('{"goods_id":"retained"}\n',encoding='utf8')
 originals={p:p.read_bytes() for p in [supervisor,status_file,products,progress,category] if p.exists()}
 payload=console.status();reported=payload['workers'][0];live=reported['live_snapshot']
 assert reported['status']=='FAILED_AFTER_3_ATTEMPTS' and reported['returncode']==1
 assert live['采集状态']=='FAILED_AFTER_3_ATTEMPTS' and '失败' in live['状态'] and '停止' in live['状态']
 assert live['阻断'] and live['需人工介入'] and live['监督任务运行中'] is owner_active
 assert ('重启' if owner_active else '开始 / 继续采集') in live['建议动作']
 assert ('仍在运行' if owner_active else '未运行') in live['建议动作']
 assert payload['active'] is owner_active and payload['can_resume'] is (not owner_active)
 assert live['最近采集状态']==(progress_status or 'NO_FRESH_PROGRESS')
 assert live['监督状态更新时间']==reported['status_updated_at']==worker['updated_at']
 assert '浏览器保留' not in live and not live.get('采集进程运行中')
 assert payload['severity']=='red' and payload['details']['阶段']['worker-01-new']==live
 if progress_status:assert live['唯一商品数']==7 and live['采集条数']==9
 assert reported['completed_queries']==['retained'] and reported['output']==str(output)
 assert all(p.read_bytes()==data for p,data in originals.items())
 # The collector/recovery view retains its exact old behavior; the new priority
 # is only a truthful UI projection of the supervisor's persisted terminal code.
 assert web.selection.worker_snapshot(base,'worker-01-new')[1]['采集状态']==(progress_status or 'NO_FRESH_PROGRESS')
 worker.update(status='RUNNING',query_status='RUNNING',collector_started_at=0)
 supervisor.write_text(json.dumps(worker),encoding='utf8')
 progress.write_text(json.dumps({'status':'RUNNING','unique_goods':8}),encoding='utf8')
 current=console.status();assert current['workers'][0]['live_snapshot']['采集状态']=='RUNNING'
 assert current['severity']==('green' if owner_active else 'yellow') and not current['workers'][0]['live_snapshot']['阻断']
 assert current['active'] is owner_active and current['can_resume'] is (not owner_active)
