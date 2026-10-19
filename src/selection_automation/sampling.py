"""Conservative duplicate sampling: never shorten category or core-function queries."""
import json
from pathlib import Path
from .merge_temu_snapshots import atomic_write


def prepare(run, output, item, plan, mode, enabled=True):
    # Sampling budgets are frontend positions; overlap cannot reduce user caps.
    enabled=False
    path=Path(output)/'sampling_policy.json'
    if path.exists(): return path
    sources=set(item.get('source_asins',[]))
    core=any(f.get('kind')=='structure' for e in item.get('source_evidence',[]) for f in e.get('features',[]))
    sole=any(sum(asin in row.get('source_asins',[]) for row in plan)==1 for asin in sources)
    protected=item.get('purpose')=='CATEGORY_SAMPLE' or core or sole or not sources
    reference=set(); reference_queries=[]
    for progress_path in Path(run).glob('temu/workers/*/*/progress.json'):
        if progress_path.parent==Path(output): continue
        try:
            progress=json.loads(progress_path.read_text('utf-8'))
            sort=progress.get('sales_sort',{})
            if progress.get('status') not in ('KEYWORD_COMPLETE','KEYWORD_LIMIT_REACHED') or sort.get('requested')!=mode or sort.get('state')!='UI_CONFIRMED': continue
            ids={str(json.loads(line)['goods_id']) for line in (progress_path.parent/'products.jsonl').read_text('utf-8').splitlines() if line}
            reference.update(ids);reference_queries.append(progress.get('search_query'))
        except (OSError,ValueError,KeyError): continue
    atomic_write(path,json.dumps({'enabled':bool(enabled and not protected and reference),'protected':protected,
        'reference_goods_ids':sorted(reference),'reference_queries':reference_queries,
        'minimum_sample':max(200,(int(item['max_products'])+1)//2),'maximum_duplicate_fraction':.9,
        'policy':'same-sort-completed-reference-v1'},ensure_ascii=False))
    return path
