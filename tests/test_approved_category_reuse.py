import sys,json
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import generate_category as generator


def fixture(monkeypatch,tmp_path):
    config={'category_key':'mice','category_name_zh':'鼠标','amazon':{
        'bestsellers_url':'https://www.amazon.com/Best-Sellers-Mice/zgbs/pc/123',
        'verification':{'url':'https://www.amazon.com/Best-Sellers-Mice/zgbs/pc/123','heading':'Best Sellers in Computer Mice','rank_count':30}},
        'temu':{'max_products_per_keyword':400,'category_aliases':['鼠标','mouse'],
                'subject_filter':{'mode':'TARGET_BODY_ONLY','reject_mixed_subjects':True,'exclude_subject_terms':['keyboard','键盘']}}}
    p=tmp_path/'runs/mice/category.json';p.parent.mkdir(parents=True);p.write_text(json.dumps(config),encoding='utf-8')
    monkeypatch.setattr(generator,'ROOT',tmp_path);monkeypatch.setattr(generator,'validate',lambda config:None)
    monkeypatch.setattr('selection_automation.node_runtime.ensure_node_runtime',lambda root,**kw:None)
    monkeypatch.setattr(generator,'model_json',lambda *a,**k:pytest.fail('Must not charge a model for an existing category'))
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    return config


def test_reuse_requires_live_matching_heading_and_updates_requested_counts(monkeypatch,tmp_path):
    config=fixture(monkeypatch,tmp_path)
    evidence={'url':config['amazon']['bestsellers_url'],'title':'Amazon Best Sellers','heading':'Best Sellers in Computer Mice','rank_count':30}
    called=[];monkeypatch.setattr(generator,'read_listing',lambda url:called.append(url) or evidence)
    result=generator.generate_category('鼠标',2,10,reuse_approved=True)
    assert called==[config['amazon']['bestsellers_url']]
    assert result['amazon']['target_count']==2 and result['temu']['max_products_per_keyword']==10
    assert result['approved_rule_source']['kind']=='VERIFIED_EXISTING_CATEGORY'


def test_other_category_and_changed_page_are_not_silently_reused(monkeypatch,tmp_path):
    config=fixture(monkeypatch,tmp_path)
    assert generator.approved_category('键盘') is None
    monkeypatch.setattr(generator,'read_listing',lambda url:{'url':url,'heading':'Best Sellers in Keyboards','rank_count':30})
    with pytest.raises(generator.CategoryGenerationError,match='页面已变化'):
        generator.generate_category('鼠标',2,10,reuse_approved=True)


def test_reviewed_aliases_are_persistent_and_scoped_to_exact_category_node(monkeypatch,tmp_path):
    config=fixture(monkeypatch,tmp_path)
    registry={'categories':[{'category_names':['鼠标'],'listing_identity':config['amazon']['bestsellers_url'],
        'aliases':['鼠标','鼠標','滑鼠'],'basis':'reviewed same category names','reviewed_at':'2026-10-13',
        'subject_filter':{'mode':'TARGET_BODY_ONLY','reject_mixed_subjects':True,'exclude_subject_terms':['键盘']}}]}
    folder=tmp_path/'config';folder.mkdir();(folder/'category_recall_aliases.json').write_text(json.dumps(registry),encoding='utf-8')
    result=generator.approved_category('鼠标')
    assert set(result['temu']['category_aliases'])=={'鼠标','mouse','鼠標','滑鼠'}
    assert result['temu']['subject_filter']['exclude_subject_terms']==['键盘']
    assert json.loads((tmp_path/'runs/mice/category.json').read_text())['temu']['category_aliases']==['鼠标','mouse']
    config['amazon']['bestsellers_url']='https://www.amazon.com/Best-Sellers/zgbs/pc/999'
    assert generator.apply_reviewed_recall_aliases(config)['temu']['category_aliases']==['鼠标','mouse']
