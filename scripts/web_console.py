"""Loopback-only local selection console; keys remain in process memory and child environments."""
from __future__ import annotations
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import uuid
import time
import webbrowser
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
from urllib.parse import urlsplit
import selection
from rule_editor import draft_view as _draft_view
from selection_automation.workers import worker_count, category_worker_count
from selection_automation.frontend_market import build_frontend_market

READONLY_PROGRESS_TIMEOUT_SECONDS=1800

def stage3_batch_capacity(category,provider):
    """Expose the dispatcher's existing bound; never alter rules or dispatch."""
    from selection_automation.stage3_provider import request_batch_size
    from selection_automation.local_d1 import LocalD1InputError
    try:
        return {'ready':True,'max_pairs':request_batch_size(provider,category)}
    except LocalD1InputError as error:
        return {'ready':False,'max_pairs':0,'message':str(error)}


def draft_form_scope(category,amazon_payload=None):
    """Expose only this saved category's validated non-secret form inputs."""
    if not isinstance(category,dict):return None
    amazon=category.get('amazon') or {};temu=category.get('temu') or {}
    if not isinstance(amazon,dict) or not isinstance(temu,dict):return None
    name=category.get('category_name_zh');url=amazon.get('bestsellers_url')
    try:workers=category_worker_count(category)
    except ValueError:return None
    values=[amazon.get('target_count'),temu.get('max_products_per_keyword'),workers]
    if (not isinstance(name,str) or not name.strip() or not isinstance(url,str) or
            not all(type(value) is int and 1<=value<=limit for value,limit in zip(values,(100,100000,32)))):
        return None
    scope={'category':name.strip(),'amazon_count':values[0],'per_keyword':values[1],
           'worker_count':values[2],'bestsellers_url':url,'commercial':{}}
    commercial=category.get('commercial') or {}
    limit=commercial.get('same_limit') if isinstance(commercial,dict) else None
    if type(limit) is int and 1<=limit<=10000:scope['commercial']['same_limit']=limit
    payload=amazon_payload if isinstance(amazon_payload,dict) else {}
    route=payload.get('amazon_browser_route') or {}
    rows=payload.get('records');rows=rows if isinstance(rows,list) else []
    records_valid=(len(rows)==values[0] and all(isinstance(row,dict) and isinstance(row.get('asin'),str) and row['asin'] for row in rows)
                   and all(type(row.get('rank')) is int for row in rows)
                   and len({row['asin'] for row in rows})==values[0]
                   and sorted(row.get('rank',0) for row in rows)==list(range(1,values[0]+1)))
    if (payload.get('schema_version')=='amazon-bestsellers-frontend-v1' and payload.get('status')=='COMPLETE'
            and payload.get('details_complete') is True and records_valid
            and payload.get('category_key')==category.get('category_key') and category.get('category_key')
            and payload.get('source_url')==url and payload.get('record_count')==values[0]
            and payload.get('target_count',values[0])==values[0]
            and isinstance(route,dict) and route.get('mode') in ('AUTO','DIRECT')):
        scope['amazon_route_mode']=route['mode']
    return scope


def draft_view(category,draft_id,keyword_plan=None):
    view=_draft_view(category,draft_id)
    view['commercial']['same_operator']='lt'
    return {**view, 'worker_count':category_worker_count(category), 'form_scope':draft_form_scope(category),
            "plain_text":category.get("plain_rules_summary",""), "plain_stage1":category.get("plain_stage1",""), "plain_stage3":category.get("plain_stage3",""),
            "change_summary":category.get("plain_rules_changes",[]),
            'match_criteria':category.get('match_criteria'),
            "keyword_coverage":(keyword_plan or {}).get('coverage',{}),
            "keyword_plan":[{'worker':index%category_worker_count(category)+1,'query':row['query'],'amazon_products':len(row.get('source_asins') or []),'purpose':row.get('purpose')}
                            for index,row in enumerate((keyword_plan or {}).get('keywords') or [])]}

ROOT = Path(__file__).resolve().parents[1]
EXCEL_KINDS=('amazon','temu','layer12','layer123')


def worker_supervisor_paths(base):
    """Read canonical live workers, never retained diagnostic snapshots."""
    return sorted(path for path in (base/'temu'/'supervisors').glob('worker-*.json')
                  if re.fullmatch(r'worker-\d+(?:-new)?', path.stem))


def reconcile_stage12_watch(root, base, details):
    """Clear only an old analysis failure backed by this task's live watcher."""
    import psutil
    config_path = (base / 'stage12_config.json').resolve()
    config = selection.read(config_path, {})
    if not config.get('output_dir'):
        return
    output = Path(config['output_dir'])
    if not output.is_absolute():
        output = root / output
    owner = selection.read(output / 'stage12_watch_owner.json', {})
    try:
        process = psutil.Process(owner['pid'])
        if (not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
                or abs(process.create_time() - owner['created']) >= .01
                or Path(owner['config']).resolve() != config_path):
            return
        command = process.cmdline()
        script = (root / 'scripts/run_incremental_stage12.py').resolve()
        index = next(i for i, arg in enumerate(command) if arg.endswith('run_incremental_stage12.py')
                     and (root / arg).resolve() == script)
        config_index = command.index('--config')
        if (command[index + 1] != 'watch'
                or (root / command[config_index + 1]).resolve() != config_path):
            return
    except (KeyError, IndexError, TypeError, ValueError, StopIteration, OSError, psutil.Error):
        return
    stage = details.setdefault('阶段', {}).setdefault('stage12', {})
    stage.update(状态='运行中，已恢复连续分析', PID=owner['pid'])
    stage.pop('退出码', None)
    for field in ('阻断阶段', '人工介入阶段'):
        if isinstance(details.get(field), list):
            details[field] = [name for name in details[field] if name != 'stage12']
    if isinstance(details.get('建议动作'), dict):
        details['建议动作'].pop('stage12', None)
    if details.get('状态') == '存在阻断' and details.get('阻断阶段') == []:
        details['状态'] = '运行中'


class ConsoleError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def require_excel_open_association():
    """Check the user's effective default, without installing or changing it.

    A missing UserChoice key does not imply no default. Query the Shell's ProgID
    rather than an executable: packaged/COM handlers need not have one.
    ShellExecute/startfile returning still cannot prove a workbook was opened.
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        from ctypes import wintypes
        query = ctypes.WinDLL('Shlwapi.dll').AssocQueryStringW
        query.argtypes = [wintypes.DWORD, ctypes.c_int, wintypes.LPCWSTR,
                          wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        query.restype = ctypes.c_long
        value = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(value))
        # NOFIXUPS | INIT_IGNOREUNKNOWN | NOTRUNCATE: read only, and do not
        # mistake the generic "Unknown"/Open With fallback for an application.
        result = query(0x520, 20, '.xlsx', None, value, ctypes.byref(length)) & 0xffffffff
    except (AttributeError, OSError):
        raise ConsoleError('EXCEL_ASSOCIATION_CHECK_FAILED',
                           '无法检查 Windows 的 Excel 文件关联，请使用下载入口，或稍后重试', 500) from None
    if result == 0x80070483 or (result == 0 and value.value.casefold() in ('', 'unknown')):
        raise ConsoleError('EXCEL_APPLICATION_REQUIRED',
                           'Windows 尚未设置 .xlsx 的默认打开程序。请在“设置 → 应用 → 默认应用”中选择已安装的表格程序后重试，或下载文件后选择打开程序', 409)
    if result != 0:
        raise ConsoleError('EXCEL_ASSOCIATION_CHECK_FAILED',
                           f'无法检查 Windows 的 Excel 文件关联（0x{result:08X}），请使用下载入口，或稍后重试', 500)


def check_stage3_local_service(provider, root=None):
    """Check connectivity without submitting inference or changing dispatch state."""
    if provider['mode'] != 'local_model':
        return
    address = urlsplit(provider['base_url'])
    host = address.hostname
    port = address.port or (443 if address.scheme == 'https' else 80)
    connected = False
    try:
        with socket.create_connection((host, port), timeout=2):
            connected = True
    except OSError:
        pass
    if connected and port != 11434:
        return
    from selection_automation.local_model_service import repair_provider
    recovered = repair_provider(root or ROOT, provider)
    if recovered.get('provider'):
        provider.update(recovered['provider'])
        return
    raise ConsoleError('LOCAL_MODEL_UNAVAILABLE',
        f'本地模型服务或指定模型尚未就绪（{host}:{port}）。{recovered["message"]}'
        '本次操作未生效，原方案、暂停状态和已完成结果均保留。', 409)


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k,v in value.items() if not any(s in k.lower() for s in ('password','api_key','secret','token','proxy_user'))}
    if isinstance(value, (list, tuple)): return [clean(v) for v in value]
    return value


def _codex_pool_process_running(row, base):
    """Require PID, creation time, pool script, and this task's category to match."""
    import psutil
    try:
        process = psutil.Process(row['pid'])
        if (not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
                or abs(process.create_time() - row['created']) >= .01):
            return False
        command = process.cmdline()
        script = (ROOT / 'scripts/run_codex_visual_pool.py').resolve()
        category = (Path(base) / 'category.json').resolve()
        script_match = any(Path(arg).resolve() == script for arg in command if arg.lower().endswith('.py'))
        category_index = command.index('--category')
        category_match = Path(command[category_index + 1]).resolve() == category
        return script_match and category_match
    except (KeyError, TypeError, ValueError, OSError, psutil.Error):
        return False


def agent_review_speed(base, provider, pool, *, now=None):
    """Read completed Agent groups only; API timings never estimate Agent work."""
    now=time.time() if now is None else now
    agents=set(provider.get('agent_ids') or [])
    models=provider.get('agent_models') or {}
    efforts=provider.get('agent_efforts') or {}
    window=min(900,max(1,now-float(pool.get('created') or now-900)))
    completed=set();durations=[]
    for path in (Path(base)/'stage123/codex_pool/jobs').glob('*/job.json'):
        job=selection.read(path,{})
        agent=job.get('agent_id','codex')
        if (job.get('state')!='complete' or agent not in agents
                or job.get('observation_only') or job.get('model')!=models.get(agent,job.get('model'))
                or agent in efforts and job.get('effort')!=efforts[agent]):continue
        identities=job.get('identities') or []
        count=job.get('imported',0);start=job.get('started_at',0);end=job.get('finished_at',0)
        if (type(count) is not int or count<=0 or count!=len(identities)
                or type(start) not in (int,float) or type(end) not in (int,float)
                or not 0<start<end<=now):continue
        if end>=now-window:completed.update(tuple(identity) for identity in identities)
        if not job.get('reused_reviews_from'):durations.append((end-start)/count)
    workers=max(1,int(pool.get('workers') or provider.get('workers') or 1))
    expected=workers*3600/(sum(durations)/len(durations)) if durations else None
    return {'recent_rate_per_hour':round(len(completed)*3600/window,1),'window_seconds':round(window),
            'estimated_rate_per_hour':round(expected,1) if expected else None,
            'sample_groups':len(durations),'updated_at':now}


def codex_review_control(base, *, scan_batches=True):
    """Read independent reviewer liveness without changing paid API accounting."""
    import psutil
    base = Path(base)
    row = selection.read(base / 'stage123/codex_review/consumer.json', {})
    pool = selection.read(base / 'stage123/codex_pool/pool.json', {})
    pool_config = selection.read(base / 'stage123/codex_pool_config.json', {})
    if not row and not pool:
        return {'available': False, 'running': False}
    batches = sorted((Path(base)/'nightly_review').glob('*/review-batch.json')) if scan_batches else []
    if batches:
        from watch_codex_visual_reviews import all_batch_tasks
        tasks, errors = all_batch_tasks(Path(base), batches[0].parent)
        if tasks:
            row = {**row, 'tasks':tasks, 'expected_tasks':len(tasks),
                   'completed':sum(task['completed'] for task in tasks),
                   'target':sum(task['target'] for task in tasks),
                   'all_reviews_complete':not errors and all(task['complete'] for task in tasks),
                   'updated_at':time.time()}
    importer_running = False
    try:
        process = psutil.Process(row['pid'])
        importer_running = (process.is_running() and process.status() != psutil.STATUS_ZOMBIE
                            and abs(process.create_time() - row['created']) < .01)
    except (KeyError, TypeError, ValueError, psutil.Error):
        pass
    pool_enabled = bool(pool_config.get('enabled'))
    pool_running = pool_enabled and _codex_pool_process_running(pool, base)
    running = importer_running or pool_running
    complete = bool(row.get('all_reviews_complete'))
    if pool_running:
        workers = max(1, int(pool.get('workers') or 1))
        group_size = max(1, int(pool.get('group_size') or 12))
        model = str(pool.get('model') or 'Codex')
        effort = str(pool.get('effort') or 'high')
        agents=pool.get('agents') or []
        engine='、'.join(str(agent.get('name') or agent.get('id')) for agent in agents) if agents else f'{model} {effort}'
        phase = (f"{pool.get('phase') or '持续并行看图'} · {workers}路并行 · 每组最多{group_size}对 · {engine} · 连续接续候选")
    else:
        phase = ('Codex看图复核已完成' if complete else
                 'Codex看图复核进行中' if importer_running else 'Codex看图复核已暂停，已导入结果保留')
    if complete and not row.get('excel_current'):
        phase += '，Excel正在更新' if running else '，Excel待更新'
    result = {'available': True, 'running': running, 'phase': phase,
            'target': row.get('target', 0), 'imported': row.get('completed', 0),
            'expected_tasks': row.get('expected_tasks', 0), 'all_reviews_complete': complete,
            'excel_current': bool(row.get('excel_current')), 'updated_at': row.get('updated_at'),
            'pid': row.get('pid'), 'created': row.get('created')}
    if pool:
        result['pool'] = {'running':pool_running, 'enabled':pool_enabled,
                          'workers':pool.get('workers'), 'group_size':pool.get('group_size'),
                          'model':pool.get('model'), 'effort':pool.get('effort'),
                          'service_tier':pool.get('service_tier'),
                          'running_groups':pool.get('running'),
                          'pending_pairs':pool.get('pending'),
                          'completed_groups':pool.get('completed_groups'),
                          'agents':pool.get('agents',[]),
                          'phase':pool.get('phase'), 'errors':pool.get('errors', []),
                          'updated_at':pool.get('updated_at')}
        if scan_batches:
            from selection_automation.stage3_provider import from_category
            provider=from_category(selection.read(base/'category.json',{}))
            if provider.get('mode')=='local_agent':result['speed']=agent_review_speed(base,provider,pool)
    return result


