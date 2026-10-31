"""Revision-aware third-layer queue counts and observed processing speed."""
import json
import math
import time
from pathlib import Path

from .vision import compatible_vision_keys, VisionRevisionContext
from .stage3_admission import admitted, threshold, concurrency
from .visual_text_evidence import used_product_text
from .stage3_billing import billing_hold, _rows as billing_rows, retry_authorizations, authorized_retry_attempt

_CANDIDATES = {}
_HISTORY = {}


def records(path):
    try:
        with Path(path).open(encoding='utf-8-sig') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict): yield row
                except ValueError: continue
    except FileNotFoundError: return


def candidates(path):
    path=Path(path)
    try:
        stat=path.stat(); signature=(stat.st_size,stat.st_mtime_ns,stat.st_ino)
    except FileNotFoundError:return []
    cached=_CANDIDATES.get(str(path))
    if cached and cached[0]==signature:return cached[1]
    rows=[]; texts={}
    for r in records(path):
        row={k:r.get(k) for k in ('asin','goods_id','amazon_image_path','temu_image_path','vision_revision_key','pair_revision','vector_score')}
        value=used_product_text(r)
        if value:
            # Intern identical text sides, retaining the exact revision input
            # without duplicated raw records or full product snapshots.
            compact={}
            for side,evidence in value.items():
                key=json.dumps(evidence,ensure_ascii=False,sort_keys=True)
                compact[side]=texts.setdefault(key,evidence)
            row['_vision_product_text']=compact
        rows.append(row)
    _CANDIDATES[str(path)]=(signature,rows)
    return rows


def controller_alive(base):
    import psutil
    try:
        row = json.loads((Path(base)/'stage123/controller.json').read_text(encoding='utf-8'))
        process = psutil.Process(row['pid'])
        return process.is_running() and abs(process.create_time()-row['created']) < .01
    except (OSError, ValueError, KeyError, psutil.Error): return False


def live_consumer_started(base):
    """A watcher alone is insufficient: verify its owned task-bound consumer."""
    import psutil
    try:
        folder=Path(base)/'stage123'
        row=json.loads((folder/'controller.json').read_text(encoding='utf-8'))
        owner=psutil.Process(row['pid'])
        if row.get('phase')!='分析中' or not owner.is_running() or abs(owner.create_time()-row['created'])>=.01:
            return None
        # Windows venv launchers may put the actual interpreter one level below.
        for child in owner.children(recursive=True):
            cmd=child.cmdline()
            if not any(Path(arg).name=='run_category_layer3.py' for arg in cmd) or '--results' not in cmd:
                continue
            target=Path(cmd[cmd.index('--results')+1])
            if target.resolve()==(folder/'results.jsonl').resolve() and child.is_running():
                return child.create_time()
    except (OSError, ValueError, KeyError, IndexError, psutil.Error): pass
    return None


def progress_billing_hold(base):
    """Presentation distinguishes live in-flight calls; paid guard stays strict."""
    folder=Path(base)/'stage123'
    strict=billing_hold(folder/'request_intents.jsonl',folder/'results.jsonl')
    strict={**strict,'inflight_pairs':0,'inflight_requests':0}
    started_at=live_consumer_started(base)
    if started_at is None: return strict
    try:
        started={};settled=set()
        for row in billing_rows(folder/'request_intents.jsonl'):
            rid=row.get('request_id')
            if not isinstance(rid,str) or not rid: raise ValueError()
            if row.get('status')=='started':
                if rid in started and started[rid]!=row: raise ValueError()
                started[rid]=row
            elif row.get('status')=='settled' and row.get('billing_state') in ('KNOWN','NO_CHARGE'):
                settled.add(rid)
            else: raise ValueError()
        facts={}
        for row in billing_rows(folder/'results.jsonl'):
            key=row.get('vision_revision_key')
            if key: facts.setdefault(key,[]).append(row)
        hold={'request_ids':set(),'keys':set(),'pairs':set(),'fail_closed':False,
              'inflight_pairs':0,'inflight_requests':0}
        authorization=retry_authorizations(folder/'request_intents.jsonl',folder/'results.jsonl')
        for rid in set(started)-settled-authorization['request_ids']:
            row=started[rid];keys=row.get('vision_revision_keys')
            if (not isinstance(keys,list) or not keys or any(not isinstance(k,str) or not k for k in keys)
                or len(set(keys))!=len(keys) or type(row.get('pairs')) is not int or row['pairs']!=len(keys)):
                raise ValueError()
            def current_unknown(key,result):
                if result.get('billing_state')!='UNKNOWN':return False
                pair=(result.get('asin'),result.get('goods_id'))
                approved=authorization['by_pair'].get(pair)
                # The original failed row shares a key with its explicitly
                # approved retry. It does not make that new live call failed.
                if approved and key==approved['vision_revision_key']:
                    return int(result.get('attempt',0))>approved['baseline_attempt']
                return True
            unknown=any(current_unknown(k,r) for k in keys for r in facts.get(k,[]))
            if not unknown and isinstance(row.get('time'),(int,float)) and row['time']>=started_at:
                hold['inflight_requests']+=1;hold['inflight_pairs']+=len(keys)
                continue
            pairs=set()
            for key in keys:
                bindings={(r.get('asin'),r.get('goods_id')) for r in facts.get(key,[])}
                if len(bindings)!=1: raise ValueError()
                pair=next(iter(bindings))
                if not all(isinstance(v,str) and v for v in pair): raise ValueError()
                pairs.add(pair)
            hold['request_ids'].add(rid);hold['keys'].update(keys);hold['pairs'].update(pairs)
        return hold
    except (ValueError,OSError,TypeError,KeyError): return strict


