from __future__ import annotations

import argparse
import json
import os
import re
import socket
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from selection_automation.category_config import load_category
from selection_automation.keyword_planner import build_plan, build_product_groups, read_json_rows
from selection_automation.merge_temu_snapshots import merge, atomic_write
from selection_automation.worker_runtime import WorkerLease, WorkerStopRequested, interruptible_wait, run_collector, transport_recovery_allowed
from selection_automation.proxy_activity import ProxyActivityLease
from selection_automation.background_launch import run_background_launcher
from selection_automation.product_keywords import candidates as product_candidates, contains as title_contains
from cleanup_completed_worker import cleanup_completed_worker
from empty_results_checkpoint import confirmed_empty_checkpoint,confirmed_empty_observation,record_empty_observation
from selection_automation.collection_quota import collected_cards,quota_reached
from selection_automation import work_dispatch
from selection_automation.runtime_audit import process_version, record_start, recovery_event
LOADED_VERSION=process_version(str(ROOT))
from selection_automation.recovery_policy import recovery_budget, recovery_kind as classify_recovery_kind, rate_limit_wait, site_access_wait


def resolved(path: str | Path) -> Path:
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def run(command: list[str]) -> None:
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80]


def build_category_keyword_plan(rows: list[dict], category: dict) -> list[dict]:
    """Keep the proven worker queues, but derive non-towel searches from this category."""
    temu = category['temu']
    feature_terms=temu.get('keyword_features',[])
    if not isinstance(feature_terms,list) or any(not isinstance(p,str) or not p.strip() for p in feature_terms):
        raise ValueError('temu.keyword_features must be a list of nonempty literal feature phrases; empty list means title evidence fallback')
    base_queries = [re.sub(r'\s+', ' ', str(q).strip().lower()) for q in temu['base_queries'] if str(q).strip()]
    maximum = int(temu['max_products_per_keyword'])
    if category.get('stage1',{}).get('text_gate','').startswith('bath_towel'):
        plan = build_plan(rows, base_queries[0], None, maximum, int(temu.get("keywords_per_product") or 6)) if rows else []
        primary = next((item for item in plan if item['query'] == base_queries[0]), None)
        if primary is None:
            primary = {'query': base_queries[0], 'source_asins': [], 'source_evidence': [],
                       'status': 'PLANNED'}
            plan = [primary, *plan]
        primary.update(purpose='CATEGORY_SAMPLE', max_products=maximum * 3)
        return plan

    planned: dict[str, dict] = {}
    def mentioned(phrase, title):
        return title_contains(phrase,title)
    def add(query: str, row: dict | None = None, evidence: dict | None = None) -> None:
        query = re.sub(r'\s+', ' ', query.strip().lower())
        if not query:
            return
        entry = planned.setdefault(query, {'query': query, 'source_asins': [], 'source_evidence': [],
                                          'max_products': maximum, 'status': 'PLANNED'})
        if row:
            asin = str(row.get('asin') or row.get('ASIN') or '').strip()
            if evidence and asin in entry['source_asins']:
                for existing in entry['source_evidence']:
                    if existing['asin']==asin: existing.update(reason=evidence['reason'],features=evidence['features'])
            if asin and asin not in entry['source_asins']:
                entry['source_asins'].append(asin)
                entry['source_evidence'].append({'asin': asin, 'rank': row.get('rank'),
                                                 'title': row.get('title') or row.get('amazon_title'),
                                                 **({'reason':evidence['reason'],'features':evidence['features']} if evidence else {})})

    primary = base_queries[0]
    add(primary)
    planned[primary]['purpose']='CATEGORY_SAMPLE'
    # Broad category searches cover a larger market than title-specific queries.
    # Persist this in each new plan so sales and recent passes use the same budget.
    planned[primary]['max_products']=maximum * 3
    category_queries = list(dict.fromkeys([
        *base_queries,
        *(query for term in temu.get('include_terms') or []
          if (query := re.sub(r'\s+', ' ', str(term).strip().lower())).endswith(primary)
          and query not in (temu.get('severe_exclude_terms') or []))
    ]))
    # The category generator's approved searches get first priority. Extract
    # additional, directly observable modifiers next to the product noun in
    # Amazon titles; never import another category's fixed keyword table.
    ignored = {'the', 'a', 'an', 'and', 'or', 'for', 'with', 'from', 'by', 'of', 'in',
               'new', 'upgraded', 'latest', '2026', '2025', 'best', 'pro'}
    vocabulary = set(re.findall(r'[a-z0-9]+', ' '.join(
        [*base_queries, *(temu.get('include_terms') or []), *(temu.get('keyword_features') or [])]).lower()))
    primary_pattern = re.compile(r'\b' + re.escape(primary) + r'\b', re.I)
    title_candidate_inventory = []
    for row in rows:
        title = str(row.get('title') or row.get('amazon_title') or row.get('Amazon标题') or '')
        searchable = re.sub(r'\s+', ' ', title.lower())
        for query in category_queries:
            if mentioned(query,searchable):
                add(query, row)
        for feature in temu.get('keyword_features') or []:
            feature = re.sub(r'\s+', ' ', str(feature).strip().lower())
            if feature and mentioned(feature,searchable) and feature not in primary:
                add(f'{feature} {primary}', row)
        for match in primary_pattern.finditer(title):
            before = re.findall(r'[a-z0-9]+', title[:match.start()].lower())[-2:]
            if before and before[-1] in vocabulary and before[-1] not in ignored and len(before[-1]) > 1:
                add(f'{before[-1]} {primary}', row)
                if len(before) == 2 and before[0] in vocabulary and before[0] not in ignored and len(before[0]) > 1:
                    add(f'{before[0]} {before[1]} {primary}', row)
        # Work even when a new category's generated feature vocabulary is empty.
        full_candidates = product_candidates(row,base_queries)
        title_candidate_inventory.append((row, full_candidates))
        for candidate in full_candidates[:max(1, int(temu.get('keywords_per_product') or 6))]:
            if not any(mentioned(term,candidate['query']) for term in temu.get('severe_exclude_terms',[])):
                add(candidate['query'],row,candidate)

    # Reconcile every exact query with all titles that support it.
    def complete_evidence(selected):
        chosen_queries = {item['query'] for item in selected}
        for row, candidates_for_title in title_candidate_inventory:
            for candidate in candidates_for_title:
                query = re.sub(r'\s+', ' ', candidate['query'].strip().lower())
                if query in chosen_queries:
                    add(query, row, candidate)
        return selected

    seeds = [query for query in category_queries if query in planned and (query==primary or not rows or planned[query]['source_asins'])]
    derived = [item for query, item in planned.items() if query not in seeds and item['source_asins']]
    derived.sort(key=lambda item: (-len(item['source_asins']), item['query']))
    candidates=[planned[query] for query in seeds if query!=primary]+derived
    from selection_automation.keyword_planner import select_useful_queries
    # Bind all shared evidence before measuring marginal product/style coverage.
    selected=select_useful_queries(complete_evidence([planned[primary], *candidates]))
    # Any configured base query that survives title-evidence coverage selection
    # remains a category sample, even when it also matches an Amazon title.
    # Do not force redundant base queries into the plan or relabel them as 400
    # product-specific searches merely because they have title evidence.
    base_set=set(base_queries)
    for item in selected:
        if item['query'] in base_set:
            item.update(purpose='CATEGORY_SAMPLE',max_products=maximum*3)
    return selected


