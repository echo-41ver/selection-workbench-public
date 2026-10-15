"""Evidence-based commercial screening. Unknown inputs never become zero sales."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from statistics import median
from urllib.parse import urlsplit

DEFAULTS = {
    'same_operator': 'lte',
    'same_limit': 20, 'new_days': 90, 'growth_min_percent': 20.0,
    'growth_period': 'either', 'top_n': 5, 'top_share_max_percent': 90.0,
    'min_new_products': 5, 'min_market_products': 30, 'min_sales_coverage': 0.8,
}
LABELS = {'PASS': '满足', 'SAMPLE_PASS': '采样内满足', 'FAIL': '不满足', 'UNKNOWN': '证据不足'}


def settings(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('商业筛选设置字段无效')
    result = {**DEFAULTS, **value}
    if result['same_operator'] not in ('lt','lte'):raise ValueError('同款数量比较方式无效')
    for key, minimum, maximum in [('same_limit', 0, 10000), ('new_days', 1, 365),
                                  ('top_n', 1, 100), ('min_new_products', 1, 1000),
                                  ('min_market_products', 2, 100000)]:
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise ValueError(f'商业筛选设置 {key} 超出范围')
    for key, minimum, maximum in [('growth_min_percent', 0, 10000), ('top_share_max_percent', 0.1, 100),
                                  ('min_sales_coverage', 0.1, 1)]:
        if type(result[key]) not in (float, int) or not math.isfinite(result[key]) or not minimum <= result[key] <= maximum:
            raise ValueError(f'商业筛选设置 {key} 超出范围')
    if result['growth_period'] not in ('week', 'month', 'either'):
        raise ValueError('商业筛选周期或跟踪开关无效')
    return result


def read_json(path, default=None):
    if not path.exists(): return {} if default is None else default
    return json.loads(path.read_text(encoding='utf-8'))


def jsonl(path):
    if path.exists():
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip(): yield json.loads(line)


def instant(value):
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError): return None


def market_key(category):
    """Stable across run IDs, isolated by site and actual approved category URL."""
    url = urlsplit(category.get('amazon', {}).get('bestsellers_url', ''))
    path = url.path.split('/ref=')[0].rstrip('/')
    identity = [category.get('temu', {}).get('site', 'TEMU_US'), url.hostname, path]
    if not path:
        identity += [category.get('category_name_zh', ''),
                     sorted(str(q).strip().lower() for q in category.get('temu', {}).get('base_queries', []))]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]


def sales_evidence(row):
    """Plain `sold` has no asserted period. Plus/rounded labels remain lower bounds."""
    raw = str(row.get('sales_text') or '').strip()
    value = row.get('sales_count')
    basis = row.get('sales_basis')
    evidence = row.get('sales_evidence_url') or row.get('source_url') or row.get('product_url')
    if (type(value) in (int, float) and math.isfinite(value) and value >= 0
            and row.get('sales_precision') == 'exact' and basis == 'lifetime' and evidence):
        return {'value': float(value), 'lower': float(value), 'upper': float(value),
                'basis': basis, 'precision': 'exact', 'raw': raw, 'evidence_url': evidence}
    match = re.search(r'([0-9][0-9,.]*)\s*([kKmM]?)\s*(\+?)\s*(?:sold|sales?)', raw)
    lower = None
    if match:
        try: lower = float(match[1].replace(',', '')) * {'': 1, 'k': 1000, 'm': 1000000}[match[2].lower()]
        except ValueError: pass
    return {'value': None, 'lower': lower, 'upper': None, 'basis': 'displayed_unspecified',
            'precision': 'lower_bound' if match and (match[2] or match[3]) else 'period_unknown',
            'raw': raw, 'evidence_url': evidence}


class History:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS observations(
              market TEXT NOT NULL, goods_id TEXT NOT NULL, observed_at TEXT NOT NULL,
              fingerprint TEXT NOT NULL, run_key TEXT NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(market,goods_id,observed_at,fingerprint));
            CREATE INDEX IF NOT EXISTS market_time ON observations(market,observed_at);
        ''')

    def close(self): self.db.close()

    def ingest(self, category, rows):
        key = market_key(category)
        count = 0
        for row in rows:
            stamp = instant(row.get('observed_at'))
            if not stamp or not row.get('goods_id'): continue
            # Only source facts, not copied run-level credentials/configuration.
            payload = {k: row.get(k) for k in ('goods_id', 'title', 'sales_text', 'sales_count', 'sales_basis',
                       'sales_precision', 'sales_evidence_url', 'listed_at', 'listed_at_source', 'price_usd',
                       'seller_id', 'source_url', 'product_url', 'source_queries', 'search_query', 'source')}
            payload['sales'] = sales_evidence(row)
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            cursor = self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)',
                                    (key, str(row['goods_id']), stamp.isoformat(),
                                     hashlib.sha256(encoded.encode()).hexdigest(), category['category_key'], encoded))
            count += cursor.rowcount
        self.db.commit()
        return count

    def rows(self, category):
        # A bounded 180-day window is enough for 90-day cohorts and adjacent months.
        cutoff = (datetime.now(timezone.utc)-timedelta(days=180)).isoformat()
        rows = []
        for gid, stamp, payload in self.db.execute(
                'SELECT goods_id,observed_at,payload FROM observations WHERE market=? AND observed_at>=? ORDER BY observed_at',
                (market_key(category), cutoff)):
            rows.append({**json.loads(payload), 'goods_id': gid, 'observed_at': stamp})
        return rows


def metric(state, reason, **extra):
    return {'state': state, 'label': LABELS[state], 'reason': reason, **extra}


def endpoint(series, target):
    candidates = [r for r in series if abs((instant(r['observed_at'])-target).total_seconds()) <= 86400]
    if not candidates: return None
    chosen = min(candidates, key=lambda r: abs((instant(r['observed_at'])-target).total_seconds()))
    sales = chosen.get('sales') or sales_evidence(chosen)
    if sales['basis'] != 'lifetime' or sales['precision'] != 'exact': return None
    # Conflicting observations at the same timestamp cannot be averaged away.
    duplicates = [r for r in series if r['observed_at'] == chosen['observed_at']]
    if len({(r.get('sales') or sales_evidence(r)).get('value') for r in duplicates}) > 1: return None
    return sales['value'], instant(chosen['observed_at'])


def changes(series, as_of, days, periods=2):
    points = [endpoint(series, as_of-timedelta(days=days*i)) for i in range(periods, -1, -1)]
    if any(point is None for point in points): return None
    if any(points[i+1][1] <= points[i][1] or points[i+1][0] < points[i][0] for i in range(len(points)-1)):
        return None
    # Normalize slight daily sampling drift; compare equal-length period rates.
    return [(points[i+1][0]-points[i][0]) / ((points[i+1][1]-points[i][1]).total_seconds()/86400) * days
            for i in range(len(points)-1)]


def market_metrics(category, current_rows, history_rows, as_of=None):
    current_rows=[r for r in current_rows if r.get('source')!='GEEKBI_SUPPLEMENT']
    history_rows=[r for r in history_rows if r.get('source')!='GEEKBI_SUPPLEMENT']
    policy = settings(category.get('commercial'))
    as_of = as_of or datetime.now(timezone.utc)
    primary_query = str((category.get('temu', {}).get('base_queries') or [''])[0]).strip().lower()
    # Only the primary query is dispatched as the category/sales-sort sample.
    # The remaining generated base_queries can be ASIN-specific feature queries.
    market = [r for r in current_rows if primary_query and primary_query in {
        str(q).strip().lower() for q in (r.get('source_queries') or [r.get('search_query') or ''])}]
    by_id = defaultdict(list)
    for row in history_rows:
        stamp = instant(row.get('observed_at'))
        if stamp and stamp <= as_of: by_id[str(row['goods_id'])].append(row)
    new = []
    for row in market:
        candidates = by_id[str(row['goods_id'])]
        dates = {instant(r.get('listed_at')) for r in candidates if r.get('listed_at_source') and instant(r.get('listed_at'))}
        if len(dates) == 1:
            listed = next(iter(dates))
            if 0 <= (as_of-listed).total_seconds() <= policy['new_days']*86400:
                new.append((row, listed))
    periods = {}
    for label, days in [('week', 7), ('month', 30)]:
        values = []
        eligible=[(row,listed) for row,listed in new if (as_of-listed).total_seconds()>=days*2*86400]
        for row, listed in eligible:
            increments = changes(by_id[str(row['goods_id'])], as_of, days)
            if increments and increments[0] > 0:
                values.append((increments[1]/increments[0]-1)*100)
        coverage = len(values)/len(eligible) if eligible else 0
        enough = len(values) >= policy['min_new_products'] and coverage >= policy['min_sales_coverage']
        value = median(values) if values else None
        periods[label] = metric('SAMPLE_PASS' if enough and value > policy['growth_min_percent'] else 'FAIL' if enough else 'UNKNOWN',
            f"按有来源上架日期的近{policy['new_days']}天新品计算增长中位数；仅反映当前类目搜索样本" if enough else
            '需要真实上架日期、相邻两个完整周期的精确销量及足够新品样本；首次观测不等于上架',
            value=value, valid_products=len(values), new_products=len(new), eligible_products=len(eligible),
            too_young_products=len(new)-len(eligible), coverage=coverage,
            rising_products=sum(v > policy['growth_min_percent'] for v in values), days=days)
    selected = [periods[policy['growth_period']]] if policy['growth_period'] != 'either' else list(periods.values())
    state = 'SAMPLE_PASS' if any(p['state']=='SAMPLE_PASS' for p in selected) else 'FAIL' if all(p['state']=='FAIL' for p in selected) else 'UNKNOWN'
    growth = metric(state, '周/月按所选口径判断；不把缺失周期当作增长为零', periods=periods,
                    threshold=policy['growth_min_percent'], scope='类目基础关键词采样')
    amounts = []
    for row in market:
        delta = changes(by_id[str(row['goods_id'])], as_of, 30, 1)
        if delta is not None: amounts.append(delta[0])
    coverage = len(amounts)/len(market) if market else 0
    total = sum(amounts)
    share = sum(sorted(amounts, reverse=True)[:policy['top_n']])/total if total > 0 else None
    enough = len(amounts) >= max(policy['min_market_products'], policy['top_n']+1) and coverage >= policy['min_sales_coverage'] and total > 0
    concentration = metric('SAMPLE_PASS' if enough and share*100 < policy['top_share_max_percent'] else 'FAIL' if enough else 'UNKNOWN',
        '头部按最近30天销量排序，分母为有精确销量的同类采样商品；不是搜索排序或全站销量' if enough else
        '需要同一类目足够商品的近30天销量；带K/+且统计周期不明的展示销量不能计算真实占比',
        value_percent=round(share*100, 2) if enough else None, valid_products=len(amounts), sample_products=len(market),
        coverage=coverage, top_n=policy['top_n'], threshold_percent=policy['top_share_max_percent'], scope='类目基础关键词采样')
    stamps = [instant(r['observed_at']) for r in history_rows if instant(r['observed_at']) and instant(r['observed_at']) <= as_of]
    return {'growth': growth, 'concentration': concentration, 'market_products': len(market),
            'primary_category_query': primary_query, 'all_frontend_products': len(current_rows),
            'dated_new_products': len(new), 'observations': len(history_rows),
            'history_days': len({s.date() for s in stamps}), 'first_observation': min(stamps).isoformat() if stamps else None,
            'last_observation': max(stamps).isoformat() if stamps else None,
            'scope_note': '采样结论不代表TEMU全站；三个维度独立展示，缺失证据不作为满足或不满足。'}


def match_metric(counts, expected, policy):
    confirmed = counts.get('confirmed', 0)
    similar = counts.get('similar', 0)
    combined = confirmed + similar
    pending = counts.get('pending', 0) + max(0, expected-sum(counts.values()))
    possible = counts.get('possible', 0)
    strict=policy.get('same_operator')=='lt'
    if combined >= policy['same_limit'] if strict else combined > policy['same_limit']:
        state, reason = 'FAIL', '最终同款与类似款合计已达到筛选门槛'
    elif not expected or pending or possible:
        state, reason = 'UNKNOWN', '候选分析或待复核尚未完成，不能将未知商品当作零同款'
    else:
        state, reason = 'SAMPLE_PASS', '当前采样最终同款与类似款合计符合门槛；尚未证明TEMU全站数量'
    return metric(state, reason, confirmed=confirmed, possible=possible, pending=pending,different=counts.get('different',0),
                  similar=similar, same_or_similar=combined,
                  screened_out=counts.get('screened_out', 0), expected=expected,
                  threshold=policy['same_limit'], scope='本次采集及三层候选核验范围')


def report_signature(root, category):
    """Cheap source/version identity; never scan the large frozen pair files."""
    import hashlib
    base = Path(root)/'runs'/category['category_key']
    frozen = base/'stage123/export_frozen'
    paths = [base/'category.json', base/'amazon/amazon_frontend_latest.json',
             base/'temu/union/products.jsonl', base/'stage123/results.jsonl',
             base/'market/geekbi.json', base/'market/status.json',
             *sorted(frozen.glob('*.json*')),
             *sorted((base/'stage123/codex_reviews').glob('*.jsonl')),
             *sorted((base/'temu/workers').glob('*/*/products.jsonl')),
             *sorted((base/'temu/workers').glob('*/*/progress.json')),
             *(Path(__file__).parent/name for name in ('commercial.py','codex_visual_review.py',
                 'rules.py','vision.py','market_provider.py','frontend_market.py','operations.py','selection_delivery.py','stage3_provider.py'))]
    sources = []
    for path in paths:
        try:
            stat = path.stat()
            name=str(path.relative_to(root)) if path.is_relative_to(root) else '/'.join(path.parts[-2:])
            size = stat.st_size
            if path.name == 'progress.json':
                progress = read_json(path)
                check = progress.get('sales_sort') or {'state': 'NOT_REQUESTED'}
                evidence = {'search_query': str(progress.get('search_query') or '').strip().lower(),
                            'sales_sort': {key: check.get(key) for key in ('state','label','observed_at')}}
                payload = json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()
                size, version = len(payload), hashlib.sha256(payload).hexdigest()
            elif path.suffix == '.py' or (path.name == 'products.jsonl' and size <= 65536):
                version = hashlib.sha256(path.read_bytes()).hexdigest()
            elif size == 0:
                version = 'empty'
            else:
                version = stat.st_mtime_ns
            sources.append((name,size,version))
        except FileNotFoundError:
            name=str(path.relative_to(root)) if path.is_relative_to(root) else '/'.join(path.parts[-2:])
            sources.append((name,None,None))
    payload = {'version':5,'category':category,'sources':sources,
               'date':datetime.now(timezone.utc).date().isoformat()}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def current_report(root, category):
    """Reuse only a report bound to current evidence, including Codex reviews."""
    path = Path(root)/'runs'/category['category_key']/'commercial_report.json'
    report = read_json(path)
    if report and report.get('evidence_signature') == report_signature(root,category):
        return report
    report = build_report(root,category)
    from .merge_temu_snapshots import atomic_write
    atomic_write(path,json.dumps(report,ensure_ascii=False,indent=2))
    return report


def build_report(root, category, *, as_of=None):
    evidence_signature = report_signature(root,category)
    from .rules import score
    from .vision import compatible_vision_keys, VisionRevisionContext
    key = category['category_key']; base = root/'runs'/key
    policy = settings(category.get('commercial'))
    products = list(jsonl(base/'temu/union/products.jsonl'))
    # Persist every worker's observation, not only the winning union snapshot.
    history = History(root/'runtime/commercial/observations.sqlite3')
    try:
        history.ingest(category, products)
        for path in (base/'temu/workers').glob('*/*/products.jsonl'):
            history.ingest(category, jsonl(path))
        market = market_metrics(category, products, history.rows(category), as_of)
    finally: history.close()
    primary_query = str((category.get('temu', {}).get('base_queries') or [''])[0]).strip().lower()
    sort_checks = []
    for path in (base/'temu/workers').glob('*/*/progress.json'):
        progress = read_json(path)
        if str(progress.get('search_query') or '').strip().lower() == primary_query:
            check = progress.get('sales_sort') or {'state': 'NOT_REQUESTED'}
            sort_checks.append({'query': primary_query, 'state': check.get('state'),
                                'label': check.get('label'), 'observed_at': check.get('observed_at')})
    confirmed = sum(item['state']=='UI_CONFIRMED' for item in sort_checks)
    market['frontend_sort'] = {
        'state': 'UI_CONFIRMED' if sort_checks and confirmed==len(sort_checks) else 'UNKNOWN',
        'checks': sort_checks,
        'reason': ('类目搜索页面显示已选销量排序；展示销量的精度和统计周期仍需分别核实'
                   if sort_checks and confirmed==len(sort_checks) else
                   '尚未确认类目搜索已按销量排序；普通搜索样本不代表销量头部，极鲸云指标另行计算')}
    from .market_provider import metrics as provider_metrics
    provider_state=read_json(base/'market/status.json')
    provider=provider_metrics(category,read_json(base/'market/geekbi.json'),as_of) if provider_state.get('status') in ('COMPLETE','PARTIAL') else None
    market['provider_status']=provider_state
    if provider:
        market['frontend_growth']=market['growth']
        market['frontend_concentration']=market['concentration']
        market['growth']=provider['growth']
        market['concentration']=provider['concentration']
        market['provider']=provider
    # Keep separately sourced period metrics, but the selection criterion uses
    # the user's all-collected denominator and a fixed, globally deduplicated Top 5.
    from .frontend_market import build_frontend_market
    frontend=build_frontend_market(base)
    sales=frontend['all_keywords']['sales']
    signal=sales['signal']
    share=sales['top5_displayed_share_approx']
    enough=(sales['sample_products']>=policy['min_market_products'] and
            signal['sales_coverage_percent']/100>=policy['min_sales_coverage'] and share is not None)
    market['period_concentration']=market['concentration']
    market['concentration']=metric('SAMPLE_PASS' if enough and share<policy['top_share_max_percent'] else 'FAIL' if enough else 'UNKNOWN',
        signal['reason'], value_percent=share, valid_products=sales['interpretable_sales_products'],
        sample_products=sales['sample_products'], coverage=signal['sales_coverage_percent']/100,
        top_n=5, threshold_percent=policy['top_share_max_percent'], scope='全部已采集去重商品',
        basis='DISPLAYED_SALES_APPROXIMATE', top5=sales['top5_products'],
        numerator_displayed_sales=sales['top5_displayed_sales_approx'],
        denominator_displayed_sales=sales['denominator_displayed_sales_total_approx'],
        missing_sales_products=sales['unknown_sales_products'])
    market['collected_sales']=sales
    market['primary_category_sales']=next((row['sales'] for row in frontend['queries']
        if row['query']==(frontend.get('primary_category_query') or primary_query)),None)
    frozen = base/'stage123/export_frozen'
    candidates = {}
    try: vision_context = VisionRevisionContext(category)
    except (OSError, KeyError, ValueError, TypeError): vision_context = None
    for row in jsonl(frozen/f'layer3_candidates_{key}.jsonl'):
        try: candidates[(row['asin'], row['goods_id'])] = set(compatible_vision_keys(category, row, context=vision_context))
        except (OSError, KeyError, ValueError): pass
    from .jsonl_records import read_jsonl
    log_warnings = []
    results = {}
    for row in read_jsonl(base/'stage123/results.jsonl', allow_incomplete_tail=True, warnings=log_warnings):
        identity = (row.get('asin'), row.get('goods_id'))
        if row.get('api_status') == 'success' and row.get('vision_revision_key') in candidates.get(identity, set()):
            results[identity] = row
    from .codex_visual_review import load_reviews, supersedes_api
    reviews = load_reviews(base, candidates, category=category)
    for pair, review in reviews.items():
        if pair in results and supersedes_api(category, review):
            results.pop(pair)
    counts = defaultdict(Counter)
    confirmed_goods=defaultdict(set)
    confirmed_pairs=defaultdict(dict)
    similar_goods=defaultdict(set)
    similar_pairs=defaultdict(dict)
    for pair in jsonl(frozen/f'layer12_rows_{key}.jsonl'):
        status = pair.get('pair_status'); asin = pair['asin'];bucket=None
        if status == 'STAGE1_EXCLUDED': counts[asin]['text_excluded'] += 1
        elif status == 'BELOW_VECTOR_THRESHOLD': counts[asin]['screened_out'] += 1
        elif status == 'CANDIDATE' and (asin, pair['goods_id']) in results:
            result_row = results[(asin, pair['goods_id'])]
            scored = score(category, result_row['visual_facts'])
            result = scored['state']
            bucket = 'confirmed' if result in ('确认同款', '同款（存在规格变体）') else 'different' if result.startswith('不同款') else 'similar' if result=='相似款' else 'possible'
            counts[asin][bucket] += 1
        elif status == 'CANDIDATE' and (asin, pair['goods_id']) in reviews:
            review = reviews[(asin, pair['goods_id'])]
            bucket = {'SAME':'confirmed','SIMILAR':'similar', 'DIFFERENT':'different', 'UNKNOWN':'possible'}[review['review_status']]
            counts[asin][bucket] += 1
        else: counts[asin]['pending'] += 1
        if bucket in ('confirmed','similar'):
            gid=str(pair['goods_id'])
            target_goods=confirmed_goods if bucket=='confirmed' else similar_goods
            target_pairs=confirmed_pairs if bucket=='confirmed' else similar_pairs
            target_goods[asin].add(gid)
            if (asin,pair['goods_id']) in results:
                from .review_evidence import visual_evidence_text, review_explanation
                facts=result_row['visual_facts']
                target_pairs[asin][gid]={'goods_id':gid,'state':scored['state'],
                    'reason':review_explanation(category,facts,scored),
                    'evidence':visual_evidence_text(category,facts) or scored['reason'],
                    'model':result_row.get('model'),'score':scored['total_score'],
                    'probability':scored.get('decision_probability')}
            else:
                target_pairs[asin][gid]={'goods_id':gid,'state':'相似款（Agent看图核验）' if bucket=='similar' else '确认同款（Agent看图核验）',
                    'reason':review['reason'],'evidence':f"Amazon：{review['amazon_evidence']}；TEMU：{review['temu_evidence']}",
                    'model':review.get('model'),'score':(review.get('module_result') or {}).get('total_score')}
    amazon = read_json(base/'amazon/amazon_frontend_latest.json').get('records', [])
    entries = []
    for item in amazon:
        counts[str(item['asin'])]['confirmed']=len(confirmed_goods[str(item['asin'])])
        counts[str(item['asin'])]['similar']=len(similar_goods[str(item['asin'])])
        match = match_metric(counts[str(item['asin'])], len(products), policy)
        states = [match, market['growth'], market['concentration']]
        signals = sum(m['state'] in ('PASS', 'SAMPLE_PASS') for m in states)
        unknown = sum(m['state']=='UNKNOWN' for m in states)
        entries.append({'asin': item['asin'], 'title': item.get('title'), 'rank': item.get('rank'),
                        'confirmed_goods_ids':sorted(confirmed_goods[item['asin']]),
                        'similar_goods_ids':sorted(similar_goods[item['asin']]),
                        'similar_pairs':list(similar_pairs[item['asin']].values()),
                        'confirmed_pairs':list(confirmed_pairs[item['asin']].values()),
                        'product_url': item.get('product_url'), 'match': match, 'signals': signals,
                        'unknown': unknown, 'priority': '优先核实并评估成本' if signals >= 2 else '有单项机会，继续观察' if signals else '等待证据' if unknown else '当前信号较弱',
                        'profitability': '成本与利润未评估，采样信号不是盈利保证'})
    entries.sort(key=lambda row: (-row['signals'], row['unknown'], row['rank'] or 999))
    from .operations import product_card
    from .selection_delivery import recommendation
    amazon_by_id={str(row['asin']):row for row in amazon}
    temu_by_id={str(row['goods_id']):row for row in products}
    for entry in entries:
        if entry['match']['confirmed'] >= policy['same_limit']:continue
        card=product_card(amazon_by_id[str(entry['asin'])],'amazon')
        rivals=[product_card(temu_by_id[g],'temu') for g in entry['confirmed_goods_ids'] if g in temu_by_id]
        entry['selection_advice']=recommendation(card,rivals,entry['match'],market)
        entry['amazon_card']=card
    return {'schema_version': 1, 'category_key': key, 'category_name': category.get('category_name_zh'),
            'evidence_signature': evidence_signature,
            'as_of': (as_of or datetime.now(timezone.utc)).isoformat(), 'policy': policy,
            'log_warnings': log_warnings,
            'market_key': market_key(category), 'market': market, 'products': entries,
            'scope': '同款来自真实前端采样；上架和增长可采用极鲸云独立来源；不声称全站覆盖，增长条件严格大于阈值'}
