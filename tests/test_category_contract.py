import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from selection_automation.category_contract import category_contract
from selection_automation.category_config import load_category

ROOT = Path(__file__).resolve().parents[1]


def test_contract_has_no_category_or_fabricated_listing():
    result = category_contract()
    assert result['template']['amazon']['bestsellers_url'] is None
    assert result['template']['stage1']['attributes'] == []
    assert result['template']['stage3']['modules'] == {}
    assert result['ready_to_prepare'] is False
    result['template']['stage1']['attributes'].append({})
    assert category_contract()['template']['stage1']['attributes'] == []


def test_agent_filled_skeleton_passes_actual_loader(tmp_path):
    cat = category_contract()['template']
    cat.update(category_key='fixture', category_name_zh='测试品类')
    # Offline fixture only: this tests parser shape, not real source verification.
    cat['amazon'].update(bestsellers_url='https://www.amazon.com/zgbs/test-fixture', target_count=3)
    cat['temu'].update(base_queries=['fixture'], keyword_limit=3, max_products_per_keyword=5)
    cat['stage1'].update(rule_version='fixture-v1', attributes=[{'name':'identity','hard_conflict':True,'values':{'A':['type a'],'B':['type b']}}])
    cat['stage3'].update(rule_version='fixture-v1',prompt_version='fixture-v1',schema_version='fixture-v1',modules={'identity':100},module_descriptions={'identity':'双边可见身份事实'},visual_instructions='遮挡UNKNOWN')
    cat['stage3']['identity_policy']={'version':'category-identity-policy-v2-effects','product_scope':'测试本体',
        'features':[{'id':'identity','label':'主体形态','role':'identity','evidence':'both','scope':'外壳形状与连接部位',
                     'rule':'双边明确结构不同时排除','modules':['identity'],'required_visual':True,
                     'region':'body','effects':['shape','connection'],'preserves':[]}]}
    from selection_automation.identity_policy import compile_policy
    cat = compile_policy(cat)
    path=tmp_path/'category.json'; path.write_text(json.dumps(cat),encoding='utf-8')
    assert load_category(path) == cat


def test_cli_and_mcp_contract_match_without_execution(monkeypatch):
    env = {**os.environ, 'PYTHONUTF8': '1',
           'PYTHONPATH': os.pathsep.join(filter(None, (str(ROOT / 'src'), os.environ.get('PYTHONPATH', ''))))}
    cli=subprocess.run([sys.executable,'-m','selection_automation.category_contract'],capture_output=True,text=True,encoding='utf-8',check=True,env=env)
    monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    server=importlib.import_module('mcp_server')
    def forbidden(*args,**kwargs):
        pytest.fail('read-only contract must not execute or write')
    monkeypatch.setattr(server.subprocess,'run',forbidden)
    monkeypatch.setattr(server,'save',forbidden)
    monkeypatch.setattr(server,'prepare',forbidden)
    monkeypatch.setattr(Path,'write_text',forbidden)
    monkeypatch.setattr(Path,'write_bytes',forbidden)
    assert server.execute('selection_category_contract',{}) == json.loads(cli.stdout)
    with pytest.raises(ValueError):
        server.execute('selection_category_contract',{'category':'unexpected'})


def test_new_category_keyword_features_use_amazon_evidence():
    from selection_automation.rules import generic_keywords
    cat = category_contract()['template']
    for key in ('keyword_features', 'include_terms', 'severe_exclude_terms'):
        assert cat['temu'][key] == []
        assert 'temu.' + key in category_contract()['field_requirements']
    assert cat['temu']['collection_source'] == 'VISIBLE_TEMU_FRONTEND'
    cat['temu'].update(base_queries=['binoculars'], keyword_features=['waterproof', 'compact'],
                       include_terms=['binoculars'], severe_exclude_terms=['case only'],
                       keyword_limit=10, max_products_per_keyword=20)
    planned = generic_keywords([{'asin':'TEST_ASIN_A', 'title':'Waterproof binoculars', 'rank':1}], cat)
    by_query = {row['query']: row for row in planned}
    assert set(by_query) == {'binoculars', 'waterproof binoculars'}
    assert by_query['waterproof binoculars']['source_asins'] == ['TEST_ASIN_A']
    assert by_query['waterproof binoculars']['source_evidence'] == [
        {'asin':'TEST_ASIN_A', 'title':'Waterproof binoculars', 'rank':1}]
    assert all('towel' not in row['query'] for row in planned)
