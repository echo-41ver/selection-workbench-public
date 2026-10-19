import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import publish_all


def test_database_created_during_first_publication_waits_until_next_round(tmp_path,monkeypatch):
    base=tmp_path/'runs/fresh';base.mkdir(parents=True)
    category=base/'category.json';category.write_text('{"category_key":"fresh"}')
    database=base/'stage12/stage12.sqlite3'
    (base/'stage12_config.json').write_text(json.dumps({'output_dir':str(database.parent)}))
    monkeypatch.setattr(publish_all,'ROOT',tmp_path)
    monkeypatch.setattr(publish_all.subprocess,'run',lambda *a,**k:None)
    import selection_automation.commercial as commercial
    def build(*args):
        database.parent.mkdir(exist_ok=True);database.write_bytes(b'fixture');return {}
    monkeypatch.setattr(commercial,'build_report',build)
    import export_category_workbooks
    exported=[]
    monkeypatch.setattr(export_category_workbooks,'export',lambda path,kind:exported.append(kind) or {'固定查看副本已更新':True})
    first=publish_all.publish(category)
    assert first['errors']=={} and exported==[] and database.exists()
    second=publish_all.publish(category)
    assert second['errors']=={} and exported==['layer123']


@pytest.mark.parametrize('has_reviews', [False, True])
def test_codex_results_publish_three_layers_without_paid_authorization(tmp_path, monkeypatch, has_reviews):
    key = 'pump'
    base = tmp_path / 'runs' / key
    folder = base / 'stage123/codex_reviews'
    folder.mkdir(parents=True)
    category = base / 'category.json'
    category.write_text(json.dumps({'category_key': key}), encoding='utf-8')
    database = base / 'stage12'
    database.mkdir()
    (database / 'stage12.sqlite3').write_bytes(b'fixture')
    (base / 'stage12_config.json').write_text(json.dumps({'output_dir': str(database)}), encoding='utf-8')
    if has_reviews:
        (folder / 'reviews.jsonl').write_text('{}\n', encoding='utf-8')
    exports = []
    monkeypatch.setattr(publish_all, 'ROOT', tmp_path)
    monkeypatch.setattr(publish_all.subprocess, 'run', lambda *args, **kwargs: None)
    import export_category_workbooks
    monkeypatch.setattr(export_category_workbooks, 'export', lambda category, kind: exports.append(kind) or {'固定查看副本已更新': True})
    import selection_automation.commercial
    monkeypatch.setattr(selection_automation.commercial, 'build_report', lambda *args: {})
    result = publish_all.publish(category)
    assert result['errors'] == {}
    assert exports == ['layer123']
    assert not (base / 'stage123/paid_enabled.json').exists()
    if has_reviews:
        from watch_stage3_excel import source_paths, signature
        assert result['stage3_excel'] == signature(source_paths(category.absolute(),base.absolute()))
        assert result['stage3_excel_updated_at']>0
    else:
        assert 'stage3_excel' not in result


def test_new_reviews_during_main_export_remain_unpublished(tmp_path,monkeypatch):
    key='pump';base=tmp_path/'runs'/key
    folder=base/'stage123/codex_reviews';folder.mkdir(parents=True)
    category=base/'category.json';category.write_text(json.dumps({'category_key':key}),encoding='utf-8')
    database=base/'stage12';database.mkdir()
    (database/'stage12.sqlite3').write_bytes(b'fixture')
    (base/'stage12_config.json').write_text(json.dumps({'output_dir':str(database)}),encoding='utf-8')
    reviews=folder/'reviews.jsonl';reviews.write_text('{}\n',encoding='utf-8')
    monkeypatch.setattr(publish_all,'ROOT',tmp_path)
    monkeypatch.setattr(publish_all.subprocess,'run',lambda *a,**k:None)
    import selection_automation.commercial
    monkeypatch.setattr(selection_automation.commercial,'build_report',lambda *a:{})
    import export_category_workbooks
    def export(path,kind):
        if kind=='layer123':
            with reviews.open('a',encoding='utf-8') as stream:stream.write('{"new":true}\n')
        return {'固定查看副本已更新':True}
    monkeypatch.setattr(export_category_workbooks,'export',export)
    result=publish_all.publish(category)
    from watch_stage3_excel import source_paths,signature
    assert result['stage3_excel'] != signature(source_paths(category.absolute(),base.absolute()))
    assert result['deliveries']['layer123']['固定查看副本已更新']


def test_visual_only_updates_reuse_existing_stage12_freeze(tmp_path,monkeypatch):
    key='pump';base=tmp_path/'runs'/key
    folder=base/'stage123/codex_reviews';folder.mkdir(parents=True)
    category=base/'category.json';category.write_text(json.dumps({'category_key':key}),encoding='utf-8')
    database=base/'stage12';database.mkdir();(database/'stage12.sqlite3').write_bytes(b'fixture')
    (base/'stage12_config.json').write_text(json.dumps({'output_dir':str(database)}),encoding='utf-8')
    frozen=base/'stage123/export_frozen';frozen.mkdir()
    for name in ('layer12_rows','layer12_summary','layer3_candidates'):
        (frozen/f'{name}_{key}.jsonl' if name!='layer12_summary' else frozen/f'{name}_{key}.json').write_text('{}',encoding='utf-8')
    reviews=folder/'reviews.jsonl';reviews.write_text('{}\n',encoding='utf-8')
    monkeypatch.setattr(publish_all,'ROOT',tmp_path)
    snapshots=[]
    monkeypatch.setattr(publish_all.subprocess,'run',lambda *a,**k:snapshots.append(a))
    import selection_automation.commercial,export_category_workbooks
    monkeypatch.setattr(selection_automation.commercial,'build_report',lambda *a:{})
    monkeypatch.setattr(export_category_workbooks,'export',lambda *a:{'固定查看副本已更新':True})
    publish_all.publish(category)
    reviews.write_text('{}\n{"new":true}\n',encoding='utf-8')
    publish_all.publish(category)
    assert len(snapshots)==1
