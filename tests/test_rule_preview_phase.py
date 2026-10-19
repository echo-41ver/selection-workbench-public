"""Authoring may lack collection inputs; execution still requires them. No API/site."""
from copy import deepcopy
from pathlib import Path
import json,sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from rule_editor import draft_view,edit_rules
from selection_automation.identity_policy import compile_policy
from selection_automation import rules
from test_generic_agent_entry import authored_case, compile_authored
import selection,pipeline

CASES=['bottle_opener','desktop_rack','hanger','official_power_drill']

def category(case):
    if case in ('bottle_opener','desktop_rack'):
        raw=json.loads((ROOT/'tests/fixtures/preview_categories'/(case+'.json')).read_text('utf8'))
        return compile_policy(raw)
    if case=='hanger':
        raw=authored_case('preview_hanger','衣架','fixed hanger','folding hanger')
        raw['amazon'].update(bestsellers_url=None,target_count=None)
        raw['temu']['max_products_per_keyword']=None
        return compile_policy(raw)  # Synthetic third class, not another model generation.
    return json.loads((ROOT/'config/categories/power_drill.json').read_text('utf8'))

@pytest.mark.parametrize('case',CASES)
def test_threshold_and_prompt_preview_does_not_need_collection_inputs(case):
    original=category(case);before=deepcopy(original)
    view=draft_view(original,'preview')
    out=edit_rules(original,{'stage1_text':view['stage1_text'],'stage3_text':view['stage3_text'],
                    'stage2_threshold':61},compile_model=lambda *a,**k:pytest.fail('No text change must not invoke model'))
    assert out['stage2']['candidate_threshold']==61
    assert out['amazon']==before['amazon'] and out['temu']['max_products_per_keyword']==before['temu']['max_products_per_keyword']
    assert original==before
    getattr(rules,'validate_rules',rules.validate)(out)
    if case!='official_power_drill':
        with pytest.raises(ValueError,match='Amazon'):rules.validate(out)
    else:rules.validate(out)

@pytest.mark.parametrize('case',CASES)
def test_cli_rule_edit_then_prepare_requires_and_preserves_collection_inputs(tmp_path,monkeypatch,case):
    import runpy,generate_category
    monkeypatch.setattr(selection,'ROOT',tmp_path);monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    original=category(case)
    original['amazon'].update(bestsellers_url=None,target_count=None)
    original['temu']['max_products_per_keyword']=None
    view=draft_view(original,'preview');view['stage2_threshold']=61
    source=tmp_path/'draft.json';source.write_text(json.dumps(original),encoding='utf8')
    edits=tmp_path/'edits.json';edits.write_text(json.dumps(view),encoding='utf8')
    output=tmp_path/'reviewed.json'
    monkeypatch.setattr(generate_category,'model_json',lambda *a,**k:pytest.fail('No API in preview edit'))
    monkeypatch.setattr(sys,'argv',['rule_editor','--input',str(source),'--edits',str(edits),'--output',str(output)])
    runpy.run_path(str(ROOT/'scripts/rule_editor.py'),run_name='__main__')
    edited=json.loads(output.read_text('utf8'))
    assert edited['stage2']['candidate_threshold']==61 and edited['amazon']['bestsellers_url'] is None
    with pytest.raises(ValueError):selection.prepare(output)
    assert not (tmp_path/'runs').exists()
    # Offline fixture URL is format-only data, not a verified actual listing.
    edited['amazon']['bestsellers_url']='https://www.amazon.com/zgbs/home-garden/123'
    output.write_text(json.dumps(edited),encoding='utf8')
    with pytest.raises(ValueError,match='Amazon'):selection.prepare(output)
    edited['amazon']['target_count']=1;output.write_text(json.dumps(edited),encoding='utf8')
    with pytest.raises(ValueError,match='TEMU'):selection.prepare(output)
    assert not (tmp_path/'runs').exists()
    edited['temu']['max_products_per_keyword']=20;output.write_text(json.dumps(edited),encoding='utf8')
    frozen,_,base=selection.prepare(output)
    assert rules.read_frozen(str(frozen))==compile_authored(edited)
    assert selection.read(base/'stage12_config.json')['candidate_threshold']==61
    assert not (base/'stage123/paid_enabled.json').exists()
    before=frozen.read_bytes();edited['stage2']['candidate_threshold']=62
    output.write_text(json.dumps(edited),encoding='utf8')
    with pytest.raises(ValueError):selection.prepare(output)
    assert frozen.read_bytes()==before
    assert json.loads(source.read_text('utf8'))==original

