import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import run_incremental_stage12 as stage12
import selection


def test_whole_task_stop_reaches_independently_owned_services_and_preserves_paid_evidence(tmp_path):
    base=tmp_path/'runs/task'
    folder=base/'stage123';folder.mkdir(parents=True)
    evidence=folder/'request_intents.jsonl';evidence.write_text('{"billing":"UNKNOWN"}\n')
    original=evidence.read_bytes()
    selection.request_task_stop(base)
    assert (base/'STOP').exists() and (base/'stage12/STOP').exists()
    assert json.loads((folder/'STOP').read_text('utf-8'))['reason']=='whole-task-user-request'
    assert evidence.read_bytes()==original
    stop_before=(folder/'STOP').read_bytes()
    selection.request_task_stop(base)
    assert (folder/'STOP').read_bytes()==stop_before


def test_root_task_stop_cancels_cycle_before_ingest_or_analysis(tmp_path):
    root_stop=tmp_path/'STOP';root_stop.touch()
    def forbidden(*args):
        raise AssertionError('stopped task must not ingest or process')
    pipeline=SimpleNamespace(stop_requested=lambda:stage12.stop_requested(tmp_path/'stage12/STOP',root_stop),
                             ingest_source=forbidden,process_batch=forbidden,materialize_candidates=forbidden)
    result=stage12.run_cycle(pipeline,{'sources':[{'key':'real-source'}]})
    assert result['处理']=={'selected':0,'cancelled':True}


def test_root_stop_interrupts_poll_without_needing_local_marker(tmp_path,monkeypatch):
    root_stop=tmp_path/'STOP';root_stop.touch()
    monkeypatch.setattr(stage12.time,'sleep',lambda _:(_ for _ in ()).throw(AssertionError('slept')))
    stage12.wait_for_poll(20,tmp_path/'stage12/STOP',root_stop)


def test_cancelled_batch_keeps_previous_candidates_instead_of_materializing_partial_work():
    batch={'selected':3,'completed':0,'cancelled':True,'stage1_committed':3}
    def forbidden(*args):
        raise AssertionError('cancelled batch must retain previous candidate publication')
    pipeline=SimpleNamespace(stop_requested=lambda:False,ingest_source=lambda key:{'source':key},
        process_batch=lambda size:batch,materialize_candidates=forbidden,status=forbidden)
    result=stage12.run_cycle(pipeline,{'sources':[{'key':'source'}]})
    assert result['处理']==batch
    assert result['接收']==[{'source':'source'}]


def test_normal_stop_during_ingest_preserves_previous_publications():
    stopped=False
    def ingest(key):
        nonlocal stopped
        stopped=True
        raise stage12.CancelledError('normal task stop')
    pipeline=SimpleNamespace(stop_requested=lambda:stopped,ingest_source=ingest)
    result=stage12.run_cycle(pipeline,{'sources':[{'key':'source'}]})
    assert result['处理']['cancelled'] and result['处理']['cancelled_phase']=='source_ingest'
    assert result['接收']==[]


def test_stopped_watch_does_not_open_database_or_validate_old_contract(tmp_path,monkeypatch,capsys):
    config=tmp_path/'stage12_config.json';config.write_text('{}')
    (tmp_path/'STOP').touch()
    monkeypatch.setattr(sys,'argv',['run_incremental_stage12.py','watch','--config',str(config)])
    monkeypatch.setattr(stage12,'open_pipeline',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('opened DB')))
    monkeypatch.setattr(stage12,'validate_category_handoff',lambda *a:(_ for _ in ()).throw(AssertionError('validated old contract')))
    stage12.main()
    assert '任务已停止' in capsys.readouterr().out
