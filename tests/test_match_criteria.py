from copy import deepcopy
from selection_automation.match_criteria import seed,merge_proposals,select,selected
import pytest


@pytest.fixture
def products():
    return {'records':[
        {'asin':'A1','title':'桌面投影仪','product_url':'https://www.amazon.com/dp/A1',
         'main_image_url':'https://m.media-amazon.com/images/I/A1.jpg',
         'specifications':[{'label':'Dimensions','value':'10 x 20 x 30 cm'},
                           {'label':'Mounting Type','value':'Tabletop'},{'label':'Price','value':'100'}]},
        {'asin':'A2','title':'吊装投影仪','product_url':'https://www.amazon.com/dp/A2',
         'main_image_url':'https://m.media-amazon.com/images/I/A2.jpg',
         'specifications':[{'label':'Dimensions','value':'12 x 20 x 30 cm'},
                           {'label':'Resolution','value':'1920 x 1080'}]}]}


@pytest.fixture
def category():
    return {'category_name_zh':'投影仪','stage3':{'identity_policy':{'features':[
        {'id':'body_shape','label':'外壳形状','evidence':'image','rule':'比较双方外壳的可见形状','role':'identity'}]}}}


def test_actual_specifications_form_union_without_discarding_per_product_values(category,products):
    catalog=seed(category,products)
    assert len(catalog['source_asins'])==2 and len(catalog['image_sources'])==2
    dimensions=next(row for row in catalog['items'] if row['label']=='Dimensions')
    assert {row['value'] for row in dimensions['observations']}=={'10 x 20 x 30 cm','12 x 20 x 30 cm'}
    assert not any(row['label']=='Price' for row in catalog['items'])
    assert {row['label'] for row in catalog['items']}=={'外壳形状','Dimensions','Mounting Type','Resolution'}


def test_unchecking_survives_refresh_and_does_not_mutate_old_snapshot(category,products):
    original=seed(category,products)
    changed=select(original,[],original['revision'])
    assert not selected({'match_criteria':changed})
    assert next(row for row in original['items'] if row['id']=='body_shape')['selected']
    refreshed=seed(category,products,changed)
    assert not any(row['selected'] for row in refreshed['items'])
    with pytest.raises(ValueError):select(changed,['invented'])
    with pytest.raises(ValueError):select(changed,[],'stale-version')


def test_image_features_require_an_image_actually_sent_and_text_requires_real_quote(category,products):
    catalog=seed(category,products)
    proposal={'items':[
        {'id':'resolution','label':'分辨率','kind':'text','rule':'分辨率一致','source_labels':['Resolution'],
         'evidence':[{'asin':'A2','quote':'1920 x 1080'}]},
        {'id':'button_layout','label':'按键排列','kind':'visual','rule':'比较同一实际位置的按键排列','source_labels':[],
         'evidence':[{'asin':'A1','image':True}]},
        {'id':'invented','label':'伪造参数','kind':'text','rule':'一致','source_labels':[],
         'evidence':[{'asin':'A1','quote':'不存在的内容'}]},
        {'id':'unseen','label':'未提供图片','kind':'visual','rule':'一致','source_labels':[],
         'evidence':[{'asin':'UNKNOWN','image':True}]}]}
    result=merge_proposals(catalog,products,proposal)
    assert not any(row['id'] in {'invented','unseen'} for row in result['items'])
    assert any(row['id']=='button_layout' and row['observations'][0]['asin']=='A1' for row in result['items'])
    assert sum(row['source_labels']==['Resolution'] for row in result['items'])==1


def test_first_layer_only_excludes_explicit_bilateral_size_conflict(category,products):
    from selection_automation.match_criteria import attach,text_gate
    category=deepcopy(category);category['stage1']={'attributes':[]};category['stage3']['rule_version']='test'
    products=deepcopy(products);products['records'][0]['specifications']=[{'label':'Screen Size','value':'7 inches'}]
    catalog=seed(category,products);size=next(row for row in catalog['items'] if row['label']=='Screen Size')
    attach(category,select(catalog,['body_shape',size['id']]))
    assert text_gate(category,'投影仪 7英寸','投影仪 8英寸')['excluded'] is True
    missing=text_gate(category,'投影仪 7英寸','投影仪 支持吊装')
    assert missing['excluded'] is False and missing['stage1_status']=='TEXT_INSUFFICIENT'
    equal=text_gate(category,'投影仪 7英寸','投影仪 17.78厘米')
    assert equal['excluded'] is False and equal['stage1_status']=='TEXT_PASS'
    attach(category,select(category['match_criteria'],['body_shape']))
    assert text_gate(category,'投影仪 7英寸','投影仪 8英寸')['excluded'] is False


