"""Read-only four-worker acceptance evidence; never infer completion from row totals.

Evidence layout: before_manifest.json, after_manifest.json; each worker name contains
before/ and after/ with events.jsonl, products.jsonl, progress.json, plus replacement.json.
Optional proxy_session_fingerprint in worker manifests is a one-way identity digest,
never a credential. This script never launches collectors or browsers.
"""
import argparse
import json
from pathlib import Path


def read(path, default):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError): return default


def rows(path):
    try:
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    except (OSError,ValueError): return []


def phase_evidence(directory, required_rounds):
    events=rows(directory/'events.jsonl')
    kinds=[event.get('type') for event in events]
    gates=[i for i,event in enumerate(events) if event.get('type')=='IMAGE_BLOCK_GATE_PASSED' and event.get('navigation_performed') is False]
    navigations=[i for i,name in enumerate(kinds) if name in ('BLANK_BOOTSTRAP_NAVIGATION','EMPTY_SEARCH_BOOTSTRAP_NAVIGATION')]
    rounds=[event for event in events if event.get('type')=='ROUND_COMPLETE']
    products={str(row['goods_id']) for row in rows(directory/'products.jsonl') if row.get('goods_id')}
    status=read(directory/'progress.json',{}).get('status','NO_PROGRESS')
    return {'checks':{
        'image_gate_before_navigation':bool(gates and navigations and gates[0]<min(navigations)),
        'blank_page_bootstrap':'BLANK_BOOTSTRAP_DWELL' in kinds and 'BLANK_BOOTSTRAP_NAVIGATION' in kinds,
        'empty_search_submitted':'EMPTY_SEARCH_QUERY_SUBMITTED' in kinds,
        'complete_rounds':len(rounds)>=required_rounds,
        'new_goods_in_complete_round':any(int(event.get('new_goods',0))>0 for event in rounds),
        'has_product_ids':bool(products)},
        'rounds':len(rounds),'status':status,'products':products,
        'waiting_captcha':status in ('WAITING_HUMAN_VERIFICATION','WAITING_SEARCH_VERIFICATION','WAITING_EMPTY_SEARCH_VERIFICATION','WAITING_CAPTCHA')}


def report(evidence_root, current_manifest):
    before=read(evidence_root/'before_manifest.json',{}).get('workers',[])
    after=read(evidence_root/'after_manifest.json',{}).get('workers',[])
    current=current_manifest.get('workers',[])
    def ids(workers):return [str(w.get('profile_id','')) for w in workers]
    before_ids,after_ids=ids(before),ids(after)
    session_fingerprints=[w.get('proxy_session_fingerprint') for w in before]
    checks={
        'four_distinct_initial_profiles':len(before)==4 and len(set(before_ids))==4 and all(before_ids),
        'four_distinct_replacement_profiles':len(after)==4 and len(set(after_ids))==4 and all(after_ids),
        'all_replacements_new':bool(before_ids and after_ids and not set(before_ids).intersection(after_ids)),
        'four_distinct_initial_proxy_sessions':len(session_fingerprints)==4 and all(session_fingerprints) and len(set(session_fingerprints))==4,
        'current_manifest_matches_after':len(current)==4 and set(ids(current))==set(after_ids)}
    outcomes=[]
    for worker in before:
        name=worker.get('name','')
        if not name or Path(name).name!=name or name in ('.','..'):
            checks['safe_worker_names']=False; continue
        directory=evidence_root/name
        a=phase_evidence(directory/'before',1); b=phase_evidence(directory/'after',1)
        replacement=read(directory/'replacement.json',{})
        updated=next((w for w in after if w.get('name')==name),{})
        local={**{'before_'+k:v for k,v in a['checks'].items()}, **{'after_'+k:v for k,v in b['checks'].items()},
               'replacement_audit_matches':replacement.get('phase')=='REPLACED' and replacement.get('old_profile_id')==worker.get('profile_id') and replacement.get('new_profile_id')==updated.get('profile_id') and replacement.get('new_proxy_session') is True,
               'new_product_after_replacement':bool(b['products']-a['products'])}
        waiting=a['waiting_captcha'] or b['waiting_captcha']
        outcomes.append({'worker':name,'status':'WAITING_CAPTCHA' if waiting else 'PASSED' if all(local.values()) else 'PARTIAL',
                         'checks':local,'before_rounds':a['rounds'],'after_rounds':b['rounds'],
                         'new_product_count_after_replacement':len(b['products']-a['products'])})
    complete=all(checks.values()) and len(outcomes)==4 and all(w['status']=='PASSED' for w in outcomes)
    return {'status':'PASSED' if complete else 'WAITING_CAPTCHA' if any(w['status']=='WAITING_CAPTCHA' for w in outcomes) else 'PARTIAL',
            'mode':'READ_ONLY_EVIDENCE','checks':checks,'workers':outcomes,'browser_actions':0,'api_requests':0}


