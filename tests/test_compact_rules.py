from copy import deepcopy
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_generate_category import generator,fixture_config
from test_match_criteria import configured,products
from selection_automation.compact_authoring import expand,authoring_prompt,FORMAT,apply_policy_repair
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


def test_invalid_first_layer_dictionary_uses_the_same_bounded_rule_repair():
    raw=proposal();config=expand(raw,'电钻','https://www.amazon.com/zgbs/tools/123',3,40)
    config['stage1']['attributes']=[{'name':'power_source','hard_conflict':True,
        'values':{'battery':['shared alias'],'corded':['shared alias']},'ambiguous_phrases':[]}]
    calls=[]
    def repair(prompt,**options):
        calls.append(prompt)
        return {'identity_policy':config['stage3']['identity_policy'],'attributes':raw['attributes']}
    result=generator.compile_generated_policy(config,model=repair)
    assert len(calls)==1 and result['stage3']['identity_policy_generation']['repair_used']
    assert result['stage1']['attributes']==raw['attributes']


@pytest.mark.parametrize('category',['电磁炉','灯泡','鼠标垫','洗衣机','投影仪','收纳盒'])
@pytest.mark.parametrize('envelope',['direct','nested','compact'])
def test_repair_envelopes_preserve_category_and_compile_without_collection_changes(category,envelope):
    config=expand(proposal(),category,'https://www.amazon.com/gp/bestsellers/tools/123',10,400)
    fixed=deepcopy(config['stage3']['identity_policy'])
    broken=deepcopy(config);broken['stage3']['identity_policy']['features'][0].pop('required_visual')
    reply={'identity_policy':fixed,'attributes':config['stage1']['attributes'],'explanation':'字段说明不应阻断有效修正'}
    if envelope=='nested':reply={'result':{'stage3':{'identity_policy':fixed},'stage1':{'attributes':config['stage1']['attributes']}}}
    if envelope=='compact':
        reply={'features':fixed['features'],'product_scope':fixed['product_scope'],'attributes':config['stage1']['attributes']}
        for feature in reply['features']:feature.pop('modules')
    calls=[]
    def model(prompt,**kwargs):calls.append(kwargs);return reply
    result=generator.compile_generated_policy(broken,model=model)
    assert len(calls)==1 and calls[0]['max_tokens']==6000
    assert result['category_name_zh']==category
    assert result['amazon']==config['amazon'] and result['temu']==config['temu']
    generator.validate(result)


def test_repair_rejects_conflicting_semantics_in_duplicate_fields():
    config=expand(proposal(),'电磁炉','https://www.amazon.com/zgbs/tools/123',10,400)
    reply={'identity_policy':config['stage3']['identity_policy'],'attributes':config['stage1']['attributes'],
           'stage1':{'attributes':[]}}
    with pytest.raises(ValueError,match='重复字段'):apply_policy_repair(config,reply)


def test_second_repair_corrects_invalid_envelope_and_still_uses_original_scope():
    config=expand(proposal(),'电磁炉','https://www.amazon.com/zgbs/tools/123',10,400)
    broken=deepcopy(config);broken['stage3']['identity_policy']['features'][0].pop('required_visual')
    answers=iter([{'explanation':'只返回了说明'}, {'identity_policy':config['stage3']['identity_policy']}])
    calls=[]
    def model(prompt,**kwargs):calls.append(prompt);return next(answers)
    result=generator.compile_generated_policy(broken,model=model)
    assert len(calls)==2 and '尚未通过' in calls[1]
    assert result['amazon']==config['amazon'] and result['stage1']['attributes']==config['stage1']['attributes']


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


def test_authoring_distinguishes_optional_structural_identity_from_context():
    prompt=authoring_prompt('测试袋','袋体',{},None)
    assert '可见结构不得设为context' in prompt
    assert '未展示用未知，不改变身份作用' in prompt
    assert 'identity且required_visual=false' in prompt


