import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import collect_market_evidence as market
from selection_automation.secrets import store


@pytest.fixture
def task(tmp_path,monkeypatch):
    import geekbi_temu_mcp.auth as sdk
    import generate_category
    import watch_temu_union
    import urllib.request
    monkeypatch.setattr(market,'ROOT',tmp_path)
    for key in ('DEEPSEEK_API_KEY','SELECTION_RULES_BASE_URL','SELECTION_RULES_MODEL','OPENAI_BASE_URL','OPENAI_VISION_MODEL'):
        monkeypatch.setenv(key,'')
    monkeypatch.setattr(urllib.request,'getproxies',lambda:{})
    monkeypatch.setattr(sdk,'urlopen',lambda *a,**k:pytest.fail('unexpected network request'))
    monkeypatch.setattr(watch_temu_union,'publish_once',lambda *a:None)
    category=tmp_path/'runs/fixture/category.json';category.parent.mkdir(parents=True)
    category.write_text(json.dumps({'category_key':'fixture','category_name_zh':'硅胶烘焙垫',
        'amazon':{'bestsellers_url':'https://www.amazon.com/fixture'},'temu':{'category_aliases':['baking mat']}}),encoding='utf8')
    auth=SimpleNamespace(state_path=tmp_path/'auth.json',server_key='https://openapi.geekbi.com')
    provider=SimpleNamespace(client=SimpleNamespace(auth=auth),
        categories=lambda query:{'total':2,'list':[
            {'catId':9768,'catName':'烘焙垫','isLeaf':True,'parentCatItems':[{'catName':'烘焙用品'}]},
            {'catId':9822,'catName':'烘焙垫','isLeaf':True,'parentCatItems':[{'catName':'厨房用具和小工具'}]}]},
        goods=lambda query:{'total':0,'list':[]})
    monkeypatch.setattr(market,'Provider',lambda **options:provider)
    monkeypatch.setattr(generate_category,'model_json',lambda *a,**k:pytest.fail('unexpected model request'))
    return SimpleNamespace(root=tmp_path,category=category,generate=generate_category,
        secret=tmp_path/'runtime/secrets/deepseek.dpapi')


@pytest.mark.parametrize('inherited_provider',[False,True])
def test_webhost_uses_saved_official_key_for_existing_bounded_category_mapper(task,monkeypatch,capsys,inherited_provider):
    import os
    if inherited_provider:
        monkeypatch.setenv('SELECTION_RULES_BASE_URL','https://fixture.example/v1')
        monkeypatch.setenv('SELECTION_RULES_MODEL','TEST_ONLY_OTHER_MODEL')
    store(task.secret,{'key':'TEST_ONLY_OFFICIAL_KEY','base_url':'https://api.deepseek.com','model':'deepseek-flash'})
    calls=[]
    def model(prompt,**options):
        assert os.environ['DEEPSEEK_API_KEY']=='TEST_ONLY_OFFICIAL_KEY'
        assert os.environ['SELECTION_RULES_BASE_URL']=='https://api.deepseek.com'
        assert os.environ['SELECTION_RULES_MODEL']=='deepseek-flash'
        assert options=={'max_tokens':600,'retry_truncated':False}
        calls.append(prompt)
        return {'cat_ids':[9768],'confidence':.95}
    monkeypatch.setattr(task.generate,'model_json',model)
    result=market.run(task.category)
    assert result['status']=='COMPLETE' and result['categories'][0]['catId']==9768
    assert len(calls)==1 and '烘焙用品' in calls[0]
    assert all('TEST_ONLY_OFFICIAL_KEY' not in path.read_text('utf8') for path in (task.category.parent/'market').glob('*.json'))
    assert capsys.readouterr()==('','')


def test_missing_saved_and_process_key_keeps_candidates_unresolved_without_model_request(task):
    result=market.run(task.category)
    assert result['status']=='CATEGORY_UNRESOLVED'
    assert [row['catId'] for row in result['category_candidates']]==[9768,9822]


def test_explicit_provider_credentials_are_not_replaced_by_saved_company_key(task,monkeypatch):
    import os
    import selection_automation.secrets as secrets
    store(task.secret,{'key':'TEST_ONLY_COMPANY_KEY'})
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_EXPLICIT_KEY')
    monkeypatch.setenv('SELECTION_RULES_BASE_URL','https://fixture.example/v1')
    monkeypatch.setenv('SELECTION_RULES_MODEL','TEST_ONLY_EXPLICIT_MODEL')
    monkeypatch.setattr(secrets,'load',lambda *a:pytest.fail('explicit provider must not read company key'))
    def model(*args,**options):
        assert os.environ['DEEPSEEK_API_KEY']=='TEST_ONLY_EXPLICIT_KEY'
        assert os.environ['SELECTION_RULES_BASE_URL']=='https://fixture.example/v1'
        assert os.environ['SELECTION_RULES_MODEL']=='TEST_ONLY_EXPLICIT_MODEL'
        return {'cat_ids':[9768],'confidence':.95}
    monkeypatch.setattr(task.generate,'model_json',model)
    assert market.run(task.category)['status']=='COMPLETE'


def test_invalid_saved_provider_fails_before_any_model_request_or_key_export(task,monkeypatch):
    import os
    store(task.secret,{'key':'TEST_ONLY_COMPANY_KEY','base_url':'https://fixture.example/v1','model':'TEST_ONLY_OTHER_MODEL'})
    result=market.run(task.category)
    assert result['status']=='FAILED' and result['error_type']=='ValueError'
    assert os.environ['DEEPSEEK_API_KEY']==''
    assert 'TEST_ONLY_COMPANY_KEY' not in json.dumps(result)