def keyword_coverage(rows,plan,category=None):
    products={str(r.get('asin') or r.get('ASIN') or '') for r in rows}-{''}
    evidenced={asin for item in plan for asin in item.get('source_asins',[])}
    primary_sample=next((item['query'] for item in plan if item.get('purpose')=='CATEGORY_SAMPLE'),None)
    # Only the first broad category sample is non-specific; additional selected
    # base queries carry useful title evidence and count toward specific coverage.
    specific={asin for item in plan if item.get('query')!=primary_sample for asin in item.get('source_asins',[])}
    gaps=[]
    if category is not None:
        temu=category['temu'];chosen={p['query'] for p in plan if p.get('query')!=primary_sample}
        for row in rows:
            asin=str(row.get('asin') or row.get('ASIN') or '')
            if not asin or asin in specific:continue
            inventory=product_candidates(row,temu['base_queries'])
            allowed=[r for r in inventory if not any(title_contains(term,r['query']) for term in temu.get('severe_exclude_terms',[]))]
            reason=('NO_SUPPORTED_TITLE_FEATURES' if not inventory else 'EXCLUDED_TITLE_QUERIES' if not allowed else 'MISSING_BINDING' if any(r['query'] in chosen for r in allowed) else 'QUERY_SELECTION_GAP')
            gaps.append({'asin':asin,'reason':reason,'supported_title_queries':len(allowed)})
    return {'amazon_products':len(products),'evidenced_products':len(products & evidenced),
            'specific_products':len(products & specific),'without_specific_query':sorted(products-specific),
            'measurement':'PLANNED_TITLE_EVIDENCE_NOT_SEARCH_EXECUTION',
            'coverage_gaps':gaps,'coverage_gap_counts':dict(__import__('collections').Counter(g['reason'] for g in gaps)),
            'category_feature_terms_supplied':bool(category and category['temu'].get('keyword_features')),
            'note':'无特征词覆盖的商品仍参与类目采样；不代表已完成该商品的精准搜索'}


def login_recovery_budget(previous: dict, goods: int, profile_id: str) -> tuple[bool, dict, bool]:
    """Track no-progress replacements without a cumulative session ceiling."""
    prior_count=previous.get('unique_goods_before')
    progressed=isinstance(prior_count,(int,float)) and goods>prior_count
    used=0 if progressed else int(previous.get('attempts_without_progress',1 if previous.get('automatic_rotation_attempted') else 0))
    record={'automatic_rotation_attempted':True,'attempts_without_progress':used+1,
            'unique_goods_before':goods,'profile_id':profile_id,'started_at':time.time()}
    return True,record,progressed


def rate_recovery_budget(previous: dict, goods: int, profile_id: str) -> tuple[bool, dict, bool]:
    """One rotation per failed profile; no cumulative ceiling across sessions."""
    before = previous.get('unique_goods_before')
    after = previous.get('unique_goods_after')
    progressed = previous.get('status') == 'RECOVERED_WITH_NEW_PRODUCTS'
    if isinstance(before, (int, float)):
        progressed = progressed or goods > before or (isinstance(after, (int, float)) and after > before)
    previous_profile = str(previous.get('profile_id') or '')
    profile_changed = bool(previous.get('automatic_rotation_attempted') and previous_profile
                           and profile_id and profile_id != previous_profile)
    used = 0 if progressed else int(previous.get('attempts_without_progress',
                                                   1 if previous.get('automatic_rotation_attempted') else 0))
    allowed = not previous.get('automatic_rotation_attempted') or progressed or profile_changed
    record = {'automatic_rotation_attempted': True, 'attempts_without_progress': used + 1,
              'unique_goods_before': goods, 'profile_id': profile_id,
              'previous_profile_id': previous_profile or None, 'started_at': time.time()}
    return allowed, record, progressed


def collector_block(status: str) -> dict | None:
    """A blocked collector must retain its query and wait for an explicit recovery."""
    if status == "STOPPED_BROWSER_COMMAND_TIMEOUT":
        return {"reason": "浏览器控制超时，采集已暂停；浏览器与代理保留",
                "action": "程序最多检查120秒；确认超时命令已结束、页面恢复后从当前关键词断点继续，连续无新增最多恢复3次",
                "verify_to_resume": False}
    if status in {"STOPPED_BROWSER_CONTROL_RECOVERY_TIMEOUT", "STOPPED_BROWSER_CONTROL_RECOVERY_BUDGET"}:
        return {"reason": "浏览器控制恢复等待超时" if status.endswith('TIMEOUT') else "浏览器控制恢复已达连续无新增上限",
                "action": "浏览器和采集断点已保留，请检查控制连接；自动恢复已停止",
                "verify_to_resume": False}
    if status in {"STOPPED_PROXY_OR_NETWORK_FAILURE", "STOPPED_PROXY_CONNECTION", "STOPPED_TRY_AGAIN_STALLED"}:
        return {"reason": "页面加载失败，采集已暂停", "action": "程序自动检查当前关键词页面；页面恢复后从断点继续", "verify_to_resume": True}
    if status == "STOPPED_INITIAL_PRODUCTS_TIMEOUT":
        return {"reason": "首屏商品等待超时，尚未获得有效搜索结果", "action": "请Agent检查页面并换session，或人工恢复当前关键词商品页后继续", "verify_to_resume": True}
    if status == "STOPPED_SALES_SORT_UNCONFIRMED":
        return {"reason": "未确认当前关键词的目标排序，采集已暂停", "action": "程序自动检查当前关键词页面；恢复后重新确认目标排序并从断点继续", "verify_to_resume": True}
    if status in {"STOPPED_ACCESS_DENIED", "STOPPED_HTTP_403", "STOPPED_HTTP_429", "STOPPED_HTTP_BLOCK"}:
        return {"reason": "页面拒绝访问，采集已暂停", "action": "请人工处理访问限制；确认当前关键词商品页正常后继续", "verify_to_resume": True}
    if status in {"WAITING_LOGIN", "STOPPED_LOGIN_REQUIRED"}:
        return {"reason": "等待人工登录，保留浏览器", "action": "请在当前浏览器完成登录，返回当前关键词商品页后继续", "verify_to_resume": True}
    if status in {"WAITING_CAPTCHA", "WAITING_SEARCH_VERIFICATION", "WAITING_HUMAN_VERIFICATION", "WAITING_EMPTY_SEARCH_VERIFICATION"}:
        return {"reason": "等待人工验证，保留浏览器", "action": "请在当前浏览器完成验证，商品页正常后继续", "verify_to_resume": True}
    if status == "STOPPED_SEE_MORE_UNAVAILABLE":
        return {"reason": "加载更多控件持续不可用，关键词采集未完成", "action": "请Agent检查当前关键词页并从断点恢复；不能把缺少控件视为采集完成", "verify_to_resume": True}
    return None


