import json
from extract_match_criteria import extract
from selection_automation.match_criteria import image_sources,select,comparison_prompt
from selection_automation.visual_criteria import VERSION


def inputs(count=100):
    return ({'category_key':'curtains','category_name_zh':'浴帘','stage1':{'attributes':[]},
             'stage3':{'identity_policy':{'features':[]}}},
            {'records':[{'asin':f'A{i:03}','title':'Shower curtain',
                         'main_image_url':f'https://m.media-amazon.com/images/I/{i}.jpg'} for i in range(count)]})


def response(question,**kwargs):
    # Match the actual attached source list, never infer from global product count.
    asins=json.loads(question.split('附件图片依次属于：',1)[1].split('\n',1)[0])
    return {'reviewed_asins':asins,'items':[
        {'id':f'pattern_{asin}','kind':'visual','dimension':dimension,'label':'任意旧名称','rule':'必须都有印花',
         'evidence':[{'asin':asin,'image':True,'quote':f'蓝色底，花朵图案位于下部 {asin}'}]}
        for asin in asins for dimension in ('color','pattern_content','pattern_position')]}


def test_all_100_images_are_reviewed_and_product_values_share_four_boxes(tmp_path):
    category,snapshot=inputs();calls=[]
    def ask(question,**kwargs):calls.append(kwargs['images']);return response(question,**kwargs)
    result=extract(category,snapshot,ask,tmp_path/'receipt.json')['match_criteria']
    assert [url for batch in calls for url in batch]==[row['url'] for row in image_sources(snapshot)]
    assert len(calls)==13 and max(map(len,calls))==8
    assert result['image_review']['completed_images']==100 and result['image_review']['reviewed_products']==100
    assert result['state']=='ready'
    visual=[row for row in result['items'] if row['kind']=='visual']
    assert not any(row['selected'] for row in visual) and result['visual_mode']=='auto'
    assert {row['label'] for row in visual}=={'外形轮廓','颜色','图案内容','图案位置'}
    assert len(next(row for row in visual if row['label']=='颜色')['observations'])==100
    assert all('必须都有印花' not in row['rule'] for row in visual)
    # A completed result and its batch receipts both prevent repeated paid calls.
    extract(category,snapshot,lambda *a,**k:(_ for _ in ()).throw(AssertionError('replayed')),tmp_path/'receipt.json')
    category.pop('match_criteria')
    assert extract(category,snapshot,lambda *a,**k:(_ for _ in ()).throw(AssertionError('replayed')),tmp_path/'receipt.json')['match_criteria']['state']=='ready'


def test_partial_unknown_request_is_never_misreported_complete_or_replayed(tmp_path):
    category,snapshot=inputs(17);calls=[]
    def ask(question,**kwargs):
        calls.append(kwargs['images'])
        if len(calls)==2:raise TimeoutError('result unknown')
        return response(question,**kwargs)
    path=tmp_path/'receipt.json'
    result=extract(category,snapshot,ask,path)['match_criteria']
    assert result['state']=='partial' and result['image_review']['completed_images']==9
    assert result['image_review']['failed_batches']==1 and len(calls)==3
    extract(category,snapshot,lambda *a,**k:(_ for _ in ()).throw(AssertionError('replayed')),path)
    assert category['match_criteria']['image_review']['completed_images']==9


def test_completed_but_incomplete_coverage_answer_is_retried_once():
    category,snapshot=inputs(2);calls=[]
    def ask(question,**kwargs):
        calls.append(1)
        return {'items':[],'reviewed_asins':['A000']} if len(calls)==1 else response(question,**kwargs)
    assert extract(category,snapshot,ask)['match_criteria']['state']=='ready'
    assert len(calls)==2


def test_missing_product_image_does_not_count_as_reviewed():
    category,snapshot=inputs(3);snapshot['records'][1]['main_image_url']='https://not-amazon.example/p.jpg'
    value=extract(category,snapshot,response)['match_criteria']
    assert value['state']=='partial' and value['image_review']['missing_asins']==['A001']
    assert value['image_review']['reviewed_products']==2