class Console:
    def __init__(self, root=ROOT):
        self.root=root; self.lock=threading.Lock(); self.busy=False
        self.phase='尚未启动'; self.key=selection.read(root/'runtime/web-console/state.json',{}).get('category_key'); self.cancel=threading.Event(); self.progress_file=None; self.draft=None; self.draft_path=None; self.draft_key=None; self.draft_used=False; self.draft_amazon_path=None; self.draft_plan_path=None
        self.pending_scope=None; self.generation_error=None
        self.preparation_path=None; self.preparation_stage=None
        self.pending_key=None
        self.workspace_stop_pending=False
        self.workspace_stop_started=None
        self.resume_launch_active=False
        saved_view=selection.read(root/'runtime/web-console/state.json',{})
        if self.key and not saved_view.get('explicitly_restored'):
            from selection_automation.stage3_progress import controller_alive
            base=root/'runs'/self.key
            if not controller_alive(base) and not codex_review_control(base,scan_batches=False).get('running'):
                self.key=None
        self.draft_used=bool(self.key)
        from selection_automation.monitor_status import MonitorStatus
        self.monitor=MonitorStatus(self.status,lambda:(self.key,self.busy,self.phase,self.draft_used,self.stage3_busy,id(self.draft),id(self.pending_scope)))
        self.live_monitor=MonitorStatus(self.live_status,lambda:self.key,interval=1,loading_key='live_loading')
        self.history_monitor=MonitorStatus(self.history,lambda:None,interval=5,loading_key='history_loading')
        self._history_monitors={};self._history_monitor_lock=threading.Lock()
        from selection_automation.rule_provider import settings
        self.draft_provider=settings()
        self.plain_application={}
        self.stage3_busy=False; self.stage3_phase='等待手动启动'; self.stage3_key=None
        self.stage3_keys={}
        self._switch_threads=set();self._switch_lock=threading.Lock()
        self.market_busy=False
        self._frontend_cache={}; self._frontend_refreshing=set()
        self._frontend_cache_lock=threading.Lock()
        self._matching_cache={};self._matching_refreshing=set();self._matching_lock=threading.Lock()
        self._codex_review_cache={}; self._codex_review_refreshing=set()
        self._codex_review_cache_lock=threading.Lock()
        self._stage3_cache={}; self._stage3_refreshing=set()
        self._stage3_started={}
        self._stage3_readers={}
        self._stage3_cache_lock=threading.Lock()
        saved=selection.read(root/'runtime/web-console/resume-draft.json',{})
        candidate=Path(saved.get('category_path',''))
        if isinstance(saved.get('draft'),dict) and candidate.is_file() and candidate.resolve().is_relative_to((root/'runtime/web-console').resolve()):
            self.draft=saved['draft'];self.draft_path=candidate
            try:self.draft_provider=settings(**saved.get('rule_provider',{}))
            except (TypeError,ValueError):self.draft_provider=settings()
            for field,attribute in [('amazon_path','draft_amazon_path'),('plan_path','draft_plan_path')]:
                path=Path(saved.get(field,''))
                if path.is_file() and path.resolve().is_relative_to((root/'runtime/web-console').resolve()):setattr(self,attribute,path)
            self.refresh_draft_form_scope()
            self.draft_key=self.saved_credential() if self.draft_amazon_path and self.draft_plan_path and self.draft_provider==settings() else None
            self.phase=(('草稿已恢复，可继续应用或运行' if self.draft_key else '草稿已恢复，请填写API key后应用或运行') if self.draft_amazon_path and self.draft_plan_path
                        else '旧草稿缺少 Amazon 商品与关键词，请重新点击生成规则与关键词')
            if self.draft_amazon_path and self.draft_plan_path:
                try: self.refresh_draft_keywords()
                except (OSError, ValueError, KeyError, TypeError): self.phase="草稿关键词更新失败，请重新生成关键词后运行"
        saved_application=selection.read(root/'runtime/web-console/plain-application.json',{})
        if self.draft and saved_application.get('draft_id')==self.draft.get('id'):
            self.plain_application=saved_application.get('states',{})
            for item in self.plain_application.values():
                if item.get('state')=='processing':
                    item.update(state='failed',message='服务重启中断了应用，请重试；输入与原规则保留。')
        pending=selection.read(root/'runtime/web-console/pending-generation.json',{})
        if isinstance(pending.get('scope'),dict) and pending['scope'].get('category'):
            self.pending_scope=pending['scope']; self.key=None
            self.phase=pending.get('message') or '准备任务被中断，请重试'
            if self.phase.startswith('安装网页采集浏览器'):
                self.phase='上次任务在旧版自动安装阶段失败；新版已移除该步骤，请重新填写 API Key 后重试生成'
            elif self.phase=='品类模型请求返回 HTTP 403；密钥权限不足或服务拒绝访问':
                self.phase='上次模型请求返回 HTTP 403；已更新 API 客户端标识，请重新填写 Key 后重试；如仍失败将显示接口具体原因'
            elif self.phase=='连接品类模型超时或网络失败；未创建任务':
                self.phase='上次完整规则请求中断；已改为流式接收，请重新填写 Key 后沿用已定位榜单重试'
            self.generation_error={'code':pending.get('code') or 'GENERATION_INTERRUPTED','message':self.phase}
            try:recovered=self.recover_preparation(self.pending_scope,pending.get('preparation'),legacy=True)
            except ConsoleError as error:
                recovered=None;self.phase=error.message
                self.generation_error={'code':error.code,'message':error.message}
            if recovered:
                self.preparation_path,self.preparation_stage=recovered
                self.progress_file=self.preparation_path.with_suffix('.progress.json')
                self.phase='准备任务可继续：沿用已生成规则与 Amazon 断点，不重复生成规则'
                self.generation_error={'code':'PREPARATION_INTERRUPTED','message':self.phase}
        self.ensure_draft_visual_criteria()

    def recover_preparation(self,scope,manifest=None,*,legacy=False):
        """Reuse only a unique, validated local generation; never invent a draft."""
        from selection_automation.rules import validate
        from generate_category import validate_subject_rules,valid_url,CategoryGenerationError
        from selection_automation.rule_provider import settings
        from selection_automation.commercial import settings as commercial_settings
        folder=self.root/'runtime/web-console'
        explicit=isinstance(manifest,dict) and manifest.get('schema_version')=='web-preparation-v1'
        name=manifest.get('category_file') if explicit else None
        if explicit and (not isinstance(name,str) or not re.fullmatch(r'category-[0-9a-f]{32}\.json',name)):
            return None
        if explicit and manifest.get('stage') not in ('rules_ready','visual_complete','visual_mismatch'):return None
        candidates=[folder/name] if explicit else sorted(folder.glob('category-*.json')) if legacy else []
        found=[]
        for path in candidates:
            if not re.fullmatch(r'category-[0-9a-f]{32}\.json',path.name):continue
            try:
                if not path.is_file() or path.resolve().parent!=folder.resolve():continue
                category=selection.read(path,{})
                validate(category);validate_subject_rules(category)
                provider=settings(scope.get('api_base_url',''),scope.get('api_model',''))
                expected_commercial={**commercial_settings(scope.get('commercial')),'same_operator':'lt'}
                if (category.get('category_name_zh')!=scope.get('category')
                        or category['amazon']['target_count']!=scope.get('amazon_count')
                        or category['temu']['max_products_per_keyword']!=scope.get('per_keyword')
                        or category_worker_count(category)!=scope.get('worker_count')
                        or category.get('rule_generation_provider')!=provider
                        or {**commercial_settings(category.get('commercial')),'same_operator':'lt'}!=expected_commercial
                        or not valid_url(category['amazon'].get('bestsellers_url'))
                        or (scope.get('bestsellers_url') and category['amazon']['bestsellers_url']!=scope['bestsellers_url'])
                        or (self.root/'runs'/category['category_key']).exists()
                        or selection.read(path.with_suffix('.progress.json'),{}).get('stage')!='complete'):
                    continue
                if not explicit:
                    # Legacy consoles lost their pointer on Stop. Require actual
                    # collector observations from this exact generated category.
                    amazon=selection.read(path.with_suffix('.amazon.json'),{})
                    checkpoint=selection.read(path.with_suffix('.amazon.json.checkpoint.json'),{})
                    source=category['amazon']['bestsellers_url'];count=category['amazon']['target_count']
                    expected={'source_url':source,'category_key':category['category_key'],'target_count':count,'detail_standard':'LATEST_DETAIL_V2'}
                    rows=amazon.get('records') or []
                    checkpoint_rows=checkpoint.get('records') or []
                    if (amazon.get('schema_version')!='amazon-bestsellers-frontend-v1'
                            or amazon.get('category_key')!=category['category_key'] or amazon.get('source_url')!=source
                            or amazon.get('target_count')!=count or amazon.get('listing_complete') is not True or len(rows)!=count
                            or len({row.get('asin') for row in rows})!=count
                            or sorted(row.get('rank',0) for row in rows)!=list(range(1,count+1))
                            or any(not row.get('asin') or not row.get('title') or not row.get('product_url') for row in rows)
                            or checkpoint.get('schema_version')!='amazon-detail-checkpoint-v1'
                            or checkpoint.get('scope')!=expected
                            or len(checkpoint_rows)!=count
                            or any(not row.get('title') or not row.get('product_url') for row in checkpoint_rows)
                            or {(row.get('asin'),row.get('rank')) for row in checkpoint_rows}!={(row['asin'],row['rank']) for row in rows}):continue
                found.append((path,manifest.get('stage','rules_ready') if explicit else 'rules_ready'))
            except (OSError,ValueError,KeyError,TypeError,AttributeError,CategoryGenerationError):continue
        if len(found)>1:
            raise ConsoleError('PREPARATION_RECOVERY_AMBIGUOUS','有多份同范围准备产物，无法安全选择；未重新请求模型，请检查准备记录',409)
        return found[0] if found else None

    def persist_preparation(self):
        value={'scope':self.pending_scope}
        if self.preparation_path:
            value['preparation']={'schema_version':'web-preparation-v1','category_file':self.preparation_path.name,
                                  'stage':self.preparation_stage or 'rules_ready'}
        if self.generation_error:value.update(self.generation_error)
        selection.save(self.root/'runtime/web-console/pending-generation.json',value)

    @staticmethod
    def visual_preparation_complete(category,snapshot):
        """Require the existing review's full coverage of these exact records."""
        from selection_automation.match_criteria import seed,SCHEMA,FIELD_MERGE_VERSION
        from selection_automation.visual_criteria import VERSION
        catalog=category.get('match_criteria') or {};review=catalog.get('image_review') or {}
        expected=seed(category,snapshot);images=expected['image_sources'];asins=expected['source_asins']
        return bool(images and len(set(asins))==category['amazon']['target_count']
            and {row['asin'] for row in images}==set(asins)
            and catalog.get('schema_version')==SCHEMA and catalog.get('state')=='ready'
            and catalog.get('visual_version')==VERSION and catalog.get('field_merge_version')==FIELD_MERGE_VERSION
            and catalog.get('source_fingerprint')==expected['source_fingerprint']
            and catalog.get('source_asins')==asins and catalog.get('image_sources')==images
            and review.get('version')==VERSION and review.get('state')=='ready'
            and review.get('total_images')==len(images) and review.get('completed_images')==len(images)
            and review.get('total_products')==len(set(asins)) and review.get('reviewed_products')==len(set(asins))
            and review.get('missing_asins')==[] and review.get('failed_batches')==0)

    def ensure_draft_visual_criteria(self):
        """Upgrade only an unused draft; frozen runs and their choices stay intact."""
        from selection_automation.visual_criteria import VERSION
        from selection_automation.match_criteria import FIELD_MERGE_VERSION
        if (not self.draft or self.draft_used or self.busy or self.pending_scope or not self.draft_key
                or not self.draft_amazon_path or not (self.root/'scripts/extract_match_criteria.py').is_file()):return
        catalog=self.draft.get('match_criteria') or {}
        if (catalog.get('visual_version')==VERSION and catalog.get('field_merge_version')==FIELD_MERGE_VERSION
                and catalog.get('state')=='ready'):return
        self.busy=True;self.phase='正在逐件核对全部 Amazon 商品主图并合并图片条件'
        threading.Thread(target=self._refresh_visual_criteria,daemon=True).start()

    def _refresh_visual_criteria(self):
        from selection_automation.rule_provider import environment
        env={**os.environ,**environment(self.draft_provider),'DEEPSEEK_API_KEY':self.draft_key,'PYTHONUTF8':'1'}
        try:
            result=self.invoke([str(self.root/'scripts/extract_match_criteria.py'),'--category',str(self.draft_path),
                                '--amazon',str(self.draft_amazon_path)],env=env,timeout=3600)
            if result.returncode:raise ConsoleError('IMAGE_CRITERIA_FAILED','商品主图条件提取未完成，已完成批次保留')
            category=selection.read(self.draft_path,{})
            with self.lock:
                self.draft=draft_view(category,uuid.uuid4().hex,selection.read(self.draft_plan_path,{}));self.persist_draft()
            self.phase='全部商品主图已汇总，第三层默认自动判断，可直接运行' if category.get('match_criteria',{}).get('state')=='ready' else '商品主图条件尚未全部提取，已完成批次保留'
        except ConsoleError as error:self.phase=error.message
        except Exception:self.phase='商品主图条件未完成，原始资料和已完成批次保留'
        finally:self.busy=False


    def live_status(self):
        """Small operational files only; never scan reviews, publish or dispatch."""
        from selection_automation.stage3_progress import controller_alive
        from selection_automation.stage3_provider import from_category
        key=self.current_run_key()[0]
        value={'ok':True,'category_key':key}
        if not key:return value
        base=self.root/'runs'/key
        category=json.loads((base/'category.json').read_text(encoding='utf-8'))
        provider=from_category(category)
        control_path=base/'stage123/runtime_control.json'
        # A read error must not masquerade as an empty, cleared verification gate.
        runtime=json.loads(control_path.read_text(encoding='utf-8')) if control_path.exists() else {}
        if not isinstance(runtime,dict):raise ValueError('invalid dispatch control')
        value.update(stage3_runtime=runtime,stage3_provider=provider,
            stage3_batch_capacity=stage3_batch_capacity(category,provider),
            stage3_concurrency=selection.read(base/'stage123/concurrency.json',{}),
            review=codex_review_control(base,scan_batches=False),
            running=controller_alive(base),
            enabled=(base/'stage123/paid_enabled.json').is_file() and not (base/'stage123/STOP').exists(),
            controller=selection.read(base/'stage123/controller.json',{}))
        from runtime_overview import stopped_state
        value.update(stopped_state(base,runtime,task_running=bool(self.owner()) or self.busy,
            stage3_running=value['running'] or bool(value['review'].get('running')) or self.stage3_busy))
        if value['stage3_stopped']:value['enabled']=False
        return clean(value)

    def monitor_status(self):
        """Overlay current dispatch state even when a full report is still reading."""
        state=self.monitor.get()
        live=self.live_monitor.get()
        state['live_sample']=live['status_sample']
        if live.get('live_loading') or live.get('category_key')!=state.get('category_key'):
            state['live_loading']=True
            return state
        for field in ('stage3_runtime','stage3_provider','stage3_concurrency','stage3_batch_capacity'):
            if field in live:state[field]=live[field]
        if not live.get('category_key'):return state
        state['task_stopped']=bool(live.get('task_stopped'))
        control=state.setdefault('stage3_control',{})
        old_review=control.get('codex_review') or {}
        review=live['review']
        # Validated pair totals retain their independent result snapshot.
        control.update(running=live['running'],enabled=live['enabled'],stopped=bool(live.get('stage3_stopped')),
            error=bool(live['controller'].get('error')),error_code=live['controller'].get('error_code'),
            can_stop=live['enabled'] or live['running'] or bool(review.get('running')),
            codex_review={**old_review,**{k:review[k] for k in ('available','running','phase','pid','created') if k in review},
                          'pool':review.get('pool',{})})
        control['phase']=live['controller'].get('phase') or '等待手动启动'
        if control['stopped']:control.update(enabled=False,can_stop=False,phase='已停止，已完成结果和历史记录已保留')
        from runtime_overview import stage3_display,local_stream_activity
        state['stage3_activity']=(local_stream_activity(self.root/'runs'/live['category_key']/'stage123',
            live['controller'].get('created',0)) if live['running'] and
            state['stage3_provider'].get('mode')=='local_model' else {})
        state.setdefault('details',{}).setdefault('阶段',{})['stage3']=stage3_display(
            control,state['stage3_runtime'],state['stage3_provider'])
        if state['task_stopped']:
            state.update(phase='已停止',severity='neutral')
        return state

    def owner(self): return selection.live_owner()

    def operator_view(self):
        from selection_automation.operations import build_shortlist,BEIJING,operator_id
        from selection_automation.operator_feedback import read_feedback
        from selection_automation.daily_collection import collection_status
        from datetime import datetime
        key,_=self.current_run_key()
        rows=selection.read(self.root/'runtime/operations/subscriptions.json',{'subscriptions':[]})['subscriptions']
        if not key:return {'ok':True,'snapshot':{'status':'NO_TASK','products':[],'message':'先准备一个品类任务'},'subscription':None}
        category=selection.read(self.root/'runs'/key/'category.json',{})
        source_key=category.get('daily_source_key',key)
        subscription=next((r for r in rows if r['category_key']==source_key),None)
        options={k:subscription[k] for k in ('same_limit','min_sales','new_days','require_new')} if subscription else {}
        snapshot=build_shortlist(self.root,category,**options)
        day=datetime.now(BEIJING).date().isoformat()
        delivery=selection.read(self.root/'runtime/operations/deliveries'/operator_id(subscription['label'])/(day+'.json'),{}) if subscription else {}
        return clean({'ok':True,'snapshot':snapshot,'subscription':subscription,'delivery':delivery,
                      'feedback':read_feedback(self.root,subscription['label']) if subscription else {},
                      'collection':collection_status(self.root,source_key)})

    def operator_feedback(self,body):
        from selection_automation.operator_feedback import save_feedback
        view=self.operator_view();subscription=view.get('subscription')
        if not subscription:raise ConsoleError('NOT_READY','请先保存当前运营的订阅')
        asin=body.get('asin');products=list(view['snapshot'].get('products',[]))
        if view.get('delivery',{}).get('product'):products.append(view['delivery']['product'])
        product=next((p for p in products if p.get('asin')==asin),None)
        prior=view.get('feedback',{}).get(asin,{}) if isinstance(asin,str) else {}
        if not product and not prior:raise ConsoleError('INVALID_INPUT','商品不在当前候选或本运营的跟进记录中，请刷新结果')
        source=(product or prior).get('source_run_key')
        try:row=save_feedback(self.root,subscription['label'],{
            'asin':asin,'state':body.get('state'),'reason':body.get('reason',''),'source_run_key':source})
        except ValueError as error:raise ConsoleError('INVALID_INPUT',str(error)) from None
        return {'ok':True,'feedback':row,'message':'跟进状态已保存。询价中、打样中、暂缓和不做的商品不再作为新的每日推荐；改回未处理可重新参加筛选。'}

    def operator_subscribe(self,body):
        from selection_automation.operations import save_subscription,ensure_daily_service
        key,_=self.current_run_key()
        if not key:raise ConsoleError('NOT_READY','请先创建品类任务')
        key=selection.read(self.root/'runs'/key/'category.json',{}).get('daily_source_key',key)
        if body.get('category_key') not in (None,key):raise ConsoleError('INVALID_INPUT','订阅品类与当前任务不一致')
        try:row=save_subscription(self.root,{**body,'category_key':key})
        except ValueError as error:raise ConsoleError('INVALID_INPUT',str(error)) from None
        ensure_daily_service(self.root)
        return {'ok':True,'subscription':row,'message':'订阅已保存；每天按北京时间生成一个本地运营候选，无合格证据时不凑数。'}

    def registered_owner(self, key, timeout=15):
        """Wait for the supervisor, not the Windows Python launcher, to register."""
        deadline=time.monotonic()+timeout
        while not self.cancel.is_set():
            owner=self.owner()
            registered=selection.read(self.root/'runtime/active.json',{})
            if (owner and owner.get('category_key')==key and
                    owner.get('pid')==registered.get('pid') and
                    owner.get('created')==registered.get('created') and
                    registered.get('category_key')==key):
                return owner
            if time.monotonic()>=deadline: return None
            time.sleep(.2)
        return None

    def refresh_draft_keywords(self):
        from pipeline import build_category_keyword_plan, keyword_coverage
        category=selection.read(self.draft_path)
        if not self.draft_used:
            from selection_automation.commercial import settings as commercial_settings
            policy={**commercial_settings(category.get('commercial')),'same_operator':'lt'}
            if category.get('commercial')!=policy:
                category['commercial']=policy;selection.save(self.draft_path,category)
            self.draft['commercial']=policy
        from selection_automation.match_criteria import enabled,preserve,attach
        if enabled(category) and not self.draft_used:
            organized=preserve(category['match_criteria'])
            if organized!=category['match_criteria']:
                attach(category,organized);selection.save(self.draft_path,category)
                self.draft['match_criteria']=organized
        rows=selection.read(self.draft_amazon_path,{}).get('records',[])
        plan=build_category_keyword_plan(rows,category)
        payload={'keywords':plan,'coverage':keyword_coverage(rows,plan,category)}
        selection.save(self.draft_plan_path,payload)
        self.draft.update(keyword_coverage=payload['coverage'],keyword_plan=[
            {'worker':i%category_worker_count(category)+1,'query':p['query'],
             'amazon_products':len(p.get('source_asins',[])),'purpose':p.get('purpose')}
            for i,p in enumerate(plan)])
        self.persist_draft()

    def invoke(self, argv, *, env=None, timeout=30, cancellable=False):
        if (env is not None or cancellable) and self.cancel.is_set():raise ConsoleError('CANCELLED','已停止')
        if argv and argv[0]=='node':
            if any(Path(arg).name=='collect_amazon_search_frontend.mjs' for arg in argv):
                from selection_automation.browser_transport import amazon_environment
                env={**(env if env is not None else os.environ),**amazon_environment(self.root,mode=(env or {}).get('AMAZON_BROWSER_ROUTE_MODE'))}
        python=self.root/'.venv/Scripts/python.exe'
        provider_runtime=str(python) if argv and Path(argv[0]).name=='collect_market_evidence.py' and python.is_file() else sys.executable
        command=([shutil.which('node') or 'node',*argv[1:]] if argv and argv[0]=='node'
                 else [provider_runtime,*argv])
        child=subprocess.Popen(command,cwd=self.root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+timeout
        while child.poll() is None:
            if (env is not None or cancellable) and self.cancel.is_set():
                selection.terminate_owned(child)
                raise ConsoleError('CANCELLED','已停止')
            if time.monotonic()>deadline:
                selection.terminate_owned(child)
                raise ConsoleError('TIMEOUT','当前阶段超时，请检查网络或本地服务')
            time.sleep(.2)
        return child

    def saved_credential(self):
        key=os.environ.get('DEEPSEEK_API_KEY','').strip()
        if not key:
            try:
                from selection_automation.secrets import load
                key=load(self.root/'runtime/secrets/deepseek.dpapi').get('key','').strip()
            except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
                return None
        return key if key and len(key)<=4096 and not any(ord(c)<32 for c in key) else None

    def start(self, body):
        if set(body)-{'worker_count','bestsellers_url','commercial','api_base_url','api_model','amazon_route_mode'}!={'category_name','amazon_count','per_keyword','api_key'}:
            raise ConsoleError('INVALID_INPUT','请填写品类、Amazon数量、每关键词数量及API key')
        body=dict(body)
        route=body.get('amazon_route_mode','AUTO')
        if route not in ('AUTO','DIRECT'):raise ConsoleError('INVALID_INPUT','Amazon 浏览器连接方式无效')
        body['amazon_route_mode']=route
        from selection_automation.rule_provider import settings, DEFAULT_URL
        try:provider=settings(body.get('api_base_url',''),body.get('api_model',''))
        except ValueError as error:raise ConsoleError('INVALID_INPUT',str(error)) from None
        body.update(api_base_url=provider['base_url'],api_model=provider['model'])
        from selection_automation.commercial import settings as commercial_settings
        try:body['commercial']={**commercial_settings(body.get('commercial') or {'same_limit':5}),'same_operator':'lt'}
        except ValueError as error:raise ConsoleError('INVALID_INPUT',str(error)) from None
        from generate_category import valid_url,approved_category
        listing=body.get('bestsellers_url','')
        if not isinstance(listing,str) or (listing.strip() and not valid_url(listing.strip())):
            raise ConsoleError('INVALID_LISTING_URL','请填写 Amazon 美国站 Best Sellers 榜单链接，不支持商品详情或搜索链接')
        body['bestsellers_url']=listing.strip()
        name=body['category_name']; key=body['api_key']
        if not isinstance(name,str) or not name.strip() or len(name)>120 or any(ord(c)<32 for c in name):
            raise ConsoleError('INVALID_INPUT','品类名称无效')
        same_pending=(self.pending_scope and self.pending_scope.get('category')==name.strip() and
            self.pending_scope.get('api_base_url')==provider['base_url'] and self.pending_scope.get('api_model')==provider['model'])
        if same_pending and not body['bestsellers_url']:
            body['bestsellers_url']=self.pending_scope.get('bestsellers_url','')
        cached=approved_category(name,body['bestsellers_url']) is not None
        if isinstance(key,str) and not key.strip():
            if same_pending and self.pending_key:
                key=self.pending_key
            elif provider['base_url']!=DEFAULT_URL:
                raise ConsoleError('MISSING_CREDENTIAL','使用其他 API 服务时，请输入该服务的 Key；不会转发本机保存的 DeepSeek 密钥')
            else:key=self.saved_credential() or ''
            if not key and not cached: raise ConsoleError('MISSING_CREDENTIAL','本机没有同品类已运行规则及可用 API key，请在网页输入后重试')
            body['api_key']=key
        try: count=worker_count(body.get('worker_count',4))
        except ValueError as exc: raise ConsoleError('INVALID_INPUT',str(exc)) from None
        if not isinstance(name,str) or not name.strip() or len(name)>120 or any(ord(c)<32 for c in name):
            raise ConsoleError('INVALID_INPUT','品类名称无效')
        if not isinstance(key,str) or (not key.strip() and not cached) or len(key)>4096 or any(ord(c)<32 for c in key):
            raise ConsoleError('INVALID_INPUT','API key无效')
        for field,maximum in [('amazon_count',100),('per_keyword',100000)]:
            if type(body[field]) is not int or not 1<=body[field]<=maximum:
                raise ConsoleError('INVALID_INPUT',f'{field}数量无效')
        scope={'category':name.strip(),'amazon_count':body['amazon_count'],'commercial':body['commercial'],
               'per_keyword':body['per_keyword'],'worker_count':count,'bestsellers_url':body['bestsellers_url'],
               'api_base_url':provider['base_url'],'api_model':provider['model'],'amazon_route_mode':route}
        with self.lock:
            if self.busy or self.stage3_busy or self.owner(): raise ConsoleError('ALREADY_RUNNING','已有任务运行，请先停止',409)
            business_scope=lambda value:{k:v for k,v in (value or {}).items() if k!='amazon_route_mode'}
            manifest=selection.read(self.root/'runtime/web-console/pending-generation.json',{}).get('preparation') if business_scope(self.pending_scope)==business_scope(scope) else None
            recovery=self.recover_preparation(scope,manifest,legacy=True)
            if manifest and not recovery:
                raise ConsoleError('PREPARATION_RECOVERY_INVALID','准备断点校验失败；原文件保留，未重新请求模型，请检查准备记录',409)
            self.busy=True; self.phase='生成品类配置并核实榜单中'; self.key=None; self.cancel.clear()
            self.workspace_stop_pending=False;self.workspace_stop_started=None
            self.pending_key=None
            self.plain_application={}
            self.progress_file=None; self.generation_error=None
            self.pending_scope=scope
            self.preparation_path,self.preparation_stage=recovery or (None,None)
            self.persist_preparation()
            previous_draft=({field:getattr(self,field) for field in
                            ('draft','draft_path','draft_amazon_path','draft_plan_path','draft_provider')}
                            if self.draft and not self.draft_used else None)
            self.draft=None; self.draft_key=None; self.draft_path=None; self.draft_used=False; self.draft_amazon_path=None; self.draft_plan_path=None
            thread=threading.Thread(target=self._start, args=(dict(body),previous_draft),daemon=True)
            thread.start()
        return {'ok':True,'phase':self.phase,'preparation_resumed':bool(recovery)}

    def _start(self, body, previous_draft=None):
        from selection_automation.rule_provider import settings, environment
        provider=settings(body.pop('api_base_url',''),body.pop('api_model',''))
        supplied_key=body.pop('api_key').strip()
        env={**os.environ,**environment(provider),'DEEPSEEK_API_KEY':supplied_key,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
        env['AMAZON_BROWSER_ROUTE_MODE']=body.pop('amazon_route_mode','AUTO')
        env.pop('AMAZON_CONTINUATION_RECHECK',None)
        if self.preparation_path:env['AMAZON_CONTINUATION_RECHECK']='1'
        try:
            folder=self.root/'runtime'/'web-console'; folder.mkdir(parents=True,exist_ok=True)
            output=self.preparation_path or folder/f'category-{uuid.uuid4().hex}.json'
            env['SELECTION_MODEL_DRAFT_CONTEXT'] = output.stem
            self.progress_file=output.with_suffix('.progress.json')
            if not self.preparation_path:
                generated=self.invoke([str(self.root/'scripts/generate_category.py'),'--category-name',body['category_name'],
                    '--amazon-count',str(body['amazon_count']),'--per-keyword',str(body['per_keyword']),'--output',str(output),'--progress-file',str(self.progress_file),
                    *(['--bestsellers-url',body['bestsellers_url']] if body.get('bestsellers_url') else [])],env=env,timeout=1800)
                if generated.returncode: raise ConsoleError('GENERATION_FAILED', self.generation_progress().get('message') or '品类生成或榜单核实失败，请检查网络和API授权后重试')
                category=json.loads(output.read_text('utf-8'))
                category['rule_generation_provider']=provider
                category['temu']['worker_count']=worker_count(body.get('worker_count',4))
                if 'commercial' in body:category['commercial']=body['commercial']
                from selection_automation.commercial import settings as commercial_settings
                category['commercial']={**commercial_settings(category.get('commercial')),'same_operator':'lt'}
                if not re.fullmatch('[a-z][a-z0-9_]{0,63}',category['category_key']): raise ValueError('invalid generated key')
                category['category_key']=category['category_key'][:48]+'_'+uuid.uuid4().hex[:12]
                selection.save(output,category)
                self.preparation_path=output;self.preparation_stage='rules_ready';self.persist_preparation()
            category=selection.read(output,{})
            self.phase='正在采集 Amazon 商品标题、详情参数并提炼关键词'
            self.draft_amazon_path,self.draft_plan_path=self.collect_amazon_and_plan(output,category,env)
            self.phase='正在逐件核对全部 Amazon 商品主图并合并图片条件'
            category=selection.read(output,category);snapshot=selection.read(self.draft_amazon_path,{})
            if not self.visual_preparation_complete(category,snapshot):
                if self.preparation_stage in ('visual_complete','visual_mismatch'):
                    self.preparation_stage='visual_mismatch';self.persist_preparation()
                    raise ConsoleError('VISUAL_CHECKPOINT_MISMATCH','已保存的主图提取与当前 Amazon 商品证据不匹配；原记录保留，未自动重发付费请求')
                extracted=self.invoke([str(self.root/'scripts/extract_match_criteria.py'),'--category',str(output),
                    '--amazon',str(self.draft_amazon_path)],env=env,timeout=3600)
                category=selection.read(output,category)
                if extracted.returncode or not self.visual_preparation_complete(category,snapshot):
                    raise ConsoleError('VISUAL_PREPARATION_INCOMPLETE','商品主图提取尚未完成；已完成批次和请求记录保留，请检查后继续准备，未知请求不会自动重复计费')
            self.preparation_stage='visual_complete';self.persist_preparation()
            category=selection.read(output,category)
            plan=selection.read(self.draft_plan_path)
            # Save usable generated rules before the optional explanation.
            with self.lock:
                self.draft=draft_view(category,uuid.uuid4().hex,plan)
                self.draft_path=output; self.draft_key=env['DEEPSEEK_API_KEY']; self.draft_provider=provider; self.draft_used=False
                self.persist_draft()
            self.phase='正在生成通俗中文规则说明'
            if not category.get('approved_rule_source') and not (
                category.get('rule_summary_source')=='generated_conditions_local_summary_v1'
                and all(category.get(k) for k in ('plain_stage1','plain_stage3','plain_rules_summary'))):
                output=self.explain_file(output,'',env)
            category=json.loads(output.read_text('utf-8'))
            with self.lock:
                if self.cancel.is_set(): self.phase='已停止'; return
                self.draft=draft_view(category,uuid.uuid4().hex,plan)
                self.draft_path=output; self.draft_key=env['DEEPSEEK_API_KEY']; self.draft_used=False
                self.persist_draft()
                self.phase='规则与 Amazon 关键词已生成，请编辑确认后点击运行'
        except ConsoleError as exc:
            self.phase=exc.message
            if exc.code=='CANCELLED':self.cancel.set()
            else:self.generation_error={'code':exc.code,'message':exc.message}
        except Exception:
            self.phase='启动失败，请检查网络、本地依赖及配置'
            self.generation_error={'code':'GENERATION_FAILED','message':self.phase}
        finally:
            if self.cancel.is_set() and self.key:
                try: self.invoke([str(self.root/'scripts/selection.py'),'stop','--category-key',self.key])
                except Exception: self.phase='停止请求失败，请检查任务状态'
            env.pop('DEEPSEEK_API_KEY',None)
            with self.lock:
                if self.draft is None and previous_draft:
                    for field,value in previous_draft.items():setattr(self,field,value)
                    # Preserve reviewed content, never inherit the prior draft key.
                    self.draft_key=None;self.persist_draft()
                if self.cancel.is_set() and self.pending_scope:
                    self.phase='准备已停止，已生成规则和 Amazon 断点已保留，可继续准备'
                    self.generation_error={'code':'PREPARATION_STOPPED','message':self.phase}
                if self.generation_error:
                    progress=selection.read(self.progress_file,{}) if self.progress_file else {}
                    if self.pending_scope and progress.get('failed_stage')=='generating':
                        from generate_category import valid_url
                        route=progress.get('route',[])
                        resolved=route[-1].get('url') if route and isinstance(route[-1],dict) else None
                        if valid_url(resolved):self.pending_scope['bestsellers_url']=resolved
                    if not self.cancel.is_set():self.pending_key=supplied_key
                    self.persist_preparation()
                else:
                    (self.root/'runtime/web-console/pending-generation.json').unlink(missing_ok=True)
                    self.preparation_path=None;self.preparation_stage=None
                self.busy=False

    def refresh_draft_form_scope(self):
        if not isinstance(self.draft,dict) or not self.draft_path:return
        category=selection.read(self.draft_path,{})
        if not isinstance(category,dict):self.draft.pop('form_scope',None);return
        amazon=category.get('amazon');amazon=amazon if isinstance(amazon,dict) else {}
        if (category.get('category_name_zh')!=self.draft.get('category_name') or
                self.draft.get('amazon_url') not in (None,amazon.get('bestsellers_url'))):
            self.draft.pop('form_scope',None);return
        payload=selection.read(self.draft_amazon_path,{}) if self.draft_amazon_path else None
        scope=draft_form_scope(category,payload)
        if scope is not None:self.draft['form_scope']=scope
        else:self.draft.pop('form_scope',None)

    def persist_draft(self):
        self.refresh_draft_form_scope()
        selection.save(self.root/'runtime/web-console/resume-draft.json',{'draft':self.draft,'category_path':str(self.draft_path),
                       'rule_provider':self.draft_provider,
                       'amazon_path':str(self.draft_amazon_path) if self.draft_amazon_path else '',
                       'plan_path':str(self.draft_plan_path) if self.draft_plan_path else ''})

    def collect_amazon_and_plan(self,category_path,category,env):
        from pipeline import build_category_keyword_plan, keyword_coverage
        amazon=category_path.with_suffix('.amazon.json'); plan_path=category_path.with_suffix('.keywords.json')
        amazon_env={key:value for key,value in env.items() if key!='DEEPSEEK_API_KEY'}
        result=self.invoke(['node',str(self.root/'scripts/collect_amazon_search_frontend.mjs'),'--config',str(category_path),
                            '--output',str(amazon)],env=amazon_env,timeout=max(1800,45*category['amazon']['target_count']))
        payload=selection.read(amazon,{})
        rows=payload.get('records') or []
        count=category['amazon']['target_count']
        if (len(rows)!=count or len({r.get('asin') for r in rows})!=count or
                sorted(r.get('rank',0) for r in rows)!=list(range(1,count+1)) or
                payload.get('source_url')!=category['amazon']['bestsellers_url'] or payload.get('category_key')!=category['category_key']):
            raise ConsoleError('AMAZON_COLLECTION_FAILED','Amazon 榜单前N名尚未采齐，关键词未生成；请检查网络后重试')
        if result.returncode not in (0,2): raise ConsoleError('AMAZON_COLLECTION_FAILED','Amazon 采集失败；请检查网络后重试')
        if (result.returncode!=0 or payload.get('status')!='COMPLETE' or payload.get('details_complete') is not True):
            raise ConsoleError('AMAZON_DETAILS_INCOMPLETE','Amazon 详情尚未采齐或遇到访问门禁；已生成规则和商品断点已保留，请处理提示后继续准备')
        # Use the collector's shared evidence contract, including image proof;
        # listing counts and a claimed COMPLETE flag cannot authorize next steps.
        verified=self.invoke(['node','--input-type=module','-e',
            "import fs from 'node:fs'; import {detailComplete} from './scripts/collection_checkpoint.mjs'; "
            "const p=JSON.parse(fs.readFileSync(process.argv[1],'utf8')); process.exit(p.records.every(detailComplete)?0:2);",
            str(amazon)],env=amazon_env,timeout=30)
        if verified.returncode:
            raise ConsoleError('AMAZON_DETAILS_INCOMPLETE','Amazon 详情和主图证据未通过完整检查；原规则与断点已保留，不能继续生成关键词或主图规则')
        keywords=build_category_keyword_plan(rows,category)
        if not keywords or any(not row.get('source_asins') and row.get('purpose')!='CATEGORY_SAMPLE' for row in keywords):
            raise ConsoleError('KEYWORD_PLAN_FAILED','Amazon 标题没有生成可追溯的关键词，请检查榜单商品')
        selection.save(plan_path,{'keywords':keywords,'coverage':keyword_coverage(rows,keywords,category)})
        return amazon,plan_path

    def accept_key(self, body):
        if 'api_key' in body:
            key=body['api_key']
            if not isinstance(key,str) or not key.strip() or len(key)>4096 or any(ord(c)<32 for c in key): raise ConsoleError('INVALID_INPUT','API key无效')
            self.draft_key=key.strip()

    def explain_file(self, source, instruction, env, scope="all"):
        token=uuid.uuid4().hex; folder=source.parent
        instruction_file=folder/f'plain-{token}.txt'; output=folder/f'plain-{token}.json'; error_file=folder/f'plain-{token}.error.json'
        instruction_file.write_text(instruction,encoding='utf-8')
        result=self.invoke([str(self.root/'scripts/plain_rules.py'),'--input',str(source),'--instruction-file',str(instruction_file),'--scope',scope,
                           '--output',str(output),'--error-file',str(error_file)],env=env,timeout=900)
        if result.returncode:
            error=selection.read(error_file,{})
            raise ConsoleError(error.get('code','PLAIN_RULES_FAILED'),error.get('message','规则说明处理失败'))
        return output

    def plain_rules(self, body):
        if body.get('scope','all') not in ('all','stage1','stage3'): raise ConsoleError('INVALID_INPUT','规则层无效')
        if set(body)-{'draft_id','instruction','api_key','scope','api_base_url','api_model'} or not {'draft_id','instruction'}<=set(body) or not isinstance(body['instruction'],str) or len(body['instruction'])>12000:
            raise ConsoleError('INVALID_INPUT','请提供草稿编号和中文要求')
        with self.lock:
            if self.busy or self.owner(): raise ConsoleError('ALREADY_RUNNING','请等待当前操作完成',409)
            if not self.draft or body['draft_id']!=self.draft['id'] or self.draft_used:
                raise ConsoleError('STALE_DRAFT','草稿已失效，请重新生成',409)
            from selection_automation.rule_provider import settings
            try:provider=settings(body.get('api_base_url',self.draft_provider['base_url']),body.get('api_model',self.draft_provider['model']))
            except ValueError as error:raise ConsoleError('INVALID_INPUT',str(error)) from None
            if provider['base_url']!=self.draft_provider['base_url'] and not body.get('api_key'):
                raise ConsoleError('KEY_REQUIRED','更换 API 服务地址时，请同时填写对应 Key',409)
            self.accept_key(body)
            self.draft_provider=provider
            if not self.draft_key: raise ConsoleError('KEY_REQUIRED','请重新填写API key，已保留草稿无需重新生成',409)
            picture_conditions=(selection.read(self.draft_path,{}) or {}).get('match_criteria') or {}
            if picture_conditions.get('visual_version') and picture_conditions.get('state')!='ready':
                raise ConsoleError('IMAGE_CRITERIA_INCOMPLETE','全部商品主图条件尚未提取完成，已完成批次保留',409)
            scope=body.get('scope','all')
            self.plain_application[scope]={'state':'processing','message':'正在应用，请等待处理结果。','updated_at':time.time()}
            self.save_plain_application()
            self.busy=True;self.cancel.clear();self.phase='正在解释或应用中文规则要求'
            threading.Thread(target=self._plain_rules,args=(body['instruction'],body.get('scope','all')),daemon=True).start()
        return {'ok':True,'phase':self.phase}

    def save_plain_application(self):
        selection.save(self.root/'runtime/web-console/plain-application.json',
                       {'draft_id':(self.draft or {}).get('id'),'states':self.plain_application})

    def select_match_criteria(self,body):
        if set(body)!={'draft_id','revision','selected_ids'}:raise ConsoleError('INVALID_INPUT','请选择当前草稿中的匹配条件')
        with self.lock:
            if self.busy or self.owner() or self.draft_used:raise ConsoleError('ALREADY_RUNNING','匹配条件只能在开始当前任务之前修改',409)
            if not self.draft or body['draft_id']!=self.draft['id']:raise ConsoleError('STALE_DRAFT','草稿已更新，请刷新后重试',409)
            from selection_automation.match_criteria import select,attach
            category=selection.read(self.draft_path,{})
            try:catalog=select(category.get('match_criteria'),body['selected_ids'],body['revision'])
            except ValueError as error:raise ConsoleError('INVALID_INPUT',str(error)) from None
            attach(category,catalog);selection.save(self.draft_path,category)
            self.draft=draft_view(category,self.draft['id'],selection.read(self.draft_plan_path,{}));self.persist_draft()
            return {'ok':True,'phase':'条件已保存：文字用于第一层，图片用于第三层','match_criteria':catalog}

    def _plain_rules(self, instruction, scope="all"):
        from selection_automation.rule_provider import environment
        env={**os.environ,**environment(self.draft_provider),'DEEPSEEK_API_KEY':self.draft_key,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
        try:
            output=self.explain_file(self.draft_path,instruction,env,scope)
            category=json.loads(output.read_text('utf-8'))
            category['rule_generation_provider']=self.draft_provider
            selection.save(output,category)
            with self.lock:
                if self.cancel.is_set():
                    self.phase='已停止'
                    self.plain_application[scope]={'state':'failed','message':'应用已取消，原规则保留。','updated_at':time.time()}
                    self.save_plain_application()
                    return
                self.draft=draft_view(category,uuid.uuid4().hex,selection.read(self.draft_plan_path,{}));self.draft_path=output
                self.persist_draft()
                self.phase='规则已更新，请检查三层规则后手工运行' if instruction.strip() else '规则说明已生成，请检查后手工运行'
                self.plain_application[scope]={'state':'applied','message':'要求已成功应用。' if instruction.strip() else '当前规则说明已生成。','updated_at':time.time()}
                self.save_plain_application()
        except ConsoleError as exc:
            self.phase=exc.message
            with self.lock:
                self.plain_application[scope]={'state':'failed','message':exc.message,'updated_at':time.time()}
                self.save_plain_application()
        except Exception:
            self.phase='规则处理失败，原草稿已保留'
            with self.lock:
                self.plain_application[scope]={'state':'failed','message':self.phase,'updated_at':time.time()}
                self.save_plain_application()
        finally:
            env.pop('DEEPSEEK_API_KEY',None)
            with self.lock:self.busy=False

    def run_draft(self, body):
        if body.get('execution_mode')=='collection':
            if set(body)-{'draft_id','execution_mode','worker_count','api_key'} or not body.get('draft_id'):
                raise ConsoleError('INVALID_INPUT','请提交当前采集草稿')
            try: worker_count(body.get('worker_count',4))
            except ValueError as exc: raise ConsoleError('INVALID_INPUT',str(exc)) from None
            with self.lock:
                if self.busy or self.stage3_busy or self.owner(): raise ConsoleError('ALREADY_RUNNING','任务正在执行',409)
                if not self.draft or body['draft_id']!=self.draft['id'] or self.draft_used:
                    raise ConsoleError('STALE_DRAFT','草稿已失效，请重新生成',409)
                self.accept_key(body)
                self.busy=True; self.cancel.clear(); self.phase='正在启动独立数据采集'
                threading.Thread(target=self._run_draft,args=({k:v for k,v in body.items() if k!='api_key'},),daemon=True).start()
            return {'ok':True,'phase':self.phase}
        required={'draft_id','stage1_text','stage2_threshold','stage3_text'}
        if not required<=set(body) or set(body)-required-{'stage3_modules_json','api_key','commercial','worker_count'}:
            raise ConsoleError('INVALID_INPUT','请提交草稿编号和三层规则')
        import math
        if 'worker_count' in body:
            try: worker_count(body['worker_count'])
            except ValueError as exc: raise ConsoleError('INVALID_INPUT',str(exc)) from None
        if 'commercial' in body:
            from selection_automation.commercial import settings
            try: settings(body['commercial'])
            except ValueError as exc: raise ConsoleError('INVALID_INPUT', str(exc)) from None
        threshold=body['stage2_threshold']
        if type(threshold) not in (int,float) or not math.isfinite(threshold) or not 0<=threshold<=100:
            raise ConsoleError('INVALID_INPUT','第二层阈值必须为0至100')
        for field in ('stage1_text','stage3_text'):
            if not isinstance(body[field],str) or not body[field].strip() or len(body[field])>30000: raise ConsoleError('INVALID_INPUT','规则文本无效')
        if 'stage3_modules_json' in body and (not isinstance(body['stage3_modules_json'],str) or len(body['stage3_modules_json'])>60000): raise ConsoleError('INVALID_INPUT','视觉模块JSON无效')
        with self.lock:
            self.accept_key(body)
            if self.busy or self.stage3_busy or self.owner(): raise ConsoleError('ALREADY_RUNNING','任务正在执行',409)
            if not self.draft or body['draft_id']!=self.draft['id'] or self.draft_used:
                raise ConsoleError('STALE_DRAFT','草稿已失效，请重新生成',409)
            if not self.draft_key: raise ConsoleError('KEY_REQUIRED','请重新填写API key，已保留草稿无需重新生成',409)
            self.draft.update({k:v for k,v in body.items() if k not in ('draft_id','api_key')})
            self.busy=True;self.cancel.clear();self.phase='正在编译并校验人工编辑规则'
            threading.Thread(target=self._run_draft,args=({k:v for k,v in body.items() if k!='api_key'},),daemon=True).start()
        return {'ok':True,'phase':self.phase}

    def _run_draft(self, edits):
        from selection_automation.rule_provider import DEFAULT_URL, environment
        env={**os.environ,**environment(self.draft_provider),'DEEPSEEK_API_KEY':self.draft_key or '', 'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
        try:
            if not self.draft_amazon_path or not self.draft_plan_path or not self.draft_amazon_path.is_file() or not self.draft_plan_path.is_file():
                raise ConsoleError('KEYWORD_PLAN_MISSING','这份旧草稿没有 Amazon 商品与关键词，请重新点击生成规则与关键词')
            edit_path=self.draft_path.with_suffix('.edits.json'); output=self.draft_path.with_suffix('.reviewed.json')
            if edits.get('execution_mode')=='collection':
                category=selection.read(self.draft_path)
                category['execution_mode']='collection'
            else:
                selection.save(edit_path,edits)
                compiled=self.invoke([str(self.root/'scripts/rule_editor.py'),'--input',str(self.draft_path),'--edits',str(edit_path),'--output',str(output)],env=env,timeout=900)
                if compiled.returncode: raise ConsoleError('RULES_INVALID','规则编译或校验失败，请检查编辑内容及API授权后重试')
                category=json.loads(output.read_text('utf-8'))
                category['execution_mode']='full'
            # A failed launch may already have frozen its configuration.
            category.setdefault('temu',{})['worker_count']=worker_count(edits.get('worker_count',category_worker_count(category)))
            category['rule_generation_provider']=self.draft_provider
            # Retried edits receive a fresh run instead of mixing old state.
            from selection_automation.commercial import settings as commercial_settings
            category['commercial']={**commercial_settings(category.get('commercial')),'same_operator':'lt'}
            category['category_key']=category['category_key'][:48]+'_'+uuid.uuid4().hex[:12]
            selection.save(output,category)
            self.key=category['category_key']
            selection.save(self.root/'runtime/web-console/state.json',{'category_key':self.key})
            if self.cancel.is_set(): raise ConsoleError('CANCELLED','已停止')
            self.seed_run_from_draft(category)
            self.phase='启动中'
            collection_env={**env}
            if self.draft_provider['base_url']!=DEFAULT_URL:collection_env['DEEPSEEK_API_KEY']=''
            result=self.invoke([str(self.root/'scripts/selection.py'),'start','--category',str(output)],env=collection_env,timeout=90)
            if result.returncode: raise ConsoleError('START_FAILED','任务启动失败，请检查本地依赖和公司配置')
            owner=self.registered_owner(self.key)
            if self.cancel.is_set(): raise ConsoleError('CANCELLED','已停止')
            if not owner or owner.get('category_key')!=self.key:
                self.invoke([str(self.root/'scripts/selection.py'),'stop','--category-key',self.key])
                raise ConsoleError('START_FAILED','监督进程未注册，已请求停止本任务')
            selection.save(self.root/'runs'/self.key/'stage123'/'web_key_inherited.json',
                           {'pid':owner.get('pid'),'created':owner.get('created')})
            self.draft_used=True;self.phase='独立数据采集已启动；分析部分未启动' if category.get('execution_mode')=='collection' else '采集和前两层已启动；第三层等待网页按钮'
            (self.root/'runtime/web-console/resume-draft.json').unlink(missing_ok=True)
            # Settle failed generation after a restored draft actually starts.
            self.pending_scope=None;self.generation_error=None;self.progress_file=None
            (self.root/'runtime/web-console/pending-generation.json').unlink(missing_ok=True)
        except ConsoleError as exc: self.phase=exc.message
        except Exception: self.phase='规则处理或启动失败，编辑内容已保留'
        finally:
            if self.cancel.is_set() and self.key:
                try:self.invoke([str(self.root/'scripts/selection.py'),'stop','--category-key',self.key])
                except Exception:self.phase='停止请求失败，请检查任务状态'
            env.pop('DEEPSEEK_API_KEY',None)
            with self.lock:self.busy=False

    def seed_run_from_draft(self,category):
        amazon=selection.read(self.draft_amazon_path,{})
        plan=selection.read(self.draft_plan_path,{})
        records=amazon.get('records') or []
        if len(records)!=category['amazon']['target_count'] or not plan.get('keywords'):
            raise ConsoleError('KEYWORD_PLAN_MISSING','Amazon 商品或关键词草稿不完整，请重新生成')
        base=self.root/'runs'/category['category_key']
        amazon['category_key']=category['category_key']
        destination=base/'amazon/amazon_frontend_latest.json'
        selection.save(destination,amazon)
        scope={'source_url':amazon['source_url'],'category_key':category['category_key'],
               'target_count':category['amazon']['target_count'],'detail_standard':'LATEST_DETAIL_V2'}
        selection.save(destination.with_suffix('.json.checkpoint.json'),{
            'schema_version':'amazon-detail-checkpoint-v1','scope':scope,
            'status':amazon.get('status') or 'RUNNING','records':records})
        selection.save(base/'temu/keyword_plan.json',plan)

    def current_run_key(self):
        owner=self.owner()
        if owner and owner.get('category_key'): return owner['category_key'], True
        if not self.draft_used and (self.draft is not None or self.pending_scope is not None):
            return None,False
        return self.key,False

    def clear_workspace(self):
        """Clear selection and draft pointers, never collected data or history."""
        self.key=None;self.draft=None;self.draft_path=None;self.draft_key=None;self.draft_used=False
        self.draft_amazon_path=None;self.draft_plan_path=None;self.pending_scope=None
        self.generation_error=None;self.progress_file=None;self.plain_application={}
        self.preparation_path=None;self.preparation_stage=None
        self.workspace_stop_pending=False;self.workspace_stop_started=None;self.phase='当前工作区已清空，可新建任务；历史任务可在历史记录中恢复'
        folder=self.root/'runtime/web-console'
        selection.save(folder/'state.json',{'category_key':None,'explicitly_restored':False})
        selection.save(folder/'resume-draft.json',{'draft':None})
        (folder/'pending-generation.json').unlink(missing_ok=True)

    def restore_history(self,body):
        if set(body)!={'category_key'}:raise ConsoleError('INVALID_INPUT','请选择一个历史任务')
        with self.lock:
            if self.busy or self.stage3_busy or self.owner():raise ConsoleError('TASK_BUSY','当前任务仍在运行，请先停止',409)
            base,category=self.history_base(body['category_key'])
            self.key=base.name;self.draft_used=True;self.pending_scope=None;self.generation_error=None
            self.phase='历史任务已恢复，可查看结果或继续运行'
            selection.save(self.root/'runtime/web-console/state.json',{'category_key':self.key,'explicitly_restored':True})
            return {'ok':True,'category_key':self.key,'phase':self.phase}

    def excel_workbook(self,kind,run_key=None):
        """Select the newest user-facing copy belonging to the visible run."""
        if kind not in EXCEL_KINDS: return None
        if run_key is None and self.key is None and self.pending_scope is not None: return None
        key=run_key if run_key is not None else self.current_run_key()[0]
        if not isinstance(key,str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}',key): return None
        base=self.root/'runs'/key
        if base.is_symlink() or not base.resolve().is_relative_to((self.root/'runs').resolve()): return None
        category=selection.read(base/'category.json',{})
        if category.get('category_key')!=key: return None
        if kind=='layer123' and not (base/'stage123'/'paid_enabled.json').is_file():
            delivery=selection.read(base/'publish_state.json',{}).get('deliveries',{}).get('layer123',{})
            if delivery.get('当前查看产物已交付') is not True: return None
        name=category.get('category_name_zh')
        if not isinstance(name,str) or not name: return None
        filenames={'amazon':f'Amazon_US_{name}_Best_Sellers.xlsx',
                   'temu':f'TEMU_US_{name}_关键词采集明细.xlsx',
                   'layer12':f'{name}_选品结果.xlsx',
                   'layer123':f'{name}_选品结果.xlsx'}
        directory=base/'excel'
        if not directory.is_dir() or not directory.resolve().is_relative_to(base.resolve()): return None
        filename=filenames[kind]
        if kind in ('layer12','layer123') and not (directory/filename).is_file() and not any(directory.glob(filename[:-5]+'_查看快照_*.xlsx')):
            filename=f'{name}_{"第一二层" if kind=="layer12" else "第一二三层"}.xlsx'
        if (directory/filename).parent!=directory: return None
        stem=filename[:-5]+'_查看快照_'
        candidates=[]
        for path in directory.iterdir():
            if path.name!=filename and not (path.name.startswith(stem) and path.suffix.lower()=='.xlsx'):
                continue
            try:
                if not path.is_symlink() and path.is_file() and path.resolve().parent==directory.resolve():
                    candidates.append((path.stat().st_mtime_ns,path))
            except OSError:
                continue
        return max(candidates,key=lambda item:(item[0], '_查看快照_' in item[1].name, item[1].name))[1] if candidates else None

    @staticmethod
    def review_source_url(value,site):
        """Expose only public product source links, without credentials or tracking."""
        from urllib.parse import parse_qsl,urlencode,urlunsplit
        try:
            parsed=urlsplit(value)
            domain='amazon.com' if site=='amazon' else 'temu.com'
            host=(parsed.hostname or '').lower()
            if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443) or not (host==domain or host.endswith('.'+domain)):return ''
            query=urlencode([(k,v) for k,v in parse_qsl(parsed.query) if site=='temu' and k=='goods_id'])
            return urlunsplit(('https',host,parsed.path,query,''))
        except ValueError:return ''

    def visual_review(self):
        """Read a bounded sample from the latest published workbook on demand."""
        path=self.excel_workbook('layer123')
        if not path:return {'ok':True,'available':False,'message':'第三层还没有已发布结果；候选未运行不代表不同款。'}
        from openpyxl import load_workbook
        snapshot_stat=path.stat()
        book=load_workbook(path,read_only=True,data_only=True)
        examples=[];scanned=0
        try:
            for sheet in book.worksheets:
                if not (sheet.title.startswith('全量综合结果') or sheet.title=='待复核依据'):continue
                headers=next(sheet.iter_rows(min_row=4,max_row=4,values_only=True),())
                fields={name:i for i,name in enumerate(headers)}
                if '依据' in fields and '判断依据' not in fields:fields['判断依据']=fields['依据']
                if 'ASIN' in fields and 'Amazon ASIN' not in fields:fields['Amazon ASIN']=fields['ASIN']
                if not {'最终判断','判断依据','视觉证据'}<=fields.keys():continue
                for row in sheet.iter_rows(min_row=5,values_only=True):
                    scanned+=1
                    def value(name):
                        i=fields.get(name);return str(row[i] or '') if i is not None and i<len(row) else ''
                    state=value('最终判断')
                    if any(word in state for word in ('复核','补充','失败','无法判断','UNKNOWN')):
                        examples.append({'asin':value('Amazon ASIN'),'goods_id':value('TEMU商品ID'),
                            'state':state,'reason':value('判断依据')[:2000],
                            'amazon_url':self.review_source_url(value('Amazon商品链接'),'amazon'),
                            'temu_url':self.review_source_url(value('TEMU商品链接'),'temu'),
                            'evidence':re.sub(r'\b(SAME|DIFFERENT|SIMILAR|UNKNOWN|VARIANT)\b',lambda m:{'SAME':'一致','DIFFERENT':'明确不同','SIMILAR':'相似但未证实','UNKNOWN':'未看清/未取得','VARIANT':'允许变体'}[m[0]],value('视觉证据'))[:6000]})
                    if len(examples)>=5 or scanned>=5000:break
                if len(examples)>=5 or scanned>=5000:break
        finally:book.close()
        latest_stat=path.stat()
        if (latest_stat.st_mtime_ns,latest_stat.st_size)!=(snapshot_stat.st_mtime_ns,snapshot_stat.st_size):
            return {'ok':True,'available':False,'message':'结果刚刚更新，请重新展开读取同一份已发布快照。'}
        return {'ok':True,'available':True,'source':'最近已发布综合 Excel',
                'filename':path.name,'updated_at':snapshot_stat.st_mtime,
                'scanned_rows':scanned,'sample_only':True,'examples':examples,
                'message':'以下是已发布快照中的待核验样例，不是全部统计；完整结果与双方商品链接请打开综合 Excel。'}

    def excel_files(self,run_key=None):
        result={}
        for kind in EXCEL_KINDS:
            path=self.excel_workbook(kind,run_key) if run_key is not None else self.excel_workbook(kind)
            try:
                result[kind]={'available':True,'filename':path.name,'updated_at':path.stat().st_mtime} if path else {'available':False}
            except OSError:
                result[kind]={'available':False}
            if run_key is not None and result[kind]['available']:
                result[kind]['url']=f'/api/history/{run_key}/excel/{kind}'
        return result

    def history_base(self,key):
        if not isinstance(key,str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}',key):
            raise ConsoleError('NOT_FOUND','历史任务不存在',404)
        base=self.root/'runs'/key
        if base.is_symlink() or not base.resolve().is_relative_to((self.root/'runs').resolve()):
            raise ConsoleError('NOT_FOUND','历史任务不存在',404)
        category=selection.read(base/'category.json',{})
        if category.get('category_key')!=key:
            raise ConsoleError('NOT_FOUND','历史任务不存在',404)
        return base,category

    def developer_info(self, key=None):
        from selection_automation.developer_handoff import describe_run
        if key is None: key,_=self.current_run_key()
        runtime={k:v for k,v in selection.LOADED_VERSION.items() if k in ('commit','loaded_at','component','source_sha256')}
        if not key: return {'ok':True,'run_key':None,'runtime':runtime,'message':'创建任务后可下载该任务的数据对接包'}
        self.history_base(key)
        info=describe_run(self.root,key)
        owner=self.owner() or {}
        info['collection']['is_collecting']=owner.get('category_key')==key
        info['collection']['status_basis']='live_supervisor_and_saved_snapshot'
        return clean({'ok':True,**info,'runtime':runtime})

    def developer_bundle(self, key):
        from selection_automation.developer_handoff import build_bundle
        self.history_base(key)
        return build_bundle(self.root,key)

    def history_counts(self,base,details):
        def count(*values):
            return next((value for value in values if type(value) is int and value>=0),None)
        amazon=selection.read(base/'amazon/amazon_frontend_latest.json',{})
        records=amazon.get('records')
        union=selection.read(base/'temu/union/summary.json',{})
        stages=details.get('阶段') or {}
        return {'amazon':count(len(records) if isinstance(records,list) else None,
                               amazon.get('record_count'),details.get('Amazon商品数'),
                               (stages.get('amazon') or {}).get('actual_count')),
                'temu':count(union.get('unique_goods_id'),details.get('TEMU商品数'),
                             (stages.get('temu_union') or {}).get('unique_goods'))}

    def history(self):
        owner=self.owner() or {}; runs=[]
        for base in (self.root/'runs').glob('*'):
            try:
                base,category=self.history_base(base.name)
                details=selection.read(base/'status.json',{})
                original_status=details.get('状态') or '已保存'
                updated=max(p.stat().st_mtime for p in (base/'category.json',base/'status.json') if p.is_file())
                runs.append({'category_key':base.name,'category_name':category.get('category_name_zh') or base.name,
                             'updated_at':updated,'status':'采集已完成' if self.history_collection_complete(base,category,details) else original_status,
                             'original_status':original_status,
                             'active':owner.get('category_key')==base.name,
                             'counts':self.history_counts(base,details),
                             'excel_files':self.excel_files(base.name)})
            except (ConsoleError,OSError,ValueError,TypeError):
                continue
        return clean({'ok':True,'runs':sorted(runs,key=lambda row:row['updated_at'],reverse=True)})

    def history_snapshot(self,key=None):
        if key is None:return self.history_monitor.get()
        self.history_base(key)
        from selection_automation.monitor_status import MonitorStatus
        with self._history_monitor_lock:
            if key not in self._history_monitors:
                self._history_monitors[key]=MonitorStatus(lambda:self.history_status(key),lambda:key,
                    interval=5,loading_key='history_loading')
            monitor=self._history_monitors[key]
        return monitor.get()

    def history_collection_complete(self,base,category,details):
        """Only distinguish proven acquisition completion from optional market gaps."""
        amazon=selection.read(base/'amazon/amazon_frontend_latest.json',{})
        target=category.get('amazon',{}).get('target_count')
        records=amazon.get('records')
        if (type(target) is not int or target<=0 or amazon.get('status')!='COMPLETE'
                or amazon.get('details_complete') is not True or not isinstance(records,list) or len(records)<target):
            return False
        assignments=selection.read(base/'temu/supervisors/assignments.json',{}).get('workers')
        if not isinstance(assignments,list) or not assignments: return False
        for row in assignments:
            if not isinstance(row,dict) or not re.fullmatch(r'worker-\d+(?:-new)?',str(row.get('name',''))): return False
            worker=selection.read(base/'temu/supervisors'/f"{row['name']}.json",{})
            if worker.get('status')!='WORKER_COMPLETE': return False
            passes=row.get('passes')
            completed={(item.get('query'),item.get('sort')) for item in worker.get('completed_passes',[]) if isinstance(item,dict)}
            if passes and any(not isinstance(item,dict) or (item.get('query'),item.get('sort')) not in completed for item in passes): return False
        errors=selection.read(base/'publish_state.json',{}).get('errors',{})
        if any(value for name,value in errors.items() if name not in ('market','commercial')): return False
        stages=details.get('阶段',{})
        for name,stage in stages.items():
            if name in ('market','commercial','stage1','stage2','stage3','stage12','stage123'): continue
            if not isinstance(stage,dict): continue
            state=str(stage.get('状态') or stage.get('status') or '').upper()
            if (stage.get('阻断') or stage.get('需人工介入') or stage.get('manual_action_required')
                    or stage.get('source_failed') or any(word in state for word in ('失败','FAILED','存在阻断'))):
                return False
        return all(self.excel_workbook(kind,base.name) for kind in ('amazon','temu'))

    def history_status(self,key):
        base,category=self.history_base(key)
        # Archived tasks have no live heartbeat. Preserve saved outcomes rather
        # than borrowing current profiles or declaring a stopped watcher faulty.
        details=selection.read(base/'status.json',{})
        original_status=details.get('状态') or '历史任务已保存'
        collection_complete=self.history_collection_complete(base,category,details)
        from dashboard_progress import enrich_progress
        details=enrich_progress(base,details,watch_running=False)
        workers=[]
        for path in worker_supervisor_paths(base):
            value=selection.read(path,{})
            workers.append({**value,'name':value.get('worker',path.stem),'rotation_control':None})
        phase='采集已完成' if collection_complete else details.get('状态') or '历史任务已保存'
        if phase in ('运行中','启动中','停止中'):
            phase='历史快照（未记录最终状态）'
        errors=selection.read(base/'publish_state.json',{}).get('errors',{})
        business_error=bool(errors.get('commercial'))
        scope={'category':category.get('category_name_zh'),'amazon_count':category.get('amazon',{}).get('target_count'),
               'per_keyword':category.get('temu',{}).get('max_products_per_keyword'),
               'worker_count':category_worker_count(category),'execution_mode':category.get('execution_mode','full')}
        failed=any(word in phase for word in ('失败','阻断')) or (business_error and not collection_complete)
        return clean({'ok':True,'historical':True,'read_only':True,'active':False,'category_key':key,
                      'phase':phase,'original_status':original_status,'severity':'red' if failed else 'green' if '完成' in phase else 'neutral',
                      'workers':workers,'details':details,'generation':{},'draft':None,'scope':scope,
                      'saved_credential_available':False,'excel_files':self.excel_files(key),
                      'commercial':selection.read(base/'commercial_report.json',{}) if not business_error else {},
                      'commercial_error':'该任务商业判断未成功更新' if business_error else None,
                      'market_status':selection.read(base/'market/status.json',{}),'market_busy':False,
                      'frontend_market':build_frontend_market(base),
                      'stage3_control':{'enabled':(base/'stage123/paid_enabled.json').is_file(),
                                        'running':False,'phase':'历史结果（只读）'}})

    def refresh_excel(self,kind):
        """A finished run can outlive an exporter update; publish on demand."""
        if kind not in EXCEL_KINDS or self.busy or self.stage3_busy:
            return
        key, active = self.current_run_key()
        if active or not isinstance(key, str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}', key):
            return
        base = self.root / 'runs' / key
        category = base / 'category.json'
        source = (base / 'amazon' / 'amazon_frontend_latest.json' if kind == 'amazon' else
                  base / 'temu' / 'union' / 'products.jsonl' if kind == 'temu' else
                  base / 'stage12_config.json')
        if not category.is_file() or not source.is_file():
            return
        try:
            result = subprocess.run([sys.executable, str(self.root / 'scripts' / 'publish_all.py'),
                                     '--category', str(category)], cwd=self.root,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=180, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ConsoleError('EXCEL_UPDATE_FAILED', 'Excel 更新超时或启动失败，请稍后重试', 500) from None
        state = selection.read(base / 'publish_state.json', {})
        errors = state.get('errors') or {}
        if result.returncode and (not errors or 'commercial' in errors or kind in errors or (kind in ('layer12', 'layer123') and 'analysis' in errors)):
            raise ConsoleError('EXCEL_UPDATE_FAILED', 'Excel 更新失败，旧文件已保留；请查看任务发布状态', 500)

    def open_excel(self,kind):
        path=self.excel_workbook(kind)
        if not path:
            self.refresh_excel(kind)
            path=self.excel_workbook(kind)
        if not path: raise ConsoleError('EXCEL_NOT_READY','当前任务的这份 Excel 尚未生成',404)
        require_excel_open_association()
        # WPS can launch another process for a path already locked by its first
        # process. Give each open its own finished copy, outside publisher files.
        directory=self.root/'runtime/web-console/opened-excel'
        directory.mkdir(parents=True,exist_ok=True)
        stem=path.stem.split('_查看快照_',1)[0]
        opened=directory/f'{stem[:48]}_查看副本_{uuid.uuid4().hex[:12]}.xlsx'
        try:
            shutil.copyfile(path,opened)
        except OSError:
            opened.unlink(missing_ok=True)
            raise ConsoleError('EXCEL_OPEN_COPY_FAILED','最新表格正在交付，请稍后重试打开',500) from None
        try:
            os.startfile(str(opened))
        except (AttributeError,OSError):
            opened.unlink(missing_ok=True)
            raise ConsoleError('EXCEL_OPEN_FAILED','未找到可打开 Excel 的本地程序，请使用下载入口',500) from None
        copies=[]
        for item in directory.glob('*.xlsx'):
            try: copies.append((item.stat().st_mtime_ns,item))
            except OSError: pass
        for _,old in sorted(copies,reverse=True)[20:]:
            try: old.unlink()
            except OSError: pass
        return {'ok':True,'filename':path.name,'updated_at':path.stat().st_mtime}

    def refresh_market(self,body):
        if body: raise ConsoleError('INVALID_INPUT','市场数据刷新无需额外参数')
        with self.lock:
            key,active=self.current_run_key()
            if self.busy or self.market_busy: raise ConsoleError('ALREADY_RUNNING','极鲸云正在读取，请等待本次完成',409)
            if not isinstance(key,str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}',key):
                raise ConsoleError('NO_TASK','请先创建选品任务',409)
            category=self.root/'runs'/key/'category.json'
            if not category.is_file(): raise ConsoleError('NO_TASK','本次任务配置不存在',409)
            self.market_busy=True
            def work():
                try:
                    selection.save(category.parent/'market/status.json',{'status':'RUNNING','message':'正在检查已有授权并读取极鲸云市场数据'})
                    self.invoke([str(self.root/'scripts/collect_market_evidence.py'),'--category',str(category),'--force'],timeout=600)
                    state=selection.read(category.parent/'market/status.json',{})
                    if state.get('status') in ('COMPLETE','PARTIAL'):
                        self.invoke([str(self.root/'scripts/publish_all.py'),'--category',str(category)],timeout=180)
                except Exception:
                    selection.save(category.parent/'market/status.json',{'status':'FAILED','message':'市场数据刷新失败或超时，请重试'})
                finally:
                    with self.lock:self.market_busy=False
            threading.Thread(target=work,daemon=True).start()
        return {'ok':True,'message':'正在自动读取市场数据并更新网页与Excel；不会启动第三层付费分析'}

    def frontend_market_snapshot(self,key):
        """Keep product-file scans off the live worker status request path."""
        now=time.monotonic()
        with self._frontend_cache_lock:
            cached=self._frontend_cache.get(key)
            result=cached[1] if cached else {'counting':True}
            if key in self._frontend_refreshing or (cached and now-cached[0]<15):
                return result
            self._frontend_refreshing.add(key)
        def update():
            value=result
            try:
                value=build_frontend_market(self.root/'runs'/key)
            except Exception:
                # A partially published file must not erase the last good view
                # or make subsequent status polls repeat the expensive scan.
                pass
            finally:
                with self._frontend_cache_lock:
                    self._frontend_cache[key]=(time.monotonic(),value)
                    self._frontend_refreshing.discard(key)
        threading.Thread(target=update,daemon=True).start()
        return result

    def matching_snapshot(self,key,category):
        """Refresh result counts independently of the slower workbook publisher."""
        from selection_automation.commercial import current_report
        if not category.get('stage3'):return selection.read(self.root/'runs'/key/'commercial_report.json',{})
        now=time.monotonic()
        with self._matching_lock:
            cached=self._matching_cache.get(key)
            result=cached[1] if cached else selection.read(self.root/'runs'/key/'commercial_report.json',{})
            if key in self._matching_refreshing or cached and now-cached[0]<3:return result
            self._matching_refreshing.add(key)
        def refresh():
            value=result
            try:value=current_report(self.root,category)
            except (OSError,ValueError,KeyError,TypeError):pass
            finally:
                with self._matching_lock:
                    self._matching_cache[key]=(time.monotonic(),value);self._matching_refreshing.discard(key)
        threading.Thread(target=refresh,daemon=True).start()
        return result

    def codex_review_snapshot(self,key):
        """Serve the latest review snapshot while a single background refresh scans batch files."""
        now=time.monotonic()
        with self._codex_review_cache_lock:
            cached=self._codex_review_cache.get(key)
            refreshing=key in self._codex_review_refreshing
            if refreshing and not cached:
                return {'available':True,'running':False,'phase':'正在读取Codex看图状态',
                        'counting':True,'refreshing':True}
            if cached and (now-cached[0]<4 or refreshing):
                result=dict(cached[1])
                if refreshing:result['refreshing']=True
                return result
            self._codex_review_refreshing.add(key)
            previous=dict(cached[1]) if cached else None
        def update():
            try:
                value=codex_review_control(self.root/'runs'/key)
                if not isinstance(value,dict):raise ValueError('Codex复核状态格式无效')
                value={**value,'counting':False,'refreshing':False}
            except Exception as error:
                message=f'{type(error).__name__}: {str(error)[:180]}'
                value=({**previous,'refreshing':False,'refresh_error':message} if previous else
                    {'available':True,'running':False,'phase':'Codex复核状态读取失败',
                     'counting':False,'refreshing':False,'refresh_error':message})
            finally:
                with self._codex_review_cache_lock:
                    self._codex_review_cache[key]=(time.monotonic(),value)
                    self._codex_review_refreshing.discard(key)
        threading.Thread(target=update,name=f'codex-review-snapshot-{key}',daemon=True).start()
        if previous:
            previous['refreshing']=True
            return previous
        return {'available':True,'running':False,'phase':'正在读取Codex看图状态',
                'counting':True,'refreshing':True}

    def stage3_snapshot(self,key):
        from selection_automation.stage3_progress import snapshot_signature
        base=self.root/'runs'/key
        category=selection.read(base/'category.json',{})
        try:signature=snapshot_signature(base,category)
        except (OSError,ValueError,KeyError,TypeError):
            return {'counting':True,'message':'当前候选版本尚不能核对，请检查任务进度'}
        with self._stage3_cache_lock:
            cached=self._stage3_cache.get(key)
            if cached and cached[0]==signature and not cached[2].get('snapshot_unstable'):
                # A successful unchanged revision needs no periodic rescan.
                # Failed reads remain unknown and retry with a short backoff.
                if not cached[2].get('counting') or time.monotonic()-cached[1]<(60 if cached[2].get('refresh_failed') else 10):
                    return {**cached[2],**self._stage3_reader_progress(key)}
            previous=({**cached[2],'counting':False,'refreshing':True,'current_inputs_pending':True,
                'message':'显示最近核验快照；新增输入正在核对，暂不能启动付费分析'}
                if cached and not cached[2].get('counting') and type(cached[2].get('total')) is int else {'counting':True})
            if cached and cached[2].get('refresh_failed') and time.monotonic()-cached[1]<60:return previous
            if key in self._stage3_refreshing:return {**previous,**self._stage3_reader_progress(key)}
            self._stage3_refreshing.add(key)
            self._stage3_started[key]=time.monotonic()
        def update():
            value={'counting':True,'message':'当前候选进度读取失败，等待重新核对'}
            read_signature=signature
            try:
                document=self._read_stage3_snapshot(base)
                if document.get('category_key')!=key:raise ValueError('Wrong snapshot task')
                value=document['progress']
                if type(value.get('total')) is not int:raise ValueError('Incomplete snapshot')
                # JSON turns signature tuples into lists; restore the exact stat identity.
                read_signature=(document['signature'][0],tuple(tuple(row) for row in document['signature'][1]),document['signature'][2])
                if not document.get('stable'):value={**value,'snapshot_unstable':True}
            except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
                value.update(counting=True,error_kind=type(exc).__name__,refresh_failed=True,
                    message=f'第三层只读进度核对失败（{type(exc).__name__}），付费分析保持禁用；诊断已保存')
            finally:
                with self._stage3_cache_lock:
                    # Cache the pre-read identity: a change during this build
                    # stays dirty and cannot authorize paid work as complete.
                    if value.get('counting') and cached and not cached[2].get('counting') and type(cached[2].get('total')) is int:
                        value={**cached[2],'snapshot_unstable':True,'refresh_failed':True,
                            'error_kind':value.get('error_kind'),'message':value.get('message')}
                    self._stage3_cache[key]=(read_signature,time.monotonic(),value)
                    self._stage3_refreshing.discard(key)
                    self._stage3_started.pop(key,None)
        threading.Thread(target=update,daemon=True).start()
        return {**previous,**self._stage3_reader_progress(key)}

    def _stage3_reader_progress(self,key):
        """Read tiny phase metadata only for this console's current live reader."""
        import psutil
        launcher=self._stage3_readers.get(key)
        if key not in self._stage3_refreshing or not launcher:return {}
        base=self.root/'runs'/key
        phase=selection.read(base/'stage123/progress_reader_state.json',{})
        try:
            owner=psutil.Process(launcher[0]);process=psutil.Process(phase['pid'])
            if (not owner.is_running() or abs(owner.create_time()-launcher[1])>=.01
                or not process.is_running() or process.status()==psutil.STATUS_ZOMBIE
                or abs(process.create_time()-phase['created'])>=.01
                or (process.pid!=owner.pid and process.ppid()!=owner.pid)
                or not self._reader_native_active(process.pid)):
                return {}
            args=process.cmdline()
            if (Path(process.cwd()).resolve()!=self.root.resolve() or '-m' not in args
                or args[args.index('-m')+1]!='selection_automation.stage3_progress'
                or '--base' not in args or Path(args[args.index('--base')+1]).resolve()!=base.resolve()):
                return {}
            counts=phase.get('counts');scanned=counts.get('scanned') if isinstance(counts,dict) else None
            message=(f'正在核对当前结果，已核对 {scanned:,} 项候选'
                if type(scanned) is int and scanned>=0 else '正在核对当前结果：'+str(phase.get('phase') or '读取已有数据')[:100])
            return {'reader_live':True,'reader_scanned':scanned if type(scanned) is int else None,
                'reader_phase':str(phase.get('phase') or '')[:100],'reader_updated_at':phase.get('updated_at'),
                'message':message}
        except (OSError,ValueError,TypeError,KeyError,IndexError,psutil.Error):return {}

    def _reader_native_active(self,pid):
        # A Windows process awaiting kernel I/O cleanup can still exist after termination.
        if os.name!='nt':return True
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return False
        try:
            code=ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally:kernel.CloseHandle(handle)

    def _read_stage3_snapshot(self,base):
        # Large candidate/result JSON parsing must not compete for the web GIL.
        env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
        env.update(PYTHONPATH=str(self.root/'src'),PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
        try:
            import tempfile,psutil
            command=[sys.executable,'-m','selection_automation.stage3_progress','--base',str(base)]
            with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
                child=subprocess.Popen(command,cwd=self.root,env=env,stdin=subprocess.DEVNULL,
                    stdout=output,stderr=errors,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                created=psutil.Process(child.pid).create_time()
                self._stage3_readers[base.name]=(child.pid,created)
                try:child.wait(timeout=READONLY_PROGRESS_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    self._stop_stage3_reader(child,base,created)
                    errors.seek(0)
                    raise subprocess.TimeoutExpired(command,READONLY_PROGRESS_TIMEOUT_SECONDS,stderr=errors.read(12000))
                finally:self._stage3_readers.pop(base.name,None)
                errors.seek(0)
                if child.returncode:raise subprocess.CalledProcessError(child.returncode,command,stderr=errors.read(12000))
                output.seek(0);document=json.loads(output.read())
            selection.save(base/'stage123/progress_reader_error.json',{'finished_at':time.time(),'error':None})
            return document
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            # Store only structural traceback information; arbitrary stderr can contain secrets.
            raw=getattr(exc,'stderr','') or ''
            if isinstance(raw,bytes):raw=raw.decode('utf-8',errors='replace')
            frames=re.findall(r'File "([^"]+)", line (\d+), in ([\w<>]+)',raw[-12000:])
            missing=re.search(r"ModuleNotFoundError: No module named '([\w.]+)'",raw[-2000:])
            safe={'kind':type(exc).__name__,'exit_code':getattr(exc,'returncode',None),
                'missing_module':missing[1] if missing else None,
                'frames':[{'file':Path(f).name,'line':int(n),'function':name} for f,n,name in frames[-5:]]}
            try:selection.save(base/'stage123/progress_reader_error.json',{'finished_at':time.time(),'error':safe})
            except OSError:pass
            raise

    def _stop_stage3_reader(self,child,base,created):
        """Terminate only this timed-out reader, including a Windows venv interpreter."""
        import psutil
        try:
            owner=psutil.Process(child.pid)
            if abs(owner.create_time()-created)>=.01:return
            targets=[]
            for process in owner.children(recursive=True):
                try:
                    args=process.cmdline()
                    if ('selection_automation.stage3_progress' in args and '--base' in args
                        and Path(args[args.index('--base')+1]).resolve()==base.resolve()
                        and Path(process.cwd()).resolve()==self.root.resolve()):
                        targets.append((process,process.create_time()))
                except (OSError,ValueError,IndexError,psutil.Error):continue
            for process,start in targets:
                try:
                    if abs(process.create_time()-start)<.01:process.terminate()
                except psutil.Error:pass
            child.kill()
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:pass
        except psutil.Error:pass

    def profile_status_once(self):
        """Locally armed, one-shot profiling; never expose values or frame locals."""
        from selection_automation.process_lock import ProcessLock
        folder=self.root/'runtime/web-console'
        with ProcessLock(folder/'status-profile.lock'):
            marker=folder/'profile-next-status.json'
            if not marker.is_file():
                raise ConsoleError('PROFILE_DISABLED','本机一次性诊断未启用',403)
            marker.unlink()
            import cProfile,pstats
            profiler=cProfile.Profile();started=time.monotonic()
            profiler.enable()
            try:state=self.status()
            finally:profiler.disable()
            elapsed=time.monotonic()-started
            functions=[]
            for (filename,line,name),(_,calls,own,total,_) in sorted(
                pstats.Stats(profiler).stats.items(),key=lambda item:item[1][3],reverse=True)[:20]:
                functions.append({'file':Path(filename).name,'line':line,'function':name[:120],
                                  'calls':calls,'own_seconds':round(own,4),'total_seconds':round(total,4)})
            stacks=[]
            for identity,frame in list(sys._current_frames().items())[:16]:
                frames=[]
                for _ in range(8):
                    if frame is None:break
                    frames.append({'file':Path(frame.f_code.co_filename).name,
                                   'line':frame.f_lineno,'function':frame.f_code.co_name[:120]})
                    frame=frame.f_back
                stacks.append({'thread':identity,'frames':frames})
            key=state.get('category_key')
            with self._stage3_cache_lock:
                cached=self._stage3_cache.get(key)
                job_started=self._stage3_started.get(key)
            progress_cache={'available':bool(cached),'refreshing':key in self._stage3_refreshing,
                    'job_seconds':round(time.monotonic()-job_started,3) if job_started is not None else None,
                    'cache_age_seconds':round(time.monotonic()-cached[1],3) if cached else None}
            if cached:progress_cache['last_error_kind']=cached[2].get('error_kind')
            if cached and key:
                from selection_automation.stage3_progress import snapshot_signature
                base=self.root/'runs'/key
                current=snapshot_signature(base,selection.read(base/'category.json',{}))
                previous=dict((row[0],row[1:]) for row in cached[0][1])
                actual=dict((row[0],row[1:]) for row in current[1])
                progress_cache['changed_inputs']=[Path(path).name for path in sorted(previous.keys()|actual.keys())
                    if previous.get(path)!=actual.get(path)][:30]
                progress_cache['category_changed']=cached[0][0]!=current[0]
                progress_cache['controller_liveness_changed']=cached[0][2]!=current[2]
            with self._frontend_cache_lock:
                frontend=self._frontend_cache.get(key)
                frontend_cache={'available':bool(frontend),'refreshing':key in self._frontend_refreshing,
                    'cache_age_seconds':round(time.monotonic()-frontend[0],3) if frontend else None}
            progress=(state.get('stage3_control') or {}).get('progress') or {}
            return {'ok':True,'status_seconds':round(elapsed,4),'functions':functions,'thread_stacks':stacks,
                    'stage3_counting':bool(progress.get('counting')),
                    'stage3_cache':progress_cache,'frontend_cache':frontend_cache}


    def launch_stage3_controller(self,key,category_path,api_key):
        import psutil
        from selection_automation.stage3_provider import from_category
        provider=from_category(selection.read(category_path))
        folder=self.root/'runs'/key/'stage123'
        with (folder/'controller.log').open('ab') as log:
            child=subprocess.Popen([sys.executable,str(self.root/'scripts/watch_stage3.py'),
                '--category',str(category_path)],cwd=self.root,
                env={**os.environ,'DEEPSEEK_API_KEY':api_key if provider['base_url']=='https://api.deepseek.com' else '',
                     'SELECTION_STAGE3_API_KEY':api_key,'SELECTION_STAGE3_ENDPOINT':provider['base_url'],
                     'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'},
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        selection.save(folder/'controller.json',{'pid':child.pid,'created':psutil.Process(child.pid).create_time(),
            'phase':'第三层启动中','updated_at':time.time()})
        def wait():
            child.wait()
            with self.lock:self.stage3_busy=False
        threading.Thread(target=wait,daemon=True).start()

    def queue_stage3_switch(self,key,base,provider,configure_only=False):
        """Persisted intent is resumed after a console restart, exactly once."""
        from selection_automation.stage3_runtime import update_control,read_control
        from selection_automation.stage3_progress import controller_alive
        with self._switch_lock:
            if key in self._switch_threads:return
            self._switch_threads.add(key)
        folder=base/'stage123'
        def complete():
            try:
                while controller_alive(base) or _codex_pool_process_running(selection.read(folder/'codex_pool/pool.json',{}),base):
                    if read_control(folder).get('switch_cancelled'):return
                    time.sleep(2)
                if read_control(folder).get('switch_cancelled'):return
                for attempt in range(60):
                    if self.current_run_key()[0]!=key:
                        update_control(folder,paused=True,switch_pending=False,switch_error='断点已保存；当前任务已改变，请在原任务继续');return
                    try:
                        self.start_stage3({'provider':provider},configure_only=configure_only)
                        update_control(folder,switch_pending=False,pending_provider=None,paused=bool(configure_only));return
                    except ConsoleError as error:
                        if error.code in {'PROGRESS_NOT_READY','ALREADY_RUNNING'}:time.sleep(2);continue
                        update_control(folder,paused=True,switch_pending=False,switch_error=error.message);return
                    except Exception:
                        update_control(folder,paused=True,switch_pending=False,switch_error='断点已保存；新方案启动失败，请重试');return
                update_control(folder,paused=True,switch_pending=False,switch_error='断点已保存；进度仍在读取，请点击继续')
            finally:
                with self._switch_lock:self._switch_threads.discard(key)
        threading.Thread(target=complete,daemon=True).start()

    def set_stage3_concurrency(self,body):
        """Change dispatch width without probing models or releasing pause gates."""
        from selection_automation.stage3_provider import from_category,settings
        from selection_automation.stage3_runtime import read_control,update_control
        from selection_automation.stage3_progress import controller_alive
        if set(body)-{'workers','concurrency_mode'} or 'workers' not in body:
            raise ConsoleError('INVALID_INPUT','请填写并发路数')
        with self.lock:
            if self.busy:raise ConsoleError('ALREADY_RUNNING','请等待当前操作完成',409)
            key,_=self.current_run_key()
            if not isinstance(key,str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}',key):
                raise ConsoleError('NO_TASK','当前没有任务',409)
            base=self.root/'runs'/key;path=base/'category.json';folder=base/'stage123'
            category=selection.read(path,{})
            if category.get('category_key')!=key or category.get('execution_mode')=='collection' or not isinstance(category.get('stage3'),dict):
                raise ConsoleError('NO_TASK','当前任务未启用第三层分析',409)
            control=read_control(folder)
            if control.get('switch_pending'):raise ConsoleError('ALREADY_RUNNING','正在保存断点并切换方案，请稍后调整并发',409)
            current=from_category(category)
            try:provider=settings({**current,**body})
            except ValueError as error:raise ConsoleError('INVALID_INPUT',str(error)) from None
            if provider['concurrency_mode']=='auto':
                from selection_automation.analysis_concurrency import recommended
                provider['workers']=recommended(provider['mode'],provider=provider,root=self.root)
            if provider['mode']!='local_agent' and controller_alive(base) and provider!=current:
                paused=bool(control.get('paused'))
                update_control(folder,paused=True,switch_pending=True,switch_cancelled=False,
                    pending_provider=provider,pending_configure_only=paused)
                selection.save(folder/'STOP',{'time':time.time(),'reason':'concurrency-change'})
                self.queue_stage3_switch(key,base,provider,configure_only=paused)
                return {'ok':True,'provider':provider,'switch_pending':True,
                    'message':f"并发已保存为 {provider['workers']} 路；当前请求保存后按新路数接续。"}
            category['stage3'].update(provider=provider,max_concurrency=provider['workers'])
            selection.save(path,category)
            pool_path=folder/'codex_pool_config.json'
            if provider['mode']=='local_agent' and pool_path.exists():
                pool=selection.read(pool_path,{})
                pool.update(workers=provider['workers'],concurrency_mode=provider['concurrency_mode'])
                pool['agent_backends']=[{**agent,'max_parallel':provider['workers']} for agent in pool.get('agent_backends',[])]
                selection.save(pool_path,pool)
            return {'ok':True,'provider':provider,'message':f"并发已设置为 {provider['workers']} 路；后续分组按新设置派发，已发送的组正常保存。"}

    def authorize_schema_retry(self,body):
        """Explicit one-pair cost consent; this endpoint itself never dispatches."""
        if set(body)!={'category_key','original_request_id','accept_unknown_cost'} or body.get('accept_unknown_cost') is not True:
            raise ConsoleError('EXPLICIT_COST_CONSENT_REQUIRED','请明确确认原费用未知，仅复验原失败候选一次',409)
        with self.lock:
            key,_=self.current_run_key()
            if key!=body['category_key'] or not isinstance(key,str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}',key):
                raise ConsoleError('TASK_CHANGED','任务已变化，请刷新后重试',409)
            from selection_automation.stage3_progress import controller_alive,candidate_path
            base=self.root/'runs'/key
            if self.busy or self.stage3_busy or controller_alive(base) or codex_review_control(base,scan_batches=False).get('running'):
                raise ConsoleError('TASK_BUSY','请等待已发送的分析结束后再确认复验',409)
            from selection_automation.schema_billing_retry import create_authorization
            try:create_authorization(base/'stage123',selection.read(base/'category.json',{}),candidate_path(base),expected_request_id=body['original_request_id'])
            except (OSError,ValueError,KeyError,TypeError):
                raise ConsoleError('SCHEMA_RETRY_NOT_AVAILABLE','当前证据不符合一次复验条件，或预算已使用；原费用记录和断点已保留',409) from None
            from selection_automation.stage3_runtime import update_control
            update_control(base/'stage123',paused=True)
            return {'ok':True,'message':'原费用仍未知；已授权仅复验原失败的1对、最多1次新请求。点击启动第三层执行，其他候选不会派发。'}

    def start_stage3(self, body, configure_only=False):
        from selection_automation.stage3_progress import controller_alive
        if set(body)-{'api_key','provider'}: raise ConsoleError('INVALID_INPUT','第三层启动参数无效')
        with self.lock:
            if self.busy: raise ConsoleError('ALREADY_RUNNING','请等待当前操作完成',409)
            key,active=self.current_run_key()
            if not isinstance(key,str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}',key):
                raise ConsoleError('NO_TASK','还没有可分析的任务，请先开始采集',409)
            base=self.root/'runs'/key; category_path=base/'category.json'
            category=selection.read(category_path,{})
            if category.get('execution_mode')=='collection':
                raise ConsoleError('COLLECTION_ONLY','本任务仅获取数据，分析部分保持不动',409)
            if category.get('category_key')!=key: raise ConsoleError('NO_TASK','当前任务配置缺失',409)
            from selection_automation.stage3_provider import settings as provider_settings, from_category, apply as apply_provider, LABELS
            try: provider=provider_settings(body['provider']) if 'provider' in body else from_category(category)
            except ValueError as exc: raise ConsoleError('INVALID_INPUT',str(exc)) from None
            provider_changed=provider!=from_category(category)
            if not configure_only or provider_changed:
                check_stage3_local_service(provider,self.root)
                provider_changed=provider!=from_category(category)
            api_running=controller_alive(base)
            codex_state=codex_review_control(base)
            running=api_running or codex_state.get('running')
            pool_config=selection.read(base/'stage123/codex_pool_config.json',{})
            pool_matches=(provider['mode']!='local_agent' or not pool_config or all(
                pool_config.get(field,{} if field=='agent_efforts' else None)==provider[field] for field in ('workers','concurrency_mode','agent_ids','agent_models','agent_efforts')))
            if running and not provider_changed and pool_matches:
                if configure_only:return {'ok':True,'provider':provider,'message':'第三层方案已保存，运行状态保持不变'}
                from selection_automation.stage3_runtime import resume_dispatch
                (base/'stage123/STOP').unlink(missing_ok=True)
                resume_dispatch(base/'stage123',provider['agent_ids'] if provider['mode']=='local_agent' else ())
                return {'ok':True,'phase':'第三层已继续，沿用已保存断点','provider':provider}
            if self.stage3_busy and self.stage3_key==key and not running and not provider_changed:
                return {'ok':True,'phase':'第三层启动中'}
            progress=self.stage3_snapshot(key)
            if not configure_only and (progress.get('counting') or progress.get('refreshing') or type(progress.get('total')) is not int):
                raise ConsoleError('PROGRESS_NOT_READY','正在核对已处理候选，请等待进度读取完成后再启动',409)
            if not configure_only and not provider_changed and not active and progress.get('total',0)>0 and progress.get('pending_after_codex',progress.get('pending'))==0:
                raise ConsoleError('NO_PENDING_CANDIDATES','当前第三层候选已全部处理，无需重复启动付费分析',409)
            supplied=body.get('api_key')
            if supplied is not None and (not isinstance(supplied,str) or not supplied.strip() or len(supplied)>4096 or any(ord(c)<32 for c in supplied)):
                raise ConsoleError('INVALID_INPUT','API key无效')
            from selection_automation.rule_provider import DEFAULT_URL
            api_key=supplied.strip() if supplied is not None else ''
            if not api_key:api_key=self.stage3_keys.get((key,provider['base_url']),'')
            if not api_key and self.key==key and self.draft_key and provider['mode']=='multimodal_api':
                generated=provider_settings({'mode':'multimodal_api','base_url':self.draft_provider['base_url'],'model':self.draft_provider['model']})
                if generated['base_url']==provider['base_url']:api_key=self.draft_key
            if not api_key and provider['mode']=='multimodal_api' and provider['base_url']==DEFAULT_URL:
                api_key=(self.draft_key if self.key==key and self.draft_provider['base_url']==DEFAULT_URL else None) or self.saved_credential() or ''
            from selection_automation.stage3_credentials import saved_key, save_key
            if not api_key:api_key=saved_key(self.root,provider)
            if not api_key and provider['mode'] in {'multimodal_api','liquid_d1'}:
                raise ConsoleError('KEY_REQUIRED','请填写与第三层服务地址配套的 API key',409)
            if provider['mode']=='multimodal_api' and provider.get('reasoning_effort','default')!='default':
                from selection_automation.api_reasoning import validate
                try:validate(provider,api_key)
                except ValueError as error:raise ConsoleError('INVALID_INPUT',str(error)) from None
            if supplied is not None and provider['mode'] in {'liquid_d1','multimodal_api'}:
                save_key(self.root,provider,api_key)
            if api_key:self.stage3_keys[(key,provider['base_url'])]=api_key
            folder=base/'stage123'; folder.mkdir(parents=True,exist_ok=True)
            if provider['mode']=='local_agent':
                from selection_automation.local_agents import ready_agents, discover
                from selection_automation.agent_models import validate_selection,validate_efforts
                installed={a['id'] for a in discover(self.root) if a['installed']}
                choices={k:v for k,v in provider['agent_models'].items() if k in installed and (not provider['agent_ids'] or k in provider['agent_ids'])}
                try:
                    validate_selection(self.root,choices)
                    validate_efforts(self.root,choices,{k:v for k,v in provider['agent_efforts'].items() if k in installed and (not provider['agent_ids'] or k in provider['agent_ids'])})
                except ValueError as error:raise ConsoleError('INVALID_MODEL',str(error),409) from None
                agents=ready_agents(self.root,provider['agent_ids'],connect=not configure_only,models=provider['agent_models'])
                if not configure_only and not agents:
                    raise ConsoleError('AGENT_UNAVAILABLE','未找到已连接且能够看图的本机 Agent；请在方案中检测并登录',409)
                if provider['concurrency_mode']=='auto':
                    from selection_automation.analysis_concurrency import recommended
                    provider['workers']=recommended('local_agent',provider=provider,root=self.root)
                    provider_changed=provider!=from_category(category)
            from selection_automation.stage3_runtime import update_control,read_control
            native_update=running and provider['mode']=='local_agent' and from_category(category)['mode']=='local_agent'
            if running and provider_changed and not native_update:
                update_control(folder,paused=True,switch_pending=True,switch_cancelled=False,pending_provider=provider,pending_configure_only=configure_only)
                if api_running:selection.save(folder/'STOP',{'time':time.time(),'reason':'provider-switch'})
                pool_path=folder/'codex_pool_config.json'
                if pool_path.exists():selection.save(pool_path,{**selection.read(pool_path,{}),'enabled':False})
                self.queue_stage3_switch(key,base,provider,configure_only)
                return {'ok':True,'phase':'正在保存已发送的结果；完成后自动切换新方案，已完成商品不重跑','switch_pending':True}
            if provider_changed:
                backup=folder/'category.before-provider.json'
                if not backup.exists(): selection.save(backup,category)
                apply_provider(category,provider)
                selection.save(category_path,category)
                with self._stage3_cache_lock:self._stage3_cache.pop(key,None)
            if configure_only:
                return {'ok':True,'provider':provider,'message':'第三层方案已保存；API Key 已在本机加密保存，尚未启动分析'}
            (folder/'STOP').unlink(missing_ok=True)
            update_control(folder,paused=False,switch_pending=False,switch_cancelled=False,switch_error=None)
            if provider['mode']=='local_agent':
                from selection_automation.stage3_runtime import resume_dispatch
                resume_dispatch(folder,provider['agent_ids'])
            selection.save(folder/'paid_enabled.json',{'category_key':key,'enabled_at':time.time(),
                'source':'web-console','controller':'independent','provider':provider['mode']})
            if provider['mode']=='local_agent':
                from run_codex_visual_pool import ensure_pool
                selection.save(folder/'codex_pool_config.json',{
                    **selection.read(folder/'codex_pool_config.json',{}),'enabled':True,
                    'workers':provider['workers'],'model':provider['model'],'group_size':12,
                    'agent_ids':[agent['id'] for agent in agents],
                    'agent_models':provider['agent_models'],
                    'agent_efforts':provider['agent_efforts'],
                    # A still-running older manager may read max_parallel.
                    # Give it the requested global width, never the old fixed six.
                    'agent_backends':[{**agent,'max_parallel':provider['workers']} for agent in agents],
                    'concurrency_mode':provider['concurrency_mode']})
                ensure_pool(category_path,self.root)
                self.stage3_key=key;self.stage3_phase='本机Agent并行看图已启动'
                return {'ok':True,'phase':self.stage3_phase,'provider':provider}
            self.stage3_busy=True; self.stage3_key=key
            self.stage3_phase=('本机看图模型：持续分析新增候选' if provider['mode']=='local_model' else '持续分析新增候选')
            try:self.launch_stage3_controller(key,category_path,api_key)
            except Exception:
                self.stage3_busy=False;selection.save(folder/'STOP',{'time':time.time()});raise
            return {'ok':True,'phase':self.stage3_phase,'provider':provider}

    def api_reasoning_catalog(self,body):
        if set(body)-{'provider','api_key','refresh'} or not isinstance(body.get('provider'),dict) or type(body.get('refresh',False)) is not bool:
            raise ConsoleError('INVALID_INPUT','模型能力读取参数无效')
        from selection_automation.stage3_provider import settings
        from selection_automation.stage3_credentials import saved_key,validate_key
        from selection_automation.api_reasoning import discover,list_models,capabilities
        try:
            provider=settings(body['provider'])
            if provider['mode']!='multimodal_api':raise ValueError('请选择外部多模态 API')
            supplied=body.get('api_key')
            key=validate_key(supplied) if supplied is not None else ''
        except ValueError as error:raise ConsoleError('INVALID_INPUT',str(error)) from None
        current,_=self.current_run_key()
        key=key or self.stage3_keys.get((current,provider['base_url']),'') or saved_key(self.root,provider)
        if not key and self.key==current and self.draft_key and self.draft_provider.get('base_url','').rstrip('/')==provider['base_url']:
            key=self.draft_key
        inventory=list_models(provider,key,refresh=body.get('refresh',False))
        model=next((row for row in inventory['models'] if row['id']==provider['model']),None)
        if model:catalog=model['reasoning']
        elif inventory['status']=='ready':catalog={**capabilities(None,provider),'message':'请从接口返回的模型列表中选择模型。'}
        else:catalog=discover(provider,key,refresh=body.get('refresh',False))
        return {'ok':True,'catalog':catalog,'inventory':inventory}

    def stop_stage3(self,body):
        if body: raise ConsoleError('INVALID_INPUT','停止第三层无需参数')
        key,_=self.current_run_key()
        pool_config=self.root/'runs'/key/'stage123/codex_pool_config.json' if key else None
        if pool_config and pool_config.exists():
            selection.save(pool_config,{**selection.read(pool_config,{}),'enabled':False})
        if not key: raise ConsoleError('NO_TASK','当前没有任务',409)
        from selection_automation.stage3_runtime import update_control
        update_control(self.root/'runs'/key/'stage123',paused=True,switch_pending=False,switch_cancelled=True,pending_provider=None)
        selection.save(self.root/'runs'/key/'stage123/STOP',{'time':time.time(),'reason':'user-request'})
        self.stage3_phase='停止中：等待已发出的请求返回并保存，采集和前两层继续运行'
        self.stage3_key=key
        return {'ok':True,'phase':self.stage3_phase}

    def control_stage3(self,body):
        if set(body)-{'action','agent_id'} or body.get('action') not in {'pause','resume','pause_agent','resume_agent'}:
            raise ConsoleError('INVALID_INPUT','请选择暂停、继续或 Agent 暂停操作')
        key,_=self.current_run_key()
        if not key:raise ConsoleError('NO_TASK','当前没有任务',409)
        from selection_automation.stage3_runtime import read_control,update_control
        folder=self.root/'runs'/key/'stage123';value=read_control(folder);action=body['action']
        if action in {'pause_agent','resume_agent'}:
            agent=body.get('agent_id')
            if agent not in {'codex','claude','gemini','opencode','workbuddy','acciowork','doubao'}:raise ConsoleError('INVALID_INPUT','Agent 无效')
            paused=set(value.get('paused_agents',[]))
            if action=='pause_agent':paused.add(agent)
            else:
                paused.discard(agent)
                from selection_automation.stage3_runtime import clear_agent_block
                clear_agent_block(folder,agent)
            value=update_control(folder,paused_agents=sorted(paused))
        else:
            if action=='resume':
                from selection_automation.stage3_runtime import resume_dispatch
                from selection_automation.stage3_provider import from_category
                provider=from_category(selection.read(folder.parent/'category.json',{}))
                check_stage3_local_service(provider,self.root)
                if provider!=from_category(selection.read(folder.parent/'category.json',{})):
                    return self.start_stage3({'provider':provider})
                value=resume_dispatch(folder,provider['agent_ids'] if provider['mode']=='local_agent' else ())
            else:value=update_control(folder,paused=True,switch_error=None)
            if action=='resume':
                from selection_automation.stage3_progress import controller_alive
                if not controller_alive(self.root/'runs'/key) and not codex_review_control(self.root/'runs'/key).get('running'):
                    return self.start_stage3({})
        return {'ok':True,'control':value,'message':'已暂停派发新组；已发送结果会继续保存' if action.startswith('pause') else '已继续，保留原有断点'}

    def _run_stage3(self,key,category_path,candidates,api_key):
        base=self.root/'runs'/key
        env={**os.environ,'DEEPSEEK_API_KEY':api_key,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
        try:
            result=self.invoke([str(self.root/'scripts/run_category_layer3.py'),'--category',str(category_path),
                                '--candidates',str(candidates),'--results',str(base/'stage123'/'results.jsonl')],
                               env=env,timeout=86400)
            incomplete=bool(result.returncode)
            self.stage3_phase=('第三层未完成，正在发布已保存的部分结果' if incomplete
                               else '第三层分析已处理当前候选，正在更新 Excel')
            published=self.invoke([str(self.root/'scripts/publish_all.py'),'--category',str(category_path)],
                                  env={k:v for k,v in env.items() if k!='DEEPSEEK_API_KEY'},timeout=3600)
            if published.returncode: raise ConsoleError('PUBLISH_FAILED','第三层已保存的结果仍保留，Excel 更新失败，请查看发布状态')
            self.stage3_phase=('第三层未完成，已保存的部分结果已更新至 Excel；请查看请求账本；请求被拒绝不代表零费用，费用待核对' if incomplete
                               else '第三层当前候选分析完成，Excel 已更新')
        except ConsoleError as exc: self.stage3_phase=exc.message
        except Exception: self.stage3_phase='第三层启动或分析失败，请查看日志和请求账本'
        finally:
            env.pop('DEEPSEEK_API_KEY',None)
            with self.lock:self.stage3_busy=False

    def resume_current(self, body):
        if body:
            raise ConsoleError('INVALID_INPUT','继续任务无需重新填写参数')
        with self.lock:
            if self.busy or self.stage3_busy or self.workspace_stop_pending or self.owner():
                raise ConsoleError('TASK_BUSY','任务仍在运行或退出中，请稍后重试',409)
            key,_ = self.current_run_key()
            if not key:key=selection.read(self.root/'runtime/last_run.json',{}).get('category_key')
            if not key:
                raise ConsoleError('NO_TASK','没有可继续的任务，请先新建任务',409)
            base,category = self.history_base(key)
            self.key=key; self.draft_used=True; self.busy=True; self.resume_launch_active=True; self.cancel.clear()
            self.phase='继续请求已提交，正在启动后台程序；尚未确认运行'
            queued_phase=self.phase
            job={'id':uuid.uuid4().hex,'category_key':key,'requested_at':time.time()}
            try:
                self.record_resume_launch(base,job,'queued')
                threading.Thread(target=self._resume_current,args=(base,job),daemon=True).start()
            except Exception:
                self.busy=False;self.resume_launch_active=False;self.phase='继续启动失败，启动线程未创建；已保留数据和断点'
                selection.request_task_stop(base)
                self.record_resume_launch(base,job,'failed',error_code='THREAD_START_FAILED',message=self.phase)
                raise ConsoleError('RESUME_FAILED',self.phase,500) from None
            return {'ok':True,'phase':queued_phase,'category_key':key,'launch_id':job['id'],'launch_pending':True}

    def record_resume_launch(self,base,job,state,**fields):
        """Keep launch evidence without recording credentials or command lines."""
        row={**job,'state':state,'updated_at':time.time(),**fields}
        selection.save(base/'resume_launch.json',row)
        folder=base/'logs';folder.mkdir(parents=True,exist_ok=True)
        with (folder/'resume_launch.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(row,ensure_ascii=False)+'\n');stream.flush()
        return row

    def _resume_current(self,base,job):
        key=job['category_key']
        try:
            if self.cancel.is_set():raise ConsoleError('CANCELLED','继续启动已取消，数据和断点已保留')
            self.record_resume_launch(base,job,'launching')
            result=self.invoke([str(self.root/'scripts/selection.py'),'start','--preserve-prepared',
                                '--category',str(base/'category.json')],timeout=90,cancellable=True)
            if self.cancel.is_set():raise ConsoleError('CANCELLED','继续启动已取消，数据和断点已保留')
            if result.returncode:
                raise ConsoleError('RESUME_EXIT_FAILED',f'继续启动失败（退出码 {result.returncode}），数据和断点已保留')
            owner=self.registered_owner(key)
            if self.cancel.is_set():raise ConsoleError('CANCELLED','继续启动已取消，数据和断点已保留')
            if not owner:
                raise ConsoleError('OWNER_NOT_REGISTERED','继续启动失败：监督进程未登记，数据和断点已保留')
            with self.lock:
                if self.cancel.is_set():raise ConsoleError('CANCELLED','继续启动已取消，数据和断点已保留')
                self.record_resume_launch(base,job,'running',owner={'pid':owner['pid'],'created':owner['created']})
                self.phase='已确认原任务监督进程启动，保留已采集数据和断点'
        except ConsoleError as exc:
            selection.request_task_stop(base)
            cancelled=exc.code=='CANCELLED' or self.cancel.is_set()
            self.phase=('继续启动已取消，数据和断点已保留' if cancelled else exc.message)
            self.record_resume_launch(base,job,'cancelled' if cancelled else 'failed',error_code=exc.code,message=self.phase)
            if not cancelled:
                selection.save(base/'status.json',{'状态':'继续启动失败，已请求停止','原因':self.phase,
                    '阶段':{'resume':{'状态':'启动失败','错误码':exc.code,'日志':str(base/'logs/resume_launch.jsonl')}}})
        except Exception as exc:
            selection.request_task_stop(base)
            self.phase='继续启动失败，已请求停止；数据和断点已保留，请查看启动记录'
            self.record_resume_launch(base,job,'failed',error_code='RESUME_FAILED',error_type=type(exc).__name__,message=self.phase)
            selection.save(base/'status.json',{'状态':'继续启动失败，已请求停止','原因':self.phase,
                '阶段':{'resume':{'状态':'启动失败','错误码':'RESUME_FAILED','日志':str(base/'logs/resume_launch.jsonl')}}})
        finally:
            # The CLI may have cleared an earlier STOP while cancellation arrived.
            # Reassert all cooperative gates; never kill an unrelated scheduled host.
            try:
                if self.cancel.is_set():selection.request_task_stop(base)
            finally:
                with self.lock:self.busy=False;self.resume_launch_active=False

    def retry_preparation(self, body):
        with self.lock:
            if set(body) != {'category_key'} or body['category_key'] != self.key:
                raise ConsoleError('INVALID_INPUT','任务已变化，请刷新后重试',409)
            if self.busy or self.owner():
                raise ConsoleError('TASK_BUSY','任务仍在运行或退出中，请稍后重试',409)
            base, category = self.history_base(self.key)
            details = selection.read(base/'status.json',{})
            if details.get('阶段',{}).get('bootstrap',{}).get('状态') != '启动准备失败':
                raise ConsoleError('NOT_PREPARATION_FAILURE','当前任务不是环境准备失败，无需重复初始化',409)
            result = self.invoke([str(self.root/'scripts/selection.py'),'start','--category',str(base/'category.json')])
            if result.returncode:
                raise ConsoleError('RETRY_FAILED','重试启动失败，已保留任务和环境断点',409)
            started = selection.read(base/'status.json',{})
            if started.get('状态') == '启动失败':
                raise ConsoleError('RETRY_FAILED','监督进程未启动，已保留断点，请查看启动日志',409)
            self.phase='正在重试准备环境'; self.draft_used=True
            return {'ok':True,'phase':self.phase,'category_key':self.key}

    def action(self, name, body):
        with self.lock:
            if name=='stop':
                if body: raise ConsoleError('INVALID_INPUT','停止不接受额外参数')
                self.cancel.set()
                self.workspace_stop_pending=True
                self.workspace_stop_started=time.monotonic()
            elif set(body)!={'worker'} or type(body['worker']) is not int or not 1<=body['worker']<=32:
                raise ConsoleError('INVALID_INPUT','采集路编号无效')
            owner=self.owner(); key=(owner or {}).get('category_key') or self.key
            if not key:
                if name=='stop':
                    if not self.busy:self.clear_workspace()
                    else:self.phase='停止中'
                    return {'ok':True,'phase':self.phase}
                raise ConsoleError('NO_ACTIVE_TASK','没有可换session的任务',409)
            if name=='stop' and self.resume_launch_active:
                # A resume job must remain cancellable before it has an owner.
                # Waiting for a second CLI here would block HTTP status and Stop.
                selection.request_task_stop(self.root/'runs'/key)
                self.phase='停止中：正在取消启动并保留任务断点'
                return {'ok':True,'phase':self.phase}
            if name!='stop':
                if not owner or (self.root/'runs'/key/'STOP').exists():
                    raise ConsoleError('NO_ACTIVE_TASK','当前没有运行中的采集监督器，请先启动采集任务',409)
                category=selection.read(self.root/'runs'/key/'category.json',{})
                manifest=selection.read(self.root/'config/sessions.json',{})
                expected=f"worker-{body['worker']:02d}-new"
                if body['worker']>category_worker_count(category) or not any(w.get('name')==expected for w in manifest.get('workers',[])):
                    raise ConsoleError('INVALID_INPUT','该采集路尚未建立或不属于当前任务')
                try: request=selection.request_rotation(self.root/'runs'/key,[body['worker']])
                except ValueError as exc: raise ConsoleError('NO_ACTIVE_TASK',str(exc),409) from None
                return {'ok':True,'phase':'换session已排队，执行结果将在对应采集路显示','request_id':request['id']}
            argv=[str(self.root/'scripts/selection.py'),'stop' if name=='stop' else 'rotate','--category-key',key]
            if name!='stop': argv+=['--workers',str(body['worker'])]
            if self.invoke(argv).returncode: raise ConsoleError('ACTION_FAILED','操作未成功，请检查任务状态',409)
            return {'ok':True,'phase':'已请求停止' if name=='stop' else '已请求换session'}

    def generation_progress(self):
        raw=selection.read(self.progress_file,{}) if self.progress_file else {}
        if raw.get('stage') not in {'dependencies','discovering','validating','generating','complete','error'}: return {}
        result={key:raw[key] for key in ('stage','message','error_code','failed_stage') if isinstance(raw.get(key),str)}
        identifier=raw.get('model_request_id') or raw.get('response_id')
        if isinstance(identifier,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',identifier):
            result['request_id']=identifier;result['message']=result.get('message','')+' · 请求编号 '+identifier
        if type(raw.get('depth')) is int: result['depth']=raw['depth']
        started=raw.get('model_started_at')
        if raw.get('model_waiting') and type(started) in (int,float):
            result['message']=result.get('message','模型生成中')+f' · 已等待 {max(0,int(time.time()-started))} 秒'
        if raw.get('stage')=='complete' and self.preparation_path:
            # Generation is only the first phase. Publish collector facts rather
            # than keeping its completed message over the real current phase.
            payload=selection.read(self.preparation_path.with_suffix('.amazon.json'),{})
            category=selection.read(self.preparation_path,{})
            target=category.get('amazon',{}).get('target_count')
            rows=payload.get('records') if isinstance(payload.get('records'),list) else []
            expected=category.get('category_key')
            valid=(payload.get('category_key')==expected and payload.get('source_url')==category.get('amazon',{}).get('bestsellers_url'))
            queue=payload.get('detail_queue') if valid and isinstance(payload.get('detail_queue'),dict) else {}
            completed=queue.get('completed_count')
            if type(completed) is not int or type(target) is not int or not 0<=completed<=target:completed=None
            gate_count=0;error_codes=set()
            for row in rows if valid else []:
                error=row.get('detail_error') or {}
                continuation='/checkpoint/continue' in str(error.get('url','')).lower()
                code='AMAZON_CONTINUATION_REQUIRED' if continuation else error.get('code')
                if isinstance(code,str) and re.fullmatch('[A-Z0-9_]{1,80}',code):error_codes.add(code)
                if continuation or error.get('retryable') is False:gate_count+=1
            stage='amazon_details' if self.busy and self.phase.startswith('正在采集 Amazon') else 'preparation'
            message=self.phase
            if stage=='amazon_details':
                message=f'正在采集 Amazon 详情：{completed if completed is not None else "待核对"}/{target or "待核对"} 件已完成'
                if gate_count:message+=f'；{gate_count} 件遇到访问门禁，原断点保留'
                elif completed is not None and completed<target:message+='；未完成商品仍需采齐详情'
            elif valid and type(target) is int:
                message+=f' · Amazon 详情：{completed if completed is not None else "待核对"}/{target} 件已完成'
                if gate_count:message+=f'，{gate_count} 件访问门禁'
            result={'stage':stage,'message':message,'rule_generation_complete':True,
                    'amazon_details_completed':completed,'amazon_details_target':target,
                    'amazon_gate_count':gate_count,'amazon_error_codes':sorted(error_codes)}
            result['amazon_route_mode']=(self.pending_scope or {}).get('amazon_route_mode','AUTO')
            route=payload.get('amazon_browser_route') if valid else None
            if (isinstance(route,dict) and route.get('mode') in ('AUTO','DIRECT')
                    and route.get('transport') in ('CONFIGURED_PROXY','SYSTEM_ROUTE','BROWSER_DIRECT')):
                result['amazon_browser_route']={key:route[key] for key in ('mode','transport')}
                result['amazon_browser_route']['egress_verified']=route.get('egress_verified') is True
            if stage=='amazon_details':result['message']+='；Amazon 浏览器'+('直连' if result['amazon_route_mode']=='DIRECT' else '自动选择出口')
            try:
                age=max(0,int(time.time()-self.preparation_path.with_suffix('.amazon.json').stat().st_mtime))
                result['observation_age_seconds']=age
                if self.busy and stage=='amazon_details' and age>=60:result['message']+=f'；最近商品断点更新于 {age} 秒前'
            except OSError:pass
        return result

    def select_local_proxy(self,body):
        with self.lock:
            if self.busy or self.stage3_busy or self.owner():raise ConsoleError('ALREADY_RUNNING','任务运行中不能更改出口；请先安全停止',409)
            if set(body)!={'host','port','protocol'}:raise ConsoleError('INVALID_INPUT','请选择检测到的本机出口')
            from selection_automation.local_proxy import discover,choose,owned
            from selection_automation.company_bundle import normalize
            from selection_automation.network_readiness import NetworkNotReady
            host,port,protocol=body['host'],body['port'],body['protocol']
            from selection_automation.local_proxy import loopback
            if not isinstance(host,str) or not loopback(host) or isinstance(port,bool) or not isinstance(port,int) or not 1<=port<=65535 or not isinstance(protocol,str) or protocol not in {'SOCKS5','HTTP'}:raise ConsoleError('INVALID_INPUT','本机出口参数无效')
            try:transport=choose(discover(),'AUTO',body)
            except NetworkNotReady as error:raise ConsoleError('NOT_READY',str(error),409)
            if not owned({**transport['local_proxy_owner'],'host':host,'port':port}):raise ConsoleError('NOT_READY','代理进程已变化，请重新检测',409)
            selection.save(self.root/'runtime/local-proxy-choice.json',body)
            return {'ok':True,'message':'已记录选择；下次空闲准备时使用，当前网络与VPN设置未改变。'}

    def workspace_writers_stopped(self,base):
        """A supervisor exit alone does not establish that detached writers stopped."""
        if base is None:return True,{}
        from local_service_recovery import service_alive
        states={}
        for name in ('temu_union','stage12'):
            try:states[name]=service_alive(self.root,base,name)
            except (OSError,ValueError,KeyError,TypeError):states[name]=None
        return all(value is False for value in states.values()),states

    def finish_workspace_stop(self,owner):
        if self.workspace_stop_pending and not owner and not self.busy and not self.stage3_busy:
            from selection_automation.stage3_progress import controller_alive
            base=self.root/'runs'/self.key if self.key else None
            writers_stopped,self.workspace_writer_states=self.workspace_writers_stopped(base)
            if writers_stopped and (base is None or (not controller_alive(base) and not codex_review_control(base,scan_batches=False).get('running'))):
                if self.pending_scope is not None:
                    self.workspace_stop_pending=False;self.workspace_stop_started=None
                    self.phase='准备已停止，已生成规则和 Amazon 断点已保留，可继续准备'
                    self.generation_error={'code':'PREPARATION_STOPPED','message':self.phase}
                    self.persist_preparation()
                else:
                    # A CLI with an earlier live_owner result can overwrite the
                    # supervisor's terminal status with "stopping". Only after
                    # every existing ownership gate above closes may history be
                    # finalized; preserve all stage evidence and task files.
                    if base is not None and (base/'STOP').is_file():
                        details=selection.read(base/'status.json',{})
                        if details.get('状态') in ('停止中','运行中','启动中'):
                            selection.save(base/'status.json',{**details,'状态':'已停止'})
                    self.clear_workspace()
            else:
                elapsed=time.monotonic()-(getattr(self,'workspace_stop_started',None) or time.monotonic())
                self.phase=('停止中：等待独立后台程序退出，当前任务和断点保留' if elapsed<30 else
                            '停止仍在等待后台退出或身份核对；任务保留，请查看后台停止状态')

    def status(self):
        if self.busy and self.draft and self.draft_path and self.phase.startswith('正在逐件核对全部'):
            self.draft['match_criteria']=selection.read(self.draft_path,{}).get('match_criteria')
        owner=self.owner()
        self.finish_workspace_stop(owner)
        key,_=self.current_run_key()
        preparing_new=self.key is None and self.pending_scope is not None
        if preparing_new: key=None
        details={}; workers=[];preparing_environment=False
        if key and re.fullmatch('[a-z][a-z0-9_]{0,63}',key):
            base=self.root/'runs'/key
            details=selection.read(base/'status.json',{})
            reconcile_stage12_watch(self.root, base, details)
            from dashboard_progress import enrich_progress
            preparing_environment=details.get('阶段',{}).get('bootstrap',{}).get('状态') in ('运行中','启动准备失败')
            if not preparing_environment:
                details=enrich_progress(base,details,watch_running=bool(owner and owner.get('category_key')==key),asynchronous=True)
                from local_service_recovery import apply_local_recovery
                apply_local_recovery(self.root,base,details)
                from collection_progress import collection_total
                current_category=selection.read(base/'category.json',{})
                if current_category:
                    temu_progress=details.setdefault('阶段',{}).setdefault('temu',{})
                    live_total=collection_total(base,current_category,rate_snapshot=temu_progress.get('overall') or {})
                    if live_total:temu_progress['overall']=live_total
            manifest=selection.read(self.root/'config/sessions.json',{})
            from selection_automation.rotation_queue import worker_requests
            controls=worker_requests(base)
            profiles={item.get('name'):item for item in manifest.get('workers',[]) if isinstance(item,dict)}
            for path in ([] if preparing_environment else worker_supervisor_paths(base)):
                value=selection.read(path,{})
                try:
                    live=selection.worker_snapshot(base,path.stem)[1]
                    if value.get('status')=='FAILED_AFTER_3_ATTEMPTS':
                        # A terminal owner record outranks the collector's last
                        # saved page state. This display must not change recovery.
                        previous=live if isinstance(live,dict) else {}
                        live={**previous,'状态':'采集重试 3 次后失败，已停止','采集状态':value['status'],
                              '阻断':True,'需人工介入':True,'最近采集状态':previous.get('采集状态'),
                              '监督状态更新时间':value.get('updated_at'),'监督任务运行中':bool(owner),
                              '建议动作':('后台监督任务仍在运行，本路已停止；既有结果和断点保留，可点击本路“重启”重试。'
                                  if owner else '后台监督任务未运行，既有结果和断点保留；请点击“开始 / 继续采集”从断点继续。')}
                        live.pop('浏览器保留',None)  # No current browser-liveness claim from an old snapshot.
                    value['live_snapshot']=live
                    if isinstance(live,dict):details.setdefault('阶段',{})[path.stem]=live
                    value['status_updated_at']=((value.get('updated_at') or path.stat().st_mtime)
                        if value.get('status')=='FAILED_AFTER_3_ATTEMPTS' else live.get('采集更新时间') or path.stat().st_mtime)
                except Exception: pass
                name=value.get('worker',path.stem)
                profile=profiles.get(name,{})
                identity={field:profile[field] for field in ('profile_id','profile_serial','serial_number') if isinstance(profile.get(field),(str,int))}
                control=controls.get(name)
                if control and control['status'] in ('QUEUED','RUNNING') and not owner:
                    control={**control,'status':'INTERRUPTED','message':'监督器已退出，此请求未完成'}
                workers.append({**value,**identity,'name':name,'rotation_control':control})
        if owner and not self.busy and not preparing_environment:
            details.update(selection.supervision_status(details.get('阶段',{})))
        phase=(self.phase if self.busy or (self.key is None and self.phase!='尚未启动') or
               (self.draft and not self.draft_used) else details.get('状态') or self.phase)
        generation=self.generation_progress()
        if generation and self.busy and self.phase.startswith('生成品类'):
            phase=generation.get('message') or phase
            if 'depth' in generation: phase=f"定位榜单 · 第{generation['depth']}步：{phase}"
        elif generation.get('stage')=='amazon_details' and self.busy:
            phase=generation.get('message') or phase
        stale=not owner and not self.busy and phase in ('运行中','启动中','停止中')
        if stale: phase='监督进程已退出，旧状态未完成更新'
        collection_complete=False
        if key and not preparing_new:
            overall=details.get('阶段',{}).get('temu',{}).get('overall',{})
            amazon=selection.read(self.root/'runs'/key/'amazon/amazon_frontend_latest.json',{})
            target=(selection.read(self.root/'runs'/key/'category.json',{}).get('amazon') or {}).get('target_count')
            collection_complete=bool(overall.get('all_passes_complete') and workers and
                all(worker.get('status')=='WORKER_COMPLETE' for worker in workers) and
                type(target) is int and target>0 and amazon.get('status')=='COMPLETE' and
                amazon.get('details_complete') is True and len(amazon.get('records') or [])>=target)
            if collection_complete and not self.resume_launch_active:phase='全部采集已完成'
        def tone(value):
            if not isinstance(value,dict): return 'neutral'
            state=' '.join(str(value.get(field) or '') for field in ('状态','status','采集状态','query_status')).upper()
            if value.get('automatic_recovery'):return 'neutral'
            if re.search(r'REPLACING_.*_ENVIRONMENT|正在自动更换环境|LOGIN_REQUIRED|WAITING_LOGIN|WAITING_INITIAL_PRODUCTS|WAITING_POST_VERIFICATION_SEARCH_BOX|WAITING_FLEET_SLOT|WAITING_HTTP_COOLDOWN|WAITING_SITE_COOLDOWN|WAITING_SORT_RETRY|WAITING_INITIAL_PRODUCTS_RETRY',state): return 'green'
            if re.search(r'FAILED|ERROR|失败|错误',state): return 'red'
            if any(value.get(flag) is True for flag in ('需人工介入','manual_action_required','human_action_required')) or re.search(
                    r'CAPTCHA|VERIFICATION|HUMAN_ACTION|LOGIN_REQUIRED|WAITING_LOGIN|ACTION_REQUIRED|验证码|等待人工|待人工|需要人工',state): return 'red'
            if value.get('阻断') or re.search(r'STOPPED_HTTP|STOPPED_PROXY|ACCESS_DENIED|PROVIDER_BLOCKED|存在阻断',state): return 'red'
            return 'neutral'
        def current_worker(worker):
            if str(worker.get('status','')).endswith('_RECOVERY_FAILED'): return worker
            if re.match(r'REPLACING_.*_ENVIRONMENT',str(worker.get('status',''))): return {'status':worker['status']}
            live=worker.get('live_snapshot')
            # A fresh collector observation replaces old supervisor warnings.
            if isinstance(live,dict) and live:
                return live
            return worker
        generation_failed=preparing_new and (bool(self.generation_error) or generation.get('stage')=='error')
        tones=[tone({'状态':phase})]+[tone(current_worker(worker)) for worker in workers]
        tones += [tone(value) for value in details.values() if isinstance(value,dict)]
        tones += [tone(value) for name,value in details.get('阶段',{}).items() if name!='stage3' and isinstance(value,dict)]
        red=generation_failed or 'red' in tones
        yellow=stale or 'yellow' in tones
        publish_errors=selection.read(self.root/'runs'/key/'publish_state.json',{}).get('errors',{}) if key else {}
        business_error=bool(publish_errors.get('commercial'))
        red=red or business_error
        market_state=selection.read(self.root/'runs'/key/'market/status.json',{}) if key else {}
        red=red or market_state.get('status') in ('FAILED','CATEGORY_UNRESOLVED')
        yellow=yellow or market_state.get('status')=='ACTION_REQUIRED'
        category=selection.read(self.root/'runs'/key/'category.json',{}) if key else {}
        scope=(self.pending_scope if preparing_new else
               {'category':category.get('category_name_zh'),'amazon_count':category.get('amazon',{}).get('target_count'),
                'per_keyword':category.get('temu',{}).get('max_products_per_keyword'),'worker_count':category_worker_count(category),
                'execution_mode':category.get('execution_mode','full')} if category else {})
        from selection_automation.stage3_progress import controller_alive
        stage3_base=self.root/'runs'/key if key else None
        from selection_automation.stage3_runtime import read_control
        runtime_control=read_control(stage3_base/'stage123') if stage3_base else {}
        if runtime_control.get('switch_pending') and isinstance(runtime_control.get('pending_provider'),dict):
            self.queue_stage3_switch(key,stage3_base,runtime_control['pending_provider'],runtime_control.get('pending_configure_only',False))
        stage3_enabled=bool(key and (stage3_base/'stage123/paid_enabled.json').is_file() and not (stage3_base/'stage123/STOP').exists())
        stage3_running=bool(key and controller_alive(stage3_base))
        stage3_state=selection.read(stage3_base/'stage123/controller.json',{}) if key else {}
        stage3_phase=('停止中，正在保存已发出的请求结果' if stage3_running and not stage3_enabled else
            stage3_state.get('phase') if stage3_state else '等待手动启动')
        stage3_ready=bool(key and not self.busy and category.get('execution_mode')!='collection')
        stage3_progress=self.stage3_snapshot(key) if key else {}
        if stage3_progress.get('counting') or stage3_progress.get('refreshing') or type(stage3_progress.get('total')) is not int:
            stage3_ready=False
            if not stage3_running: stage3_phase=('显示最近核验快照；新增输入正在核对，当前待处理数尚未确认'
                if stage3_progress.get('refreshing') else stage3_progress.get('message') or '正在核对已处理候选，请等待进度读取完成')
        if not stage3_progress.get('refreshing') and not owner and stage3_progress.get('total',0)>0 and stage3_progress.get('pending_after_codex',stage3_progress.get('pending'))==0:
            stage3_ready=False
            if not stage3_running: stage3_phase='当前候选已全部处理，结果已保留；新增候选后可继续分析'
        codex_control=self.codex_review_snapshot(key) if key else {'available':False,'running':False}
        from runtime_overview import stage3_display,stopped_state
        provider=__import__('selection_automation.stage3_provider',fromlist=['from_category']).from_category(category)
        stopped=stopped_state(stage3_base,runtime_control,task_running=bool(owner) or self.busy,
            stage3_running=stage3_running or bool(codex_control.get('running')) or self.stage3_busy) if key else {}
        stage3_control={'enabled':stage3_enabled,'can_start':stage3_ready,'running':stage3_running,
                       'stopped':bool(stopped.get('stage3_stopped')),
                       'can_stop':stage3_enabled or stage3_running or bool(codex_control.get('running')),
                       'progress':stage3_progress,'phase':stage3_phase,'codex_review':codex_control,
                       'error':bool(stage3_state.get('error')),'error_code':stage3_state.get('error_code')}
        if stage3_control['stopped']:stage3_control.update(enabled=False,can_stop=False,phase='已停止，已完成结果和历史记录已保留')
        if key and category.get('execution_mode')!='collection':
            live_stage3=stage3_display(stage3_control,runtime_control,provider)
            details.setdefault('阶段',{})['stage3']=live_stage3
            if live_stage3.get('需人工介入'):red=True
            if owner and not self.busy and not preparing_environment:
                details.update(selection.supervision_status(details['阶段']))
                if not collection_complete:phase=details['状态']
                current_tones=[tone({'状态':phase})]+[tone(current_worker(worker)) for worker in workers]
                current_tones.extend(tone(value) for value in details['阶段'].values() if isinstance(value,dict))
                red=(generation_failed or business_error or market_state.get('status') in ('FAILED','CATEGORY_UNRESOLVED')
                     or 'red' in current_tones)
        from selection_automation.network_readiness import describe
        network_readiness=selection.read(self.root/'runtime/network-readiness.json',{}) or describe(selection.read(self.root/'runtime/secrets/proxyshare.settings.json',{}))
        collection_sources={}
        if key and category:
            from selection_automation.merge_temu_snapshots import current_source_summary
            try:collection_sources=current_source_summary(self.root,category)
            except (OSError,ValueError,KeyError,TypeError):collection_sources={'source_count_errors':['task_source']}
        if self.workspace_stop_pending:
            phase=self.phase
            details['后台停止状态']={name:('已退出' if value is False else '仍在运行' if value is True else '身份待核对')
                for name,value in getattr(self,'workspace_writer_states',{}).items()}
        resume_launch=selection.read(self.root/'runs'/key/'resume_launch.json',{}) if key else {}
        resume_error=(resume_launch.get('message') if resume_launch.get('state')=='failed' and not owner and not self.busy else None)
        if resume_error:phase=resume_error
        return clean({'ok':True,'loaded_runtime':selection.LOADED_VERSION,'api_reasoning_supported':True,
                      'task_stopped':bool(stopped.get('task_stopped')) and not self.workspace_stop_pending,'match_criteria':category.get('match_criteria'),
                      'network_readiness':network_readiness,
                      'task_runtime':selection.read(self.root/'runs'/key/'runtime_version.json',{}) if key else {},
                      'console_busy':self.busy,'active':bool(owner) or self.busy or self.stage3_busy or self.workspace_stop_pending,'category_key':key,'phase':phase if self.workspace_stop_pending or resume_error else '已停止' if stopped.get('task_stopped') else phase,
                      'severity':'yellow' if self.workspace_stop_pending else 'red' if resume_error else 'neutral' if stopped.get('task_stopped') else 'red' if red else 'yellow' if yellow else 'green' if owner or self.busy or self.stage3_busy else 'neutral','workers':workers,'details':details,
                      'resume_launch':resume_launch,
                      'generation':generation,'generation_error':self.generation_error if preparing_new else None,
                      'can_retry_generation':bool(preparing_new and generation_failed and not self.busy),
                      'can_resume_preparation':bool(preparing_new and self.preparation_path and not self.busy),
                      'can_resume':bool(not owner and not self.busy and not preparing_new and key and (self.root/'runs'/key/'category.json').is_file()),
                      'can_retry_preparation':bool(not owner and not self.busy and not preparing_new and details.get('阶段',{}).get('bootstrap',{}).get('状态')=='启动准备失败'),
                      'preparation_error':resume_error or (details.get('原因') if details.get('阶段',{}).get('bootstrap',{}).get('状态')=='启动准备失败' else None),
                      'draft':None if self.draft_used else self.draft,'scope':scope,
                      'plain_application':self.plain_application,
                      'saved_credential_available':bool(self.saved_credential()),
                      'rule_provider':self.draft_provider if self.draft and not preparing_new else {
                          'base_url':(self.pending_scope or {}).get('api_base_url','https://api.deepseek.com'),
                          'model':(self.pending_scope or {}).get('api_model','deepseek-flash')},
                      'excel_files':self.excel_files(key),
                      'commercial': self.matching_snapshot(key,category) if key and not preparing_new and not business_error else {},
                      'stage3_runtime':runtime_control,
                      'commercial_error': '商业判断更新失败，暂不展示旧结论，请检查任务发布状态' if business_error else None,
                      'market_status':market_state,'market_busy':self.market_busy,
                      'frontend_market':self.frontend_market_snapshot(key) if key and not preparing_new else {},
                      'collection_complete':collection_complete,
                      'collection_sources':collection_sources,
                      'stage3_key_configured':bool((key,__import__('selection_automation.stage3_provider',fromlist=['from_category']).from_category(category)['base_url']) in self.stage3_keys or __import__('selection_automation.stage3_credentials',fromlist=['saved_key']).saved_key(self.root,__import__('selection_automation.stage3_provider',fromlist=['from_category']).from_category(category))),
                      'stage3_concurrency':selection.read(self.root/'runs'/key/'stage123/concurrency.json',{}) if key else {},
                      'stage3_provider':provider,
                      'stage3_batch_capacity':stage3_batch_capacity(category,provider),
                      'stage3_control':stage3_control})


class LocalConsoleServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits multiple listeners and can serve stale code.
    allow_reuse_address=False

    def __init__(self,*args,**kwargs):
        self.release_lock=threading.RLock()
        self.release=None
        super().__init__(*args,**kwargs)

    def server_bind(self):
        if os.name=='nt':
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send_json(self,value,status=200):
        raw=json.dumps(value,ensure_ascii=False).encode('utf-8'); self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def trusted(self,write=False):
        expected=f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host')!=expected: raise ConsoleError('INVALID_ORIGIN','仅允许本机页面访问',403)
        origin=self.headers.get('Origin')
        if (write and origin!=f'http://{expected}') or (origin and origin!=f'http://{expected}'):
            raise ConsoleError('INVALID_ORIGIN','请求来源不匹配',403)
    def send_workbook(self,path):
        if not path: raise ConsoleError('EXCEL_NOT_READY','这份 Excel 尚未生成',404)
        with path.open('rb') as stream:
            self.send_response(200)
            self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(path.name,safe=''))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(os.fstat(stream.fileno()).st_size))
            self.end_headers()
            shutil.copyfileobj(stream,self.wfile,length=64*1024)
    def do_GET(self):
        try:
            self.trusted()
            if self.path=='/api/health':
                return self.send_json({'ok':True,'service':'selection-workbench','pid':os.getpid(),'time':time.time()})
            if self.path=='/api/local-models/scan':
                from selection_automation.local_model_inventory import scan_status
                return self.send_json({'ok':True,**scan_status(self.server.console.root)})
            if self.path=='/api/status':
                state=self.server.console.status()
                if getattr(self.server,'release',None):
                    with self.server.release_lock:state['release_update']=self.server.release.check()
                return self.send_json(state)
            if self.path=='/api/status-profile': return self.send_json(self.server.console.profile_status_once())
            if self.path=='/api/monitor-status':
                state=self.server.console.monitor_status()
                if getattr(self.server,'release',None):state['release_update']=self.server.release.snapshot()
                return self.send_json(state)
            if self.path=='/api/visual-review': return self.send_json(self.server.console.visual_review())
            if self.path=='/api/operator': return self.send_json(self.server.console.operator_view())
            if self.path=='/api/history': return self.send_json(self.server.console.history_snapshot())
            if self.path=='/api/developer': return self.send_json(self.server.console.developer_info())
            if self.path=='/api/local-agents':
                from selection_automation.local_agents import discover
                return self.send_json({'ok':True,'agents':discover(self.server.console.root)})
            if self.path.startswith('/api/developer/'):
                parts=self.path.removeprefix('/api/developer/').split('/')
                if len(parts)==1: return self.send_json(self.server.console.developer_info(parts[0]))
                if len(parts)==2 and parts[1]=='bundle':
                    data=self.server.console.developer_bundle(parts[0])
                    self.send_response(200)
                    self.send_header('Content-Type','application/zip')
                    self.send_header('Content-Disposition','attachment; filename="handoff-'+parts[0]+'.zip"')
                    self.send_header('Cache-Control','no-store')
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.send_header('Content-Length',str(len(data)))
                    self.end_headers();self.wfile.write(data);return
                raise ConsoleError('NOT_FOUND','对接文件不存在',404)
            if self.path.startswith('/api/history/'):
                parts=self.path.removeprefix('/api/history/').split('/')
                if len(parts)==1: return self.send_json(self.server.console.history_snapshot(parts[0]))
                if len(parts)==3 and parts[1]=='excel':
                    self.server.console.history_base(parts[0])
                    return self.send_workbook(self.server.console.excel_workbook(parts[2],parts[0]))
                raise ConsoleError('NOT_FOUND','历史任务不存在',404)
            if self.path.startswith('/api/excel/'):
                kind=self.path.removeprefix('/api/excel/')
                path=self.server.console.excel_workbook(kind)
                if not path:
                    self.server.console.refresh_excel(kind)
                    path=self.server.console.excel_workbook(kind)
                return self.send_workbook(path)
            pages = {'/': 'index.html', '/index.html': 'index.html', '/accuracy-benchmark': 'accuracy_benchmark.html'}
            if self.path not in pages: raise ConsoleError('NOT_FOUND','不存在',404)
            data=(self.server.console.root/'web'/pages[self.path]).read_bytes()
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
        except (ConnectionError,BrokenPipeError): return
        except ConsoleError as exc: self.send_json({'ok':False,'error':{'code':exc.code,'message':exc.message}},exc.status)
        except Exception: self.send_json({'ok':False,'error':{'code':'READ_FAILED','message':'页面或状态读取失败'}},500)
    def do_POST(self):
        # Model metadata discovery does not mutate task/provider state. Never
        # queue a scan/cancel behind a synchronous repair or block task Stop.
        if self.path in ('/api/local-models/repair','/api/local-models/scan'):
            return self._do_POST()
        with getattr(self.server,'release_lock',nullcontext()):self._do_POST()

    def _do_POST(self):
        body_read=False
        try:
            self.trusted(True)
            if self.headers.get('Content-Type','').split(';')[0]!='application/json': raise ConsoleError('INVALID_INPUT','仅接受JSON')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=524288: raise ConsoleError('INVALID_INPUT','请求体大小无效')
            body=json.loads(self.rfile.read(length))
            body_read=True
            if not isinstance(body,dict): raise ConsoleError('INVALID_INPUT','请求体应为对象')
            if getattr(self.server,'release',None) and self.path not in ('/api/stop','/api/stop-stage3','/api/open-excel','/api/refresh-market',
                    '/api/local-agents/models','/api/local-agents/refresh','/api/local-models/repair','/api/local-models/scan','/api/stage3/api-reasoning','/api/stage3/concurrency','/api/stage3/concurrency-default','/api/control-stage3'):
                update=self.server.release.check(force=True)
                if update['pending']:raise ConsoleError('UPDATE_PENDING',update['message'],409)
            if self.path in ('/api/start','/api/generate'): return self.send_json(self.server.console.start(body),202)
            if self.path=='/api/restore': return self.send_json(self.server.console.restore_history(body))
            if self.path=='/api/network/proxy-choice': return self.send_json(self.server.console.select_local_proxy(body))
            if self.path=='/api/operator/subscribe': return self.send_json(self.server.console.operator_subscribe(body))
            if self.path=='/api/operator/feedback': return self.send_json(self.server.console.operator_feedback(body))
            if self.path=='/api/resume': return self.send_json(self.server.console.resume_current(body),202)
            if self.path=='/api/retry-preparation': return self.send_json(self.server.console.retry_preparation(body),202)
            if self.path=='/api/plain-rules': return self.send_json(self.server.console.plain_rules(body),202)
            if self.path=='/api/match-criteria': return self.send_json(self.server.console.select_match_criteria(body))
            if self.path=='/api/run': return self.send_json(self.server.console.run_draft(body),202)
            if self.path=='/api/start-stage3': return self.send_json(self.server.console.start_stage3(body),202)
            if self.path=='/api/stage3/authorize-schema-retry': return self.send_json(self.server.console.authorize_schema_retry(body))
            if self.path=='/api/configure-stage3': return self.send_json(self.server.console.start_stage3(body,configure_only=True))
            if self.path=='/api/stage3/concurrency': return self.send_json(self.server.console.set_stage3_concurrency(body))
            if self.path=='/api/stage3/api-reasoning': return self.send_json(self.server.console.api_reasoning_catalog(body))
            if self.path=='/api/stage3/concurrency-default':
                from selection_automation.stage3_provider import settings
                from selection_automation.analysis_concurrency import recommendation
                return self.send_json({'ok':True,**recommendation(settings(body),self.server.console.root)})
            if self.path=='/api/local-models/scan':
                from selection_automation.local_model_inventory import start_scan,cancel_scan
                if set(body)-{'action','restart'} or body.get('action','start') not in ('start','cancel') or type(body.get('restart',False)) is not bool:
                    raise ConsoleError('INVALID_INPUT','本机模型扫描参数无效')
                result=cancel_scan(self.server.console.root) if body.get('action')=='cancel' else start_scan(self.server.console.root,restart=body.get('restart',False))
                return self.send_json({'ok':True,**result})
            if self.path=='/api/local-models/repair':
                from selection_automation.stage3_provider import settings
                from selection_automation.local_model_service import repair_provider
                if set(body)!={'provider'} or not isinstance(body['provider'],dict):
                    raise ConsoleError('INVALID_INPUT','本地模型检测参数无效')
                try:provider=settings(body['provider'])
                except ValueError as error:raise ConsoleError('INVALID_INPUT',str(error)) from None
                if provider['mode']!='local_model':raise ConsoleError('INVALID_INPUT','请选择本地模型服务')
                result=repair_provider(self.server.console.root,provider,
                    allow_default=not bool(body['provider'].get('model','').strip()),background=True)
                return self.send_json({'ok':True,**result})
            if self.path=='/api/local-agents/models':
                from selection_automation.agent_models import catalogs
                from selection_automation.local_agents import IDS
                if set(body)-{'ids'} or not isinstance(body.get('ids',[]),list) or any(i not in IDS for i in body.get('ids',[])):
                    raise ConsoleError('INVALID_INPUT','Agent 模型选择无效')
                return self.send_json({'ok':True,'catalogs':catalogs(self.server.console.root,body.get('ids'),refresh=True)})
            if self.path in ('/api/local-agents/refresh','/api/local-agents/connect','/api/local-agents/login'):
                from selection_automation.local_agents import discover,check_connection,login,IDS
                if self.path.endswith('/refresh'):
                    if set(body)-{'agent_models'}:raise ConsoleError('INVALID_INPUT','检测参数无效')
                    from selection_automation.stage3_provider import settings
                    try:models=settings({'mode':'local_agent','agent_models':body.get('agent_models',{})})['agent_models']
                    except ValueError:raise ConsoleError('INVALID_INPUT','Agent 模型选择无效') from None
                    return self.send_json({'ok':True,'agents':discover(self.server.console.root,refresh=True,models=models)})
                if set(body)-{'id','model'} or body.get('id') not in IDS:raise ConsoleError('INVALID_INPUT','Agent 选择无效')
                if self.path.endswith('/login'):return self.send_json(login(self.server.console.root,body['id']))
                model=body.get('model') or None
                if model:
                    from selection_automation.stage3_provider import settings
                    from selection_automation.agent_models import validate_selection
                    try:
                        chosen=settings({'mode':'local_agent','agent_models':{body['id']:model}})['agent_models'];validate_selection(self.server.console.root,chosen)
                    except ValueError as error:raise ConsoleError('INVALID_MODEL',str(error),409) from None
                return self.send_json({'ok':True,'agent':check_connection(self.server.console.root,body['id'],model)})
            if self.path=='/api/local-agents/enable-doubao':
                from selection_automation.desktop_agents import enable_doubao
                if set(body)!={'confirmation'}:raise ConsoleError('INVALID_INPUT','连接确认无效')
                return self.send_json(enable_doubao(self.server.console.root,body['confirmation']))
            if self.path=='/api/stop-stage3': return self.send_json(self.server.console.stop_stage3(body),202)
            if self.path=='/api/control-stage3': return self.send_json(self.server.console.control_stage3(body),202)
            if self.path=='/api/refresh-market': return self.send_json(self.server.console.refresh_market(body),202)
            if self.path=='/api/open-excel':
                if set(body)!={'kind'}: raise ConsoleError('INVALID_INPUT','Excel 类型无效')
                return self.send_json(self.server.console.open_excel(body['kind']))
            if self.path in ('/api/stop','/api/change-session'):
                return self.send_json(self.server.console.action('stop' if self.path.endswith('/stop') else 'rotate',body))
            raise ConsoleError('NOT_FOUND','不存在',404)
        except ConsoleError as exc:
            if exc.status==403 and not body_read:
                # Windows can reset a closed HTTP/1.0 connection with unread
                # request bytes before the client receives the rejection.
                previous_timeout=self.connection.gettimeout()
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if 0<length<=8192:
                        self.connection.settimeout(.25);self.rfile.read(length)
                except (OSError,ValueError):pass
                finally:self.connection.settimeout(previous_timeout)
            self.send_json({'ok':False,'error':{'code':exc.code,'message':exc.message}},exc.status)
        except Exception: self.send_json({'ok':False,'error':{'code':'REQUEST_FAILED','message':'请求失败，请检查输入'}},400)


def watch_release(server,stopping):
    from selection_automation.release_reload import idle_for_reload
    while not stopping.wait(3):
        try:
            with server.release_lock:
                update=server.release.check(force=True)
                if not update['pending'] or not server.release.stable():continue
                state=server.console.monitor.get()
                sample=state.get('status_sample') or {}
                server.release.waiting=(state.get('status_loading') is True or
                    sample.get('age_seconds') is None or sample['age_seconds']>15 or
                    not idle_for_reload(state,server.console))
                if server.release.waiting:continue
                server.release.restarting=True
                server.shutdown()
                return
        except Exception as error:
            # Fail closed: keep the existing service and surface pending-update state.
            print('Automatic release reload deferred: '+type(error).__name__,flush=True)


def main():
    # Scheduled hosts may survive an update and retain their old low priority.
    # Keep this interactive HTTP service at normal priority regardless of parent.
    if os.name=='nt':
        import psutil
        process=psutil.Process()
        process.nice(psutil.NORMAL_PRIORITY_CLASS)
        process.ionice(psutil.IOPRIO_NORMAL)
    parser=argparse.ArgumentParser(); parser.add_argument('--host',choices=['127.0.0.1'],default='127.0.0.1'); parser.add_argument('--port',type=int,default=8765); parser.add_argument('--open-browser',action='store_true')
    args=parser.parse_args(); server=LocalConsoleServer((args.host,args.port),Handler); server.console=Console()
    from selection_automation.release_reload import ReleaseReload
    server.release=ReleaseReload(ROOT,selection.LOADED_VERSION)
    server.release.check(force=True)
    stopping=threading.Event()
    threading.Thread(target=watch_release,args=(server,stopping),daemon=True).start()
    from local_service_recovery import LocalServiceRecovery
    recovery=LocalServiceRecovery(ROOT,server.console.owner)
    threading.Thread(target=recovery.run,args=(stopping,),name='local-service-recovery',daemon=True).start()
    if any(row.get('enabled') for row in selection.read(ROOT/'runtime/operations/subscriptions.json',{'subscriptions':[]})['subscriptions']):
        from selection_automation.operations import ensure_daily_service
        ensure_daily_service(ROOT)
    print(f'http://127.0.0.1:{server.server_port}',flush=True)
    if args.open_browser: threading.Timer(.5,lambda:webbrowser.open(f'http://127.0.0.1:{server.server_port}/')).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        stopping.set();server.server_close()
    if server.release.restarting:
        logs=ROOT/'runtime/web-console';logs.mkdir(parents=True,exist_ok=True)
        with (logs/f'service-{args.port}.log').open('ab') as output:
            subprocess.Popen([sys.executable,str(ROOT/'scripts/web_console.py'),'--host',args.host,'--port',str(args.port)],
                cwd=ROOT,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)


if __name__=='__main__': main()
