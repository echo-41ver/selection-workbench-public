import json
from pathlib import Path
from selection_automation.category_subject_filter import filter_category_union
import watch_temu_union as watcher

ROOT=Path(__file__).resolve().parents[1]
CATEGORY={'category_name_zh':'鼠标','temu':{'include_terms':['mouse'],
 'subject_filter':{'mode':'TARGET_BODY_ONLY','reject_mixed_subjects':True,'exclude_subject_terms':['键盘','keyboard']}}}


def test_union_publishes_only_target_body_and_separates_unknown_without_deleting_sources(tmp_path):
    rows=[{'goods_id':'1','title':'无线鼠标','source':'VISIBLE_TEMU_FRONTEND'},
          {'goods_id':'2','title':'keyboard and mouse'}, {'goods_id':'3','title':'X3MAX'}]
    accepted,summary=filter_category_union(ROOT,CATEGORY,rows,{'source_rows':3},tmp_path)
    assert [r['goods_id'] for r in accepted]==['1']
    assert summary['category_excluded_goods']==1 and summary['category_unclassified_goods']==1
    assert json.loads((tmp_path/'excluded_products.jsonl').read_text())['goods_id']=='2'
    assert json.loads((tmp_path/'unclassified_products.jsonl').read_text())['goods_id']=='3'
    assert len(rows)==3


def test_watcher_reclassifies_existing_snapshot_when_subject_rules_change(tmp_path):
    base=tmp_path/'temu';source=base/'workers/w/q/products.jsonl';source.parent.mkdir(parents=True)
    source.write_text(json.dumps({'goods_id':'2','title':'keyboard and mouse'})+'\n')
    prior=watcher.publish_once(base);assert prior['unique_goods']==1
    (tmp_path/'category.json').write_text(json.dumps(CATEGORY))
    current=watcher.publish_once(base,prior)
    assert current['status']=='PUBLISHED' and current['unique_goods']==0
    assert (base/'union/products.jsonl').read_text()==''
    assert json.loads(source.read_text())['goods_id']=='2'


def test_complete_unclassified_frontend_goods_reach_union_without_being_called_confirmed(tmp_path):
    base=tmp_path/'temu';folder=base/'workers/w/q';folder.mkdir(parents=True)
    complete={'goods_id':'usb-1','title':'USB-C flash drive 128GB','price_usd':6.5,
              'product_url':'https://www.temu.com/g-usb-1.html',
              'main_image_url':'https://example.org/usb.jpg','source':'VISIBLE_TEMU_FRONTEND'}
    (folder/'products.jsonl').write_text('')
    source=folder/'unclassified_products.jsonl'
    source.write_text(json.dumps(complete)+'\n'+json.dumps({'goods_id':'missing','title':'X3MAX'})+'\n')
    (tmp_path/'category.json').write_text(json.dumps(CATEGORY))
    current=watcher.publish_once(base)
    assert current['status']=='PUBLISHED' and current['unique_goods']==1
    rows=[json.loads(x) for x in (base/'union/products.jsonl').read_text().splitlines()]
    assert rows[0]['goods_id']=='usb-1'
    assert rows[0]['category_relevance']['state']=='UNKNOWN'
    summary=json.loads((base/'union/summary.json').read_text())
    assert summary['category_confirmed_goods']==0
    assert summary['category_recalled_unclassified_goods']==1
    assert summary['category_incomplete_goods']==1
    assert len(source.read_text().splitlines())==2
    # A later observation of the same ID in the accepted file stays one product.
    (folder/'products.jsonl').write_text(json.dumps(complete)+'\n')
    again=watcher.publish_once(base,current)
    assert again['unique_goods']==1
