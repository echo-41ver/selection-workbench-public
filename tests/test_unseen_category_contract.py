from copy import deepcopy
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation.category_contract import category_contract
from selection_automation.identity_policy import VERSION, compile_policy, validate_policy_consistency
from selection_automation.rules import text_gate, score, validate
from selection_automation.vision import protocol,vision_revision,compatible_vision_keys
from identity_fixtures import identity
from rule_editor import edit_rules,draft_view

@pytest.mark.parametrize('key,name,left,right',[
 ('fixture_bulb','灯泡','threaded connector','bayonet connector'),
 ('fixture_mirror','镜子','wall mount','floor support'),
 ('fixture_planter','花盆','fixed body','hinged body')])
def test_unseen_category_one_contract_across_edit_text_vision_and_cache(tmp_path,key,name,left,right):
    # Mechanism fixtures from the generic authoring contract, not existing category templates.
    c=deepcopy(category_contract()['template']);c.update(category_key=key,category_name_zh=name)
    c['amazon'].update(bestsellers_url='https://www.amazon.com/zgbs/home-garden/fixture',target_count=1)
    c['temu'].update(base_queries=['fixture product'],max_products_per_keyword=3)
    c['stage1'].update(rule_version='fixture-v1',attributes=[
      {'name':'construction','hard_conflict':True,'values':{'one':[left],'two':[right]}},
      {'name':'finish','hard_conflict':True,'values':{'red':['red'],'blue':['blue']}}])
    c['stage2'].update(candidate_threshold=55,threshold_reason_zh='离线测试建议，未校准。')
    c['stage3'].update(rule_version='fixture-v1',prompt_version='fixture-v1',schema_version='fixture-v1',modules={'body':80,'appearance':20},identity_policy={'version':VERSION,'product_scope':name+'完整商品主体','features':[
      {'id':'construction','label':'主体连接结构','role':'identity','evidence':'both','region':'body','effects':['shape','connection'],'preserves':[],'scope':'商品主体连接部位','rule':'连接构造不同是身份差异','modules':['body'],'required_visual':True},
      {'id':'finish','label':'外观颜色','role':'variant','evidence':'both','region':'body','effects':['color'],'preserves':['construction'],'scope':'本体表面颜色','rule':'主体结构一致时可变色','modules':['appearance'],'required_visual':False}]})
    c=compile_policy(c);validate(c);validate_policy_consistency(c)
    assert not text_gate(c,left+' red',left+' blue')['excluded']
    assert text_gate(c,left,right)['excluded']
    assert not text_gate(c,'missing evidence',right)['excluded']
    prompt,_=protocol(c,1)
    assert name in prompt and '浴帘' not in prompt and '浴巾' not in prompt
    d=draft_view(c,'fixture');edited=edit_rules(c,{'stage1_text':d['stage1_text'],'stage3_text':d['stage3_text'],'stage2_threshold':61,'stage3_modules_json':d['stage3_modules_json']},compile_model=lambda *a,**k:pytest.fail('Unchanged authored rules must not call paid compiler'))
    validate_policy_consistency(edited)
    f={'overall_confidence':.95,'identity':identity(),'modules':{'body':{'relation':'SAME','amazon_evidence':left,'temu_evidence':left,'hard_conflict_code':'NONE'},'appearance':{'relation':'UNKNOWN','amazon_evidence':'','temu_evidence':'','hard_conflict_code':'NONE'}},'feature_comparisons':{'construction':{'relation':'SAME','amazon':left,'temu':left},'finish':{'relation':'UNKNOWN','amazon':'','temu':''}}}
    f['identity']['matches'] = f['identity']['matches'][:1]
    assert score(edited,f)['state']=='确认同款'
    f['feature_comparisons']['construction']['relation']='DIFFERENT';f['feature_comparisons']['construction']['temu']=right
    assert score(edited,f)['state']=='不同款'
    a=tmp_path/'a';t=tmp_path/'t';a.write_bytes(b'not-an-image-fixture-a');t.write_bytes(b'not-an-image-fixture-t')
    p={'asin':'A','goods_id':'T','pair_revision':'fixture','amazon_image_path':str(a),'temu_image_path':str(t)}
    previous=vision_revision(c,p);changed=deepcopy(c);changed['stage3']['identity_policy']['features'][1]['scope']='颜色只限外侧表面';changed=compile_policy(changed)
    assert previous not in compatible_vision_keys(changed,p)
