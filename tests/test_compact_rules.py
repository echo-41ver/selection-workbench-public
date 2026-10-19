from copy import deepcopy
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_generate_category import generator,fixture_config
from test_match_criteria import configured,products
from selection_automation.compact_authoring import expand,authoring_prompt,FORMAT
from selection_automation.vision import protocol,decode
from selection_automation.rules import score


def proposal():
    full=fixture_config();features=deepcopy(full['stage3']['identity_policy']['features'])
    for f in features:f.pop('modules')
    return {'format':FORMAT,'category_key':'drill','product_scope':'比较电钻本体',
        'queries':['power drill'],'aliases':['电钻','電鑽','power drill'],
        'exclude_terms':['nail drill','磨甲机'],'keyword_features':['cordless'],
        'attributes':full['stage1']['attributes'],'threshold':54,'threshold_reason_zh':'按拍摄角度差异保守召回，避免漏检。',
        'features':features}


def test_semantic_plan_produces_valid_rules_and_chinese_summary_without_explanation_call(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only')
    evidence={'url':'https://www.amazon.com/zgbs/tools/123','title':'Best Sellers',
              'heading':'Best Sellers in Power Drills','rank_count':50,'links':[]}
    monkeypatch.setattr(generator,'read_listing',lambda _:evidence)
    calls=[]
    def ask(prompt,**kwargs):calls.append((prompt,kwargs));return proposal()
    monkeypatch.setattr(generator,'model_json',ask)
    category=generator.generate_category('电钻',3,40,bestsellers_url=evidence['url'])
    assert len(calls)==1 and calls[0][1]['max_tokens']==6000
    assert sum(category['stage3']['modules'].values())==100
    assert category['amazon']['target_count']==3 and category['temu']['max_products_per_keyword']==40
    assert category['stage2']['candidate_threshold']==54 and category['stage3']['identity_required_modules']
    assert all(category[k] for k in ('plain_stage1','plain_stage3','plain_rules_summary'))
    assert category['rule_summary_source']=='generated_conditions_local_summary_v1'


def test_compact_authoring_keeps_meaning_but_omits_mechanical_config_and_navigation_links():
    prompt=authoring_prompt('电钻','电钻本体',{'url':'https://www.amazon.com/zgbs/tools/123','links':[{'title':'irrelevant-navigation'}]},None)
    assert 'irrelevant-navigation' not in prompt and 'detail_concurrency' not in prompt
    bad=proposal();bad['features']=[]
    with pytest.raises(ValueError):expand(bad,'电钻','https://www.amazon.com/zgbs/tools/123',3,40)


def test_independent_text_attribute_does_not_require_a_same_named_image_feature(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only')
    evidence={'url':'https://www.amazon.com/zgbs/tools/123','title':'Best Sellers',
              'heading':'Best Sellers in Power Drills','rank_count':50,'links':[]}
    monkeypatch.setattr(generator,'read_listing',lambda _:evidence)
    value=proposal();value['attributes'][0]['name']='power_supply'
    calls=[]
    def ask(*args,**kwargs):calls.append(1);return deepcopy(value)
    monkeypatch.setattr(generator,'model_json',ask)
    category=generator.generate_category('电钻',3,40,bestsellers_url=evidence['url'])
    assert category['stage1']['attributes'][0]['name']=='power_supply' and len(calls)==1
    assert category['stage3']['identity_policy']['features'][0]['id']=='power'
    from rule_editor import draft_view,edit_rules
    out=edit_rules(category,draft_view(category,'draft'),compile_model=lambda *a,**k:pytest.fail('unchanged rules'))
    assert out['stage1']['attributes'][0]['name']=='power_supply'
    assert 'prompt_override' not in out['stage3']


def test_real_monitor_response_is_compiled_and_reused_without_another_paid_request(tmp_path,monkeypatch):
    receipt=json.loads((Path(__file__).parent/'fixtures/compact-monitor-model-response.json').read_text('utf-8'))
    evidence={'url':receipt['scope']['listing'],'title':'Best Sellers','heading':'Best Sellers in Computer Monitors','rank_count':50,'links':[]}
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.delenv('SELECTION_RULES_BASE_URL',raising=False);monkeypatch.delenv('SELECTION_RULES_MODEL',raising=False)
    monkeypatch.setattr(generator,'approved_category',lambda *args:None)
    monkeypatch.setattr(generator,'read_listing',lambda _:evidence)
    calls=[]
    def ask(*args,**kwargs):calls.append(1);return deepcopy(receipt['proposal'])
    monkeypatch.setattr(generator,'model_json',ask)
    for count in (10,12):
        category=generator.generate_category('电脑显示器',count,400,bestsellers_url=evidence['url'],reuse_approved=True,draft_store=tmp_path)
        assert category['amazon']['target_count']==count
        assert next(f for f in category['stage3']['identity_policy']['features'] if f['id']=='back_structure')['role']=='context'
    assert len(calls)==1 and 'TEST_ONLY_KEY' not in next(tmp_path.glob('*.json')).read_text('utf-8')


def test_field_name_aliases_and_optional_metadata_do_not_discard_valid_rules():
    original=proposal();value=deepcopy(original)
    value.pop('format');value.pop('keyword_features');value['provider_note']='metadata only'
    value['stage1_attributes']=value.pop('attributes')
    value['identity_policy']={'product_scope':value.pop('product_scope'),'features':value.pop('features')}
    result=expand({'configuration':value},'电钻','https://www.amazon.com/zgbs/tools/123',3,40)
    assert result['stage1']['attributes']==original['attributes']
    assert result['stage3']['identity_policy']['product_scope']==original['product_scope']
    value['attributes']=[]
    with pytest.raises(ValueError,match='重复字段含义冲突'):expand(value,'电钻','url',3,40)


def test_default_deepseek_authoring_avoids_spending_output_budget_on_thinking(monkeypatch):
    from io import BytesIO
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only')
    monkeypatch.delenv('SELECTION_RULES_BASE_URL',raising=False);monkeypatch.delenv('SELECTION_RULES_MODEL',raising=False)
    calls=[]
    def send(request,**kw):
        calls.append(json.loads(request.data))
        return BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}).encode())
    monkeypatch.setattr(generator,'open_model_url',send)
    assert generator.model_json('test',max_tokens=6000)=={'ok':True}
    assert calls[0]['thinking']=={'type':'disabled'} and 'reasoning_effort' not in calls[0]
    assert calls[0]['max_tokens']==6000 and len(calls)==1


