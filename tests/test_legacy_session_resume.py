import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import selection


@pytest.mark.parametrize('status,kind',[
    ('WAITING_HUMAN_VERIFICATION','CAPTCHA'),('WAITING_LOGIN','LOGIN'),
    ('STOPPED_HTTP_403','ACCESS'),('STOPPED_HTTP_429','RATE_LIMIT')])
def test_legacy_stopped_gate_relaunches_without_page_verification(tmp_path,monkeypatch,status,kind):
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    output=tmp_path/'temu/workers/worker1/query'
    output.mkdir(parents=True)
    checkpoint=output/'products.jsonl'
    checkpoint.write_text('{"goods_id":"retained"}\n',encoding='utf-8')
    (output/'progress.json').write_text(json.dumps({'status':status,'scroll_collection_started':True,'manual_action_required':True,'automatic_retry':False}))
    worker_path=tmp_path/'temu/supervisors/worker1.json'
    worker_path.parent.mkdir(parents=True)
    worker_path.write_text(json.dumps({'status':'HUMAN_ACTION_REQUIRED','current_query':'mouse','output':str(output)}))
    attempts={'worker1':3}; next_at={'worker1':100}; finished={'worker1'}; state={}
    def verify(*args):pytest.fail('No manual verification needed for automatic session replacement')
    assert not selection.hold_blocked_worker(tmp_path,{'name':'worker1'},children={},finished=finished,
        attempts=attempts,next_at=next_at,state=state,verify=verify)
    assert attempts['worker1']==0 and next_at['worker1']==0 and not finished
    assert selection.read(worker_path)['session_recovery_kind']==kind
    assert selection.read(worker_path)['status']=='RESUMING'
    assert checkpoint.read_text('utf-8')=='{"goods_id":"retained"}\n'
    assert state['worker1']['需人工介入'] is False


def test_legacy_resume_preserves_exhausted_recovery_budget(tmp_path,monkeypatch):
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    output=tmp_path/'temu/workers/worker1/query'; output.mkdir(parents=True)
    (output/'progress.json').write_text(json.dumps({'status':'WAITING_HUMAN_VERIFICATION'}))
    worker_path=tmp_path/'temu/supervisors/worker1.json'; worker_path.parent.mkdir(parents=True)
    worker_path.write_text(json.dumps({'status':'HUMAN_ACTION_REQUIRED','output':str(output),
                                      'recovery_budget_exhausted':True}))
    assert selection.hold_blocked_worker(tmp_path,{'name':'worker1'},children={},finished=set(),
        attempts={},next_at={},state={},verify=lambda *args:False)
    assert selection.read(worker_path)['recovery_budget_exhausted'] is True


def test_old_goods_and_rounds_do_not_restart_startup_captcha(tmp_path,monkeypatch):
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    output=tmp_path/'temu/workers/worker1/query'; output.mkdir(parents=True)
    (output/'progress.json').write_text(json.dumps({'status':'WAITING_CAPTCHA','round':37,
        'unique_goods':1504,'automatic_retry':True,'automatic_recovery_eligible':True}))
    worker_path=tmp_path/'temu/supervisors/worker1.json'; worker_path.parent.mkdir(parents=True)
    worker_path.write_text(json.dumps({'status':'HUMAN_ACTION_REQUIRED','output':str(output)}))
    attempts={'worker1':2}; next_at={'worker1':100}
    assert selection.hold_blocked_worker(tmp_path,{'name':'worker1'},children={},finished=set(),
        attempts=attempts,next_at=next_at,state={},verify=lambda *args:False)
    assert attempts=={'worker1':2} and next_at=={'worker1':100}
    assert selection.read(worker_path)['status']=='HUMAN_ACTION_REQUIRED'
