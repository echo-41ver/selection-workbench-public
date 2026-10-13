import json
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import export_category_workbooks as exporter
from selection_automation.process_lock import ProcessLock


@pytest.mark.parametrize('fixed',[False,True])
def test_cli_registers_actual_delivery_under_lock_without_touching_other_state(tmp_path,monkeypatch,capsys,fixed):
    base=tmp_path/'runs/fixture';base.mkdir(parents=True)
    state_path=base/'publish_state.json'
    prior={'analysis':'old-signature','stage3_excel':'old-source-signature',
           'deliveries':{'amazon':{'old':'keep'},'layer123':{'查看快照':'old.xlsx'}},
           'errors':{'commercial':'preserve-existing'},
           'pending_views':{'temu':{'keep':True},'layer123':{'old':True}}}
    state_path.write_text(json.dumps(prior),encoding='utf-8')
    delivered={'固定查看副本已更新':fixed,'时间戳查看快照已生成':True,
               '查看快照':'new.xlsx','数据源工作簿':'data.xlsx','固定查看副本':'fixed.xlsx'}
    def export(category,kind):
        assert kind=='layer123'
        with pytest.raises(RuntimeError,match='Another supervisor'):
            with ProcessLock(base/'publish.lock'):pass
        return delivered
    monkeypatch.setattr(exporter,'ROOT',tmp_path)
    monkeypatch.setattr(exporter,'load_category',lambda _: {'category_key':'fixture'})
    monkeypatch.setattr(exporter,'export',export)
    exporter.main(['--category',str(base/'category.json'),'--kind','layer123'])
    current=json.loads(state_path.read_text('utf-8'))
    assert current['deliveries']['layer123']==delivered
    assert current['deliveries']['amazon']==prior['deliveries']['amazon']
    assert current['errors']==prior['errors']
    assert current['analysis']==prior['analysis'] and current['stage3_excel']==prior['stage3_excel']
    assert current['pending_views']['temu']==prior['pending_views']['temu']
    if fixed:assert 'layer123' not in current['pending_views']
    else:assert current['pending_views']['layer123']=={'数据源工作簿':'data.xlsx','固定查看副本':'fixed.xlsx'}
    assert json.loads(capsys.readouterr().out)==delivered


@pytest.mark.parametrize('failure',['export_error','not_delivered'])
def test_failed_cli_export_preserves_previous_publication(tmp_path,monkeypatch,failure):
    base=tmp_path/'runs/fixture';base.mkdir(parents=True)
    path=base/'publish_state.json';path.write_text('{"deliveries":{"layer123":{"snapshot":"keep.xlsx"}}}',encoding='utf-8')
    before=path.read_bytes()
    monkeypatch.setattr(exporter,'ROOT',tmp_path)
    monkeypatch.setattr(exporter,'load_category',lambda _: {'category_key':'fixture'})
    def export(*args):
        if failure=='export_error':raise RuntimeError('cannot publish')
        return {'固定查看副本已更新':False,'时间戳查看快照已生成':False}
    monkeypatch.setattr(exporter,'export',export)
    with pytest.raises(RuntimeError):
        exporter.main(['--category',str(base/'category.json'),'--kind','layer123'])
    assert path.read_bytes()==before