def test_context_structure_is_repaired_as_optional_identity_without_relaxing_matching():
    from test_identity_policy import visual_facts
    raw=proposal()
    raw['features'].append({'id':'carry_loop','label':'附加提环','role':'context',
        'evidence':'both','region':'component','effects':['connection'],'preserves':[],
        'scope':'袋体侧边提环的连接形式','rule':'记录提环形式，不作为确认必需项',
        'required_visual':False})
    config=expand(raw,'测试袋','https://www.amazon.com/zgbs/tools/123',3,40)
    calls=[]
    def repair(prompt,**kwargs):
        calls.append(prompt)
        assert '可见结构不得设为context' in prompt
        assert 'identity且required_visual=false' in prompt
        fixed=deepcopy(config['stage3']['identity_policy'])
        fixed['features'][-1].update(role='identity',
            rule='双方提环均清晰可见时连接形式不同判不同款；未展示记未知，不作为确认必需项')
        return {'identity_policy':fixed,'attributes':config['stage1']['attributes']}
    result=generator.compile_generated_policy(config,model=repair)
    assert len(calls)==1
    assert config['stage3']['identity_policy']['features'][-1]['role']=='context'
    assert result['stage3']['identity_policy']['features'][-1]['role']=='identity'
    assert 'view_carry_loop' not in result['stage3']['identity_required_modules']
    assert 'IDENTITY_CARRY_LOOP' in result['stage3']['identity_hard_conflicts']
    facts=visual_facts(result)
    facts['feature_comparisons']['carry_loop'].update(relation='UNKNOWN',amazon='',temu='')
    facts['modules']['view_carry_loop'].update(relation='UNKNOWN',amazon_evidence='',temu_evidence='')
    assert score(result,facts)['state']=='确认同款'
    facts['feature_comparisons']['carry_loop'].update(relation='DIFFERENT',
        amazon='单段固定环扣',temu='双段铰接环扣')
    assert score(result,facts)['state']=='不同款'


def test_repair_cannot_keep_visible_structure_as_context():
    raw=proposal();feature=deepcopy(raw['features'][0])
    feature.update(id='carry_loop',role='context',required_visual=False)
    raw['features'].append(feature)
    config=expand(raw,'测试袋','https://www.amazon.com/zgbs/tools/123',3,40)
    calls=[]
    def repair(prompt,**kwargs):
        calls.append(prompt)
        return {'identity_policy':config['stage3']['identity_policy']}
    with pytest.raises(ValueError,match='可见结构差异'):
        generator.compile_generated_policy(config,model=repair)
    assert len(calls)==2


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
        from selection_automation.match_criteria import seed,attach
        from selection_automation.category_config import load_category
        from rule_editor import draft_view,edit_rules
        snapshot={'records':[{'asin':'A1','title':'Monitor','specifications':[{'label':'Display Type','value':'LCD'}]}]}
        attach(category,seed(category,snapshot))
        compiled=edit_rules(category,draft_view(category,'draft'),compile_model=lambda *a,**k:pytest.fail('no repeated model request'))
        path=tmp_path/'launch-config.json';path.write_text(json.dumps(compiled),encoding='utf-8')
        assert load_category(path,compile_authored_policy=True)['category_key']==category['category_key']
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
    from offline_preparation import completed_amazon_snapshot,extract_complete_visual
    console=web.Console(tmp_path)
    def invoke(args,**kwargs):
        if 'generate_category.py' in args[0]:
            category=expand(proposal(),'电钻','https://www.amazon.com/zgbs/tools/123',1,5)
            category=generator.compile_stage1_rules(generator.compile_generated_policy(category))
            Path(args[args.index('--output')+1]).write_text(json.dumps(add_summary(category)),encoding='utf-8')
        elif len(args)>1 and 'collect_amazon_search_frontend.mjs' in args[1]:
            category=json.loads(Path(args[args.index('--config')+1]).read_text('utf-8'))
            Path(args[args.index('--output')+1]).write_text(json.dumps(completed_amazon_snapshot(category)))
        elif 'extract_match_criteria.py' in args[0]:
            extract_complete_visual(args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    def no_explanation(*args,**kwargs):pytest.fail('generated semantic rules must not incur an explanation call')
    monkeypatch.setattr(console,'explain_file',no_explanation)
    console._start({'category_name':'电钻','amazon_count':1,'per_keyword':5,
        'api_key':'TEST_ONLY_CUSTOM_KEY','api_base_url':'http://127.0.0.1:8000/v1','api_model':'test-model'})
    assert console.draft and console.draft['plain_stage1'] and not console.generation_error, (console.phase,console.generation_error)