@pytest.mark.parametrize('dimension',['13.78X12.6inch','41.8 X 30 Inches','7 X 15 Inches'])
def test_uppercase_dimension_separator_does_not_abort_text_batch(category,products,dimension):
    from selection_automation.match_criteria import attach,text_gate
    category=deepcopy(category);category['stage1']={'attributes':[]};category['stage3']['rule_version']='test'
    products=deepcopy(products);products['records'][0]['specifications']=[{'label':'Dimensions','value':dimension}]
    products['records'][1]['specifications']=[]
    catalog=seed(category,products);size=next(row for row in catalog['items'] if row['label']=='Dimensions')
    attach(category,select(catalog,[size['id']]))
    assert text_gate(category,'Washer '+dimension,'Washer '+dimension.lower())['stage1_status']=='TEXT_PASS'
    assert text_gate(category,'Washer '+dimension,'Washer 1 x 2 inches')['excluded']
    assert not text_gate(category,'Washer '+dimension,'Washer without stated dimensions')['excluded']


@pytest.mark.parametrize('name',['显示器','五金配件','浴帘'])
@pytest.mark.parametrize('left,right',[
    ('13.78X12.6inch','13.78×12.6 inches'),('７ Ｘ １５ ｉｎｃｈｅｓ','7*15 inches'),
    ('10✕20㎝','100x200 mm'),('1,000mm','100 cm'),('1/2 inch','12.7mm'),
    ('1½ inches','38.1mm'),('1⅝ inch','41.275mm'),('1000g','1 kg'),
    ('60HZ','60赫兹'),('500W','500瓦'),('尺寸10厘米','尺寸100mm'),
])
def test_generic_numeric_formats_work_across_categories(category,products,name,left,right):
    from selection_automation.match_criteria import attach,text_gate
    category=deepcopy(category);category.update(category_name_zh=name,stage1={'attributes':[]})
    category['stage3']['rule_version']='test'
    products=deepcopy(products);products['records'][0]['specifications']=[{'label':'Parameters','value':left}]
    products['records'][1]['specifications']=[]
    catalog=seed(category,products);item=next(row for row in catalog['items'] if row['kind']=='text')
    attach(category,select(catalog,[item['id']]))
    result=text_gate(category,left,right)
    assert result['stage1_status']=='TEXT_PASS' and not result['excluded']


@pytest.mark.parametrize('bad',['5/0 cm','0/0 cm','10-20cm','10cm–20cm','12,5cm','1e3mm',None,{},float('nan')])
def test_unusable_numeric_fact_is_missing_evidence_not_a_batch_exception(category,products,bad):
    from selection_automation.match_criteria import attach,text_gate
    category=deepcopy(category);category['stage1']={'attributes':[]};category['stage3']['rule_version']='test'
    products=deepcopy(products);products['records'][0]['specifications']=[{'label':'Dimensions','value':'10cm'}]
    products['records'][1]['specifications']=[]
    catalog=seed(category,products);item=next(row for row in catalog['items'] if row['kind']=='text')
    attach(category,select(catalog,[item['id']]))
    result=text_gate(category,bad,'20cm')
    assert not result['excluded'] and result['stage1_status']=='TEXT_INSUFFICIENT'


def configured(products):
    from test_identity_policy import category
    from selection_automation.identity_policy import compile_policy
    from selection_automation.match_criteria import attach
    result=compile_policy(category('投影仪'))
    return attach(result,seed(result,products))