def candidate(identity='C00',relation='S',condition='S'):
    return {'i':identity,'r':relation,'a':'圆形镜头，右侧弧形支架','t':'圆形镜头，右侧弧形支架',
        'why':'已对齐外壳、镜头和支架位置','q':{'body_type':{'r':condition,'a':'右侧弧形支架',
        't':'右侧弧形支架' if condition=='S' else '右侧完整可见，没有支架' if condition=='D' else '右侧被包装遮挡'}},'c':.95}


@pytest.mark.parametrize('relation,condition,state',[
    ('S','S','确认同款'),('L','D','相似款'),('D','D','不同款'),('U','U','待补充证据'),('S','U','待补充证据')])
def test_compact_visual_preserves_clear_absence_missing_view_and_three_classes(products,relation,condition,state):
    category=configured(products)
    value=decode(category,{'candidates':[candidate(relation=relation,condition=condition)]},['C00'])['C00']
    assert score(category,value)['state']==state
    assert value['condition_comparisons']['body_type']['temu']
    assert value['modules']=={}  # No fake auxiliary scores derived from the verdict.


def test_group_and_local_agent_compact_schema_rejects_wrong_or_missing_bindings(products):
    from selection_automation.local_visual_wire import prepare,unpack
    category=configured(products);prompt,schema=protocol(category,2)
    assert set(schema['properties']['candidates']['items']['properties'])=={'i','r','a','t','why','q','c'}
    assert 'properties' in schema and 'amazon' not in schema['properties']
    _,wire=prepare(prompt,schema,['C00','C01'])
    values=[candidate(),candidate('C01')]
    raw=unpack({'candidates':{v['i']:{k:x for k,x in v.items() if k!='i'} for v in values}},['C00','C01'])
    assert set(decode(category,raw,['C00','C01']))=={'C00','C01'}
    bad=deepcopy(raw);bad['candidates'][1]['i']='C00'
    with pytest.raises(ValueError):decode(category,bad,['C00','C01'])
    bad=deepcopy(raw);bad['candidates'][0]['q']={}
    with pytest.raises(ValueError):decode(category,bad,['C00','C01'])
    category['stage3']['prompt_override']='必须比较镜头外圈形状'
    assert '必须比较镜头外圈形状' in protocol(category,1)[0]