def report_existing_qa(root, manifest):
    """Consume existing cumulative QA events, separating phases by replacement time."""
    from datetime import datetime
    def epoch(value):
        try: return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
        except (ValueError,TypeError): return 0
    proof=read(root/'qa-before_rotation-proof.json',{})
    workers=proof.get('workers',{})
    current={w.get('name'):w for w in manifest.get('workers',[])}
    initial_ids=[w.get('profile_id') for w in workers.values()]
    digests=[w.get('session_sha256') for w in workers.values()]
    checks={'four_distinct_initial_profiles':len(initial_ids)==4 and all(initial_ids) and len(set(initial_ids))==4,
            'four_distinct_initial_sessions':len(digests)==4 and all(digests) and len(set(digests))==4,
            'four_current_workers':len(current)==4}
    outcomes=[]
    for name,worker in workers.items():
        progress=worker.get('two_page_progress',{})
        dedicated=read(root/'qa-third-two-pages-proof.json',{}) if name=='worker-03-new' else {}
        if dedicated.get('before'):
            progress=dedicated['before']
        directory=Path(progress.get('output_directory',''))
        if not directory.resolve().is_relative_to(root.resolve()):
            outcomes.append({'worker':name,'status':'PARTIAL','reason':'missing or out-of-scope evidence directory'});continue
        number=name.split('-')[1] if name.startswith('worker-') else ''
        if not number.isdigit():continue
        audit=read(root/'runtime'/'proxyshare-chains'/f'replacement-worker-{int(number)}.json',{})
        rotated=epoch(audit.get('replaced_at'))
        events=rows(directory/'events.jsonl')
        start=epoch(progress.get('started_at')) or float(proof.get('started_at',0))
        phases={'before':[e for e in events if epoch(e.get('at'))>=start and (not rotated or epoch(e.get('at'))<rotated)],
                'after':[e for e in events if rotated and epoch(e.get('at'))>=rotated]}
        local={}
        for phase,events in phases.items():
            types=[e.get('type') for e in events]
            gates=[i for i,e in enumerate(events) if e.get('type')=='IMAGE_BLOCK_GATE_PASSED' and e.get('navigation_performed') is False]
            nav=[i for i,t in enumerate(types) if t in ('BLANK_BOOTSTRAP_NAVIGATION','EMPTY_SEARCH_BOOTSTRAP_NAVIGATION')]
            local[phase+'_gate_before_navigation']=bool(gates and nav and gates[0]<min(nav))
            local[phase+'_blank_bootstrap']='BLANK_BOOTSTRAP_DWELL' in types and 'BLANK_BOOTSTRAP_NAVIGATION' in types
            local[phase+'_empty_search']='EMPTY_SEARCH_QUERY_SUBMITTED' in types
            local[phase+'_two_pages']=any(e.get('type')=='ROUND_COMPLETE' and int(e.get('new_goods',0))>0 for e in events)
        local['initial_profile_recorded']=bool(worker.get('profile_id') and worker.get('session_sha256'))
        local['latest_replacement_matches_current']=bool(audit.get('phase')=='REPLACED' and audit.get('old_profile_id') and audit.get('new_profile_id')!=audit.get('old_profile_id') and audit.get('new_profile_id')==current.get(name,{}).get('profile_id') and audit.get('new_proxy_session') is True)
        # Only the latest audit is retained. Intermediate rotations cannot be
        # reconstructed from it; do not mislabel this as a manifest mismatch.
        local['initial_to_latest_replacement_chain_verified']=bool(audit.get('phase')=='REPLACED' and audit.get('old_profile_id')==worker.get('profile_id'))
        baseline=int(progress.get('unique_goods',0))
        local['post_rotation_added_goods']=any(e.get('type')=='ROUND_COMPLETE' and int(e.get('new_goods',0))>0 and int(e.get('unique_goods',0))>baseline for e in phases['after'])
        latest=read(directory/'progress.json',{})
        waiting='VERIFICATION' in latest.get('status','') or 'CAPTCHA' in latest.get('status','')
        missing=[] if local['initial_to_latest_replacement_chain_verified'] else ['Intermediate replacement history is unavailable; initial-to-latest chain is unverified']
        outcomes.append({'worker':name,'status':'WAITING_CAPTCHA' if waiting else 'PASSED' if all(local.values()) else 'PARTIAL','checks':local,'evidence_gaps':missing,'before_goods':baseline,'before_source':'qa-third-two-pages-proof.json' if dedicated.get('before') else 'qa-before_rotation-proof.json'})
    complete=all(checks.values()) and len(outcomes)==4 and all(w['status']=='PASSED' for w in outcomes)
    return {'status':'PASSED' if complete else 'WAITING_CAPTCHA' if any(w['status']=='WAITING_CAPTCHA' for w in outcomes) else 'PARTIAL',
            'mode':'READ_ONLY_EXISTING_QA','checks':checks,'workers':outcomes,'browser_actions':0,'api_requests':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--category',type=Path,required=True)
    parser.add_argument('--sessions',type=Path,required=True)
    parser.add_argument('--evidence-root',type=Path,required=True)
    args=parser.parse_args()
    category=read(args.category,{})
    manifest=read(args.sessions,{})
    result=report_existing_qa(args.evidence_root,manifest) if (args.evidence_root/'qa-before_rotation-proof.json').exists() else report(args.evidence_root,manifest)
    result['category_key']=category.get('category_key')
    if not result['category_key']:result['status']='PARTIAL'; result['checks']['category_available']=False
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['status']=='PASSED' else 2


if __name__=='__main__':raise SystemExit(main())