def confirmed_sales_checkpoint(output: Path, progress: dict, query: str, sort: str = "sales") -> bool:
    def confirmed(value):
        return (isinstance(value, dict) and value.get('requested') == sort
                and value.get('state') == 'UI_CONFIRMED'
                and str(value.get('query', '')).strip().lower() == query.strip().lower())
    if not confirmed(progress.get('sales_sort')):
        return False
    try:
        rows = [json.loads(line) for line in (output / 'products.jsonl').read_text('utf-8').splitlines() if line.strip()]
        return len(rows) == progress.get('unique_goods') and all(confirmed(row.get('search_sort')) for row in rows)
    except (OSError, ValueError, AttributeError):
        return False


def legacy_relevance_completion(progress: dict) -> bool:
    return progress.get('status')=='KEYWORD_COMPLETE' and 'NO_MORE_RESULTS_CONFIRMED' not in (
        progress.get('stop_reason'),progress.get('completion_basis'))


def confirmed_checkpoint_meets_budget(output: Path, progress: dict, query: str, sort: str, target: int) -> bool:
    if confirmed_empty_checkpoint(progress,query):return True
    status=str(progress.get('status') or '')
    if status not in {'KEYWORD_COMPLETE','KEYWORD_LIMIT_REACHED'}:
        return False
    if legacy_relevance_completion(progress) and not quota_reached(progress,int(target)):
        return False
    if not confirmed_sales_checkpoint(output,progress,query,sort):
        return False
    # A genuine NoMoreResults checkpoint remains complete at any larger target.
    return status!='KEYWORD_LIMIT_REACHED' or int(progress.get('raw_collected_count',progress.get('unique_goods')) or 0)>=int(target)


def limit_upgrade_resume_queries(run: Path, keywords: list[dict]) -> set[str]:
    """Resume raised limits and legacy completions based on incomplete language rules."""
    old=work_dispatch.read(run)
    targets={item['query']:int(item['max_products']) for item in keywords}
    resume=set()
    for job in old.get('jobs',[]):
        target=targets.get(job.get('query'))
        if not job.get('complete') or target is None:continue
        for sort in ('sales','recent'):
            worker=((job.get('sort_passes') or {}).get(sort) or {}).get('owner') or job.get('owner')
            if not worker:continue
            output=Path(run)/'temu'/'workers'/str(worker)/(slug(job['query'])+('--recent' if sort=='recent' else ''))
            try:progress=json.loads((output/'progress.json').read_text('utf-8'))
            except (OSError,ValueError):continue
            if legacy_relevance_completion(progress) and not quota_reached(progress,target):
                resume.add(job['query']);break
            if (progress.get('status')=='KEYWORD_LIMIT_REACHED'
                    and collected_cards(progress)<target
                    and confirmed_sales_checkpoint(output,progress,job['query'],sort)):
                resume.add(job['query']);break
    return resume


def run_root(category: dict) -> Path:
    return ROOT / "runs" / category["category_key"]


def amazon_paths(category: dict) -> tuple[Path, Path]:
    base = run_root(category) / "amazon"
    return base / "amazon_frontend_latest.json", base / "amazon_frontend_latest.jsonl"


def keyword_plan_path(category: dict) -> Path:
    return run_root(category) / "temu" / "keyword_plan.json"


