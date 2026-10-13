"""Use the collector's exact subject rules before publishing category goods."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from .merge_temu_snapshots import atomic_write


def filter_category_union(root, category, rows, summary, union):
    if category.get('temu', {}).get('subject_filter', {}).get('mode') != 'TARGET_BODY_ONLY':
        return rows, summary
    node=shutil.which('node')
    if not node:raise RuntimeError('商品主体筛选需要Node，旧汇总已保留')
    result=subprocess.run([node,str(Path(root)/'scripts/filter_category_products.mjs')],
        input=json.dumps({'category':category,'rows':rows},ensure_ascii=False),
        capture_output=True,text=True,encoding='utf-8',timeout=30,cwd=root)
    if result.returncode:raise RuntimeError('商品主体筛选失败，旧汇总已保留：'+result.stderr[:200])
    data=json.loads(result.stdout)
    accepted=data['accepted'];excluded=data['excluded'];unknown=data['unclassified']
    if len(accepted)+len(excluded)+len(unknown)!=len(rows):raise ValueError('商品主体筛选记录数不一致')
    for name,values in [('excluded_products.jsonl',excluded),('unclassified_products.jsonl',unknown)]:
        atomic_write(Path(union)/name,''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in values))
    summary={**summary,'source_unique_goods_id':len(rows),'unique_goods_id':len(accepted),
        'frontend_unique_goods':sum(str(row.get('source','')).startswith('VISIBLE_TEMU') for row in accepted),
        'provider_supplement_goods':sum(row.get('source')=='GEEKBI_SUPPLEMENT' for row in accepted),
        'category_subject_filter':'TARGET_BODY_ONLY','category_excluded_goods':len(excluded),
        'category_unclassified_goods':len(unknown),'relevance_policy_version':data['version'],
        'relevance_policy_fingerprint':data['fingerprint'],
        'content_sha256':hashlib.sha256(''.join(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n' for row in accepted).encode()).hexdigest()}
    return accepted,summary