def test_compact_agent_facts_import_without_requiring_duplicate_module_evidence(products):
    from selection_automation.codex_visual_review import checked_facts
    category=configured(products);category['stage3']['codex_require_module_facts']=True
    value=decode(category,{'candidates':[candidate()]},['C00'])['C00']
    checked=checked_facts(category,value,'T1')
    assert score(category,checked)['state']=='确认同款'
    missing=deepcopy(value);missing['condition_comparisons']={}
    with pytest.raises(ValueError):checked_facts(category,missing,'T1')


def test_native_agent_saves_compact_evidence_with_original_product_bindings(products,tmp_path,monkeypatch):
    from selection_automation.native_agent_transport import write_response
    category=configured(products)
    monkeypatch.setattr('selection_automation.category_config.load_category',lambda _:category)
    (tmp_path/'analysis-contract.json').write_text(json.dumps({'category':'bound-category.json'}))
    (tmp_path/'compact-output.json').write_text('{}')
    (tmp_path/'manifest.jsonl').write_text(json.dumps({'asin':'A1','goods_id':'T1','vision_revision_key':'bound-revision'})+'\n')
    assert write_response(tmp_path,json.dumps({'candidates':[candidate()]}))
    review=json.loads((tmp_path/'reviews.jsonl').read_text('utf-8'))
    assert review['vision_revision_key']=='bound-revision' and review['goods_id']=='T1'
    assert review['amazon_evidence']=='圆形镜头，右侧弧形支架'
    assert review['temu_evidence']=='圆形镜头，右侧弧形支架'


def test_web_uses_local_generated_summary_without_a_second_model_request(tmp_path,monkeypatch):
    import web_console as web
    from pathlib import Path
    from types import SimpleNamespace
    from selection_automation.compact_authoring import add_summary
    console=web.Console(tmp_path)
    def invoke(args,**kwargs):
        if 'generate_category.py' in args[0]:
            category=expand(proposal(),'电钻','https://www.amazon.com/zgbs/tools/123',1,5)
            category=generator.compile_stage1_rules(generator.compile_generated_policy(category))
            Path(args[args.index('--output')+1]).write_text(json.dumps(add_summary(category)),encoding='utf-8')
        elif len(args)>1 and 'collect_amazon_search_frontend.mjs' in args[1]:
            category=json.loads(Path(args[args.index('--config')+1]).read_text('utf-8'))
            Path(args[args.index('--output')+1]).write_text(json.dumps({'records':[
                {'asin':'B000000001','rank':1,'title':'Cordless power drill kit'}], 'record_count':1,
                'source_url':category['amazon']['bestsellers_url'],'category_key':category['category_key']}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    def no_explanation(*args,**kwargs):pytest.fail('generated semantic rules must not incur an explanation call')
    monkeypatch.setattr(console,'explain_file',no_explanation)
    console._start({'category_name':'电钻','amazon_count':1,'per_keyword':5,
        'api_key':'TEST_ONLY_CUSTOM_KEY','api_base_url':'http://127.0.0.1:8000/v1','api_model':'test-model'})
    assert console.draft and console.draft['plain_stage1'] and not console.generation_error, (console.phase,console.generation_error)
