"""Durable query ownership. Only unstarted work may move between lanes."""
import json
import time
from contextlib import contextmanager
from pathlib import Path
from .process_lock import ProcessLock
from .merge_temu_snapshots import atomic_write


@contextmanager
def locked(run):
    path=Path(run)/'temu/supervisors/dispatch.lock'
    lock=ProcessLock(path)
    for attempt in range(250):
        try: lock.__enter__();break
        except RuntimeError:
            if attempt==249: raise
            time.sleep(.02)
    try: yield
    finally: lock.__exit__(None,None,None)


def read(run):
    path=Path(run)/'temu/supervisors/assignments.json'
    return json.loads(path.read_text('utf-8')) if path.exists() else {}


def save(run, data):
    for job in data.get('jobs',[]): _ensure_pass_states(job)
    data['workers']=[{'name':name,'queries':[r['query'] for r in data['jobs'] if r['owner']==name],
        'passes':[{'query':r['query'],'sort':order} for r in data['jobs']
                  for order,state in _pass_states(r).items() if state.get('owner')==name and not state.get('complete')]}
        for name in data['lanes']]
    data['schema_version']='temu-worker-assignments-v3'
    atomic_write(Path(run)/'temu/supervisors/assignments.json',json.dumps(data,ensure_ascii=False,indent=2))


_SORTS=('sales','recent')


def _pass_states(job):
    states=job.get('sort_passes')
    if isinstance(states,dict): return states
    # A v2 query claim owns both passes. Protect both during a rolling upgrade;
    # the old collector may still be running sales or may be about to run recent.
    legacy_started=bool(job.get('started'))
    legacy_complete=bool(job.get('complete'))
    return {sort:{'owner':job.get('owner'),'started':legacy_started or legacy_complete,
                  'complete':legacy_complete,**({'completed_at':job.get('completed_at')} if legacy_complete else {})}
            for sort in _SORTS}


def _ensure_pass_states(job):
    if not isinstance(job.get('sort_passes'),dict):
        job['sort_passes']=_pass_states(job)
    for sort in _SORTS:
        job['sort_passes'].setdefault(sort,{'owner':job.get('owner'),'started':False,'complete':False})
    if job.get('complete'):
        for state in job['sort_passes'].values():
            state.update(owner=state.get('owner') or job.get('owner'),started=True,complete=True,
                         completed_at=state.get('completed_at') or job.get('completed_at') or time.time())
    elif job.get('started') and not any(state.get('started') for state in job['sort_passes'].values()):
        # Compatibility with callers that still mutate a v2 query-level record.
        for state in job['sort_passes'].values(): state.update(owner=job.get('owner'),started=True)
    job['started']=any(state.get('started') for state in job['sort_passes'].values())
    job['complete']=all(state.get('complete') for state in job['sort_passes'].values())


def sync(run, keywords, names, resume_queries=()):
    resume_queries=set(resume_queries)
    with locked(run):
        data=read(run)
        if 'jobs' not in data:
            legacy={q:w['name'] for w in data.get('workers',[]) for q in w.get('queries',[])}
            data={'jobs':[],'lanes':names}
            # Existing ownership is never shuffled on upgrade.
            for row in keywords:
                if row['query'] in legacy:
                    data['jobs'].append({**row,'owner':legacy[row['query']],'started':True,'complete':False})
        data['lanes']=names
        by_query={r['query']:r for r in data['jobs']}
        known=set(by_query)
        for job in data['jobs']:
            _ensure_pass_states(job)
        for row in keywords:
            existing=by_query.get(row['query'])
            if existing is None:continue
            old_limit=int(existing.get('max_products') or 0)
            new_limit=int(row.get('max_products') or 0)
            if new_limit>old_limit:existing['max_products']=new_limit
            # Keep prior evidence and monotonically add newly observed source facts.
            source_asins=list(existing.get('source_asins') or [])
            for asin in row.get('source_asins') or []:
                if asin not in source_asins:source_asins.append(asin)
            if 'source_asins' in existing or 'source_asins' in row:
                existing['source_asins']=source_asins
            evidence=list(existing.get('source_evidence') or [])
            known_evidence={str(item.get('asin') or '') for item in evidence}
            for item in row.get('source_evidence') or []:
                asin=str(item.get('asin') or '')
                if asin and asin not in known_evidence:
                    evidence.append(item);known_evidence.add(asin)
            if 'source_evidence' in existing or 'source_evidence' in row:
                existing['source_evidence']=evidence
            if existing.get('complete') and row['query'] in resume_queries and new_limit>old_limit:
                existing['complete']=False
                existing.pop('completed_at',None)
                _ensure_pass_states(existing)
                for state in existing['sort_passes'].values():
                    state.update(started=True,complete=False)
                    state.pop('completed_at',None)
                existing['started']=True
                existing['complete']=False
        loads={n:sum(int(r['max_products'])*2 for r in data['jobs'] if r['owner']==n and not r['complete']) for n in names}
        for row in keywords:
            if row['query'] in known: continue
            owner=min(names,key=lambda n:loads[n])
            data['jobs'].append({**row,'owner':owner,'started':False,'complete':False,
                'sort_passes':{sort:{'owner':owner,'started':False,'complete':False} for sort in _SORTS}})
            loads[owner]+=int(row['max_products'])*2;known.add(row['query'])
        save(run,data);return data


def claim(run, worker):
    """Legacy query-level claim; new collectors should use claim_pass."""
    with locked(run):
        data=read(run)
        available=[r for r in data['jobs'] if not r['complete'] and (r['owner']==worker or not r['started'])]
        if not available: return None
        job=min(available,key=lambda r:(not(r['started'] and r['owner']==worker),r['owner']!=worker))
        _ensure_pass_states(job)
        job.update(owner=worker,started=True,claimed_at=time.time())
        # Legacy callers consume both sorts as one query. Reserve both so a
        # newer collector cannot duplicate the old process's second pass.
        for state in job['sort_passes'].values():
            if not state.get('complete'):
                state.update(owner=worker,started=True,claimed_at=job['claimed_at'])
        save(run,data);return dict(job)


