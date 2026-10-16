import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_sales_sort_state_machine_and_semantic_dom():
    subprocess.run(['node', '--test', str(ROOT/'tests/temu_sales_sort.test.mjs')], check=True,
                   capture_output=True, text=True, encoding='utf-8', timeout=30)


def test_merge_preserves_sorted_source_when_latest_product_is_from_other_search(tmp_path):
    from selection_automation.merge_temu_snapshots import merge
    paths=[]
    for index,(query,sort) in enumerate([('drill',{'state':'UI_CONFIRMED','label':'Top sales'}),('cordless drill',None)]):
        path=tmp_path/f'{index}.jsonl';paths.append(path)
        path.write_text(json.dumps({'goods_id':'1','search_query':query,'observed_at':f'2026-09-2{index}T00:00:00Z',
                                   'search_sort':sort}),encoding='utf-8')
    rows,_=merge(paths)
    assert rows[0]['search_query']=='cordless drill'
    assert rows[0]['observation_sources'][0]['search_sort']['state']=='UI_CONFIRMED'
    assert rows[0]['observation_sources'][1]['search_sort'] is None