def facts():
    return {'overall_confidence':.95,'condition_comparisons':{'body_type':{
        'relation':'SAME','amazon':'前置圆形镜头及右侧弧形支架','temu':'前置圆形镜头及右侧弧形支架'}},
        'identity':{'verdict':'SAME','matches':[], 'differences':[], 'uncertainties':[], 'reason':'主体款式已核对',
                    'design_comparison':{'relation':'SAME','amazon':'圆形镜头配右侧弧形支架','temu':'圆形镜头配右侧弧形支架'}}}


@pytest.mark.parametrize('relation,state',[('SAME','确认同款'),('DIFFERENT','不同款'),('UNKNOWN','待补充证据')])
def test_third_layer_checks_only_selected_image_conditions(products,relation,state):
    from selection_automation.rules import score
    cat=configured(products);value=facts();value['condition_comparisons']['body_type']['relation']=relation
    assert score(cat,value)['state']==state
    assert any(row['kind']=='text' and row['selected'] for row in cat['match_criteria']['items'])
    # Selected first-layer text has no corresponding L3 row and does not block confirmation.
    assert set(value['condition_comparisons'])=={'body_type'}


def test_missing_selected_visual_fact_and_conflicting_design_are_not_confirmed(products):
    from selection_automation.rules import score
    cat=configured(products);value=facts();value['condition_comparisons']={}
    assert score(cat,value)['state']=='待补充证据'
    value=facts();value['identity']['design_comparison']['relation']='UNKNOWN'
    assert score(cat,value)['state']=='待补充证据'


def test_unselected_color_difference_is_not_a_hidden_visual_veto(products):
    from selection_automation.rules import score
    cat=configured(products);value=facts()
    value['identity']['verdict']='DIFFERENT'
    value['identity']['design_comparison']['relation']='DIFFERENT'
    value['identity']['differences']=[{'field':'finish','amazon':'黑色外壳','temu':'白色外壳','kind':'IDENTITY','variant_field':'NONE'}]
    assert score(cat,value)['state']=='确认同款'


def test_protocol_schema_and_decoder_bind_image_conditions_but_not_text(products):
    from selection_automation.vision import protocol,decode
    from selection_automation.local_visual_wire import prepare,unpack
    from selection_automation.match_criteria import attach
    cat=configured(products);prompt,schema=protocol(cat,1)
    assert set(schema['properties']['candidates']['items']['properties']['q']['properties'])=={'body_type'}
    assert 'selected-visual-compact-v1' in prompt and '未拍到不等于没有' in prompt
    item={'i':'C00','c':.95,'r':'S','a':'圆形镜头配右侧弧形支架','t':'圆形镜头配右侧弧形支架','why':'可见基础款一致',
        'q':{'body_type':{'r':'S','a':'圆形镜头支架','t':'圆形镜头支架'}}}
    assert decode(cat,{'candidates':[item]},['C00'])['C00']['condition_comparisons']['body_type']['relation']=='SAME'
    _,wire=prepare(prompt,schema,['C00'])
    assert 'q' in wire['properties']['candidates']['properties']['C00']['required']
    assert unpack({'candidates':{'C00':{k:v for k,v in item.items() if k!='i'}}},['C00'])['candidates'][0]['q']==item['q']
    old=deepcopy(item);old.pop('q')
    with pytest.raises(ValueError):decode(cat,{'candidates':[old]},['C00'])


def test_selection_change_invalidates_all_visual_cache_keys(products,tmp_path):
    from selection_automation.vision import compatible_vision_keys
    from selection_automation.match_criteria import attach
    cat=configured(products);image=tmp_path/'image';image.write_bytes(b'fixed')
    pair={'asin':'A1','goods_id':'T1','pair_revision':'same','amazon_image_path':str(image),'temu_image_path':str(image)}
    before=compatible_vision_keys(cat,pair)
    attach(cat,select(cat['match_criteria'],[]))
    assert not set(before)&set(compatible_vision_keys(cat,pair))


