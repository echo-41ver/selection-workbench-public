"""Unrelated fields in one paragraph and reused model IDs stay independent."""
from copy import deepcopy

import pytest

from selection_automation.match_criteria import attach,merge_proposals,seed,select,text_gate,FIELD_MERGE_VERSION


def fixture():
    category={'category_name_zh':'洗衣机','stage1':{'attributes':[]},
        'stage3':{'rule_version':'test','modules':{},'identity_policy':{'features':[
            {'id':'capacity_class','label':'容量档位','evidence':'text','role':'identity','rule':'比较洗衣容量'}]}}}
    snapshot={'records':[{'asin':'A1','title':'Washer capacity: 18lbs, Spin capacity: 10lbs; Foldable; Drain pump',
        'specifications':[]},{'asin':'A2','title':'Washer 20L; Four water levels','specifications':[]}]}
    def proposal(fid,label,quote,asin='A1'):
        return {'items':[{'id':fid,'label':label,'kind':'text','rule':'比较双方明确参数',
            'source_labels':['卖点3'],'evidence':[{'asin':asin,'quote':quote}]}]}
    return category,snapshot,proposal


def test_generic_source_paragraph_never_merges_unrelated_parameters():
    category,snapshot,proposal=fixture()
    catalog=seed(category,snapshot)
    for fid,label,quote in [('capacity_class','容量档位','Washer capacity: 18lbs'),
                            ('drain_pump','排水泵','Drain pump'),('water_levels','水位档位','Four water levels')]:
        catalog=merge_proposals(catalog,snapshot,proposal(fid,label,quote,'A2' if fid=='water_levels' else 'A1'))
    rows={row['id']:row for row in catalog['items']}
    assert set(rows)=={'capacity_class','drain_pump','water_levels'}
    assert rows['capacity_class']['label_zh']=='容量档位'
    assert [p['value'] for p in rows['capacity_class']['observations']]==['Washer capacity: 18lbs']
    assert rows['capacity_class']['selected']


def test_reused_cross_batch_id_cannot_overwrite_the_selected_parameter():
    category,snapshot,proposal=fixture()
    catalog=merge_proposals(seed(category,snapshot),snapshot,proposal('capacity_class','容量档位','Washer capacity: 18lbs'))
    catalog=merge_proposals(catalog,snapshot,proposal('capacity_class','是否可折叠','Foldable'))
    capacity=next(row for row in catalog['items'] if row['id']=='capacity_class')
    folded=next(row for row in catalog['items'] if row['label']=='是否可折叠')
    assert capacity['label']=='容量档位' and capacity['selected']
    assert folded['id']!='capacity_class' and not folded['selected']
    # Another batch can name the equivalent capacity field differently.
    catalog=merge_proposals(catalog,snapshot,proposal('wash_capacity_lbs','洗衣机容量(磅)','Washer capacity: 18lbs'))
    assert len(catalog['items'])==2


def test_saved_proposals_repair_legacy_catalog_without_api_and_keep_choices():
    from extract_match_criteria import repair_cached_text_fields
    category,snapshot,proposal=fixture()
    attach(category,seed(category,snapshot))
    original=deepcopy(category)
    category['match_criteria']['items'][0].update(label='水温选项数量',label_zh='容量档位',observations=[{'value':'wrong'}])
    ledger={'source_fingerprint':category['match_criteria']['source_fingerprint'],'batches':{
        'first':{'state':'complete','proposal':proposal('capacity_class','容量档位','Washer capacity: 18lbs')},
        'second':{'state':'complete','proposal':proposal('capacity_class','是否可折叠','Foldable')}}}
    fixed=repair_cached_text_fields(category,snapshot,ledger)
    capacity=next(row for row in fixed['match_criteria']['items'] if row['id']=='capacity_class')
    assert capacity['label']==capacity['label_zh']=='容量档位' and capacity['selected']
    assert fixed['match_criteria']['field_merge_version']==FIELD_MERGE_VERSION
    assert {row['id'] for row in fixed['match_criteria']['items'] if row['selected']}=={'capacity_class'}
    assert category['match_criteria']['items'][0]['label']=='水温选项数量'
    assert original['match_criteria']['items'][0]['label']=='容量档位'


