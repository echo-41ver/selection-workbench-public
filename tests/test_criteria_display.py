from copy import deepcopy
from selection_automation.match_criteria import seed,select,selected
from selection_automation.criteria_display import organize


def test_sizes_are_one_chinese_parameter_with_each_products_own_value():
    snapshot={'records':[{'asin':asin,'specifications':[{'label':label,'value':value}]} for asin,label,value in
        [('A','Screen Size','8 inches'),('B','Display Size','7 inches'),('C','Standing screen display size','9 inches')]]}
    catalog=seed({'stage3':{}},snapshot)
    assert len(catalog['items'])==1
    item=catalog['items'][0]
    assert item['label_zh']=='屏幕尺寸'
    assert {(r['asin'],r['value']) for r in item['observations']}=={('A','8 inches'),('B','7 inches'),('C','9 inches')}
    assert set(item['source_labels'])=={'Screen Size','Display Size','Standing screen display size'}
    chosen=select(catalog,[item['id']])
    assert len(selected({'match_criteria':chosen},'text'))==1


def test_different_meanings_are_not_merged_and_choices_survive():
    labels=['Screen Size','Item Dimensions D x W x H','Native Resolution','Display Resolution Maximum']
    catalog=seed({'stage3':{}},{'records':[{'asin':'A','specifications':[{'label':v,'value':'actual value'} for v in labels]}]})
    assert len(catalog['items'])==4
    assert {r['label_zh'] for r in catalog['items']}=={'屏幕尺寸','商品长宽高','原生分辨率','最高支持分辨率'}
    catalog=select(catalog,[r['id'] for r in catalog['items']])
    assert organize(catalog)==organize(organize(catalog))
    assert all(r['selected'] for r in organize(catalog)['items'])


def test_eight_core_fields_and_all_selected_fields_stay_visible_without_discarding_others():
    items=[{'id':str(i),'label':'Screen Size' if i==0 else '参数'+str(i),'kind':'text','selected':i==10,
            'source_labels':[],'observations':[]} for i in range(12)]
    result=organize({'items':deepcopy(items)})
    assert result['core_text_count']==8 and result['more_text_count']==4
    assert next(r for r in result['items'] if r['id']=='10')['display_group']=='core'
    assert len(result['items'])==12 and all(r.get('label_zh') for r in result['items'])


def test_unknown_english_parameters_do_not_fill_core_with_generic_labels():
    items=[{'id':str(i),'label':'unknown field '+str(i),'kind':'text','selected':i==3,
            'source_labels':[],'observations':[]} for i in range(4)]
    result=organize({'items':items})
    assert result['core_text_count']==1 and result['more_text_count']==3
    assert next(row for row in result['items'] if row['id']=='3')['selected']
    assert organize(result)==result


def test_capacity_synonyms_merge_preserving_source_values_and_selection():
    items=[{'id':identity,'label':label,'kind':'text','selected':chosen,
            'source_labels':[label],'observations':[{'asin':identity,'value':value}]}
        for identity,label,value,chosen in [('raw','Weight Capacity Maximum','11 pounds',False),
            ('cn','最大称重','5 kg',True),('accuracy','Readout Accuracy','1 g',False)]]
    result=organize({'items':items})
    assert len(result['items'])==2
    capacity=next(row for row in result['items'] if row['label_zh']=='最大称重')
    assert capacity['selected'] and capacity['merged_ids']==['cn']
    assert {row['value'] for row in capacity['observations']}=={'11 pounds','5 kg'}
    assert organize(result)==result


def test_irrelevant_metadata_is_archived_but_explicit_selection_survives():
    labels=['Department ‏ : ‎ All Ages','Date First Available ‏ : ‎ March 18, 2016',
        'Manufacturer','Brand Name','ASIN','Global Trade Identification Number',
        'Some unknown field: value','Manufacturer ‏ : ‎ selected company']
    items=[{'id':str(index),'label':label,'kind':'text','selected':index==7,
            'source_labels':[label],'observations':[{'asin':'A','value':'source evidence'}]}
           for index,label in enumerate(labels)]
    result=organize({'items':items})
    assert len(result['items'])==1 and result['items'][0]['selected']
    assert result['raw_parameter_count']==7
    assert all(row['observations'] for row in result['raw_parameters'])
    assert organize(result)==result


def test_more_than_eight_user_selections_are_retained_without_expanding_core():
    items=[{'id':str(i),'label':'自定义参数'+str(i),'kind':'text','selected':True,
            'source_labels':[],'observations':[]} for i in range(12)]
    result=organize({'items':items})
    assert result['core_text_count']==8 and result['more_text_count']==4
    assert all(row['selected'] for row in result['items'])


def test_actual_parameter_values_win_over_a_reused_authoring_name():
    from selection_automation.match_criteria import text_gate,attach
    category={'stage1':{'attributes':[{'name':'display_type','hard_conflict':True,
        'values':{'monitor':['monitor'],'television':['television']}}]},'stage3':{'rule_version':'test'}}
    snapshot={'records':[{'asin':asin,'specifications':[{'label':'Display Type','value':value}]}
                         for asin,value in [('A','LCD'),('B','OLED')]]}
    catalog=seed(category,snapshot);assert len(catalog['items'])==1
    attach(category,catalog)
    assert text_gate(category,'LCD monitor','OLED monitor')['excluded']
    assert not text_gate(category,'LCD monitor','monitor')['excluded']