def test_choices_survive_refresh_and_only_checked_dimensions_reach_l3(tmp_path):
    category,snapshot=inputs(2)
    value=extract(category,snapshot,response)['match_criteria']
    category['match_criteria']=select(value,['visual_color'])
    value=extract(category,snapshot,response,tmp_path/'new-ledger.json')['match_criteria']
    assert [row['id'] for row in value['items'] if row['selected']]==['visual_color']
    prompt=comparison_prompt(category)
    assert json.loads(prompt.split('额外强制一致条件：')[1].split('\n')[0])[0]['id']=='visual_color'


def test_eight_dimensions_bound_holds_even_with_many_part_names():
    from selection_automation.visual_criteria import AXES
    category,snapshot=inputs(4)
    def ask(question,**kwargs):
        value=response(question,**kwargs)
        value['items']=[{'id':f'part_{n}','kind':'visual','dimension':axis,
                         'evidence':[{'asin':'A000','image':True,'quote':f'部位{n}可见特征'}]}
                        for n in range(20) for axis in AXES]
        return value
    value=extract(category,snapshot,ask)['match_criteria']
    assert len([row for row in value['items'] if row['kind']=='visual'])==8


def test_model_transport_sends_all_eight_attachments_without_old_three_image_slice(monkeypatch):
    import generate_category as generator
    captured=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"items":[]}'}}]}).encode()
    def transport(request,**kwargs):captured.append(json.loads(request.data));return Response()
    monkeypatch.setattr(generator,'open_model_url',transport)
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_KEY')
    monkeypatch.setenv('SELECTION_RULES_BASE_URL','http://localhost:1234/v1')
    monkeypatch.setenv('SELECTION_RULES_MODEL','test')
    images=[f'https://m.media-amazon.com/{i}.jpg' for i in range(8)]
    generator.model_json('test',images=images)
    assert [part['image_url']['url'] for part in captured[0]['messages'][-1]['content'][1:]]==images


def test_default_auto_mode_returns_three_classes_without_any_checkbox():
    from test_match_criteria import configured
    from selection_automation.compact_visual import protocol,decode
    from selection_automation.rules import score
    _,snapshot=inputs(2);category=configured(snapshot)
    extract(category,snapshot,response)
    question,schema=protocol(category,1)
    assert schema['properties']['candidates']['items']['properties']['q']['properties']=={}
    assert len(question)<1300
    for relation,expected in [('S','确认同款'),('L','相似款'),('D','不同款')]:
        raw={'candidates':[{'i':'C00','r':relation,'a':'蓝底下部花朵浴帘','t':'蓝底下部花朵浴帘',
                            'why':'按整体构图与造型比较','q':{},'c':.95}]}
        facts=decode(category,raw,['C00'])['C00']
        assert score(category,facts)['state']==expected
    category['match_criteria']=select(category['match_criteria'],['visual_color'])
    raw['candidates'][0].update(r='L',q={'visual_color':{'r':'D','a':'蓝色','t':'红色'}})
    facts=decode(category,raw,['C00'])['C00']
    assert score(category,facts)['state']=='不同款'


def test_provider_image_limit_splits_rejected_batches_and_caches_accepted_children(tmp_path):
    from generate_category import CategoryGenerationError
    category,snapshot=inputs(10);calls=[]
    def ask(question,**kwargs):
        calls.append(kwargs['images'])
        if len(kwargs['images'])>3:raise CategoryGenerationError('image limit','MODEL_HTTP_400')
        return response(question,**kwargs)
    receipt=tmp_path/'receipt.json'
    result=extract(category,snapshot,ask,receipt)['match_criteria']
    assert result['state']=='ready' and result['image_review']['completed_images']==10
    accepted=[url for batch in calls if len(batch)<=3 for url in batch]
    assert len(accepted)==len(set(accepted))==10
    category.pop('match_criteria')
    extract(category,snapshot,lambda *a,**k:(_ for _ in ()).throw(AssertionError('replayed')),receipt)
    assert category['match_criteria']['state']=='ready'