def claim_pass(run, worker):
    """Atomically claim one sort pass, preserving active and completed passes."""
    with locked(run):
        data=read(run)
        jobs=data.get('jobs',[])
        for job in jobs: _ensure_pass_states(job)
        candidates=[]
        for job in jobs:
            states=job['sort_passes']
            # A currently claimed pass protects the whole query until it
            # completes. The same lane may reclaim it after a restart.
            active=[state for state in states.values() if state.get('started') and not state.get('complete')]
            if active:
                own=[(job,sort,state) for sort,state in states.items()
                     if state.get('started') and not state.get('complete') and state.get('owner')==worker]
                candidates.extend(own)
                continue
            candidates.extend((job,sort,state) for sort,state in states.items() if not state.get('started') and not state.get('complete'))
        own=[item for item in candidates if item[2].get('owner')==worker]
        pool=own or candidates
        if not pool: return None
        job,sort,state=min(pool,key=lambda item:(not item[2].get('started'),item[0]['owner']!=worker,
            not (item[1]=='recent' and item[0]['sort_passes']['sales'].get('complete')),
            item[1]!='sales',int(item[0].get('max_products') or 0)))
        state.update(owner=worker,started=True,claimed_at=time.time())
        job['started']=True
        job['claimed_at']=state['claimed_at']
        save(run,data)
        return {**job,'sort':sort,'pass_owner':worker,'pass_started':True}


def complete_empty_query(run,worker,query,evidence):
    """A confirmed empty search needs neither sort; retain prior collected passes."""
    with locked(run):
        data=read(run);job=next(row for row in data['jobs'] if row['query']==query)
        _ensure_pass_states(job)
        active=[state for state in job['sort_passes'].values() if state.get('started') and not state.get('complete')]
        if not active or any(state.get('owner')!=worker for state in active):raise RuntimeError('Empty query ownership changed')
        for state in job['sort_passes'].values():
            if not state.get('complete'):
                state.update(owner=worker,started=True,complete=True,skipped=True,
                    completion_reason='EXPLICIT_EMPTY_QUERY_RESULTS',completed_at=time.time())
        job.update(started=True,complete=True,no_results_evidence=evidence,completed_at=time.time())
        save(run,data)


def requeue_unsettled_empty_queries(run):
    """Revisit old single-sample emptiness without touching collected passes."""
    changed=[]
    with locked(run):
        data=read(run)
        for job in data.get('jobs',[]):
            evidence=job.get('no_results_evidence') or {}
            if not job.get('complete') or not evidence:continue
            if (evidence.get('protocol')=='VISIBLE_EMPTY_RESULTS_STABLE_V2'
                    and evidence.get('loading') is False and (evidence.get('stable_ms') or 0)>=15000
                    and (evidence.get('samples') or 0)>=3):continue
            _ensure_pass_states(job);reset=False
            for state in job['sort_passes'].values():
                if state.get('skipped') and state.get('completion_reason')=='EXPLICIT_EMPTY_QUERY_RESULTS':
                    state.update(started=False,complete=False);state.pop('completed_at',None)
                    state.pop('skipped',None);state.pop('completion_reason',None);reset=True
            if reset:
                job['complete']=False;job['started']=any(s.get('started') for s in job['sort_passes'].values())
                job.pop('completed_at',None)
                job.pop('no_results_evidence',None);job['empty_requeued_at']=time.time()
                changed.append(job['query'])
        if changed:save(run,data)
    return changed


def complete_pass(run, worker, query, sort):
    if sort not in _SORTS: raise ValueError(f'Unknown TEMU sort pass: {sort}')
    with locked(run):
        data=read(run)
        job=next(r for r in data['jobs'] if r['query']==query)
        _ensure_pass_states(job)
        state=job['sort_passes'][sort]
        if state.get('owner')!=worker or not state.get('started'):
            raise RuntimeError('Sort pass ownership changed')
        state.update(complete=True,completed_at=time.time())
        job['complete']=all(row.get('complete') for row in job['sort_passes'].values())
        if job['complete']: job['completed_at']=time.time()
        save(run,data)


def complete(run, worker, query):
    with locked(run):
        data=read(run);job=next(r for r in data['jobs'] if r['query']==query)
        if job['owner']!=worker or not job['started']: raise RuntimeError('Query ownership changed')
        _ensure_pass_states(job)
        for state in job['sort_passes'].values():
            state.update(owner=worker,started=True,complete=True,completed_at=time.time())
        job.update(complete=True,completed_at=time.time());save(run,data)


def available(data, worker):
    for job in data.get('jobs',[]):
        states=_pass_states(job)
        active=[state for state in states.values() if state.get('started') and not state.get('complete')]
        if active:
            if any(state.get('owner')==worker for state in active): return True
            continue
        if any(not state.get('complete') and (state.get('owner')==worker or not state.get('started'))
               for state in states.values()): return True
    return False


def completed_for_lane(data, worker):
    """Return lane-local evidence; a query is credited where its recent pass finished."""
    queries=[]; passes=[]
    for job in data.get('jobs',[]):
        states=_pass_states(job)
        for sort in _SORTS:
            state=states.get(sort,{})
            if state.get('complete') and state.get('owner')==worker:
                passes.append({'query':job['query'],'sort':sort})
        recent=states.get('recent',{})
        if job.get('complete') and recent.get('complete') and recent.get('owner')==worker:
            queries.append(job['query'])
    return queries,passes
