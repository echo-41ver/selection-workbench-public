"""Observe retained browsers after the collection owner exits; never resume them."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.request


PAGE_SCRIPT = r"""
const target=arguments[0],url=new URL(location.href);
const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
const text=(document.body?.innerText||'').replace(/\u200b/g,'');
const gate=/captcha|verification|login|neterror/i.test(url.pathname+' '+document.title)
 ||/security verification|verify you are human|access denied|403 Forbidden|502 Bad Gateway|no connection/i.test(text)
 ||[...document.querySelectorAll('button,[role="button"],div,span')].some(e=>visible(e)&&/^(try again|retry|重试)$/i.test(e.textContent.trim()));
return {matching_query:/(^|\.)temu\.com$/.test(url.hostname)&&url.pathname.includes('search_result')&&url.searchParams.get('search_key')===target,
 gate,goods:[...document.querySelectorAll('[data-goods-id],a[href*="-g-"]')].filter(visible).length,
 responses:performance.getEntriesByType('resource').filter(e=>/\/api\/poppy\/v[12]\/(search|search_activation)(?:\?|$)/.test(e.name)).map(e=>({endpoint:new URL(e.name).pathname,status:e.responseStatus,at_ms:performance.timeOrigin+e.startTime+e.duration}))};
"""


def recovery_evidence(page, failed_endpoint=None, failed_at_ms=0):
    if page.get('gate'):
        return 'WAITING_PAGE_RECOVERY'
    if not page.get('matching_query') or not page.get('goods'):
        return 'WAITING_CURRENT_QUERY'
    responses=page.get('responses') or []
    if failed_endpoint:
        responses=[r for r in responses if r.get('endpoint')==failed_endpoint]
    if not responses:
        return 'WAITING_SUCCESS_RESPONSE'
    latest=max(responses,key=lambda r:r.get('at_ms',0))
    if latest.get('status') in {403,429}:
        return 'WAITING_SITE_ACCEPTANCE'
    if latest.get('status')!=200 or latest.get('at_ms',0)<=failed_at_ms:
        return 'WAITING_SUCCESS_RESPONSE'
    return 'RECOVERY_CONFIRMED'


def read(path, fallback=None):
    try:return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return {} if fallback is None else fallback


def observe(root, base, row):
    name=row['name'];worker=read(base/'temu/supervisors'/f'{name}.json')
    progress=read(Path(worker['output'])/'progress.json') if worker.get('output') else {}
    block=progress.get('http_block') or {};failed=block.get('endpoint')
    if failed:
        from urllib.parse import urlsplit
        failed=urlsplit(failed).path
    try:failed_at=datetime.fromisoformat(block['observed_at'].replace('Z','+00:00')).timestamp()*1000
    except (KeyError,ValueError,TypeError):failed_at=0
    result={'worker':name,'query':worker.get('current_query'),'worker_status':worker.get('status')}
    try:
        sid=(root/row['session_id_file']).read_text(encoding='utf-8').strip()
        request=urllib.request.Request(row['webdriver_url']+'/session/'+sid+'/execute/sync',
            data=json.dumps({'script':PAGE_SCRIPT,'args':[result['query']]}).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as response:
            page=json.load(response)['value']
        result.update(status=recovery_evidence(page,failed,failed_at),goods=page.get('goods',0),
            matching_query=page.get('matching_query',False),gate=page.get('gate',True),responses=page.get('responses',[])[-4:])
    except (OSError,ValueError,KeyError,TypeError) as error:
        result.update(status='BROWSER_UNAVAILABLE',error_type=type(error).__name__)
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--category-key',required=True);parser.add_argument('--once',action='store_true')
    parser.add_argument('--interval',type=float,default=3);args=parser.parse_args()
    root=args.root.resolve();base=root/'runs'/args.category_key
    if base.resolve().parent!= (root/'runs').resolve():raise SystemExit('Invalid task directory')
    destination=root/'runtime/collection-recovery-observation.json'
    stop=root/'runtime/collection-recovery-observer.STOP'
    while not stop.exists():
        rows=read(root/'config/sessions.json').get('workers',[])
        with ThreadPoolExecutor(max_workers=max(1,min(32,len(rows)))) as pool:
            workers=list(pool.map(lambda row:observe(root,base,row),rows))
        payload={'mode':'READ_ONLY_NO_AUTOMATIC_COLLECTION','category_key':args.category_key,
            'user_stop_present':(base/'STOP').exists(),
            'updated_at':datetime.now(timezone.utc).isoformat(),'workers':workers}
        temporary=destination.with_suffix('.tmp');temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(destination)
        if args.once:break
        time.sleep(max(1,args.interval))


if __name__=='__main__':main()
