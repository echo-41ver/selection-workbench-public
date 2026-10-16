"""Fresh authoring entry fixtures; no real generated quality, websites or APIs."""
from copy import deepcopy
from pathlib import Path
import json,sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation.category_contract import category_contract
from selection_automation.identity_policy import VERSION,compile_policy
import selection
import pipeline

CASES=[('fixture_cap','杯盖','threaded join','snap join'),
       ('fixture_clamp','桌面夹具','fixed joint','hinged joint'),
       ('fixture_organizer','收纳盒','solid partitions','mesh partitions')]

def authored_case(key,name,left,right):
    c=deepcopy(category_contract()['template']);c.update(category_key=key,category_name_zh=name)
    c['amazon'].update(bestsellers_url='https://www.amazon.com/zgbs/home-garden/123',target_count=1)
    c['temu'].update(base_queries=['fixture product'],include_terms=['fixture'],keyword_limit=2,max_products_per_keyword=3)
    c['stage1'].update(rule_version='authored-fixture-v1',attributes=[
        {'name':'construction','hard_conflict':True,'values':{'one':[left],'two':[right]}},
        {'name':'finish','hard_conflict':True,'values':{'red':['red'],'blue':['blue']}}])
    c['stage2']['candidate_threshold']=60
    c['stage3'].update(rule_version='authored-fixture-v1',prompt_version='authored-fixture-v1',schema_version='authored-fixture-v1',modules={'body':80,'finish':20},
        visual_instructions='由统一主体/颜色边界编译',identity_policy={'version':VERSION,'product_scope':name+'单件商品', 'features':[
            {'id':'construction','label':'主体构造','role':'identity','evidence':'both','region':'body','effects':['shape','connection'],'preserves':[], 'scope':'主体可见连接构造','rule':'明确不同的主体构造为不同款','modules':['body'],'required_visual':True},
            {'id':'finish','label':'颜色','role':'variant','evidence':'both','region':'body','effects':['color'],'preserves':['construction'],'scope':'主体表面颜色','rule':'主体构造一致时允许变色','modules':['finish'],'required_visual':False}]})
    return c

@pytest.mark.parametrize('key,name,left,right',CASES)
def test_actual_cli_prepare_accepts_raw_agent_contract_and_matches_mcp(tmp_path,monkeypatch,capsys,key,name,left,right):
    import mcp_server
    monkeypatch.setattr(selection,'ROOT',tmp_path);monkeypatch.setattr(pipeline,'ROOT',tmp_path);monkeypatch.setattr(mcp_server,'ROOT',tmp_path)
    raw=authored_case(key,name,left,right);incoming=tmp_path/'raw-agent.json';incoming.write_text(json.dumps(raw),encoding='utf8')
    monkeypatch.setattr(sys,'argv',['selection','prepare','--category',str(incoming)])
    selection.main()
    cli=selection.read(tmp_path/'runs'/key/'category.json')
    mcp_server.execute('selection_prepare',{'category':raw})
    assert selection.read(tmp_path/'runs'/key/'category.json')==cli==compile_policy(raw)
    assert json.loads(incoming.read_text(encoding='utf8'))==raw
    cfg=selection.read(tmp_path/'runs'/key/'stage12_config.json')
    assert cfg['candidate_threshold']==60 and cfg['text_gate'].startswith('declarative:')
    assert not (tmp_path/'runs'/key/'stage123'/'paid_enabled.json').exists()
    assert 'API' in capsys.readouterr().out

