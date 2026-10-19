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
    items=[{'id':str(i),'label':'Screen Size' if i==0 else 'field'+str(i),'kind':'text','selected':i==10,
            'source_labels':[],'observations':[]} for i in range(12)]
    result=organize({'items':deepcopy(items)})
    assert result['core_text_count']==8 and result['more_text_count']==4
    assert next(r for r in result['items'] if r['id']=='10')['display_group']=='core'
    assert len(result['items'])==12 and all(r.get('label_zh') for r in result['items'])


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
