"""Completed draft form metadata is recovered without executing model requests."""
import copy,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import web_console as web

def category():
    return {'category_key':'keyboard_case','category_name_zh':'键盘',
            'amazon':{'target_count':10,'bestsellers_url':'https://www.amazon.com/zgbs/videogames/402051011'},
            'temu':{'max_products_per_keyword':400,'worker_count':4},'commercial':{'same_limit':5}}

def amazon_payload(original):
    return {'schema_version':'amazon-bestsellers-frontend-v1','status':'COMPLETE','details_complete':True,
            'category_key':original['category_key'],'source_url':original['amazon']['bestsellers_url'],
            'record_count':10,'records':[{'asin':f'B00000{n:04d}','rank':n} for n in range(1,11)],
            'amazon_browser_route':{'mode':'DIRECT','transport':'BROWSER_DIRECT'}}

def test_only_valid_category_bound_metadata_can_restore_scope():
    original=category();before=copy.deepcopy(original)
    payload=amazon_payload(original)
    scope=web.draft_form_scope(original,payload)
    assert scope=={'category':'键盘','amazon_count':10,'per_keyword':400,'worker_count':4,
                   'bestsellers_url':original['amazon']['bestsellers_url'],
                   'commercial':{'same_limit':5},'amazon_route_mode':'DIRECT'}
    assert original==before
    legacy=copy.deepcopy(original);legacy['temu'].pop('worker_count')
    assert web.draft_form_scope(legacy)['worker_count']==4
    for changes in ({'category_key':'unrelated'},{'source_url':'https://www.amazon.com/zgbs/tools/123'},
                    {'target_count':1},{'record_count':1},{'records':[]},{'status':'RUNNING'},
                    {'amazon_browser_route':{'mode':'unexpected'}}):
        assert 'amazon_route_mode' not in web.draft_form_scope(original,{**payload,**changes})
    for section,key,bad in [('amazon','target_count',0),('temu','max_products_per_keyword','400'),
                            ('temu','worker_count',33)]:
        broken=copy.deepcopy(original);broken[section][key]=bad
        assert web.draft_form_scope(broken) is None
    malformed=copy.deepcopy(payload);malformed['records'][0]['rank']=None
    assert 'amazon_route_mode' not in web.draft_form_scope(original,malformed)

def test_legacy_saved_draft_only_gains_metadata_preserving_reviewed_fields(tmp_path,monkeypatch):
    console=web.Console(tmp_path)
    folder=tmp_path/'runtime/web-console';path=folder/'plain-existing.json'
    original=category();web.selection.save(path,original)
    amazon=path.with_suffix('.amazon.json')
    web.selection.save(amazon,amazon_payload(original))
    saved={'id':'unchanged-reviewed-id','category_name':'键盘','stage1_text':'人工确认规则',
           'match_criteria':{'state':'ready','selected_ids':['shape']}}
    web.selection.save(folder/'resume-draft.json',{'draft':saved,'category_path':str(path),'amazon_path':str(amazon)})
    monkeypatch.setattr(web.Console,'invoke',lambda *a,**k:(_ for _ in ()).throw(AssertionError('no model or browser child')))
    restored=web.Console(tmp_path)
    assert {key:restored.draft[key] for key in saved}==saved
    assert restored.draft['form_scope']['per_keyword']==400
    assert restored.draft['form_scope']['amazon_route_mode']=='DIRECT'
    assert web.selection.read(path)==original
    saved['amazon_url']='https://www.amazon.com/zgbs/tools/123'
    web.selection.save(folder/'resume-draft.json',{'draft':saved,'category_path':str(path),'amazon_path':str(amazon)})
    assert 'form_scope' not in web.Console(tmp_path).draft
