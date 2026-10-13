"""Fresh, isolated normal-navigation trials; never solves gates or changes live manifests."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import psutil

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'src')]
from replace_proxyshare_worker_environment import (api_base,api_call,start_profile_with_retry,
    stop_profile_and_wait,delete_profile_after_release)
from selection_automation.adspower_kernel import REQUIRED_KERNEL
from selection_automation.background_launch import run_background_launcher
from selection_automation.merge_temu_snapshots import atomic_write
from selection_automation.process_lock import ProcessLock
from selection_automation.proxy_activity import ProxyActivityLease

HOME='https://www.temu.com/'
EMPTY='https://www.temu.com/search_result.html'
PAGE_SCRIPT=r"""const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
return {url:location.href,documentURI:document.documentURI,title:document.title,ready:document.readyState,
text:(document.body?.innerText||'').slice(0,3000),
goods:document.querySelectorAll('a[href*="goods_id="],a[href*="goodsId="],a[href*="-g-"]').length,
searchReady:[...document.querySelectorAll('input[type=search],input[placeholder*="search" i],input[aria-label*="search" i]')].some(e=>visible(e)&&!e.disabled&&!e.readOnly),
loginFormVisible:[...document.querySelectorAll('input[type=password],[role=dialog] input[type=email],[role=dialog] input[autocomplete=username]')].some(visible)};"""

def save(path,value):
    atomic_write(path,json.dumps(value,ensure_ascii=False,indent=2))

def read(path):
    try: return json.loads(path.read_text('utf-8-sig'))
    except (OSError,ValueError): return {}

def gate(page):
    pathname=urllib.parse.urlsplit(str(page.get('url',''))).path
    text=page.get('text','')
    if re.search(r'bgn_verification|captcha|challenge|security[-_]?verification',pathname,re.I) or re.search(r'security verification|verify (?:that )?you are human|complete the verification|验证码|安全验证|拖动滑块',text,re.I): return 'CAPTCHA'
    if re.search(r'/login(?:\.html)?/?$',pathname,re.I) or page.get('loginFormVisible'): return 'LOGIN_WALL'
    if not page.get('goods') and re.search(r'access denied|request blocked|unusual traffic|\b(?:403|429)\b',text,re.I): return 'ACCESS_DENIED'
    if str(page.get('documentURI','')).startswith('about:neterror'): return 'NETWORK_FAILURE'
    return None

def routes(query,include_percent=False):
    target=EMPTY+'?'+urllib.parse.urlencode({'search_key':query})
    # (name, actual AdsPower startup tab, subsequent ordinary navigations, collector mode, submit, dwell)
    planned=[
      ('startup_keyword',target,[],'direct_search','icon',0),
      ('blank_direct','about:blank',[],'direct_search','icon',0),
      ('startup_empty',EMPTY,[],'empty_search','icon',0),
      ('blank_empty','about:blank',[],'empty_search','icon',0),
      ('startup_home',HOME,[],'home_search','icon',0),
      ('blank_home','about:blank',[],'home_search','icon',0),
      ('empty_home',EMPTY,[HOME],'home_search','icon',0),
      ('home_empty',HOME,[EMPTY],'empty_search','icon',0),
      ('empty_direct',EMPTY,[],'direct_search','icon',0),
      ('home_direct',HOME,[],'direct_search','icon',0),
      ('empty_home_empty',EMPTY,[HOME,EMPTY],'empty_search','icon',0),
      ('home_empty_home',HOME,[EMPTY,HOME],'home_search','icon',0),
      ('empty_enter',EMPTY,[],'empty_search','enter',0),
      ('home_enter',HOME,[],'home_search','enter',0),
      ('blank_direct_settled','about:blank',[],'direct_search','icon',10),
      ('startup_empty_settled',EMPTY,[],'empty_search','icon',10),
      ('startup_home_settled',HOME,[],'home_search','icon',10),
    ]
    if include_percent:
        planned.append(('startup_keyword_percent20',EMPTY+'?search_key='+urllib.parse.quote(query,safe=''),[],'direct_search','icon',0))
    return planned

def trial_fingerprint(version,mode):
    kernel={'type':'firefox','version':str(version)}
    if mode=='defaults':return {'browser_kernel_config':kernel}
    return {'automatic_timezone':'1','location_switch':'1','language_switch':'1','page_language_switch':'1','screen_resolution':'1920_1080','webrtc':'disabled',
      'ua':f'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}.0) Gecko/20100101 Firefox/{version}.0','browser_kernel_config':kernel}

def selected_routes(query,include_percent=False,names=None):
    planned=routes(query,include_percent)
    if names is None:return planned
    wanted=[name.strip() for name in names.split(',')]
    available={route[0]:route for route in planned}
    if not wanted or len(set(wanted))!=len(wanted) or any(name not in available for name in wanted):
        raise ValueError('Unknown or duplicate normal trial route')
    return [available[name] for name in wanted]

def driver_call(base,sid,route,payload):
    request=urllib.request.Request(f'{base}/session/{sid}{route}',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=8) as response:
        value=json.load(response)['value']
        # Navigation can transiently destroy the execution context and return null.
        # Keep polling within the existing deadline instead of ending a valid trial.
        if route=='/execute/sync' and not isinstance(value,dict):return {'unavailable':True}
        return value

def owned_terminate(owner,marker):
    if not owner: return
    try:
        process=psutil.Process(owner['pid'])
        if abs(process.create_time()-owner['created'])>.1 or marker not in ' '.join(process.cmdline()): return
        process.terminate()
        try: process.wait(10)
        except psutil.TimeoutExpired: process.kill();process.wait(5)
    except psutil.NoSuchProcess: pass

def bound_driver_owner(driver,profile):
    if driver.get('profile_id')!=profile:
        raise RuntimeError('ISOLATED_DRIVER_OWNERSHIP_MISMATCH')
    try:
        process=psutil.Process(int(driver['pid']))
        created=process.create_time()
        ticks=621355968000000000+round(created*10000000)
        if abs(ticks-int(driver['started_ticks']))>100000 or 'geckodriver' not in process.name().lower():
            raise RuntimeError('ISOLATED_DRIVER_OWNERSHIP_MISMATCH')
        return {'pid':process.pid,'created':created}
    except (KeyError,TypeError,ValueError,psutil.Error):
        raise RuntimeError('ISOLATED_DRIVER_UNAVAILABLE') from None

def cleanup_profile(info,driver_owner):
    # Release the attached WebDriver before asking AdsPower to close its browser.
    # Otherwise vendor shutdown may leave an active attached environment behind.
    owned_terminate(driver_owner,'geckodriver')
    profile=info.get('profile_id')
    if not profile:return
    for attempt in range(3):
        info['cleanup_attempts']=attempt+1
        try:
            info['cleanup_phase']='STOP_OWN_PROFILE'
            stop_profile_and_wait(api_base(),profile,timeout=45)
            info['cleanup_phase']='DELETE_RELEASED_OWN_PROFILE'
            delete_profile_after_release(api_base(),profile,attempts=3)
            info['profile_closed_and_deleted']=True
            info.pop('cleanup_error_type',None)
            info.pop('cleanup_error_category',None)
            info['cleanup_phase']='COMPLETE'
            return
        except Exception as error:
            info['cleanup_error_type']=type(error).__name__
            diagnostic=getattr(error,'diagnostic',{})
            if diagnostic:info['cleanup_error_category']=diagnostic.get('category')
            if attempt==2 or diagnostic.get('retryable') is False:return
            time.sleep(2*(attempt+1))

def terminal_reason(reason,progress,exit_code=None):
    if reason!='COLLECTOR_EXIT':return reason
    status=progress.get('status','')
    if status=='STOPPED_LOGIN_REQUIRED':return 'LOGIN_WALL'
    if status.startswith('STOPPED_HTTP_'):return 'ACCESS_DENIED'
    if status in ('STOPPED_PROXY_OR_NETWORK_FAILURE','STOPPED_PROXY_CONNECTION'):return 'NETWORK_FAILURE'
    if status=='STOPPED_BROWSER_COMMAND_TIMEOUT':return status
    if status=='STOPPED_INITIAL_PRODUCTS_TIMEOUT':return status
    if exit_code not in (None,0):return 'INFRASTRUCTURE_ERROR'
    if status in ('KEYWORD_COMPLETE','KEYWORD_LIMIT_REACHED'):return status
    return 'COLLECTOR_EXIT_WITHOUT_FINAL_PROGRESS'

def successful_step(reason,progress,exit_code):
    return (exit_code==0 and reason in ('KEYWORD_COMPLETE','KEYWORD_LIMIT_REACHED')
            and (progress.get('sales_sort') or {}).get('state')=='UI_CONFIRMED'
            and not progress.get('manual_action_required'))

def continuation_plan(query,sort,queries,all_queries=False):
    if len(queries)<2 or query not in queries:raise ValueError('Continuous trial needs two source queries')
    other_sort='recent' if sort=='sales' else 'sales'
    if all_queries:
        index=queries.index(query)
        following=queries[index+1:]+queries[:index]
        return [(query,other_sort)]+[(word,order) for word in following for order in (sort,other_sort)]
    following=queries[(queries.index(query)+1)%len(queries)]
    return [(query,other_sort),(following,sort)]

def successful_flow(plan,steps,verification_visits):
    return (not verification_visits and len(steps)==len(plan) and all(
        step.get('passed') and (step.get('query'),step.get('sort'))==target
        for step,target in zip(steps,plan)))

def keyword_switch_navigation(method,previous_query,query):
    if method not in ('current','home','empty','direct'):
        raise ValueError('Unknown keyword switch entry')
    if query==previous_query or method=='current':return None
    return {'home':(HOME,'home_search'),'empty':(EMPTY,'empty_search'),
            'direct':(EMPTY+'?'+urllib.parse.urlencode({'search_key':query}),'direct_search')}[method]

def run_followup(args,out,directory,command,query,sort,base,sid,record_page,previous_query=None):
    directory.mkdir(parents=True,exist_ok=True)
    stop=directory/'STOP'
    command=list(command)
    for key,value in {'--query':query,'--sort':sort,'--output':str(directory/'data'),'--stop-file':str(stop)}.items():
        command[command.index(key)+1]=value
    step={'query':query,'sort':sort,'started_at':time.time(),'reason':'COLLECTOR_EXIT'}
    child=None
    try:
      navigation=keyword_switch_navigation(getattr(args,'keyword_switch_entry','current'),previous_query,query)
      if navigation:
        destination,entry_mode=navigation
        step['switch_navigation']={'destination':destination,'entry_mode':entry_mode}
        command[command.index('--entry-mode')+1]=entry_mode
        driver_call(base,sid,'/url',{'url':destination})
        stable=0;ready_end=time.monotonic()+60
        while time.monotonic()<ready_end:
            page=driver_call(base,sid,'/execute/sync',{'script':PAGE_SCRIPT,'args':[]})
            blocked=record_page(page)
            if blocked:step['reason']=blocked;return step
            if (out/'STOP').exists() or time.time()>=args.deadline_timestamp:
                step['reason']='BOUNDED_TIMEOUT_OR_STOP';return step
            stable=stable+1 if page.get('ready') in ('interactive','complete') else 0
            if stable>=3:break
            time.sleep(1)
        else:step['reason']='STARTUP_READINESS_TIMEOUT';return step
      with (directory/'collector.log').open('a',encoding='utf-8') as log:
        child=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                               creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        step['collector_pid']=child.pid
        end=time.monotonic()+args.case_seconds
        while child.poll() is None:
            try:page=driver_call(base,sid,'/execute/sync',{'script':PAGE_SCRIPT,'args':[]})
            except (OSError,ValueError,KeyError):page={'unavailable':True}
            blocked=record_page(page)
            progress=read(directory/'data/progress.json')
            if blocked or progress.get('manual_action_required'):
                step['reason']=blocked or progress.get('status','MANUAL_REQUIRED');break
            if (out/'STOP').exists() or time.time()>=args.deadline_timestamp or time.monotonic()>=end:
                step['reason']='BOUNDED_TIMEOUT_OR_STOP';break
            time.sleep(3)
    finally:
        if child is not None and child.poll() is None:
            stop.write_text('Isolated followup complete','utf-8')
            try:child.wait(20)
            except subprocess.TimeoutExpired:child.terminate();child.wait(15)
        step['collector_exit_code']=child.poll() if child is not None else None
        step['result']=read(directory/'data/progress.json')
        step['reason']=terminal_reason(step['reason'],step['result'],step['collector_exit_code'])
        step['passed']=successful_step(step['reason'],step['result'],step['collector_exit_code'])
        step['finished_at']=time.time()
        save(directory/'step.json',step)
    return step

def report(out,state):
    completed=[read(p) for p in sorted((out/'cases').glob('*/case.json'))]
    state['completed_cases']=sum(c.get('status')=='FINISHED' for c in completed)
    save(out/'state.json',state)
    lines=['# 夜间入口真实试验','',f"更新时间：{dt.datetime.now().isoformat(timespec='seconds')}",'',
      '所有实验使用新环境、新 session，正式采集数据不变。出现门禁立即结束该实验，不自动处理验证。',
      '单次无门禁不代表永久保证；完整关键词成功和重复验证分别记录。','',
      '|编号|入口|关键词|排序|结果|验证码|商品|排序确认|连续排序/切词|',
      '|---|---|---|---|---|---:|---:|---|---|']
    for c in completed:
        p=c.get('result',{})
        flow='通过' if c.get('full_flow_passed') else c.get('followup_stop_reason','未执行')
        lines.append(f"|{c.get('index')}|{c.get('route')}|{c.get('query')}|{c.get('sort')}|{c.get('reason',c.get('status'))}|{c.get('verification_visits',0)}|{p.get('unique_goods',0)}|{(p.get('sales_sort') or {}).get('state','')}|{flow}|")
    atomic_write(out/'report.md','\n'.join(lines)+'\n')

def run_case(args,out,index,query,route,sort,state):
    name,start,pre,mode,submit,dwell=route
    directory=out/'cases'/f'{index:03d}-{name}-{sort}'
    directory.mkdir(parents=True,exist_ok=True)
    info={'index':index,'route':name,'query':query,'sort':sort,'startup_url':start,'pre_navigation':pre,
          'kernel_version':args.kernel_version,'fingerprint_mode':args.fingerprint_mode,
          'entry_mode':mode,'submission':submit,'started_at':time.time(),'status':'PREPARING','verification_visits':0}
    state['current_case']=str(directory)
    save(directory/'case.json',info);report(out,state)
    chain=None;child=None;profile=None;driver_owner=None
    def record_page(page):
        previous=info.get('page',{})
        blocked=gate(page)
        if blocked=='CAPTCHA' and gate(previous)!='CAPTCHA':info['verification_visits']+=1
        info['page']=page
        with (directory/'page_history.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'at':time.time(),**page},ensure_ascii=False)+'\n')
        save(directory/'case.json',info)
        return blocked
    try:
      # Permission must precede browser startup, including a configured keyword URL.
      with ProxyActivityLease(ROOT,32,purpose='collector'):
        proxy=run_background_launcher(['pwsh','-NoProfile','-File',str(ROOT/'scripts/prepare_entry_trial_proxy.ps1'),'-OutputDirectory',str(directory),'-Port',str(args.proxy_port)],cwd=ROOT,timeout=25)
        if proxy.returncode: raise RuntimeError('ISOLATED_PROXY_PREPARATION_FAILED')
        chain=json.loads(proxy.stdout.strip());chain['created']=psutil.Process(chain['pid']).create_time()
        save(directory/'proxy-state.json',chain)
        end=time.monotonic()+45
        while True:
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1',args.proxy_port))==0:break
            if time.monotonic()>end:raise RuntimeError('ISOLATED_PROXY_NOT_LISTENING')
            time.sleep(.25)
        profile_name=f'NIGHT-ENTRY-{out.name}-{index:03d}-{int(time.time())}'
        body={'group_id':'0','country':'us','name':profile_name,'tabs':[start],
          'remark':'Owned isolated entry trial; never a production worker',
          'user_proxy_config':{'proxy_soft':'other','proxy_type':'http','proxy_host':'127.0.0.1','proxy_port':str(args.proxy_port),'proxy_user':'','proxy_password':''},
          'fingerprint_config':trial_fingerprint(args.kernel_version,args.fingerprint_mode)}
        info['profile_name']=profile_name;save(directory/'case.json',info)
        try:
            data=api_call(api_base(),'/api/v2/browser-profile/create',method='POST',body=body,timeout=45)['data']
            profile=data if isinstance(data,str) else data.get('profile_id') or data.get('user_id') or data.get('id')
        except RuntimeError:
            rows=api_call(api_base(),'/api/v1/user/list?page=1&page_size=100',timeout=12).get('data',{}).get('list',[])
            recovered=[r for r in rows if r.get('name')==profile_name]
            if len(recovered)!=1: raise RuntimeError('PROFILE_CREATION_OUTCOME_UNCONFIRMED') from None
            profile=recovered[0]['user_id']
        if not profile: raise RuntimeError('PROFILE_ID_MISSING')
        info['profile_id']=profile;save(directory/'case.json',info)
        started=start_profile_with_retry(api_base(),profile)
        binding_name=f'nightly-entry-{index:03d}'
        binding_dir=ROOT/'runtime'/binding_name
        bind=run_background_launcher(['pwsh','-NoProfile','-File',str(ROOT/'scripts/bind_flower_worker.ps1'),'-WorkerName',binding_name,'-ProfileId',profile,'-WebDriverPort',str(args.driver_port),'-GeckoDriverPath',started['webdriver']],cwd=ROOT,timeout=85)
        info['binding_logs']={'stdout':str(bind.stdout_path),'stderr':str(bind.stderr_path)}
        driver=read(binding_dir/'driver.json')
        if bind.returncode:
            try:driver_owner=bound_driver_owner(driver,profile)
            except RuntimeError:pass
            raise RuntimeError('ISOLATED_BROWSER_BINDING_FAILED')
        driver_owner=bound_driver_owner(driver,profile)
        base=f'http://127.0.0.1:{args.driver_port}'
        sid=(binding_dir/'flower_session_id.txt').read_text('utf-8').strip()
        info.update(status='RUNNING',binding_directory=str(binding_dir));save(directory/'case.json',info)
        # Observe the actual startup page before advancing a route.
        for destination in [None,*pre]:
            if destination is not None:driver_call(base,sid,'/url',{'url':destination})
            stable=0;end=time.monotonic()+60
            while time.monotonic()<end:
                page=driver_call(base,sid,'/execute/sync',{'script':PAGE_SCRIPT,'args':[]})
                blocked=record_page(page)
                if blocked:info['reason']=blocked;return
                stable=stable+1 if page.get('ready') in ('interactive','complete') else 0
                if stable>=3:break
                time.sleep(1)
            else:info['reason']='STARTUP_READINESS_TIMEOUT';return
        until=time.monotonic()+dwell
        while time.monotonic()<until:
            if record_page(driver_call(base,sid,'/execute/sync',{'script':PAGE_SCRIPT,'args':[]})):
                info['reason']=gate(info['page']);return
            time.sleep(1)
        cfg=read(Path(args.config));cfg['temu'].pop('bootstrap_entry_by_worker',None)
        cfg['temu']['entry_mode']=mode;cfg['temu']['search_submit_method']=submit
        save(directory/'category.json',cfg)
        command=['node',str(ROOT/'scripts/temu_flower_collect.mjs'),'--config',str(directory/'category.json'),'--query',query,'--output',str(directory/'data'),
          '--entry-mode',mode,'--search-submit',submit,'--max-products',str(args.max_products),'--sort',sort,'--webdriver-url',base,
          '--session-id-file',str(binding_dir/'flower_session_id.txt'),'--bidi-url-file',str(binding_dir/'flower_bidi_url.txt'),'--stop-file',str(directory/'STOP')]
        with (directory/'collector.log').open('a',encoding='utf-8') as log:
          child=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
          info['collector_pid']=child.pid
          end=time.monotonic()+args.case_seconds
          while child.poll() is None:
            try:page=driver_call(base,sid,'/execute/sync',{'script':PAGE_SCRIPT,'args':[]})
            except (OSError,ValueError,KeyError):page={'unavailable':True}
            blocked=record_page(page)
            progress=read(directory/'data/progress.json')
            info['result']=progress
            if blocked or progress.get('manual_action_required'):
                info['reason']=blocked or progress.get('status','MANUAL_REQUIRED');break
            if (out/'STOP').exists() or time.time()>=args.deadline_timestamp or time.monotonic()>=end:
                info['reason']='BOUNDED_TIMEOUT_OR_STOP';break
            time.sleep(3)
          else:info['reason']='COLLECTOR_EXIT'
        initial=read(directory/'data/progress.json')
        initial_reason=terminal_reason(info.get('reason'),initial,child.poll())
        info['result']=initial
        info['reason']=initial_reason
        info['primary_verification_visits']=info['verification_visits']
        if args.continuous_search and successful_step(initial_reason,initial,child.poll()) and not info['verification_visits']:
            info['followups']=[]
            followup_plan=continuation_plan(query,sort,cfg['temu']['base_queries'],getattr(args,'continuous_all_queries',False))
            info['continuation_plan']=[{'query':word,'sort':order} for word,order in followup_plan]
            previous_query=query
            for number,(next_query,next_sort) in enumerate(followup_plan,1):
                if time.time()>=args.deadline_timestamp or (out/'STOP').exists():
                    info['followup_stop_reason']='BOUNDED_TIMEOUT_OR_STOP';break
                info['current_followup']={'query':next_query,'sort':next_sort}
                save(directory/'case.json',info)
                step=run_followup(args,out,directory/f'followup-{number:03d}',command,next_query,next_sort,base,sid,record_page,previous_query=previous_query)
                info['followups'].append(step)
                save(directory/'case.json',info)
                if not step['passed']:
                    info['followup_stop_reason']=step['reason'];break
                previous_query=next_query
            info['full_flow_passed']=successful_flow(followup_plan,info['followups'],info['verification_visits'])
    except Exception as error:
        # Avoid exception bodies containing request details or credential values.
        info['reason']='INFRASTRUCTURE_ERROR';info['error_type']=type(error).__name__
        if hasattr(error,'diagnostic'):
            info['error_category']=error.diagnostic.get('category')
            info['error_retryable']=bool(error.diagnostic.get('retryable'))
        if isinstance(error,RuntimeError) and re.fullmatch('[A-Z_]+',str(error)):info['error_code']=str(error)
    finally:
        if child is not None and child.poll() is None:
            (directory/'STOP').write_text('Isolated trial complete','utf-8')
            try:child.wait(20)
            except subprocess.TimeoutExpired:child.terminate();child.wait(15)
        info['result']=read(directory/'data/progress.json') or info.get('result',{})
        if child is not None:info['collector_exit_code']=child.poll()
        info['reason']=terminal_reason(info.get('reason'),info['result'],info.get('collector_exit_code'))
        info['full_keyword_passed']=successful_step(info.get('reason'),info['result'],info.get('collector_exit_code')) and not info.get('primary_verification_visits',info['verification_visits'])
        cleanup_profile(info,driver_owner)
        owned_terminate(chain,'proxyshare_chain_proxy.mjs')
        info.update(status='FINISHED',finished_at=time.time())
        save(directory/'case.json',info);report(out,state)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--deadline',required=True)
    parser.add_argument('--max-products',type=int,default=400)
    parser.add_argument('--case-seconds',type=int,default=1200)
    parser.add_argument('--proxy-port',type=int,default=17941)
    parser.add_argument('--driver-port',type=int,default=4497)
    parser.add_argument('--max-cases',type=int,default=96)
    parser.add_argument('--kernel-version',default='144')
    parser.add_argument('--fingerprint-mode',choices=['configured','defaults'],default='configured')
    parser.add_argument('--include-percent-startup',action='store_true')
    parser.add_argument('--query-count',type=int,default=3)
    parser.add_argument('--routes',help='Comma-separated known normal routes, in test order')
    parser.add_argument('--continuous-search',action='store_true',help='After success, confirm the other sort and then search the next source keyword in the same active profile')
    parser.add_argument('--continuous-all-queries',action='store_true',help='Continue every configured source query in both sorts within the same active profile')
    parser.add_argument('--keyword-switch-entry',choices=['current','home','empty','direct'],default='current',help='Ordinary navigation before a changed keyword; same-keyword sorting stays on the existing page')
    args=parser.parse_args();args.deadline_timestamp=dt.datetime.fromisoformat(args.deadline).timestamp()
    args.continuous_search=args.continuous_search or args.continuous_all_queries
    out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
    cfg=read(Path(args.config));queries=cfg['temu']['base_queries'][:args.query_count]
    if not queries or args.max_products<1 or args.case_seconds<1:raise ValueError('Invalid bounded trial plan')
    selected=selected_routes(queries[0],args.include_percent_startup,args.routes)
    if args.continuous_search:continuation_plan(queries[0],'sales',cfg['temu']['base_queries'],args.continuous_all_queries)
    plan={'queries':queries,'routes':[route[0] for route in selected],
          'kernel_version':args.kernel_version,'fingerprint_mode':args.fingerprint_mode,
          'max_products':args.max_products,'case_seconds':args.case_seconds,
          'proxy_port':args.proxy_port,'driver_port':args.driver_port,'max_cases':args.max_cases}
    if args.continuous_search:plan['continuous_source_queries']=cfg['temu']['base_queries']
    if args.continuous_all_queries:plan['continuous_all_queries']=True
    if args.keyword_switch_entry!='current':plan['keyword_switch_entry']=args.keyword_switch_entry
    if args.kernel_version!='144':
        inventory=api_call(api_base(),'/api/v2/browser-profile/kernels',timeout=15).get('data',{}).get('list',[])
        if not any(str(k.get('kernel_type','')).lower()=='firefox' and str(k.get('kernel'))==args.kernel_version and k.get('is_downloaded') for k in inventory):
            raise RuntimeError('Selected real browser kernel is not installed')
    with ProcessLock(out/'runner.lock'):
        state=read(out/'state.json')
        if state.get('trial_plan') is not None and state['trial_plan']!=plan:
            raise ValueError('Resume plan differs; use a new isolated output directory')
        state['trial_plan']=plan
        state.pop('finished_at',None)
        state.update(status='RUNNING',pid=os.getpid(),created=psutil.Process().create_time(),deadline=args.deadline,automation_id='automation-3',source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),configuration=str(Path(args.config).resolve()),scope='isolated normal entry trials; official pump collectors and six Luna high workers remain active')
        report(out,state)
        index=0
        for repeat in range(2):
          for query in queries:
            for route in selected_routes(query,args.include_percent_startup,args.routes):
                index+=1
                if index>args.max_cases or time.time()>=args.deadline_timestamp or (out/'STOP').exists():break
                prior=read(out/'cases'/f'{index:03d}-{route[0]}-{"sales" if repeat==0 else "recent"}'/'case.json')
                if prior.get('status')=='FINISHED':continue
                # An interrupted used profile must not be opened again.
                if prior:state['status']='INTERRUPTED_CASE_NEEDS_CLEANUP';state['current_case_index']=index;report(out,state);return
                pending=[read(p) for p in (out/'cases').glob('*/case.json')]
                if any(c.get('profile_id') and not c.get('profile_closed_and_deleted') for c in pending):
                    state['status']='WAITING_OWN_PROFILE_CLEANUP';report(out,state);return
                run_case(args,out,index,query,route,'sales' if repeat==0 else 'recent',state)
                time.sleep(5)
            if index>=args.max_cases or time.time()>=args.deadline_timestamp or (out/'STOP').exists():break
          if index>=args.max_cases or time.time()>=args.deadline_timestamp or (out/'STOP').exists():break
        state.update(status='FINISHED',finished_at=time.time());report(out,state)

if __name__=='__main__':main()
