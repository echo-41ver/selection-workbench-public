"""Synthetic queue acceptance: no real browser, model or message is invoked."""
from datetime import datetime,timezone
import json
from selection_automation.operations import save_subscription,daily_tick
from selection_automation.daily_collection import collection_tick,collection_status

NOW=datetime(2026,10,12,23,tzinfo=timezone.utc) # Beijing 07:00, scheduled window.


class Backend:
    active=None
    def __init__(self):self.starts=[]
    def owner(self):return self.active
    def start(self,category):
        self.starts.append(category);self.active={'category_key':category['category_key']}
        return {'状态':'启动中','PID':123}


def setup(root):
    path=root/'runs/mice/category.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'category_key':'mice','category_name_zh':'鼠标'}),'utf-8')
    save_subscription(root,{'category_key':'mice','label':'运营1'})


def test_daily_queue_waits_for_live_task_and_expires_without_preemption(tmp_path):
    setup(tmp_path);backend=Backend();backend.active={'category_key':'preserved'}
    first=collection_tick(tmp_path,now=NOW,backend=backend)
    assert first[0]['status']=='QUEUED_WAITING_TASK' and not backend.starts
    assert first[0]['queued_at']==NOW.isoformat()
    assert first[0]['scheduled_for']=='2026-10-13T06:30:00+08:00'
    assert collection_tick(tmp_path,now=NOW,backend=backend)==[]
    backend.active=None
    late=datetime(2026,10,13,1,tzinfo=timezone.utc)
    assert collection_tick(tmp_path,now=late,backend=backend)[0]['status']=='SKIPPED_WINDOW'
    assert collection_status(tmp_path,'mice',late)['queued_at']==NOW.isoformat()
    assert not backend.starts


def test_new_daily_run_starts_once_across_operators_and_ticks(tmp_path):
    setup(tmp_path);save_subscription(tmp_path,{'category_key':'mice','label':'运营2'})
    backend=Backend();collection_tick(tmp_path,now=NOW,backend=backend)
    assert len(backend.starts)==1
    category=backend.starts[0]
    assert category['category_key']!='mice' and category['daily_source_key']=='mice'
    collection_tick(tmp_path,now=NOW,backend=backend)
    assert len(backend.starts)==1
    gate=tmp_path/'runs'/category['category_key']/'temu/workers/w1/query/progress.json'
    gate.parent.mkdir(parents=True);gate.write_text('{"status":"WAITING_SEARCH_VERIFICATION"}','utf-8')
    assert collection_tick(tmp_path,now=NOW,backend=backend)[0]['status']=='WAITING_SITE_VERIFICATION'
    assert len(backend.starts)==1


def test_no_automatic_retry_after_uncertain_crash_or_failed_launch(tmp_path):
    setup(tmp_path);backend=Backend();collection_tick(tmp_path,now=NOW,backend=backend)
    backend.active=None
    assert collection_tick(tmp_path,now=NOW,backend=backend)[0]['status']=='STOPPED_INCOMPLETE'
    assert collection_tick(tmp_path,now=NOW,backend=backend)==[]
    assert len(backend.starts)==1


def test_requested_fresh_delivery_never_uses_yesterdays_template(tmp_path):
    setup(tmp_path)
    def forbidden(*args,**kwargs):raise AssertionError('must not use stale template')
    result=daily_tick(tmp_path,now=datetime(2026,10,13,1,tzinfo=timezone.utc),shortlist_builder=forbidden)
    assert result[0]['status']=='NO_QUALIFIED_PRODUCT'
    assert '今日新采集' in result[0]['message']


def test_daily_delivery_uses_actual_daily_run_and_keeps_source_subscription(tmp_path):
    setup(tmp_path);backend=Backend();collection_tick(tmp_path,now=NOW,backend=backend)
    category=backend.starts[0]
    path=tmp_path/'runs'/category['category_key']/'category.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps(category),'utf-8');seen=[]
    def snapshot(root,cat,**kwargs):
        seen.append(cat['category_key']);return {'products':[{'asin':'fixture'}],'status':'READY','message':'fixture'}
    result=daily_tick(tmp_path,now=datetime(2026,10,13,1,tzinfo=timezone.utc),shortlist_builder=snapshot)
    assert seen==[category['category_key']]
    assert result[0]['subscription']['category_key']=='mice'


def test_before_schedule_does_not_even_load_backend(tmp_path):
    setup(tmp_path)
    assert collection_tick(tmp_path,now=datetime(2026,10,12,18,tzinfo=timezone.utc))==[]


def test_real_launcher_contract_never_enables_paid_vision(tmp_path,monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    import selection
    from selection_automation.daily_collection import SelectionBackend
    monkeypatch.setattr(selection,'ROOT',tmp_path);calls=[]
    monkeypatch.setattr(selection,'prepare',lambda path:(path,{},tmp_path/'runs/fixture'))
    monkeypatch.setattr(selection,'request_start',lambda *args:calls.append(args) or {'状态':'启动中'})
    SelectionBackend(tmp_path).start({'category_key':'daily_fixture'})
    assert len(calls)==1 and calls[0][3] is False


def test_no_product_record_preserves_first_actual_check_time(tmp_path):
    setup(tmp_path)
    first=datetime(2026,10,13,0,30,15,tzinfo=timezone.utc)
    later=datetime(2026,10,13,0,40,15,tzinfo=timezone.utc)
    snapshot=lambda *args,**kwargs:{'status':'READY','message':'no evidence','products':[]}
    original=daily_tick(tmp_path,now=first,shortlist_builder=snapshot)[0]
    updated=daily_tick(tmp_path,now=later,shortlist_builder=snapshot)[0]
    assert original['first_checked_at']==updated['first_checked_at']==first.isoformat()
    assert updated['checked_at']==later.isoformat() and updated['delivered_at'] is None
