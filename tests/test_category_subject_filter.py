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