@pytest.mark.parametrize('key,name,left,right',CASES)
def test_human_three_layer_edits_reach_fresh_run_and_preserve_frozen_rules(tmp_path,monkeypatch,key,name,left,right):
    from plain_rules import apply_plain_rules, UnsupportedRules
    from rule_editor import draft_view
    from selection_automation.rules import text_gate
    from selection_automation.vision import protocol, vision_revision
    from selection_automation.category_config import load_category
    monkeypatch.setattr(selection,'ROOT',tmp_path);monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    original=compile_policy(authored_case(key,name,left,right))
    attrs=deepcopy(original['stage1']['attributes'])
    attrs[0]['values']['one'].append('reviewed connector')
    note='只按实际可见连接构造比较，缺失保持未知。'
    response={'supported':True,'stage1_attributes':attrs,'stage2_threshold':63,
              'stage3_prompt':draft_view(original,'')['stage3_text']+'\n'+note,
              'plain_summary':'第一层增加连接别名，第二层阈值调整为六十三，第三层强调缺失保持未知。',
              'changes':['增加连接别名。','调整候选阈值。','补充图片判断说明。']}
    calls=[]
    def stub(prompt,**kwargs):
        calls.append(prompt);return deepcopy(response)
    edited=apply_plain_rules(original,'增加连接别名，阈值设为63，图片缺失保持未知。',model=stub)
    assert len(calls)==1  # Injected response proves wiring, not model authoring quality.
    assert edited['stage2']['candidate_threshold']==63
    assert text_gate(edited,'reviewed connector red',right+' blue')['excluded']
    assert not text_gate(original,'reviewed connector red',right+' blue')['excluded']
    assert not text_gate(edited,left+' red',left+' blue')['excluded']
    assert not text_gate(edited,'',right+' blue')['excluded']
    assert note in protocol(edited,1)[0]
    image=tmp_path/'image.bin';image.write_bytes(b'fixture image only')
    pair={'asin':'fixture','goods_id':'fixture','amazon_image_path':str(image),'temu_image_path':str(image)}
    assert vision_revision(edited,pair)!=vision_revision(original,pair)
    source=tmp_path/'edited.json';source.write_text(json.dumps(edited),encoding='utf8')
    frozen,_,base=selection.prepare(source)
    assert load_category(frozen)==edited
    assert selection.read(base/'stage12_config.json')['candidate_threshold']==63
    assert not (base/'stage123'/'paid_enabled.json').exists()
    before=frozen.read_bytes()
    changed=deepcopy(edited);changed['stage2']['candidate_threshold']=64
    source.write_text(json.dumps(changed),encoding='utf8')
    with pytest.raises(ValueError): selection.prepare(source)
    assert frozen.read_bytes()==before
    raw=tmp_path/'uncompiled.json';raw.write_text(json.dumps(authored_case(key,name,left,right)),encoding='utf8')
    with pytest.raises(ValueError): load_category(raw)  # Frozen/default loader stays strict.
    response['supported']=False
    with pytest.raises(UnsupportedRules): apply_plain_rules(edited,'改变主体同款边界。',model=stub)
    assert frozen.read_bytes()==before

@pytest.mark.parametrize('key,name,left,right',CASES)
def test_web_generation_cli_writes_compiled_policy_using_injected_response(tmp_path,monkeypatch,key,name,left,right):
    import generate_category as generator
    raw=authored_case(key,name,left,right)
    evidence={'url':raw['amazon']['bestsellers_url'],'title':'Best Sellers',
              'heading':'Best Sellers in '+name,'rank_count':1,'links':[]}
    replies=iter([deepcopy(raw)])
    calls=[]
    def model(prompt,**kwargs):
        calls.append(prompt);return next(replies)
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-placeholder-not-a-credential')
    monkeypatch.setattr(generator,'read_listing',lambda url:deepcopy(evidence))
    monkeypatch.setattr(generator,'model_json',model)
    output=tmp_path/'draft.json';status=tmp_path/'generation.json'
    monkeypatch.setattr(sys,'argv',['generate_category','--category-name',name,'--amazon-count','1',
        '--per-keyword','3','--bestsellers-url',evidence['url'],'--output',str(output),'--status-file',str(status)])
    assert generator.main()==0
    generated=json.loads(output.read_text('utf8'))
    assert len(calls)==1 and generated['stage3']['identity_required_modules']==['body']
    assert generated['stage3']['variant_fields']==['finish']
    assert json.loads(status.read_text('utf8'))['stage']=='complete'
    before=output.read_bytes()
    def failed(*args,**kwargs): raise generator.CategoryGenerationError('离线模拟网络失败','MODEL_NETWORK_ERROR')
    monkeypatch.setattr(generator,'model_json',failed)
    assert generator.main()==1
    assert output.read_bytes()==before