@pytest.mark.parametrize('left,right,status',[
    ('Washer 18lbs','Washer 20lbs','TEXT_HARD_EXCLUDE'),
    ('Washer 1lb','Washer 453.59237g','TEXT_PASS'),
    ('Washer 20L','Washer 20000ml','TEXT_PASS'),
    ('Washer 20L','Washer 10L','TEXT_HARD_EXCLUDE'),
    ('Washer 1cu. ft.','Washer 28.316846592 L','TEXT_PASS'),
    ('Washer 18lbs','Washer 20L','TEXT_INSUFFICIENT'),
    ('Washer 18lbs','Washer 20x30x40cm','TEXT_INSUFFICIENT'),
    ('Washer capacity: 18lbs, Spin capacity: 10lbs','Washer 20lbs','TEXT_HARD_EXCLUDE'),
])
def test_capacity_compares_only_compatible_capacity_units(left,right,status):
    category,snapshot,proposal=fixture()
    catalog=seed(category,snapshot)
    catalog=merge_proposals(catalog,snapshot,proposal('capacity_class','容量档位','Washer capacity: 18lbs'))
    catalog=merge_proposals(catalog,snapshot,proposal('capacity_volume','洗衣容量','Washer 20L','A2'))
    attach(category,select(catalog,['capacity_class']))
    assert text_gate(category,left,right)['stage1_status']==status


def test_processing_revision_tracks_selected_text_but_not_visual_choices():
    from selection_workflow.incremental_stage12 import declarative_text_revision
    category,snapshot,_=fixture();attach(category,seed(category,snapshot))
    before=declarative_text_revision(category)
    changed=deepcopy(category);changed['match_criteria']['items'].append({'id':'color','kind':'visual','selected':True})
    assert declarative_text_revision(changed)==before
    changed['match_criteria']['items'][0]['selected']=False
    assert declarative_text_revision(changed)!=before
    changed=deepcopy(category);changed['match_criteria']['items'][0]['observations']=[{'value':'20L'}]
    assert declarative_text_revision(changed)!=before


@pytest.mark.parametrize('name,label,other,value,different,equal',[
    ('显示器','屏幕尺寸','显示类型','27 inches','24 inches','68.58cm'),
    ('电钻','电压','最大扭矩','18V','20V','18伏'),
    ('浴帘','商品长宽高','印花图案','180X180cm','180x200cm','1800×1800mm'),
    ('电池','容量','连接接口','5000mAh','6000mAh','5Ah'),
])
def test_different_categories_keep_field_scope_and_continue_after_unusable_data(name,label,other,value,different,equal):
    category={'category_name_zh':name,'stage1':{'attributes':[]},'stage3':{'rule_version':'test',
        'identity_policy':{'features':[{'id':'required_field','label':label,'evidence':'text','role':'identity','rule':'比较该参数'}]}}}
    snapshot={'records':[{'asin':'A1','title':value+'; Extra fact','specifications':[]}]}
    def proposal(caption,quote):return {'items':[{'id':'required_field','label':caption,'kind':'text',
        'rule':'比较双方明确参数','source_labels':['卖点3'],'evidence':[{'asin':'A1','quote':quote}]}]}
    catalog=merge_proposals(seed(category,snapshot),snapshot,proposal(label,value))
    catalog=merge_proposals(catalog,snapshot,proposal(other,'Extra fact'))
    assert len(catalog['items'])==2
    attach(category,select(catalog,['required_field']))
    assert text_gate(category,value,different)['excluded']
    assert text_gate(category,value,equal)['stage1_status']=='TEXT_PASS'
    assert text_gate(category,None,different)['stage1_status']=='TEXT_INSUFFICIENT'
    # A malformed product does not stop the next valid pair in the batch.
    assert text_gate(category,value,different)['excluded']