def write_stage12_config(category: dict) -> Path:
    base = run_root(category)
    _, amazon_jsonl = amazon_paths(category)
    temu_union = base / "temu" / "union" / "products.jsonl"
    stage2 = category["stage2"]
    configured_stage12_dir = str(category.get("stage12_output_dir") or "stage12")
    windows_dir = PureWindowsPath(configured_stage12_dir)
    if windows_dir.drive or windows_dir.root or ".." in windows_dir.parts or Path(configured_stage12_dir).is_absolute():
        raise ValueError("stage12_output_dir must remain inside the category run directory")
    stage12_dir = (base / configured_stage12_dir).resolve()
    if not stage12_dir.is_relative_to(base.resolve()):
        raise ValueError("stage12_output_dir resolves outside the category run directory")
    local_rule_version = category["stage1"].get("rule_version") or category["stage1"]["text_gate"]
    payload = {
        "schema_version": "incremental-stage12-category-v1",
        "rule_version": local_rule_version,
        "stage12_rule_version": local_rule_version,
        "text_gate": ("declarative:" + str(base / "category.json")) if category["stage1"]["text_gate"] == "declarative" else category["stage1"]["text_gate"],
        "output_dir": str(stage12_dir),
        "poll_seconds": 2,
        "batch_size": 1000,
        "text_gate_workers": min(4, max(1, (os.cpu_count() or 2) // 2)),
        "vector_batch_size": 16,
        "max_image_retries": 3,
        "candidate_threshold": stage2["candidate_threshold"],
        "below_threshold_action": stage2["below_threshold_action"],
        "candidate_materialize_interval_seconds": 60,
        "candidate_materialize_background": True,
        "excel": {
            "auto_update": False,
            "background": True,
            "interval_seconds": 60,
            "min_processed_delta": 1,
            "output": str(base / "excel" / f"{category['category_name_zh']}_第一二层.xlsx"),
        },
        "image_download": {"workers": 16, "timeout_seconds": 30, "require_temu_proxy": False, "network_route": "local_direct_without_system_proxy"},
        "sources": [
            {"key": f"amazon_{category['category_key']}_frontend", "platform": "AMAZON", "adapter": "amazon_generic", "snapshot_mode": "replace", "path": str(amazon_jsonl)},
            {"key": f"temu_{category['category_key']}_keyword_union", "platform": "TEMU", "adapter": "temu_frontend", "snapshot_mode": "replace", "path": str(temu_union)},
        ],
        "paths": [{
            "key": category["category_key"], "label": f"Amazon真实前端 × TEMU网页优先、极鲸云补充并集",
            "amazon_source": f"amazon_{category['category_key']}_frontend", "temu_source": f"temu_{category['category_key']}_keyword_union",
        }],
    }
    destination = base / "stage12_config.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def ensure_proxy_chain(worker_number: int, port: int, lease: WorkerLease) -> bool:
    lease.check_stop()
    from selection_automation.browser_transport import local_vpn, adspower_proxy
    settings_path = ROOT / 'runtime/secrets/proxyshare.settings.json'
    settings = json.loads(settings_path.read_text('utf-8')) if settings_path.exists() else {}
    if local_vpn(settings):
        adspower_proxy(settings)  # Recheck the saved local route, without starting residential chains.
        return False
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            return False
    except OSError:
        pass
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if not shell:
        raise RuntimeError('PowerShell is required to restore the local proxy chain')
    result = run_background_launcher([
        shell, '-NoProfile', '-File', str(ROOT / 'scripts/proxyshare_chain_manager.ps1'),
        '-Action', 'start', '-Worker', str(worker_number),
        '-StateFile', f'runtime/proxyshare-chains/state-worker-{worker_number}.json',
    ], cwd=ROOT, timeout=45)
    lease.check_stop()
    if result.returncode:
        raise RuntimeError(f'worker {worker_number} local proxy chain restart failed')
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            return True
    except OSError as error:
        raise RuntimeError(f'worker {worker_number} local proxy chain still unavailable after restart') from error


def collect_temu(category_path: Path, category: dict, sessions_path: Path, worker_name: str | None = None) -> None:
    sessions = json.loads(sessions_path.read_text(encoding="utf-8")).get("workers") or []
    if not sessions:
        raise SystemExit("sessions manifest has no workers")
    # Hard project invariant: “换session” is never an in-place token edit.
    # It must be a new AdsPower profile plus a new ProxyShare session, with
    # AdsPower pointing at one of the local chained proxy ports below.
    policy_path = ROOT / "runtime" / "proxy_policy.json"
    if not policy_path.exists(): policy_path = ROOT / "config" / "proxy_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if policy.get("session_rotation_policy") != "NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION":
        raise SystemExit("invalid proxy policy: session replacement must create a new profile and a new proxy session")
    if policy.get("delete_old_profile_first_via_adspower") is not True:
        raise SystemExit("invalid proxy policy: old AdsPower profile must be deleted before session replacement")
    chain_ports = list(policy.get("worker_chain_ports") or [])
    if len(chain_ports) != len(sessions):
        raise SystemExit("proxy policy worker_chain_ports does not match sessions manifest")
    selected = [(index, session) for index, session in enumerate(sessions) if not worker_name or session["name"] == worker_name]
    if worker_name and not selected:
        raise SystemExit(f"worker not found: {worker_name}")
    plan_document = json.loads(keyword_plan_path(category).read_text(encoding="utf-8"))
    plan = plan_document['keywords']
    resume_queries=limit_upgrade_resume_queries(run_root(category),plan)
    work_dispatch.requeue_unsettled_empty_queries(run_root(category))
    assignment=work_dispatch.sync(run_root(category),plan,[s['name'] for s in sessions],repair_queries=resume_queries)
    queues=[[r for r in assignment['jobs'] if r['owner']==session['name'] or any(
        state.get('owner')==session['name'] and not state.get('complete')
        for state in (r.get('sort_passes') or {}).values())] for session in sessions]
    supervisor_root=run_root(category)/'temu/supervisors'

    current_passes = {}

    def write_worker_state(name: str, payload: dict) -> None:
        atomic_write(
            supervisor_root / f"{name}.json",
            json.dumps({"worker": name, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **current_passes.get(name, {}), **payload}, ensure_ascii=False, indent=2),
        )

    def owned_worker(session: dict, items: list[dict], lease: WorkerLease, worker_number: int) -> bool:
        runtime_version=record_start(ROOT,run_root(category),'temu-supervisor',category,session['name'],version=LOADED_VERSION)
        initial_dispatch=work_dispatch.read(run_root(category))
        completed_queries,completed_passes=work_dispatch.completed_for_lane(initial_dispatch,session['name'])
        current_passes[session['name']]={'completed_passes':completed_passes}
        def next_passes():
            while True:
                lease.check_stop()
                job=work_dispatch.claim_pass(run_root(category),session['name'])
                if job is None: return
                yield job
        def completed(query, sort):
            if not any(row['query']==query and row['sort']==sort for row in completed_passes):
                completed_passes.append({'query': query, 'sort': sort})
            work_dispatch.complete_pass(run_root(category),session['name'],query,sort)
            if sort == 'recent':
                if any(row['query']==query and row.get('complete')
                       for row in work_dispatch.read(run_root(category))['jobs']) and query not in completed_queries:
                    completed_queries.append(query)
        write_worker_state(session["name"], {"status": "STARTING", "assigned": len(items), "completed_queries": completed_queries})
        try:
            restored = ensure_proxy_chain(worker_number, int(chain_ports[worker_number - 1]), lease)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            write_worker_state(session['name'], {'status': 'PROXY_STARTUP_FAILED', 'reason': str(error)})
            raise
        if restored:
            write_worker_state(session['name'], {'status': 'PROXY_CHAIN_RESTORED', 'assigned': len(items)})
        for item_index, item in enumerate(next_passes(), start=1):
            lease.check_stop()
            query = item["query"]
            sort = item["sort"]
            output = run_root(category) / "temu" / "workers" / str(session["name"]) / (slug(query) + ("--recent" if sort == "recent" else ""))
            current_passes[session["name"]] = {"current_sort": sort, "completed_passes": completed_passes, "output": str(output)}
            output.mkdir(parents=True, exist_ok=True)
            log = output / "collector.log"
            progress_path = output / "progress.json"
            existing = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
            from selection_automation.browser_control_recovery import CONTROL_RECOVERY_TERMINAL, control_stop_was_replaced
            if control_stop_was_replaced(ROOT, session, existing):
                previous_status = existing['status']
                existing = {**existing, 'status':'RESUMING', 'manual_action_required':False,
                    'automatic_retry':True, 'automatic_resume_pending':True,
                    'confirmed_login_redirect':False, 'http_block':None,
                    'session_recovery_kind':None, 'command_execution_unknown':False,
                    'control_recovery_profile_id':str(session.get('profile_id') or ''),
                    'previous_control_stop':previous_status}
                atomic_write(progress_path,json.dumps(existing,ensure_ascii=False))
                recovery_event(run_root(category),session['name'],'REPLACED_CONTROL_STOP_CLEARED',
                    query=query,sort=sort,profile_id=session.get('profile_id'),previous_status=previous_status)
            def stop_control_recovery(progress, status, record):
                blocked={**progress,'status':status,'manual_action_required':False,'automatic_retry':True,
                         'automatic_resume_pending':True,
                         'control_recovery':record,'browser_preserved':True,
                         'control_recovery_profile_id':str(session.get('profile_id') or '')}
                atomic_write(progress_path,json.dumps(blocked,ensure_ascii=False))
                write_worker_state(session['name'],{
                    'status':'BINDING_RETRY_WAIT','query_status':status,
                    'current_query':query,'output':str(output),'log':str(log),
                    'unique_goods':progress.get('unique_goods'),'completed_queries':completed_queries,
                    'browser_preserved':True,'manual_action_required':False,'automatic_retry':True,
                    'automatic_resume_pending':True,'control_recovery':record,
                    'recovery_budget_exhausted':status.endswith('_BUDGET'),
                    'recovery_action':collector_block(status)['action'],'verify_to_resume':False,
                })
                return blocked
            def wait_for_control(progress):
                from selection_automation.browser_control_recovery import (
                    wait_until_ready, BrowserGateDetected, BrowserControlRecoveryTimeout, reserve_control_recovery)
                lease.check_stop()
                allowed,record=reserve_control_recovery(output/'browser_control_recovery.json',progress.get('unique_goods'))
                if not allowed:
                    recovery_event(run_root(category),session['name'],'BROWSER_CONTROL_RECOVERY_BUDGET_EXHAUSTED',query=query,sort=sort,**record)
                    return stop_control_recovery(progress,'STOPPED_BROWSER_CONTROL_RECOVERY_BUDGET',record)
                def observe_control(checks, fenced, usable):
                    write_worker_state(session['name'], {
                        'status':'WAITING_BROWSER_CONTROL_RECOVERY','query_status':'STOPPED_BROWSER_COMMAND_TIMEOUT',
                        'current_query':query,'output':str(output),'log':str(log),
                        'unique_goods':progress.get('unique_goods'),
                        'completed_queries':completed_queries,'browser_preserved':True,
                        'manual_action_required':False,'automatic_resume_pending':True,
                        'control_recovery_checks':checks,'control_command_finished':fenced,
                        'control_page_ready':usable,'next_control_check_seconds':10,
                        'control_recovery_timeout_seconds':120,'control_recovery':record,
                    })
                try:
                    wait_until_ready(ROOT,session,query,lease=lease,
                        stopped=lambda:(run_root(category)/'STOP').exists(),observe=observe_control)
                except BrowserControlRecoveryTimeout as error:
                    recovery_event(run_root(category),session['name'],'BROWSER_CONTROL_RECOVERY_TIMEOUT',query=query,sort=sort,**record)
                    return stop_control_recovery(progress,error.status,record)
                except BrowserGateDetected as error:
                    blocked={**progress,'status':error.status,'manual_action_required':True,'automatic_retry':False}
                    atomic_write(progress_path,json.dumps(blocked,ensure_ascii=False))
                    recovery=collector_block(error.status)
                    write_worker_state(session['name'],{
                        'status':'HUMAN_ACTION_REQUIRED','query_status':error.status,
                        'current_query':query,'output':str(output),'log':str(log),
                        'unique_goods':progress.get('unique_goods'),'completed_queries':completed_queries,
                        'browser_preserved':True,'manual_action_required':True,
                        'recovery_action':recovery['action'],'verify_to_resume':recovery['verify_to_resume'],
                    })
                    recovery_event(run_root(category),session['name'],'BROWSER_CONTROL_GATE_DETECTED',query=query,sort=sort,status=error.status)
                    return blocked
                recovery_event(run_root(category),session['name'],'BROWSER_CONTROL_AUTOMATIC_RESUME',
                    query=query,sort=sort,unique_goods=progress.get('unique_goods'),same_profile=True)
                return True
            pending_progress=None
            if existing.get('status') in CONTROL_RECOVERY_TERMINAL:
                stop_control_recovery(existing,existing['status'],existing.get('control_recovery') or {})
                return False
            if existing.get('status')=='STOPPED_BROWSER_COMMAND_TIMEOUT':
                control_result=wait_for_control(existing)
                if isinstance(control_result,dict):
                    if control_result['status'] in CONTROL_RECOVERY_TERMINAL:return False
                    pending_progress=control_result
            if pending_progress is None and confirmed_checkpoint_meets_budget(output,existing,query,sort,int(item['max_products'])):
                completed(query, sort)
                write_worker_state(session["name"], {
                    "status": "QUERY_ALREADY_COMPLETE",
                    "assigned": len(items),
                    "current_index": item_index,
                    "current_query": query,
                    "query_status": existing.get("status"),
                    "unique_goods": existing.get("unique_goods"),
                    "completed_queries": completed_queries,
                })
                continue
            from selection_automation.sampling import prepare as prepare_sampling
            sampling_path=prepare_sampling(run_root(category),output,item,plan,sort,category.get('temu',{}).get('adaptive_sampling',True))
            command = [
                "node", str(ROOT / "scripts" / "temu_flower_collect.mjs"), "--config", str(category_path),
                "--query", query, "--output", str(output), "--webdriver-url", str(session["webdriver_url"]),
                "--session-id-file", str(resolved(session["session_id_file"])),
                "--bidi-url-file", str(resolved(session["bidi_url_file"])),
                "--max-products", str(item["max_products"]),
                "--sort", sort, "--sampling-policy", str(sampling_path),
                "--runtime-version", str(run_root(category)/'temu/workers'/session['name']/'runtime_version.json'),
                "--stop-file", str(lease.stop_path),
                "--auto-session-recovery", "true",
            ]
            recovery_path = output / 'automatic_recovery.json'
            recovery_record = json.loads(recovery_path.read_text('utf-8')) if recovery_path.exists() else {}
            attempt=0
            while attempt<3:
                attempt+=1
                lease.check_stop()
                for flag, value in (('--webdriver-url', session['webdriver_url']),
                                    ('--session-id-file', resolved(session['session_id_file'])),
                                    ('--bidi-url-file', resolved(session['bidi_url_file']))):
                    command[command.index(flag)+1] = str(value)
                if pending_progress is not None:
                    # A gate found while fencing a prior process goes through
                    # the same phase-aware policy, before any collector starts.
                    progress,pending_progress=pending_progress,None
                    returncode=1
                else:
                    write_worker_state(session["name"], {
                        "status": "RUNNING",
                        "assigned": len(items),
                        "current_index": item_index,
                        "current_query": query,
                        "attempt": attempt,
                        "completed_queries": completed_queries,
                        "output": str(output),
                        "collector_started_at": time.time(),
                    })
                    previous_progress_mtime = progress_path.stat().st_mtime_ns if progress_path.exists() else None
                    with log.open("a", encoding="utf-8") as stream:
                        returncode = run_collector(command, lease=lease, cwd=ROOT, stream=stream,
                            health_check=lambda: ensure_proxy_chain(worker_number, chain_ports[worker_number - 1], lease))
                    fresh_progress = progress_path.exists() and progress_path.stat().st_mtime_ns != previous_progress_mtime
                    progress = json.loads(progress_path.read_text(encoding="utf-8")) if fresh_progress else {}
                status = str(progress.get("status") or "NO_FRESH_PROGRESS")
                if status == 'STOPPED_BROWSER_COMMAND_TIMEOUT':
                    control_result = wait_for_control(progress)
                    if isinstance(control_result, dict):
                        progress = control_result
                        status = str(progress['status'])
                        if status in CONTROL_RECOVERY_TERMINAL:return False
                    else:
                        attempt=0
                        continue
                # One gate precedes local repair, environment recovery and
                # ordinary retry, regardless of the collector status name.
                from selection_automation.recovery_policy import automatic_retry_stopped
                if automatic_retry_stopped(progress) and not confirmed_empty_checkpoint(progress,query):
                    write_worker_state(session['name'], {
                        'status': 'HUMAN_ACTION_REQUIRED', 'query_status': status,
                        'current_query': query, 'output': str(output), 'log': str(log),
                        'unique_goods': progress.get('unique_goods'), 'completed_queries': completed_queries,
                        'browser_preserved': True, 'manual_action_required': True,
                        'automatic_retry': False,
                    })
                    return False
                if status == 'STOPPED_BROWSER_CONTROL_FAILURE':
                    # This is a local driver failure, not a site refusal. Keep
                    # the browser/CAPTCHA, rebind this lane only, then retry.
                    from repair_browser_control import repair as repair_control
                    from selection_automation.browser_control_recovery import reserve_control_recovery
                    permitted,binding_record=reserve_control_recovery(output/'binding_recovery.json',int(progress.get('unique_goods') or 0),limit=3)
                    if attempt >= 3 or not permitted:
                        write_worker_state(session['name'],{'status':'BINDING_FAILED','current_query':query,
                            'output':str(output),'browser_preserved':True,'manual_action_required':True})
                        return False
                    write_worker_state(session['name'],{'status':'BINDING_LOCAL','current_query':query,
                        'output':str(output),'manual_action_required':False,'browser_preserved':True})
                    lease.check_stop()
                    try:
                        repair_control(session)
                        recovery_event(run_root(category),session['name'],'LOCAL_CONTROL_REPAIRED',query=query,sort=sort)
                    except (RuntimeError,OSError,subprocess.SubprocessError):
                        recovery_event(run_root(category),session['name'],'LOCAL_CONTROL_REPAIR_RETRY',query=query,sort=sort,attempt=attempt)
                    interruptible_wait(5*attempt,lease)
                    continue
                if confirmed_empty_observation(progress,query):
                    evidence=record_empty_observation(output/'empty_search_checks.json',progress,query,session.get('profile_id'))
                    if evidence:
                        progress={**progress,'status':'KEYWORD_NO_RESULTS','completion_basis':'EXPLICIT_EMPTY_QUERY_RESULTS',
                            'no_results_evidence':evidence,'keyword_complete':True}
                        status='KEYWORD_NO_RESULTS';returncode=0
                    else:
                        status='STOPPED_EMPTY_QUERY_RESULTS'
                        progress={**progress,'status':status,'keyword_complete':False,'automatic_retry':True,
                            'manual_action_required':False,'automatic_recovery_eligible':True}
                    atomic_write(progress_path,json.dumps(progress,ensure_ascii=False))
                if returncode==0 and confirmed_empty_checkpoint(progress,query):
                    work_dispatch.complete_empty_query(run_root(category),session['name'],query,progress['no_results_evidence'])
                    completed_queries.append(query) if query not in completed_queries else None
                    for empty_sort in ('sales','recent'):
                        if not any(row['query']==query and row['sort']==empty_sort for row in completed_passes):
                            completed_passes.append({'query':query,'sort':empty_sort,'skipped':True,'reason':'EXPLICIT_EMPTY_QUERY_RESULTS'})
                    write_worker_state(session['name'],{'status':'QUERY_COMPLETE','current_query':query,
                        'query_status':status,'unique_goods':0,'completed_queries':completed_queries,
                        'message':'两次独立搜索均确认无结果，进入下一个关键词'})
                    break
                if returncode==0 and status in {'KEYWORD_COMPLETE','KEYWORD_LIMIT_REACHED'} and (
                        status=='KEYWORD_COMPLETE' and legacy_relevance_completion(progress) and not quota_reached(progress,int(item['max_products']))
                        or status=='KEYWORD_LIMIT_REACHED' and not quota_reached(progress,int(item['max_products']))):
                    status='STOPPED_COLLECTION_BELOW_QUOTA'
                    progress={**progress,'status':status,'keyword_complete':False,'automatic_retry':True,
                        'manual_action_required':False,'automatic_recovery_eligible':True}
                    atomic_write(progress_path,json.dumps(progress,ensure_ascii=False))
                if returncode == 0 and status in {"KEYWORD_COMPLETE", "KEYWORD_LIMIT_REACHED"}:
                    completed(query, sort)
                    write_worker_state(session["name"], {
                        "status": "QUERY_COMPLETE",
                        "assigned": len(items),
                        "current_index": item_index,
                        "current_query": query,
                        "query_status": status,
                        "unique_goods": progress.get("unique_goods"),
                        "completed_queries": completed_queries,
                    })
                    break
                if status == 'STOPPED_INITIAL_PRODUCTS_TIMEOUT' and not progress.get('http_block') and not progress.get('confirmed_login_redirect'):
                    retry_path = output / 'initial_products_retry.json'
                    prior_retry = json.loads(retry_path.read_text('utf-8')) if retry_path.exists() else {}
                    retry_count = int(prior_retry.get('attempts', 0)) + 1
                    delay = min(60, 10 * retry_count)
                    atomic_write(retry_path, json.dumps({'attempts':retry_count,'query':query,'sort':sort,'same_profile':True}))
                    write_worker_state(session['name'], {'status':'WAITING_INITIAL_PRODUCTS_RETRY',
                        'current_query':query,'query_status':status,'output':str(output),
                        'unique_goods':progress.get('unique_goods'),'manual_action_required':False,
                        'automatic_resume_pending':True,'same_profile':True,'retry_seconds':delay})
                    recovery_event(run_root(category),session['name'],'INITIAL_PRODUCTS_SAME_PROFILE_RETRY',query=query,sort=sort,attempt=retry_count)
                    interruptible_wait(delay,lease)
                    attempt=0
                    continue
                recovery_kind = classify_recovery_kind(status, progress, transport_recovery_allowed(progress))
                http_recovery = (status in {'STOPPED_HTTP_403', 'STOPPED_HTTP_429', 'STOPPED_ACCESS_DENIED'}
                    or str((progress.get('http_block') or {}).get('status') or progress.get('http_status') or '') in {'403', '429'})
                limited_login_recovery = recovery_kind == 'LOGIN' and (
                    status in {'STOPPED_LOGIN_REQUIRED', 'WAITING_LOGIN', 'LOGIN_REQUIRED'} or not http_recovery)
                recovery_allowed, next_record = recovery_budget(recovery_record,
                    int(progress.get('unique_goods') or 0), str(session.get('profile_id') or ''), recovery_kind,
                    limit=3 if limited_login_recovery else None)
                if recovery_kind and recovery_allowed:
                    cooldown_path = output / 'session_cooldown.json'
                    prior_cooldown = json.loads(cooldown_path.read_text('utf-8')) if cooldown_path.exists() else {}
                    cooldown = rate_limit_wait(progress, prior_cooldown)
                    if cooldown is None and recovery_kind in {'ACCESS','EMPTY_RESULTS','BELOW_QUOTA'}:
                        delay = max(site_access_wait(recovery_kind, recovery_record, worker_number),
                                    float(prior_cooldown.get('retry_at', 0)) - time.time())
                        cooldown = {'wait_seconds': delay, 'retry_at': time.time() + delay}
                    if cooldown:
                        atomic_write(cooldown_path, json.dumps(cooldown))
                        write_worker_state(session['name'], {
                            'status': 'WAITING_HTTP_COOLDOWN' if recovery_kind == 'RATE_LIMIT' else 'WAITING_SITE_COOLDOWN',
                            'current_query': query, 'query_status': status, 'output': str(output),
                            'unique_goods': progress.get('unique_goods'), 'manual_action_required': False,
                            'automatic_resume_pending': True, **cooldown,
                        })
                        recovery_event(run_root(category), session['name'], 'SESSION_RECOVERY_COOLDOWN', kind=recovery_kind, **cooldown)
                        interruptible_wait(cooldown['wait_seconds'], lease)
                    recovery_record = {**next_record, 'query': query, 'sort': sort, 'trigger': status, 'recovery_id':uuid.uuid4().hex}
                    atomic_write(recovery_path, json.dumps(recovery_record))
                    recovery_event(run_root(category),session['name'],'AUTOMATIC_RECOVERY_STARTED',**recovery_record)
                    write_worker_state(session["name"], {
                        "status": f"REPLACING_{recovery_kind}_ENVIRONMENT", "current_query": query,
                        "completed_queries": completed_queries, "output": str(output),
                    })
                    with log.open("a", encoding="utf-8") as stream:
                        for local_attempt in range(2):
                            replacement = subprocess.run([
                                sys.executable, str(ROOT / "scripts" / "replace_proxyshare_worker_environment.py"),
                                "--worker", str(worker_number), "--sessions", str(sessions_path),
                                "--supervisor-token", lease.token,
                                "--task-key", run_root(category).name,
                            ], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
                            if replacement.returncode == 0: break
                            if local_attempt == 0:
                                recovery_event(run_root(category), session['name'], 'REPLACEMENT_LOCAL_RETRY',
                                    recovery_id=recovery_record['recovery_id'], resume_saved_replacement=True)
                                interruptible_wait(3, lease)
                    recovery_event(run_root(category),session['name'],'REPLACEMENT_FAILED' if replacement.returncode else 'REPLACEMENT_CONNECTED',recovery_id=recovery_record['recovery_id'],query=query,sort=sort)
                    if replacement.returncode:
                        write_worker_state(session["name"], {"status": f"{recovery_kind}_RECOVERY_FAILED", "current_query": query, "output": str(output), "log": str(log)})
                        raise RuntimeError(f"{recovery_kind.lower()} recovery failed for {session['name']}; see {log}")
                    refreshed = json.loads(sessions_path.read_text(encoding='utf-8')).get('workers', [])
                    replacement_session = next((w for w in refreshed if w.get('name') == session['name']), None)
                    if not replacement_session or any(not replacement_session.get(k) for k in ('webdriver_url','session_id_file','bidi_url_file')):
                        raise RuntimeError('Replacement manifest has no complete worker connection')
                    session.update(replacement_session)
                    attempt=0
                    lease.check_stop()
                    continue
                recovery = collector_block(status)
                if recovery:
                    recovery_event(run_root(category),session['name'],'RECOVERY_NEEDS_ATTENTION',query=query,sort=sort,status=status,budget_exhausted=bool(recovery_kind and not recovery_allowed))
                    write_worker_state(session["name"], {
                        "status": "HUMAN_ACTION_REQUIRED",
                        "assigned": len(items),
                        "current_index": item_index,
                        "current_query": query,
                        "query_status": status,
                        "attempt": attempt,
                        "returncode": returncode,
                        "output": str(output),
                        "log": str(log),
                        "unique_goods": progress.get("unique_goods"),
                        "completed_queries": completed_queries,
                        "browser_preserved": True,
                        "manual_action_required": True,
                        "recovery_action": ("当前关键词/排序连续 3 次换环境仍无新增商品，请完成登录或验证后继续采集；原断点已保留"
                            if recovery_kind == 'LOGIN' and not recovery_allowed else recovery["action"]),
                        "recovery_budget_exhausted": bool(recovery_kind and not recovery_allowed),
                        **({"automatic_retry": False, "automatic_resume_pending": False}
                            if recovery_kind == 'LOGIN' and not recovery_allowed else {}),
                        "verify_to_resume": recovery["verify_to_resume"],
                    })
                    return False
                if attempt >= 3:
                    write_worker_state(session["name"], {
                        "status": "FAILED_AFTER_3_ATTEMPTS",
                        "assigned": len(items),
                        "current_index": item_index,
                        "current_query": query,
                        "query_status": status,
                        "returncode": returncode,
                        "completed_queries": completed_queries,
                        "log": str(log),
                    })
                    raise RuntimeError(f"TEMU worker {session['name']} failed for {query}; see {log}")
                interruptible_wait(10 * attempt, lease)
        write_worker_state(session["name"], {"status": "WORKER_COMPLETE", "assigned": len(items), "completed_queries": completed_queries})
        # Parent owns cleanup after the final queue drains; idle lanes can take more work.
        return True

    def worker(session: dict, items: list[dict], worker_number: int) -> bool:
        try:
            with WorkerLease(ROOT / "runtime" / "worker-leases", session["name"], task_stop_path=run_root(category)/'STOP') as lease, \
                    ProxyActivityLease(ROOT, worker_number, purpose="collector"):
                try:
                    return owned_worker(session, items, lease, worker_number)
                except WorkerStopRequested:
                    previous = supervisor_root / f"{session['name']}.json"
                    state = json.loads(previous.read_text('utf-8')) if previous.exists() else {}
                    write_worker_state(session["name"], {**state,
                        "status": "PAUSED_FOR_ENVIRONMENT_REPLACEMENT", "browser_preserved": True})
                    return False
        except WorkerStopRequested:
            return False

    with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
        futures = [
            pool.submit(worker, session, items, index + 1)
            for index, (session, items) in enumerate(zip(sessions, queues))
            if not worker_name or session["name"] == worker_name
        ]
        if worker_name and not futures:
            raise SystemExit(f"worker not found or has no assignments: {worker_name}")
        outcomes = [future.result() for future in futures]
        if not all(outcomes):
            raise RuntimeError("TEMU collection remains incomplete; see per-worker supervisor states")


def merge_temu(category: dict) -> None:
    workers = run_root(category) / 'temu' / 'workers'
    inputs = sorted([*workers.glob('*/*/products.jsonl'), *workers.glob('*/*/unclassified_products.jsonl')])
    provider=run_root(category)/'market/geekbi.json'
    if not inputs and not provider.is_file():
        raise SystemExit("no TEMU worker snapshots found")
    rows, summary = merge(inputs,provider)
    union = run_root(category) / "temu" / "union"
    from selection_automation.category_subject_filter import filter_category_union
    rows,summary=filter_category_union(ROOT,category,rows,summary,union)
    summary['analysis_unique_goods']=len(rows)
    atomic_write(union / "products.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    atomic_write(union / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "collect-amazon", "plan-keywords", "collect-temu", "merge-temu", "stage12-once", "prepare"))
    parser.add_argument("--category", type=Path, default=ROOT / "config" / "categories" / "bath_towel_set.json")
    parser.add_argument("--sessions", type=Path)
    parser.add_argument("--worker-name", help="Run only one worker while preserving the full assignment map")
    args = parser.parse_args()
    category_path = args.category.resolve()
    category = load_category(category_path)
    base = run_root(category); base.mkdir(parents=True, exist_ok=True)
    if args.command == "validate":
        print(json.dumps({"状态": "配置通过", "品类": category["category_name_zh"], "运行目录": str(base)}, ensure_ascii=False))
        return
    amazon_json, amazon_jsonl = amazon_paths(category)
    if args.command in {"collect-amazon", "prepare"}:
        run(["node", str(ROOT / "scripts" / "collect_amazon_search_frontend.mjs"), "--config", str(category_path), "--output", str(amazon_json)])
        run([sys.executable, str(ROOT / "scripts" / "normalize_amazon_snapshot.py"), "--input", str(amazon_json), "--output", str(amazon_jsonl)])
    if args.command in {"plan-keywords", "prepare"}:
        rows = read_json_rows(amazon_jsonl)
        plan = build_category_keyword_plan(rows, category)
        product_groups = (build_product_groups(
            rows,
            category["temu"]["base_queries"][0],
            int(category["temu"].get("keywords_per_product") or 6),
        ) if category.get('stage1',{}).get('text_gate','').startswith('bath_towel') else [
            {'asin': str(row.get('asin') or row.get('ASIN') or ''), 'rank': row.get('rank'),
             'title': row.get('title') or row.get('amazon_title'),
             'queries': [entry['query'] for entry in plan if str(row.get('asin') or row.get('ASIN') or '') in entry['source_asins']]}
            for row in rows if row.get('asin') or row.get('ASIN')
        ])
        destination = keyword_plan_path(category); destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({
            "schema_version": "temu-keyword-plan-v2",
            "amazon_product_groups": product_groups,
            "unique_search_queries": len(plan),
            "keywords": plan,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"Amazon商品关键词组": len(product_groups), "TEMU唯一搜索词": len(plan), "输出": str(destination)}, ensure_ascii=False))
    if args.command == "collect-temu":
        if not args.sessions:
            raise SystemExit("--sessions is required")
        collect_temu(category_path, category, args.sessions.resolve(), args.worker_name)
    if args.command == "merge-temu":
        merge_temu(category)
    if category.get('execution_mode')=='collection':
        if args.command=='stage12-once': raise SystemExit('本任务仅数据获取，未启动分析')
        return
    config = write_stage12_config(category)
    if args.command == "stage12-once":
        run([sys.executable, str(ROOT / "scripts" / "run_incremental_stage12.py"), "once", "--config", str(config)])
    if args.command == "prepare":
        print(json.dumps({"状态": "Amazon采集和TEMU关键词计划已完成", "下一步": "collect-temu --sessions <sessions.json>", "stage12配置": str(config)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
