from pathlib import Path

import pytest

@pytest.mark.parametrize('module_name',['watch_stage3_excel','publish_all'])
def test_same_file_through_a_junction_has_one_publication_signature(tmp_path,monkeypatch,module_name):
    import importlib
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    module=importlib.import_module(module_name)
    actual=tmp_path/'actual/category.json'
    actual.parent.mkdir()
    actual.write_text('{"category_key":"pump"}',encoding='utf-8')
    alias=tmp_path/'junction/category.json'
    original_resolve=Path.resolve
    def resolve(path,*args,**kwargs):
        return actual if path==alias else original_resolve(path,*args,**kwargs)
    monkeypatch.setattr(Path,'resolve',resolve)
    assert module.signature([alias])==module.signature([actual])
    before=module.signature([alias])
    actual.write_text('{"category_key":"pump","changed":true}',encoding='utf-8')
    assert module.signature([alias])!=before
