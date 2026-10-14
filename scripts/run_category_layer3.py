"""Incremental paid consumer. --validate-only never constructs an API client."""
from __future__ import annotations
import argparse
from contextlib import contextmanager, nullcontext, ExitStack
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from itertools import chain, islice
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from selection_automation.category_config import load_category
from selection_automation.vision import protocol, decode, compatible_vision_keys, parse_visual_response, parse_visual_response_compatible
from selection_automation.process_lock import ProcessLock
from selection_automation.rules import score
from selection_automation.stage3_admission import admitted, concurrency
from selection_automation.visual_text_evidence import evidence_block
from selection_automation.stage3_billing import billing_hold, retry_authorizations, authorized_retry_attempt
from selection_workflow.bath_towel_deepseek import _data_url


class IncompleteVisualResponse(RuntimeError):
    """A billed response ended before its structured visual facts were complete."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__('Visual response incomplete: ' + reason)


def output_token_budget(stage, count):
    # Responses output includes internal reasoning as well as compact JSON facts.
    # Reserve reasoning room even for one pair, then scale with visual modules.
    configured = stage.get('max_output_tokens')
    if configured is not None:
        if isinstance(configured, bool) or not isinstance(configured, int) or not 1024 <= configured <= 65536:
            raise ValueError('stage3.max_output_tokens must be an integer from 1024 to 65536')
    # A saved single-pair budget is a floor, not a cap on larger groups. Reserve
    # room for bilateral per-type inventories as well as the visual modules;
    # a bounded 65536 output allowance still applies. Incomplete responses
    # retain their billed usage and fail the group without an immediate retry.
    inventory_features = len(stage.get('set_inventory_features') or {})
    scaled = 8192 + (640 * len(stage['modules']) + 1200 * inventory_features) * count
    return min(65536, max(16384, configured or 0, scaled))


def safe_exception_causes(exc):
    """Diagnostic class names only; exception messages may contain API secrets."""
    names=[];seen=set()
    while exc is not None and id(exc) not in seen and len(names)<8:
        seen.add(id(exc));names.append(type(exc).__name__)
        exc=exc.__cause__ or exc.__context__
    return names


def request_timeout_seconds(stage):
    value=stage.get('request_timeout_seconds',600)
    if type(value) is not int or not 30<=value<=1800:
        raise ValueError('stage3.request_timeout_seconds must be an integer from 30 to 1800')
    return value


def rows(path):
    if path.exists():
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


@contextmanager
def frozen_candidates(path):
    """Close the published input before any network work; retain bounded memory.

    The producer atomically replaces its JSONL while this consumer can spend
    minutes on API calls.  Only a private temporary snapshot stays open then.
    The producer retries the brief Windows sharing race during this local copy.
    """
    with tempfile.TemporaryDirectory(prefix='selection-candidates-') as directory:
        frozen = Path(directory) / 'candidates.jsonl'
        try:
            with path.open('rb') as source, frozen.open('wb') as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
        except FileNotFoundError:
            # A not-yet-published initial candidate set means no work this round.
            frozen.touch()
        yield frozen


def result_index(path):
    cached = {}
    for row in rows(path):
        key = row.get('vision_revision_key')
        if cached.get(key, {}).get('api_status') == 'success':
            continue
        attempt = int(row.get('attempt', 0))
        provider_blocked = row.get('failure_status') in (401, 402, 403) or re.search(
            r'\bHTTP\s+(401|402|403)\b', str(row.get('failure_reason', ''))
        )
        if row.get('api_status') != 'success' and provider_blocked:
            attempt = max(0, attempt - 1)
        cached[key] = {'api_status': row.get('api_status'), 'attempt': attempt}
    return cached


def unresolved_request_intents(path):
    """Crash uncertainty must block paid retries, never be mistaken for zero cost."""
    started = set()
    settled = set()
    for row in rows(path):
        identity = row.get('request_id')
        if not isinstance(identity, str) or not identity:
            raise ValueError('Invalid visual request intent')
        if row.get('status') == 'started':
            started.add(identity)
        elif row.get('status') == 'settled':
            settled.add(identity)
        else:
            raise ValueError('Invalid visual request intent status')
    return started - settled


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--category', type=Path, required=True)
    p.add_argument('--candidates', type=Path, required=True)
    p.add_argument('--results', type=Path, required=True)
    p.add_argument('--validate-only', action='store_true')
    p.add_argument('--max-pairs', type=int, default=0)
    p.add_argument('--max-requests', type=int, default=0)
    p.add_argument('--independent', action='store_true')
    args = p.parse_args()
    if not args.validate_only:
        cat = load_category(args.category)
        authorization = ROOT / 'runs' / cat['category_key'] / 'stage123' / 'paid_enabled.json'
        if not authorization.is_file():
            raise SystemExit('第三层付费分析尚未在网页中点击启动；API请求数为0')
        marker = json.loads(authorization.read_text(encoding='utf-8'))
        if marker.get('controller') == 'independent' and not args.independent:
            return
        args.authorization = authorization
    lock = nullcontext() if args.validate_only else ProcessLock(args.results.parent / 'consumer.lock')
    with lock, frozen_candidates(args.candidates) as candidate_path:
        run(args, candidate_path)


def run(args, candidate_path):
    cat = load_category(args.category)
    from selection_automation.codex_visual_review import load_reviews
    codex_reviews = load_reviews(args.results.parent.parent, by_revision=True)
    cached = result_index(args.results)
    authorization=retry_authorizations(args.results.with_name('request_intents.jsonl'),args.results)
    held_pairs = set() if args.validate_only else billing_hold(
        args.results.with_name('request_intents.jsonl'), args.results)['pairs']
    fatal_file=args.results.with_name('provider_blocked.json')
    key_identity=__import__('hashlib').sha256((os.getenv('SELECTION_STAGE3_ENDPOINT','')+'\n'+
        os.getenv('SELECTION_STAGE3_API_KEY',os.getenv('DEEPSEEK_API_KEY',''))).encode()).hexdigest()
    def pending_rows():
        count = 0
        for row in rows(candidate_path):
            if not admitted(cat, row):
                continue
            if (row.get('asin'), row.get('goods_id')) in held_pairs:
                continue
            keys = compatible_vision_keys(cat, row)
            row['vision_revision_key'] = keys[0]
            priors = [cached.get(identity, {}) for identity in keys]
            prior = next((item for item in priors if item.get('api_status') == 'success'),
                         max(priors, key=lambda item: int(item.get('attempt', 0)), default={}))
            pair=(row.get('asin'),row.get('goods_id'))
            if prior.get('api_status') == 'success' or pair in authorization['success_pairs']:
                continue
            # Independent image reviews settle the analysis queue without
            # pretending they are API successes or settling billing intents.
            if any((*pair,key) in codex_reviews for key in keys):
                continue
            if pair in authorization['by_pair']:
                attempt=authorized_retry_attempt(authorization,pair,keys)
                if attempt is None: continue
            else:
                if int(prior.get('attempt',0))>=2:continue
                attempt=int(prior.get('attempt',0))+1
            row['attempt'] = attempt
            yield row
            count += 1
            if args.max_pairs and count >= args.max_pairs:
                return
    pending = pending_rows()
    try:
        consume(args, cat, fatal_file, key_identity, pending)
    finally:
        pending.close()


def consume(args, cat, fatal_file, key_identity, pending):
    def authorized():
        marker = getattr(args, 'authorization', None)
        return marker is None or (marker.is_file() and not (marker.parent/'STOP').exists())
    stage = cat['stage3']
    width = concurrency(stage)
    timeout_seconds = request_timeout_seconds(stage)
    initial_effort = stage.get('reasoning_effort', 'low')
    if not isinstance(initial_effort, str) or initial_effort not in ('low', 'medium', 'high'):
        raise ValueError('stage3.reasoning_effort must be low, medium, or high')
    if args.validate_only:
        print(json.dumps({'候选': sum(1 for _ in pending), 'API请求数': 0}, ensure_ascii=False))
        return
    intent_log = args.results.with_name('request_intents.jsonl')
    held = billing_hold(intent_log, args.results)
    if held['fail_closed']:
        raise SystemExit('检测到未结算视觉请求，已暂停付费重发；请先核对 request_intents.jsonl 和供应商用量，已保存结果仍可导出')
    pending = (row for row in pending if (row.get('asin'),row.get('goods_id')) not in held['pairs'])
    retry_scope=retry_authorizations(intent_log,args.results)
    pending = (row for row in pending if (row.get('asin'),row.get('goods_id')) not in retry_scope['by_pair']
               or (row.get('attempt') is not None and row.get('attempt')==authorized_retry_attempt(
                   retry_scope,(row.get('asin'),row.get('goods_id')),[row.get('vision_revision_key')])))
    first = next(pending, None)
    if first is None:
        if held['request_ids']:
            raise SystemExit('未结算视觉请求对应商品对已隔离，费用待核对；本轮无可运行候选，API请求数为0')
        return
    if not authorized():return
    if fatal_file.exists():
        blocked=json.loads(fatal_file.read_text(encoding='utf-8'))
        if blocked.get('key_identity')==key_identity:
            raise SystemExit('API鉴权或余额阻断，等待更新key或清除 provider_blocked.json 后恢复')
        fatal_file.unlink()
    pending = chain([first], pending)
    from selection_automation.stage3_provider import from_category, request_key, VisualClient
    provider=from_category(cat)
    try: key=request_key(provider)
    except ValueError as exc: raise SystemExit(str(exc)) from None
    endpoint = provider['base_url']
    from openai import OpenAI, DefaultHttpxClient
    from httpx2 import Timeout, Limits
    lock = threading.Lock()
    stop = threading.Event()
    fatal_stop = threading.Event()
    incomplete_stop = threading.Event()
    local_service_stop = threading.Event()
    requests = 0
    sent_requests = 0
    ledger = args.results.with_name('requests.jsonl')
    args.results.parent.mkdir(parents=True, exist_ok=True)

    def append(path, row):
        with lock, path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())

    def call(batch, effort):
        nonlocal requests, sent_requests
        if stop.is_set() or not authorized():
            stop.set()
            return
        ids = [f'C{i:02}' for i in range(len(batch))]
        started = time.monotonic()
        sent = False
        received = False
        usage = {}
        response_id = None
        request_id = uuid.uuid4().hex
        intent_started = False
        billing_uncertain = False
        def settle_intent():
            if intent_started:
                append(intent_log, {'request_id': request_id, 'status': 'settled', 'time': time.time(), 'billing_state': 'KNOWN' if received else 'NO_CHARGE'})
        try:
            from selection_automation.visual_examples import example_content
            content = example_content(cat,[p for row in batch for p in [row['amazon_image_path'],row['temu_image_path']]])
            content += [evidence_block(batch[0],'amazon','Amazon基准'), {'type': 'input_image', 'image_url': _data_url(batch[0]['amazon_image_path']), 'detail': 'high'}]
            for identity, row in zip(ids, batch):
                content += [evidence_block(row,'temu',identity), {'type': 'input_image', 'image_url': _data_url(row['temu_image_path']), 'detail': 'high'}]
            prompt, schema = protocol(cat, len(batch))
            if provider['protocol']=='decisions':
                # Native decisions are pairwise; reference examples would change image positions.
                content=[evidence_block(batch[0],'amazon','Amazon基准'),
                         {'type':'input_image','image_url':_data_url(batch[0]['amazon_image_path'])},
                         evidence_block(batch[0],'temu','TEMU候选'),
                         {'type':'input_image','image_url':_data_url(batch[0]['temu_image_path'])}]
            from selection_automation.vision import GROUP_WIRE_VERSION
            wire_version = 'product-choice-v1' if provider['protocol']=='decisions' else GROUP_WIRE_VERSION if 'amazon' in schema.get('properties', {}) else 'independent-candidates-v1'
            budget = output_token_budget(stage, len(batch))
            with lock:
                if stop.is_set() or not authorized() or (args.max_requests and requests >= args.max_requests):
                    stop.set()
                    return
                requests += 1
            if client_setup_error is not None:raise client_setup_error
            prepared_data=client.prepare(content,prompt)
            append(intent_log, {'request_id': request_id, 'status': 'started', 'time': time.time(),
                                'pairs': len(batch), 'vision_revision_keys': [row['vision_revision_key'] for row in batch],
                                'effort': effort, 'max_output_tokens': budget, 'wire_version': wire_version})
            intent_started = True
            with lock:
                sent_requests += 1
                sent = True
            response = client.responses.create(model=stage.get('model', 'deepseek-flash'), instructions=prompt,
                input=[{'role': 'user', 'content': content}], reasoning={'effort': effort},
                text={'format': {'type': 'json_schema', 'name': 'category_facts', 'strict': True, 'schema': schema}},
                max_output_tokens=budget,_prepared_data=prepared_data)
            usage = response.usage.model_dump() if response.usage else {}
            received = True
            response_id = response.id
            response_status = getattr(response, 'status', None)
            details = getattr(response, 'incomplete_details', None)
            incomplete_reason = (details.get('reason') if isinstance(details, dict) else getattr(details, 'reason', None)) or 'unknown'
            append(ledger, {'request_id': request_id, 'time': time.time(), 'pairs': len(batch), 'usage': usage, 'response_id': response.id, 'status': 'received', 'effort': effort, 'max_output_tokens': budget, 'wire_version': wire_version, 'response_status': response_status, 'incomplete_reason': incomplete_reason if response_status == 'incomplete' else None})
            if response_status == 'incomplete':
                raise IncompleteVisualResponse(incomplete_reason)
            parsed, format_repairs = parse_visual_response_compatible(response.output_text, allow_preamble=True)
            facts = {ids[0]:parsed['decision_facts']} if provider['protocol']=='decisions' else decode(cat, parsed, ids)
            for i, (identity, row) in enumerate(zip(ids, batch)):
                scored = score(cat, facts[identity])
                if effort == 'low' and scored['hard_conflicts'] and facts[identity].get('overall_confidence',1) < .65:
                    call([row], 'high')
                    continue
                append(args.results, {**{k: row[k] for k in ('asin','goods_id','pair_revision','vision_revision_key','attempt')},
                    'api_status': 'success', 'visual_facts': facts[identity], 'module_result': scored,
                    'model': stage.get('model', 'deepseek-flash'), 'response_id': response.id, 'effort': effort,
                    'usage': usage if i == 0 else {}, 'elapsed_seconds': round(time.monotonic()-started, 3),
                    **({'response_format_compatibility': {'version': 'lossless-envelope-v2-syntax-only', 'repairs': format_repairs}} if format_repairs else {})})
        except Exception as exc:
            from selection_workflow.api_image_input import ImageInputError
            if isinstance(exc,IncompleteVisualResponse):
                incomplete_stop.set();stop.set()
            if getattr(exc,'response_id',None):response_id=exc.response_id
            if getattr(exc,'model_usage',None):usage=exc.model_usage
            status = getattr(exc, 'status_code', None)
            cause_types = safe_exception_causes(exc)
            append(args.results.with_name('exception_diagnostics.jsonl'),
                   {'request_id':request_id,'time':time.time(),'cause_types':cause_types,
                    'status_code':status,'response_received':received})
            if received:
                # Preserve received facts for local review even when decoding,
                # truncation or a stopped split leaves no successful pair row.
                append(args.results.with_name('response_errors.jsonl'),
                       {'request_id':request_id,'response_id':response_id,'time':time.time(),
                        'vision_revision_keys':[row['vision_revision_key'] for row in batch],
                        'cause_types':cause_types,'output_text':response.output_text})
            reason = f'{type(exc).__name__}: HTTP {status}' if status else type(exc).__name__
            if isinstance(exc, ImageInputError):
                reason = str(exc)
                if not sent and len(batch) > 1:
                    # Isolate the invalid input locally; other candidates can
                    # still complete. No parent provider call or paid retry.
                    middle = len(batch) // 2
                    call(batch[:middle], effort)
                    call(batch[middle:], effort)
                    return
            if isinstance(exc, IncompleteVisualResponse):
                reason = 'VISUAL_RESPONSE_INCOMPLETE: ' + exc.reason
            billing_uncertain = sent and not received and provider['mode']!='local_model'
            if provider['mode']=='local_model' and not received:
                local_service_stop.set();stop.set()
            billing_state = 'UNKNOWN' if billing_uncertain else 'KNOWN' if received else 'NO_CHARGE'
            if billing_uncertain:
                reason += ('；请求被拒绝，但费用待核对，已阻止自动重发' if status in (401, 402, 403, 429)
                           else '；传输结果不明，费用未知，请核对供应商用量')
            if not received:
                append(ledger, {'request_id': request_id, 'time': time.time(), 'pairs': len(batch), 'status': 'failed' if sent else 'local_error', 'reason': reason, 'cause_types':cause_types, 'effort': effort, 'billing_state': billing_state})
            if status in (401, 402, 403):
                stop.set()
                fatal_stop.set()
                with lock:
                    fatal_file.write_text(json.dumps({'status':status,'key_identity':key_identity}),encoding='utf-8')
            if received and isinstance(exc, (ValueError, KeyError, TypeError)) and len(batch) > 1:
                middle = len(batch)//2
                call(batch[:middle], effort); call(batch[middle:], effort)
                settle_intent()
                return
            for i, row in enumerate(batch):
                append(args.results, {**{k: row[k] for k in ('asin','goods_id','pair_revision','vision_revision_key','attempt')},
                                      'api_status': 'failed', 'failure_reason': reason, 'failure_status': status, 'failure_cause_types':cause_types, 'billing_state': billing_state,
                                      'response_id': response_id, 'usage': usage if i == 0 else {}, 'effort': effort})
            if status == 429:
                stop.set()
                fatal_stop.set()
        if not billing_uncertain:
            settle_intent()
    size = 1 if provider['protocol']=='decisions' else max(1, min(12, stage.get('batch_size', 12)))
    def batches():
        # At most one partial batch per Amazon image, instead of N*M pending rows.
        groups = defaultdict(list)
        for row in pending:
            group = groups[(row['asin'], row['amazon_image_path'])]
            group.append(row)
            if len(group) >= size:
                yield list(group)
                group.clear()
        yield from (group for group in groups.values() if group)
    batch_stream = batches()
    # HTTPx2's sync Client is thread-safe. Build one SSL context/connection pool
    # per pass; do not mutate client settings while worker calls are active.
    # ExitStack outlives the executor, including exceptional worker exits.
    client=None;client_setup_error=None
    with ExitStack() as resources:
        try:
            api_timeout=Timeout(timeout_seconds,connect=30,pool=30)
            transport=resources.enter_context(DefaultHttpxClient(trust_env=False,timeout=api_timeout,
                limits=Limits(max_connections=width,max_keepalive_connections=min(width,256))))
            client=resources.enter_context(VisualClient(provider,transport,key,api_timeout))
        except Exception as exc:
            # Preserve local NO_CHARGE bookkeeping, without a sent intent.
            client_setup_error=exc
        with ThreadPoolExecutor(max_workers=width) as pool:
            # Bounded scheduler: only one wave is submitted, allowing rate-limit backoff.
            while wave := list(islice(batch_stream, width)):
                for future in as_completed([pool.submit(call, b, initial_effort) for b in wave]):
                    future.result()
                if stop.is_set():
                    break
    after = billing_hold(intent_log, args.results)
    if after['fail_closed'] or fatal_stop.is_set():
        raise SystemExit('未结算视觉请求：请求被拒绝或传输结果不明，费用待核对，已阻止付费重发；请按交付说明核对供应商用量，部分结果仍可导出')
    if incomplete_stop.is_set():
        raise SystemExit('模型输出被截断，已保存请求编号与用量并暂停；可能已计费，不自动重复请求。请调整输出预算后手动继续')
    if local_service_stop.is_set():
        raise SystemExit('本地模型服务未返回有效响应，已暂停；没有远程费用，请启动或修复本地服务后手动继续')
    print(json.dumps({'API请求数': sent_requests, '暂停': stop.is_set(), '费用待核对商品对':len(after['pairs'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