def candidate_path(base):
    base = Path(base)
    try: config = json.loads((base/'stage12_config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): config = {}
    output = Path(config.get('output_dir') or base/'stage12')
    if not output.is_absolute(): output = base/output
    if not output.resolve().is_relative_to(base.resolve()): raise ValueError('候选目录不在当前任务内')
    return output/f'layer3_candidates_{base.name}.jsonl'


def snapshot_signature(base, category):
    """Version progress inputs, excluding only the controller's heartbeat time."""
    base = Path(base)
    folder = base/'stage123'
    paths = [candidate_path(base), base/'stage12_config.json', base/'category.json',
             *(folder/name for name in ('results.jsonl','requests.jsonl','request_intents.jsonl',
                                        'billing_retry_authorizations.json','billing_schema_retry_authorization.json',
                                        'provider_blocked.json','controller.json')),
             *sorted((folder/'schema-retry-claims').glob('*.json')),
             *sorted((folder/'schema-retry-audit').glob('*/provider_blocked.json')),
             *sorted((folder/'codex_reviews').glob('*.jsonl')),
             *(Path(__file__).parent/name for name in ('stage3_progress.py','vision.py',
                 'rules.py','stage3_admission.py','stage3_billing.py','schema_billing_retry.py','codex_visual_review.py',
                 'visual_text_evidence.py'))]
    versions = []
    for path in paths:
        try:
            if path == folder/'controller.json':
                # The watcher atomically rewrites this small document while
                # paused or waiting. File stats change even when queue state
                # does not; retain every state field except that heartbeat.
                state = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(state,dict):raise ValueError('第三层控制器状态格式无效')
                versions.append((str(path),'controller-state',json.dumps(
                    {k:v for k,v in state.items() if k!='updated_at'},ensure_ascii=False,sort_keys=True)))
                continue
            stat = path.stat()
            versions.append((str(path),stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
        except FileNotFoundError:
            versions.append((str(path),None))
    return (json.dumps(category,ensure_ascii=False,sort_keys=True),tuple(versions),controller_alive(base))


def observed_rate(base, model=None):
    if not model:return None
    successes = {}
    native_successes = {}
    for row in records(Path(base)/'stage123/results.jsonl'):
        if row.get('api_status') != 'success' or row.get('model') != model:continue
        if row.get('response_id'):
            successes.setdefault(row['response_id'],set()).add((row.get('asin'),row.get('goods_id')))
        elif (isinstance(row.get('vision_revision_key'),str) and row['vision_revision_key']
                and row.get('asin') and row.get('goods_id')):
            native_successes.setdefault(row['vision_revision_key'],[]).append(row)
    requests = list(records(Path(base)/'stage123/requests.jsonl'))
    completed_requests = {row.get('request_id'):len(successes.get(row.get('response_id'),set()))
                          for row in requests}
    requests_by_id = {}
    for row in requests:requests_by_id.setdefault(row.get('request_id'),[]).append(row)
    intent_rows = list(records(Path(base)/'stage123/request_intents.jsonl'))
    starts = {}; revision_requests = {}; settlements = {}
    for row in intent_rows:
        if row.get('status') == 'settled':
            identity = row.get('request_id');settlements[identity] = settlements.get(identity,0)+1
        if row.get('status') != 'started':continue
        identity = row.get('request_id')
        starts.setdefault(identity,[]).append(row)
        keys = row.get('vision_revision_keys')
        if isinstance(keys,list):
            for key in keys:
                if isinstance(key,str) and key:revision_requests.setdefault(key,set()).add(identity)
    intents = {}
    spans = []
    pairs = 0
    for row in intent_rows:
        identity = row.get('request_id')
        if row.get('status') == 'started': intents[identity] = row
        elif row.get('status') == 'settled' and identity in intents:
            start = intents.pop(identity)
            a, b = start.get('time', 0), row.get('time', 0)
            if (type(a) in (int,float) and type(b) in (int,float)
                    and math.isfinite(a) and math.isfinite(b) and b > a > 0
                    and row.get('billing_state') == 'KNOWN'):
                completed = completed_requests.get(identity,0)
                # Native D1 has no response ID. A unique single-pair revision
                # can link a real received/settled request to its model's
                # success; retry/duplicate ambiguity must remain unmeasured.
                keys = start.get('vision_revision_keys')
                linked = requests_by_id.get(identity,[])
                if (not completed and isinstance(identity,str) and identity
                        and len(starts.get(identity,[])) == 1 and settlements.get(identity) == 1 and start.get('pairs') == 1
                        and isinstance(keys,list) and len(keys) == 1 and isinstance(keys[0],str)
                        and revision_requests.get(keys[0]) == {identity}
                        and len(native_successes.get(keys[0],[])) == 1 and len(linked) == 1):
                    request = linked[0]
                    if (request.get('status') == 'received' and not request.get('response_id')
                            and request.get('pairs') == 1 and request.get('response_status') != 'incomplete'):
                        completed = 1
                if completed:
                    spans.append((a,b)); pairs += completed
    duration = 0
    end = 0
    for a,b in sorted(spans):
        duration += max(0,b-max(a,end)); end=max(end,b)
    return pairs/duration if duration and pairs else None


def historical_group_duration(base, model, batch_size):
    """Successful whole groups, normalized to this group size; cached read-only."""
    key = (str(Path(base)), model, batch_size)
    cached = _HISTORY.get(key)
    now = time.monotonic()
    if cached and now-cached[0]<60:
        return cached[1]
    durations = []
    for task in Path(base).parent.iterdir():
        if not task.is_dir() or task == Path(base): continue
        grouped = {}
        for row in records(task/'stage123/results.jsonl'):
            if row.get('api_status')!='success' or not row.get('response_id'): continue
            if model and row.get('model')!=model: continue
            group = grouped.setdefault(row['response_id'], {'pairs':set(),'seconds':0})
            group['pairs'].add((row.get('asin'),row.get('goods_id')))
            elapsed = row.get('elapsed_seconds')
            if type(elapsed) in (int,float) and elapsed>0:
                group['seconds'] = max(group['seconds'],elapsed)
        for request in records(task/'stage123/requests.jsonl'):
            n = request.get('pairs',0); group=grouped.get(request.get('response_id'))
            if type(n) is not int or n<2 or not group or len(group['pairs'])!=n or not group['seconds']: continue
            durations.append(group['seconds']*batch_size/n)
    result = (sum(durations)/len(durations), len(durations)) if durations else (None,0)
    _HISTORY[key] = (now,result)
    return result


def _qualified_api_facts(category, row):
    from .codex_visual_review import qualified_api_facts
    return qualified_api_facts(category, row)


def snapshot(base, category, *, history=True, on_phase=None):
    base = Path(base)
    def phase(name,**counts):
        if on_phase:on_phase(name,counts)
    queue=candidates(candidate_path(base))
    phase('读取已保存的Codex复核')
    from .codex_visual_review import load_reviews, supersedes_api, preferred_api_result
    codex_reviews = load_reviews(base, by_revision=True, category=category, current_pairs=queue)
    review_pairs = {identity[:2] for identity in codex_reviews}
    codex_counts = {'SAME':0,'SIMILAR':0,'DIFFERENT':0,'UNKNOWN':0}
    cached = {}
    phase('读取已保存的API结果')
    for row in records(base/'stage123/results.jsonl'):
        identity = row.get('vision_revision_key') or row.get('pair_revision')
        if identity:
            cached[identity] = preferred_api_result(category, cached.get(identity), row)
    completed_pairs = {(row.get('asin'),row.get('goods_id')) for row in cached.values()}
    total = success = failed = retained = source_candidates = 0
    waiting_groups = {}
    phase('核对计费隔离记录')
    held = progress_billing_hold(base)
    phase('核对既有重试授权')
    try: authorization=retry_authorizations(base/'stage123/request_intents.jsonl',base/'stage123/results.jsonl')
    except (ValueError,OSError,TypeError,KeyError): authorization={'request_ids':set(),'by_pair':{},'success_pairs':set(),'attempts':{}}
    blocked = pending_blocked = retry_consumed = scope_blocked = 0
    stage = category.get('stage3',{})
    batch_size = max(1,min(12,stage.get('batch_size',12)))
    from .stage3_provider import from_category
    provider=from_category(category)
    agent_mode=provider['mode']=='local_agent'
    if provider['mode']=='liquid_d1':batch_size=1
    agent_fact_pending=agent_fact_runnable=api_authorized_runnable=0
    parallel = concurrency(stage)
    seen = set()
    phase('读取当前候选文件')
    try: vision_context = VisionRevisionContext(category)
    except (OSError, KeyError, ValueError, TypeError): vision_context = None
    phase('逐对核验当前图片与结果版本',source_candidates=len(queue))
    for row in queue:
        if not row.get('asin') or not row.get('goods_id'): continue
        identity = (row['asin'],row['goods_id'],row.get('amazon_image_path'),row.get('temu_image_path'))
        if identity in seen: continue
        seen.add(identity); source_candidates += 1
        if source_candidates%500==0:phase('逐对核验当前图片与结果版本',scanned=source_candidates)
        if not admitted(category, row):
            retained += 1
            continue
        total += 1
        pair = (row['asin'], row['goods_id'])
        is_held = pair in held['pairs']
        if is_held: blocked += 1
        keys = ()
        if pair in completed_pairs or pair in review_pairs:
            try: keys = compatible_vision_keys(category, row, context=vision_context)
            except (OSError, KeyError, ValueError, TypeError): pass
        priors = [cached.get(k,{}) for k in keys]
        review = next((codex_reviews[(*pair,key)] for key in keys if (*pair,key) in codex_reviews),None)
        api_priors = [p for p in priors if p.get('api_status') == 'success']
        if review and (supersedes_api(category, review)
                or api_priors and all(supersedes_api(category, review, p) for p in api_priors)):
            codex_counts[review['review_status']] += 1
            continue
        api_success=any(p.get('api_status') == 'success' for p in priors)
        qualified=any(_qualified_api_facts(category,p) for p in priors)
        needs_agent_facts=api_success and not qualified and review is None
        if needs_agent_facts:
            agent_fact_pending+=1
            if (not is_held and pair not in authorization['success_pairs'] and
                    pair not in authorization['by_pair'] and not authorization.get('schema_retry')):
                agent_fact_runnable+=1
        # An explicit Agent review requires the same complete facts as its batch
        # preparer. Decision-only API success stays saved and complete for API use.
        if api_success and (not agent_mode or qualified):
            success += 1
            continue
        if review:
            codex_counts[review['review_status']] += 1
            continue
        # Pair-only retry history protects billing; it cannot establish that
        # the current image bytes/questions have a matching visual result.
        if pair in authorization['success_pairs']:
            pending_blocked += 1; retry_consumed += 1
            continue
        # A one-shot authorization for the original API cannot be consumed by
        # the independent Agent retry loop or expanded to its other candidates.
        if agent_mode and pair in authorization['by_pair']:
            pending_blocked += 1
            continue
        if is_held and provider['mode']!='local_model':
            pending_blocked += 1
            continue
        if authorization.get('schema_retry') and pair not in authorization['by_pair']:
            scope_blocked+=1;pending_blocked+=1
            continue
        group = (row['asin'],row.get('amazon_image_path'))
        waiting_groups[group] = waiting_groups.get(group,0)+1
        if agent_mode and needs_agent_facts:continue
        if (row['asin'],row['goods_id']) not in completed_pairs: continue
        if pair in authorization['by_pair']:
            if authorized_retry_attempt(authorization,pair,keys) is None:
                failed += 1; waiting_groups[group] -= 1
            else:api_authorized_runnable+=1
        elif max((int(p.get('attempt') or 0) for p in priors),default=0) >= 2:
            failed += 1; waiting_groups[group] -= 1
    completed = success + sum(codex_counts.values())
    pending = total-completed-failed
    runnable = 0 if held['fail_closed'] else pending-pending_blocked
    phase('核对历史处理速度')
    current_model = from_category(category)['model']
    rate = observed_rate(base,current_model)
    source = 'current' if rate else None
    group_seconds = None; history_groups = 0
    if not rate and history:
        group_seconds, history_groups = historical_group_duration(base,current_model,batch_size)
        if group_seconds:
            rate = batch_size*parallel/group_seconds
            source = 'history_group_normalized'
    pending_requests = sum(math.ceil(n/batch_size) for n in waiting_groups.values() if n>0)
    eta = round(runnable/rate) if rate else (0 if total and not runnable else None)
    if source == 'history_group_normalized':
        eta = round(math.ceil(pending_requests/parallel)*group_seconds)
    if held['fail_closed']:
        pending_requests = 0
        eta = None
    return {'total':total,'completed':completed,'api_completed':success,'failed':failed,'processed':completed+failed,
            'codex_completed':sum(codex_counts.values()),'codex_same':codex_counts['SAME'],'codex_similar':codex_counts['SIMILAR'],
            'codex_different':codex_counts['DIFFERENT'],'codex_unknown':codex_counts['UNKNOWN'],
            'pending_after_codex':pending,
            'agent_pending_after_codex':pending if agent_mode else pending+agent_fact_pending,
            'agent_runnable_pending':(0 if held['fail_closed'] or authorization.get('schema_retry') else
                runnable if agent_mode else runnable-api_authorized_runnable+agent_fact_runnable),
            'source_candidates':source_candidates,'retained_for_review':retained,
            'admission_threshold':threshold(category),
            'pending':pending,'percent':round((completed+failed)*100/total,1) if total else 0,
            'billing_unknown_pairs':blocked,'billing_unknown_requests':len(held['request_ids']),
            'billing_unknown_request_id':next(iter(held['request_ids'])) if len(held['request_ids'])==1 else None,
            'billing_unknown_pair':list(next(iter(held['pairs']))) if len(held['pairs'])==1 else None,
            'billing_hold_fail_closed':held['fail_closed'],'runnable_pending':runnable,
            'billing_retry_authorized_pairs':len(authorization['by_pair']),
            'billing_retry_consumed_pairs':retry_consumed,
            'schema_retry':({'authorized':True,'claimed':authorization['schema_retry']['claimed'],
                'pairs':len(authorization['by_pair']),'excluded_pairs':scope_blocked,
                'original_cost':'UNKNOWN'} if authorization.get('schema_retry') else None),
            'inflight_pairs':held['inflight_pairs'],'inflight_requests':held['inflight_requests'],
            'rate_per_hour':round(rate*3600) if rate else None,
            'eta_seconds':eta,
            'batch_size':batch_size,'max_concurrency':parallel,'pending_requests':pending_requests,
            'historical_group_seconds':round(group_seconds,2) if group_seconds else None,
            'historical_sample_groups':history_groups,
            'rate_source':source,'updated_at':time.time()}


def snapshot_document(base):
    """A local read-only worker document; changing inputs never authorize paid work."""
    base=Path(base)
    category=json.loads((base/'category.json').read_text(encoding='utf-8-sig'))
    before=snapshot_signature(base,category)
    import os,psutil
    from .merge_temu_snapshots import atomic_write
    identity={'pid':os.getpid(),'created':psutil.Process().create_time()}
    def report(name,counts):
        atomic_write(base/'stage123/progress_reader_state.json',json.dumps({**identity,
            'phase':name,'counts':counts,'updated_at':time.time()},ensure_ascii=False))
    value=snapshot(base,category,on_phase=report)
    report('本次只读核对已完成',{'total':value['total'],'completed':value['completed']})
    return {'signature':before,'progress':value,
            'stable':before==snapshot_signature(base,category),'category_key':base.name}


def configure_reader_priority():
    """Keep the isolated Windows reader scheduled; Unix retains its existing nice value."""
    import os,psutil
    try:psutil.Process().nice(psutil.NORMAL_PRIORITY_CLASS if os.name=='nt' else 10)
    except (OSError,psutil.Error):pass


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',type=Path,required=True)
    args=parser.parse_args()
    configure_reader_priority()
    print(json.dumps(snapshot_document(args.base),ensure_ascii=False))
