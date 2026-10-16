import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import watch_temu_union as watcher
import dashboard_progress


def test_bad_snapshot_is_reported_and_recovers_without_process_exit(tmp_path):
    base=tmp_path/'temu';source=base/'workers/w/q/products.jsonl'
    source.parent.mkdir(parents=True)
    source.write_text('{"goods_id":"1","title":"drill"}\n')
    first=watcher.publish_once(base)
    assert first['status']=='PUBLISHED'
    old=(base/'union/products.jsonl').read_bytes()
    source.write_text('invalid json\n')
    failed=watcher.publish_once(base,first)
    assert failed['status']=='SOURCE_READ_FAILED_PRESERVED_PREVIOUS_UNION'
    assert (base/'union/products.jsonl').read_bytes()==old
    assert watcher.watch_health(tmp_path)['阻断'] is True
    enriched=dashboard_progress.enrich_progress(tmp_path,{'状态':'运行中'})
    assert enriched['状态']=='存在阻断'
    assert enriched['阶段']['temu_union']['error']
    again=watcher.publish_once(base,failed)
    assert again['consecutive_failures']==2
    source.write_text('{"goods_id":"2","title":"drill"}\n')
    recovered=watcher.publish_once(base,again)
    assert recovered['status']=='PUBLISHED'
    assert watcher.watch_health(tmp_path)['阻断'] is False
    assert json.loads((base/'union/products.jsonl').read_text())['goods_id']=='2'


def test_idle_watcher_emits_heartbeat_and_stall_is_distinct(tmp_path):
    state=watcher.publish_once(tmp_path/'temu')
    assert state['status']=='WAITING_SOURCE'
    assert not watcher.watch_health(tmp_path)['阻断']
    assert watcher.watch_health(tmp_path,now=state['heartbeat_at']+91)['stale']


def test_completed_task_is_not_reported_as_a_stalled_watcher(tmp_path):
    state=watcher.publish_once(tmp_path/'temu')
    state.update(heartbeat_at=0,status='PUBLISHED')
    (tmp_path/'temu/union/watch_state.json').write_text(json.dumps(state))
    result=dashboard_progress.enrich_progress(tmp_path,{'状态':'完成'},watch_running=False)
    assert result['状态']=='完成'
    assert result['阶段']['temu_union']['阻断'] is False


def test_concurrent_source_update_publishes_then_catches_up(tmp_path, monkeypatch):
    base=tmp_path/'temu';source=base/'workers/w/q/products.jsonl'
    source.parent.mkdir(parents=True)
    source.write_text('{"goods_id":"1"}\n')
    real_merge=watcher.merge
    def changing_merge(inputs):
        result=real_merge(inputs)
        watcher.atomic_write(source,'{"goods_id":"1"}\n{"goods_id":"2"}\n')
        return result
    monkeypatch.setattr(watcher,'merge',changing_merge)
    first=watcher.publish_once(base)
    assert first['status']=='PUBLISHED' and first['unique_goods']==1
    assert not watcher.watch_health(tmp_path)['阻断']
    monkeypatch.setattr(watcher,'merge',real_merge)
    second=watcher.publish_once(base,first)
    assert second['status']=='PUBLISHED' and second['unique_goods']==2


def test_locked_health_file_does_not_kill_local_watcher(tmp_path, monkeypatch):
    base=tmp_path/'temu';source=base/'workers/w/q/products.jsonl'
    source.parent.mkdir(parents=True)
    source.write_text('{"goods_id":"1"}\n')
    original=watcher.atomic_write
    def locked(path, content):
        if path.name=='watch_state.json':raise PermissionError('reader holds file')
        return original(path,content)
    monkeypatch.setattr(watcher,'atomic_write',locked)
    state=watcher.publish_once(base)
    assert state['status']=='PUBLISHED' and state['health_write_pending']
    assert json.loads((base/'union/products.jsonl').read_text())['goods_id']=='1'
    monkeypatch.setattr(watcher,'atomic_write',original)
    recovered=watcher.publish_once(base,state)
    assert not recovered.get('health_write_pending')
    assert not watcher.watch_health(tmp_path)['阻断']


def test_cached_stale_heartbeat_cannot_hide_exhausted_recovery(tmp_path):
    state=watcher.publish_once(tmp_path/'temu')
    state.update(heartbeat_at=0,status='PUBLISHED')
    (tmp_path/'temu/union/watch_state.json').write_text(json.dumps(state))
    result=dashboard_progress.enrich_progress(tmp_path,{'状态':'存在阻断','阶段':{
        'temu_union':{'recovery_exhausted':True,'自动恢复':False,'阻断':True}}})
    row=result['阶段']['temu_union']
    assert row['需人工介入'] and row['阻断']
    assert '需要处理' in row['状态'] and '等待自动重启' not in row['状态']