def test_native_decision_questions_and_score_are_split_by_layer(products):
    import json
    from types import SimpleNamespace
    from selection_automation.stage3_provider import VisualClient,settings,decision_score
    cat=configured(products);provider=settings({'mode':'liquid_d1','model':'d1'})
    cat['stage3']['provider']=provider
    client=VisualClient.__new__(VisualClient);client.provider=provider;client.criteria=cat['match_criteria']
    content=[{'type':'input_image','image_url':'data:image/png;base64,A'},{'type':'input_image','image_url':'data:image/png;base64,B'}]
    data=client.prepare(content,'比较')
    questions=json.loads(data)['questions']
    assert set(questions)=={'same_product','same_product__condition__body_type'}
    def answer(choices):return {'choice':max(choices,key=choices.get),'probabilities':choices}
    answers={'same_product':answer({'same':.96,'similar':.02,'different':.02}),
             'same_product__condition__body_type':answer({'same':.95,'different':.02,'unknown':.03})}
    client.transport=SimpleNamespace(post=lambda *a,**k:SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'answers':answers}))
    client.key='test-only';client.timeout_seconds=30
    response=client.create(_prepared_data=data,input=[{'content':content}],instructions='比较')
    value=json.loads(response.output_text)['decision_facts']
    assert decision_score(cat,value)['state']=='确认同款'
    value['decision_api']['conditions']['body_type']=answer({'same':.02,'different':.03,'unknown':.95})
    assert decision_score(cat,value)['state']=='待补充证据'


def test_extractor_sends_only_actual_images_once_and_reuses_complete_result(category,products,tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    from extract_match_criteria import extract
    calls=[]
    def ask(prompt,**kwargs):calls.append(kwargs);return {'items':[],'reviewed_asins':['A1','A2']}
    receipt=tmp_path/'receipt.json'
    result=extract(deepcopy(category),products,ask,receipt)
    assert len(calls)==1 and len(calls[0]['images'])==2 and calls[0]['retry_truncated'] is False
    extract(result,products,ask,receipt)
    assert len(calls)==1
    receipt.write_text('{"state":"started"}')
    assert extract(deepcopy(category),products,ask,receipt)['match_criteria']['state']=='interrupted'
    assert len(calls)==1


def test_extractor_failure_preserves_structured_specs_and_rejects_invalid_image_url(category,products):
    from extract_match_criteria import extract
    from selection_automation.match_criteria import image_sources
    changed=deepcopy(products);changed['records'][0]['main_image_url']='https://m.media-amazon.com:bad/a'
    assert len(image_sources(changed))==1
    def fail(*a,**k):raise RuntimeError('offline')
    result=extract(deepcopy(category),products,fail)
    assert result['match_criteria']['state']=='structured'
    assert any(row['label']=='Dimensions' for row in result['match_criteria']['items'])


def test_visual_conditions_do_not_become_first_layer_text_vetoes(products):
    from selection_automation.rules import text_gate
    from selection_automation.match_criteria import attach
    cat=configured(products)
    attach(cat,select(cat['match_criteria'],['body_type']))
    assert not text_gate(cat,'fixed body','jointed body')['excluded']
    attach(cat,select(cat['match_criteria'],['body_type','text_body_type']))
    assert text_gate(cat,'fixed body','jointed body')['excluded']


def test_web_saves_split_selections_and_freezes_them_after_task_start(products,tmp_path,monkeypatch):
    import web_console as web
    cat=configured(products);console=web.Console(tmp_path)
    console.draft_path=tmp_path/'category.json';console.draft_plan_path=tmp_path/'plan.json'
    web.selection.save(console.draft_path,cat);web.selection.save(console.draft_plan_path,{'keywords':[]})
    console.draft=web.draft_view(cat,'draft',{})
    monkeypatch.setattr(console,'owner',lambda:None)
    body={'draft_id':'draft','revision':cat['match_criteria']['revision'],'selected_ids':['body_type']}
    result=console.select_match_criteria(body)
    saved=web.selection.read(console.draft_path,{})
    assert result['ok'] and selected(saved,'text')==[] and len(selected(saved,'visual'))==1
    assert console.draft['match_criteria']['revision']==saved['stage3']['match_criteria_revision']
    with pytest.raises(web.ConsoleError):console.select_match_criteria(body)
    console.draft_used=True;body['revision']=saved['match_criteria']['revision']
    with pytest.raises(web.ConsoleError):console.select_match_criteria(body)
