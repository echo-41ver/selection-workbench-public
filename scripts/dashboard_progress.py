"""Bounded read-only dashboard metrics; absent evidence stays absent."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
import sqlite3

_CACHE={}
_LOCK=threading.Lock()
REFRESH_SECONDS=10
MAX_JSON_BYTES=8*1024*1024
MAX_RESULT_BYTES=2*1024*1024


def _bar(done,total,scope,dynamic=True):
    return {'completed':done,'total':total,'remaining':max(0,total-done) if done is not None and total is not None else None,
            'percent':round(min(done,total)/total*100,1) if done is not None and total else None,
            'scope_label':scope,'dynamic_total':dynamic}


def _database(stage12):
    path=stage12/'stage12.sqlite3'
    if not path.is_file():return {}
    con=None
    try:
        con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        deadline=time.monotonic()+.5
        con.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
        groups=con.execute('SELECT status,stage1_status,COUNT(*) FROM pairs GROUP BY status,stage1_status').fetchall()
        total=sum(n for _,_,n in groups)
        text=sum(n for _,gate,n in groups if gate is not None)
        eligible=sum(n for _,gate,n in groups if gate is not None and gate!='TEXT_HARD_EXCLUDE')
        vectors=sum(n for status,gate,n in groups if gate is not None and gate!='TEXT_HARD_EXCLUDE' and status in ('CANDIDATE','BELOW_VECTOR_THRESHOLD'))
        failed=sum(n for status,gate,n in groups if gate is not None and gate!='TEXT_HARD_EXCLUDE' and status in ('FAILED_BOUNDED','FAILED_RETRYABLE','FAILED_IMAGE_ACTION'))
        waiting=sum(n for status,gate,n in groups if gate is not None and gate!='TEXT_HARD_EXCLUDE' and status.startswith('WAIT_'))
        return {'stage1':{**_bar(text,total,'当前已登记商品对；随采集增长'),'excluded':sum(n for _,g,n in groups if g=='TEXT_HARD_EXCLUDE')},
                'stage2':{**_bar(vectors,eligible,'当前通过文字筛选的商品对；不含文字排除或尚未文字处理'),'failed':failed,'waiting':waiting}}
    except sqlite3.Error:
        return {s:{**_bar(None,None,'当前数据库统计暂不可用'),'reason':'数据库忙、查询超时或结构不支持'} for s in ('stage1','stage2')}
    finally:
        if con is not None:con.close()


def _current_vision(base,stage12,state):
    unknown={**_bar(None,None,'当前候选版本匹配结果'),'reason':'候选或版本核验尚不可用'}
    category=_json(base/'category.json')
    if not category:return unknown
    path=stage12/f'layer3_candidates_{category.get("category_key", "")}.jsonl'
    if not path.is_file() or state.get('offset',0)<state.get('size',0):return unknown
    try:
        stat=path.stat()
        if stat.st_size>MAX_JSON_BYTES:return {**unknown,'reason':'候选文件超出有界核验大小'}
        signature=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)
        cache=state.setdefault('vision_cache',{})
        if cache.get('file_signature')!=signature:
            candidates={}
            for line in path.read_text('utf-8').splitlines():
                row=json.loads(line)
                candidates[(str(row['asin']),str(row['goods_id']))]=row
            cache['candidates']=candidates
            cache['file_signature']=signature
        candidates=cache['candidates']
        rules=json.dumps(category,sort_keys=True,ensure_ascii=False)
        if cache.get('rules')!=rules:
            cache['keys']={};cache['rules']=rules
        cached_keys=cache.setdefault('keys',{})
        for removed in cached_keys.keys()-candidates.keys():del cached_keys[removed]
        from selection_automation.vision import compatible_vision_keys
        done=failed=verified=processed=bytes_read=0
        for pair,row in candidates.items():
            images=[Path(row[f'{side}_image_path']) for side in ('amazon','temu')]
            stats=[image.stat() for image in images]
            pair_signature=(json.dumps(row,sort_keys=True,ensure_ascii=False),tuple(
                (str(image.resolve()),info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
                for image,info in zip(images,stats)))
            cached=cached_keys.get(pair)
            if not cached or cached[0]!=pair_signature:
                cached_keys.pop(pair,None)
                size=sum(info.st_size for info in stats)
                if processed>=200 or bytes_read+size>64*1024*1024:continue
                keys=compatible_vision_keys(category,row)
                cached_keys[pair]=(pair_signature,keys)
                processed+=1;bytes_read+=size
            else:keys=cached[1]
            verified+=1
            statuses=[state.get('revisions',{}).get((pair,key)) for key in keys]
            if 'success' in statuses:done+=1
            elif 'failed' in statuses:failed+=1
        if verified<len(candidates):
            return {**unknown,'reason':'当前候选版本正在分批核验','verification_completed':verified,
                    'verification_total':len(candidates),'metrics_catching_up':True}
        return {**_bar(done,len(candidates),'当前候选及当前视觉版本；随候选刷新变化'),'failed':failed,
                'verification_completed':verified,'verification_total':len(candidates),'metrics_catching_up':False}
    except (OSError,ValueError,KeyError,TypeError):return unknown


def _temu(base,category):
    target=category.get('temu',{}).get('max_products_per_keyword')
    budgets={row.get('query'):row.get('max_products') for row in
             (_json(base/'temu/keyword_plan.json') or {}).get('keywords',[]) if isinstance(row,dict)}
    budgets.update({row.get('query'):row.get('max_products') for row in
                    (_json(base/'temu/supervisors/assignments.json') or {}).get('jobs',[]) if isinstance(row,dict)})
    workers=[]
    assignments={x['name']:x.get('passes',x.get('queries',[])) for x in (_json(base/'temu/supervisors/assignments.json') or {}).get('workers',[]) if isinstance(x,dict) and x.get('name')}
    for supervisor in sorted((base/'temu/supervisors').glob('worker-*.json')):
        status=_json(supervisor) or {};name=supervisor.stem;query=status.get('current_query')
        snapshots=[_json(p) for p in (base/'temu/workers'/name).glob('*/progress.json')]
        current=next((x for x in snapshots if x and x.get('search_query',x.get('query'))==query and (not status.get('current_sort') or (x.get('sales_sort') or {}).get('requested')==status['current_sort'])),None)
        goods=current.get('unique_goods') if current else None
        complete=sum(1 for x in snapshots if x and x.get('status') in ('KEYWORD_LIMIT_REACHED','KEYWORD_COMPLETE'))
        total_keywords=len(assignments[name]) if name in assignments else status.get('assigned')
        collected=sum(x.get('unique_goods',0) for x in snapshots if x and type(x.get('unique_goods')) is int)
        dual=any(isinstance(item,dict) for item in assignments.get(name,[]))
        query_target=budgets.get(query) or target
        progress=(_bar(goods,query_target,f'当前排序目标 {query_target} 条；每词依次采销量和最近上架',False) if query else
                  _bar(complete,total_keywords,'已完成采集批次；每词包含销量和最近上架两批' if dual else '历史任务采集进度；此任务尚未包含最近上架轮次',False))
        workers.append({'name':name,'current_query':query,'current_sort':status.get('current_sort'),**progress,'actual_count':collected,
                        'completed_keywords':complete,'total_keywords':total_keywords})
    return {'workers':workers,'scope_label':'每路当前关键词；关键词计划随Amazon采集变化','dynamic_total':True} if workers else None


def _json(path):
    try:
        if path.stat().st_size>MAX_JSON_BYTES:return None
        value=json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value,dict) else None
    except (OSError,ValueError):return None


def _updated(path):
    return datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()


def _results(path,state):
    stat=path.stat(); identity=(stat.st_dev,stat.st_ino)
    if state.get('identity')!=identity or stat.st_size<state.get('offset',0) or (stat.st_size==state.get('offset',0) and stat.st_mtime_ns!=state.get('mtime')):
        state.clear()
    state.setdefault('offset',0);state.setdefault('pairs',{});state.setdefault('revisions',{})
    with path.open('rb') as stream:
        stream.seek(state['offset']); data=stream.read(MAX_RESULT_BYTES)
    last=data.rfind(b'\n')
    if last>=0:
        for line in data[:last+1].splitlines():
            try: row=json.loads(line)
            except (ValueError,UnicodeDecodeError):continue
            if not isinstance(row,dict) or not row.get('asin') or not row.get('goods_id'):continue
            key=(str(row['asin']),str(row['goods_id']))
            status=row.get('api_status');revision=row.get('vision_revision_key')
            if status not in ('success','failed'):continue
            revision_identity=(key,revision)
            if state['revisions'].get(revision_identity)!='success':state['revisions'][revision_identity]=status
            previous=state['pairs'].get(key)
            if previous and previous==(revision,'success') and status=='failed':continue
            state['pairs'][key]=(revision,status)
        state['offset']+=last+1
    state.update(identity=identity,mtime=stat.st_mtime_ns,size=stat.st_size)
    metrics={'updated_at':_updated(path),'count_scope':'本地最新结果记录；未重新核验候选版本',
             'metrics_catching_up':state['offset']<stat.st_size}
    if not metrics['metrics_catching_up']:
        metrics['completed_pairs']=sum(status=='success' for _,status in state['pairs'].values())
        metrics['failed_pairs']=sum(status=='failed' for _,status in state['pairs'].values())
    return metrics


def _collect(base,entry):
    metrics={}
    from watch_temu_union import watch_health
    health=watch_health(base)
    if health: metrics['temu_union']=health
    category=_json(base/'category.json') or {}
    amazon=base/'amazon'/'amazon_frontend_latest.json';data=_json(amazon)
    if data is not None and isinstance(data.get('records'),list):
        metrics['amazon']={'actual_count':len(data['records']),'updated_at':_updated(amazon)}
        metrics['amazon']['public_original_image_count']=sum(
            isinstance(row,dict) and row.get('public_image_evidence',{}).get('verified') is True
            for row in data['records'])
        target=data.get('target_count')
        if target is None:target=(_json(base/'category.json') or {}).get('amazon',{}).get('target_count')
        if isinstance(target,int):
            metrics['amazon']['target_count']=target
            metrics['amazon'].update(_bar(len(data['records']),target,'Amazon榜单前N名',False))
        if isinstance(data.get('status'),str):metrics['amazon']['snapshot_status']=data['status']
        if data.get('status')=='LISTING_COMPLETE':
            metrics['amazon'].update(_bar(0,target,'榜单已取得；等待并行补全详情与核验图片',False))
            metrics['amazon']['状态']='榜单已取得，详情待补全'
        checkpoint=_json(amazon.with_name(amazon.name+'.checkpoint.json')) or {}
        queue=checkpoint.get('detail_queue') or data.get('detail_enrichment',{}).get('queue')
        if isinstance(queue,dict) and isinstance(queue.get('items'),list):
            items=queue['items'];amazon_metrics=metrics['amazon']
            done=sum(item.get('status')=='COMPLETE' for item in items)
            pending=sum(item.get('status') in ('PENDING','IN_PROGRESS','RETRY_PENDING') for item in items)
            manual=sum(item.get('status')=='MANUAL_ACTION_REQUIRED' for item in items)
            exhausted=sum(item.get('status')=='EXHAUSTED' for item in items)
            amazon_metrics.update(detail_complete_count=done,detail_retry_pending=pending,
                                  detail_manual_count=manual,detail_exhausted_count=exhausted,
                                  detail_retry_wait_count=sum(item.get('status')=='RETRY_PENDING' for item in items)+exhausted)
            if isinstance(target,int):
                amazon_metrics.update(_bar(done,target,'Amazon 前N名商品的详情与主图；榜单入库不代表详情完成',False))
            if pending:
                amazon_metrics['状态']='正在获取详情或自动队尾补采'
            elif manual or exhausted:
                amazon_metrics['状态']='部分详情需要处理'
                amazon_metrics['manual_action_required']=True
            elif (target and done==target==len(items)==len(data['records'])
                  and data.get('status')=='COMPLETE' and data.get('details_complete') is True):
                amazon_metrics.update(状态='本轮完成',退出码=0,manual_action_required=False,建议动作='')
            if manual or exhausted:
                amazon_metrics['建议动作']=f'{manual} 件因验证或访问限制需要处理；{exhausted} 件旧失败记录等待恢复持续补采。已成功商品保留。'
            elif pending:
                amazon_metrics['建议动作']='未完成详情持续自动重试，直到获取成功或停止任务；已成功商品保留。'
    config=_json(base/'stage12_config.json') or {}
    stage12=Path(config.get('output_dir') or base/'stage12')
    if not stage12.is_absolute():stage12=base/stage12
    if stage12.resolve().is_relative_to(base.resolve()):metrics.update(_database(stage12))
    temu=_temu(base,category)
    if temu:metrics['temu']=temu
    from collection_progress import collection_total
    total=collection_total(base,category)
    if total:metrics.setdefault('temu',{})['overall']=total
    directories=[base/'stage123'/'export_frozen']
    if stage12.resolve().is_relative_to(base.resolve()):directories.append(stage12)
    summaries=[path for directory in directories for path in directory.glob('layer12_summary_*.json')]
    for path in sorted(summaries,key=lambda p:p.stat().st_mtime,reverse=True):
        summary=_json(path)
        if not summary:continue
        states=summary.get('states')
        if not isinstance(states,dict):continue
        completed=sum(value for key,value in states.items() if key in ('STAGE1_EXCLUDED','CANDIDATE','BELOW_VECTOR_THRESHOLD') and isinstance(value,int))
        metrics['stage12']={'processed_pairs':completed,'updated_at':summary.get('snapshot_at') or _updated(path)}
        candidate=summary.get('candidates',states.get('CANDIDATE'))
        if isinstance(candidate,int):metrics['stage12']['candidate_count']=candidate
        break
    results=base/'stage123'/'results.jsonl'
    if results.is_file():metrics['stage3']=_results(results,entry.setdefault('results',{}))
    if category and stage12.resolve().is_relative_to(base.resolve()):
        metrics.setdefault('stage3',{}).update(_current_vision(base,stage12,entry.setdefault('results',{})))
    books=[p for p in (base/'excel').glob('*.xlsx') if not p.name.startswith(('~$','.')) and not p.name.endswith('_data.xlsx')]
    if books:
        latest=max(books,key=lambda p:p.stat().st_mtime)
        metrics['excel']={'updated_at':_updated(latest),'actual_count':len(books),'latest_file':latest.name}
    return metrics


def enrich_progress(base:Path,details:dict,*,watch_running=True,asynchronous=False)->dict:
    """Return a copy. No network, database mutations, or full JSONL rescans per poll."""
    result=deepcopy(details)
    key=str(base.resolve())
    if not _LOCK.acquire(blocking=False):return result
    try:
        now=time.monotonic();entry=_CACHE.setdefault(key,{})
        if now-entry.get('checked',float('-inf'))>=REFRESH_SECONDS and not entry.get('refreshing'):
            entry['checked']=now
            if asynchronous:
                entry['refreshing']=True
                def update():
                    metrics=None
                    try:metrics=_collect(base,entry)
                    except (OSError,ValueError,TypeError):pass
                    finally:
                        with _LOCK:
                            if metrics is not None:
                                entry['metrics']=metrics;entry['snapshot_at']=time.time()
                            entry.update(refreshing=False,checked=time.monotonic())
                threading.Thread(target=update,daemon=True).start()
            else:
                try:
                    entry['metrics']=_collect(base,entry);entry['snapshot_at']=time.time()
                except (OSError,ValueError,TypeError):pass
        if asynchronous:
            result['progress_snapshot']={'updated_at':entry.get('snapshot_at'),
                'refreshing':bool(entry.get('refreshing')),'scope':'最近本地进度快照；实时采集状态仍读取监督器'}
        stages=result.setdefault('阶段',{})
        if not isinstance(stages,dict):return result
        for name,metrics in entry.get('metrics',{}).items():
            current=stages.setdefault(name,{})
            if isinstance(current,dict):current.update(deepcopy(metrics))
        amazon=stages.get('amazon',{})
        union=stages.get('temu_union',{})
        if not watch_running and union.get('stale') and not union.get('source_failed'):
            union.update(状态='汇总已停止，保留最近快照',stale=False,阻断=False)
        if stages.get('temu_union',{}).get('阻断'):
            result['状态']='存在阻断'
            result['阻断阶段']=list(dict.fromkeys([*result.get('阻断阶段',[]),'temu_union']))
        # A repaired snapshot can arrive after the supervisor exits. Reconcile
        # its stale failure without hiding any other stage's outstanding block.
        if amazon.get('状态')=='本轮完成' and amazon.get('snapshot_status')=='COMPLETE':
            try: newer=datetime.fromisoformat(amazon['updated_at']).timestamp()>=float(result.get('时间',0))
            except (KeyError,ValueError,TypeError):newer=False
            if newer:
                for field in ('阻断阶段','人工介入阶段'):
                    if isinstance(result.get(field),list):result[field]=[name for name in result[field] if name!='amazon']
                if isinstance(result.get('建议动作'),dict):result['建议动作'].pop('amazon',None)
                if result.get('状态')=='存在阻断' and result.get('阻断阶段')==[]:
                    result['状态']='阻断已解除，最新进度见下方'
                if result.get('人工介入阶段')==[]:result['需人工介入']=False
        return result
    finally:_LOCK.release()
