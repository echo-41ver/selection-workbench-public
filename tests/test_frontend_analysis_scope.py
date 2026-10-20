"""Keep the normal market merge and accurately report its separate sources."""
import json
import sys
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import pipeline
import watch_temu_union as watcher
from selection_automation.merge_temu_snapshots import current_source_summary


def inputs(tmp_path):
    base=tmp_path/'runs/fixture'
    worker=base/'temu/workers/worker-01-new/q/products.jsonl'
    worker.parent.mkdir(parents=True)
    worker.write_text('{"goods_id":"front","title":"frontend"}\n',encoding='utf-8')
    provider=base/'market/geekbi.json';provider.parent.mkdir()
    provider.write_text(json.dumps({'status':'COMPLETE','site_id':48,'records':[
        {'goodsId':'market','goodsName':'market reference'}]}),encoding='utf-8')
    category={'category_key':'fixture','temu':{}}
    (base/'category.json').write_text(json.dumps(category),encoding='utf-8')
    return base,category,provider


@pytest.mark.parametrize('entry',['watcher','pipeline'])
def test_both_merge_entry_points_preserve_market_merge_and_report_groups(tmp_path,monkeypatch,entry):
    base,category,provider=inputs(tmp_path)
    before=provider.read_bytes()
    monkeypatch.setattr(watcher,'ROOT',tmp_path);monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    if entry=='watcher':
        result=watcher.publish_once(base/'temu')
        assert result['status']=='PUBLISHED'
    else:pipeline.merge_temu(category)
    rows=[json.loads(line) for line in (base/'temu/union/products.jsonl').read_text('utf-8').splitlines()]
    summary=json.loads((base/'temu/union/summary.json').read_text('utf-8'))
    assert {row['goods_id'] for row in rows}=={'front','market'}
    assert summary['provider_reference_goods']==1
    assert summary['provider_supplement_goods']==1
    assert summary['analysis_unique_goods']==len(rows)
    assert provider.read_bytes()==before


def test_current_counts_read_old_union_source_tags_without_republication(tmp_path):
    base,category,provider=inputs(tmp_path)
    folder=base/'temu/union';folder.mkdir()
    union=folder/'products.jsonl'
    union.write_text(json.dumps({'goods_id':'front','source':'VISIBLE_TEMU_FRONTEND_KEYWORD_UNION'})+'\n'+
                     json.dumps({'goods_id':'market','source':'GEEKBI_SUPPLEMENT'})+'\n',encoding='utf-8')
    old_summary=folder/'summary.json';old_summary.write_text('{"unique_goods_id":5939}',encoding='utf-8')
    progress=base/'temu/workers/worker-01-new/q/progress.json'
    progress.write_text('{"raw_collected_count":8}',encoding='utf-8')
    before={path:path.read_bytes() for path in (union,old_summary,provider,progress)}
    counts=current_source_summary(tmp_path,category)
    assert counts['frontend_unique_goods']==1 and counts['provider_supplement_goods']==1
    assert counts['analysis_unique_goods']==2 and counts['source_rows']==1
    assert counts['frontend_raw_collected_count']==8 and counts['provider_reference_goods']==1
    assert counts['source_count_errors']==[]
    assert all(path.read_bytes()==content for path,content in before.items())


def test_missing_or_broken_source_does_not_fallback_to_legacy_unique(tmp_path):
    base,category,_=inputs(tmp_path);folder=base/'temu/union';folder.mkdir()
    (folder/'summary.json').write_text('{"unique_goods_id":5939}')
    counts=current_source_summary(tmp_path,category)
    assert counts['frontend_unique_goods'] is None and counts['analysis_unique_goods'] is None
    assert counts['frontend_raw_collected_count'] is None
    (folder/'products.jsonl').write_text('broken json')
    counts=current_source_summary(tmp_path,category)
    assert counts['frontend_unique_goods'] is None and counts['source_count_errors']==['union']


def test_counts_refresh_after_atomic_source_change(tmp_path):
    base,category,_=inputs(tmp_path);folder=base/'temu/union';folder.mkdir()
    path=folder/'products.jsonl'
    path.write_text('{"goods_id":"front","source":"VISIBLE_TEMU_FRONTEND_KEYWORD_UNION"}\n')
    assert current_source_summary(tmp_path,category)['analysis_unique_goods']==1
    path.write_text(path.read_text()+'{"goods_id":"market","source":"GEEKBI_SUPPLEMENT"}\n')
    counts=current_source_summary(tmp_path,category)
    assert counts['analysis_unique_goods']==2 and counts['frontend_unique_goods']==1


def test_independent_union_watcher_does_not_start_after_normal_task_stop(tmp_path,monkeypatch):
    base,_,_=inputs(tmp_path);(base/'STOP').write_text('normal task stop')
    union=base/'temu/union';union.mkdir();artifact=union/'products.jsonl'
    artifact.write_bytes(b'last completed publication')
    monkeypatch.setattr(watcher,'ROOT',tmp_path)
    monkeypatch.setattr(watcher,'load_category',lambda path:{'category_key':'fixture'})
    monkeypatch.setattr(watcher,'publish_once',lambda *a:pytest.fail('stopped task must not publish'))
    watcher.watch(base/'category.json')
    assert artifact.read_bytes()==b'last completed publication'
    assert not (union/'watcher.lock').exists()


def test_normal_stop_ends_independent_watcher_before_another_cycle_or_long_sleep(tmp_path,monkeypatch):
    base,_,_=inputs(tmp_path);calls=[]
    monkeypatch.setattr(watcher,'ROOT',tmp_path)
    monkeypatch.setattr(watcher,'load_category',lambda path:{'category_key':'fixture'})
    def publish(*args):
        calls.append(args);(base/'STOP').write_text('normal task stop')
        return {'status':'PUBLISHED'}
    monkeypatch.setattr(watcher,'publish_once',publish)
    monkeypatch.setattr(watcher.time,'sleep',lambda seconds:pytest.fail('must notice Stop before sleeping'))
    watcher.watch(base/'category.json',interval=30)
    assert len(calls)==1


def test_union_watcher_interval_observes_stop_without_waiting_full_interval(tmp_path,monkeypatch):
    stop=tmp_path/'STOP';sleeps=[]
    def sleep(seconds):sleeps.append(seconds);stop.write_text('stop during interval')
    monkeypatch.setattr(watcher.time,'sleep',sleep)
    assert not watcher.wait_for_task_interval(stop,30)
    assert len(sleeps)==1 and 0<sleeps[0]<=.25