@pytest.mark.parametrize('case',CASES)
def test_natural_language_edit_uses_rule_phase_without_inventing_collection_inputs(case):
    from plain_rules import apply_plain_rules
    original=category(case);original['amazon'].update(bestsellers_url=None,target_count=None)
    original['temu']['max_products_per_keyword']=None
    before=deepcopy(original);view=draft_view(original,'preview')
    response={'supported':True,'stage1_attributes':original['stage1']['attributes'],
              'stage2_threshold':61,'stage3_prompt':view['stage3_text']+'\n未展示保持未知。',
              'plain_summary':'候选阈值改为六十一，未展示信息保持未知。','changes':['修改候选阈值。','补充图片未知规则。']}
    calls=[]
    def model(*args,**kwargs):calls.append(1);return deepcopy(response)
    edited=apply_plain_rules(original,'阈值改为61，未展示保持未知。',model=model)
    rules.validate_rules(edited)
    assert calls==[1] and edited['stage2']['candidate_threshold']==61
    assert edited['amazon']==original['amazon'] and edited['temu']['max_products_per_keyword'] is None
    assert original==before

@pytest.mark.parametrize('mutation',['key','name','output_path','nan','inf','bool','score_bool','alias_type','action','conflict_type','source_type','count_type','gate_type'])
def test_bad_rule_preview_is_rejected_without_mutating_original(mutation):
    original=category('bottle_opener')
    if mutation=='key':original['category_key']='../escape'
    elif mutation=='name':original['category_name_zh']='bad:name'
    elif mutation=='output_path':original['stage12_output_dir']='..\\escape'
    elif mutation in ('nan','inf','bool'):original['stage2']['candidate_threshold']={'nan':float('nan'),'inf':float('inf'),'bool':True}[mutation]
    elif mutation=='score_bool':original['stage3']['same_threshold']=True
    elif mutation=='alias_type':original['stage1']['attributes'][0]['values']['wing_corkscrew']='not a list'
    elif mutation=='action':original['stage2']['below_threshold_action']='FINAL_DIFFERENT'
    elif mutation=='conflict_type':original['stage1']['attributes'][0]['hard_conflict']='true'
    elif mutation=='source_type':original['amazon']['bestsellers_url']={}
    elif mutation=='count_type':original['amazon']['target_count']=True
    elif mutation=='gate_type':original['stage1']['text_gate']=None
    before=json.dumps(original,sort_keys=True)
    with pytest.raises(ValueError):draft_view(original,'preview')
    assert json.dumps(original,sort_keys=True)==before

@pytest.mark.parametrize('case',['bottle_opener','desktop_rack','hanger'])
def test_plain_cli_invalid_edit_keeps_saved_standard(tmp_path,monkeypatch,case):
    import runpy,generate_category
    original=category(case);view=draft_view(original,'preview')
    source=tmp_path/'draft.json';source.write_text(json.dumps(original),encoding='utf8');before=source.read_bytes()
    output=tmp_path/'prior.json';output.write_text('PREVIOUS SUCCESSFUL STANDARD',encoding='utf8');kept=output.read_bytes()
    instruction=tmp_path/'instruction.txt';instruction.write_text('候选阈值改为无穷。',encoding='utf8')
    error=tmp_path/'error.json'
    response={'supported':True,'stage1_attributes':original['stage1']['attributes'],'stage2_threshold':float('inf'),
              'stage3_prompt':view['stage3_text'],'plain_summary':'尝试修改候选阈值。','changes':['修改候选阈值。']}
    monkeypatch.setattr(generate_category,'model_json',lambda *a,**k:deepcopy(response))
    monkeypatch.setattr(sys,'argv',['plain_rules','--input',str(source),'--instruction-file',str(instruction),'--output',str(output),'--error-file',str(error)])
    with pytest.raises(SystemExit) as exc:runpy.run_path(str(ROOT/'scripts/plain_rules.py'),run_name='__main__')
    assert exc.value.code==1
    assert source.read_bytes()==before and output.read_bytes()==kept
    assert json.loads(error.read_text('utf8'))['code']=='PLAIN_RULES_FORMAT'

@pytest.mark.parametrize('field,value',[('amazon_count',None),('amazon_count',0),('amazon_count',True),('amazon_count',1.5),('temu_count',None),('temu_count',0),('temu_count',True)])
def test_default_execution_validator_keeps_collection_count_gates(field,value):
    original=category('official_power_drill')
    if field=='amazon_count':original['amazon']['target_count']=value
    else:original['temu']['max_products_per_keyword']=value
    with pytest.raises(ValueError):rules.validate(original)
