"""Descriptive market hints from the two visible TEMU sorting passes.

Displayed sold counts have an unspecified period. Never call these month sales,
growth rates, or evidence of an exact listing date.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path
import re
from statistics import median
from urllib.parse import urlsplit


def displayed_sales(value):
    raw = str(value or '').strip()
    match = re.fullmatch(r'([\d][\d,]*(?:\.\d+)?)\s*([KM]?)\s*(\+?)\s*(?:sold|sales?)', raw, re.I)
    if not match:
        return None
    number = float(match[1].replace(',', '')) * {'': 1, 'K': 1000, 'M': 1000000}[match[2].upper()]
    return number if math.isfinite(number) else None


def _product(row):
    url = str(row.get('product_url') or '')
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == 'https' and (parsed.hostname == 'temu.com' or (parsed.hostname or '').endswith('.temu.com'))
    except ValueError:
        valid = False
    if not valid:
        url = ''
    return {'goods_id': str(row['goods_id']), 'title': str(row.get('title') or row['goods_id']),
            'url': url, 'sales_text': str(row.get('sales_text') or ''),
            'displayed_sales_approx': displayed_sales(row.get('sales_text'))}


def sponsored(row):
    if type(row.get('is_sponsored')) is bool: return row['is_sponsored']
    if any(re.fullmatch(r'AD|Sponsored|广告',line.strip(),re.I) for line in str(row.get('card_text') or '').splitlines()):
        return True
    return None  # Older snapshots did not record a negative ad observation.


def _prefer_observation(current, candidate):
    """Deduplicate a product across keywords without losing known sales."""
    if current is None: return True
    old_natural=sponsored(current) is False
    new_natural=sponsored(candidate) is False
    if new_natural != old_natural: return new_natural
    old_known=displayed_sales(current.get('sales_text')) is not None
    new_known=displayed_sales(candidate.get('sales_text')) is not None
    if new_known != old_known: return new_known
    return str(candidate.get('observed_at') or '') >= str(current.get('observed_at') or '')


def _stats(products, mode):
    amounts = [p['displayed_sales_approx'] for p in products if p['displayed_sales_approx'] is not None]
    sold = sum(value > 0 for value in amounts)
    total = sum(amounts)
    return {'sample_products': len(products), 'sales_display_products': sum(bool(p['sales_text']) for p in products),
            'interpretable_sales_products': len(amounts), 'unknown_sales_products': len(products)-len(amounts),
            'with_sales_products': sold,
            'with_sales_percent': round(100*sold/len(amounts), 1) if amounts else None,
            'displayed_sales_median_approx': median(amounts) if amounts else None,
            'top5_displayed_share_approx': round(100*sum(sorted(amounts, reverse=True)[:5])/total, 1)
                if mode == 'sales' and len(amounts) > 5 and total > 0 else None,
            'examples': sorted(products, key=lambda p: p['displayed_sales_approx'] or 0, reverse=True)[:3]}


def _summary(rows, mode, progress):
    groups={False:[],True:[],None:[]}
    for row in rows.values(): groups[sponsored(row)].append(_product(row))
    completed=bool(progress) and all(item.get('status') in ('KEYWORD_COMPLETE','KEYWORD_LIMIT_REACHED') for item in progress)
    updated=max((str(item.get('updated_at')) for item in progress if item.get('updated_at')), default=None)
    return {**_stats(groups[False],mode), 'captured_products':len(rows),
            'sponsored_products':len(groups[True]),'unknown_sponsorship_products':len(groups[None]),
            'sponsored':_stats(groups[True],mode),'unknown_sponsorship':_stats(groups[None],mode),
            'sample_basis':'OBSERVED_NON_AD_CARDS','progress':progress,
            'pass_completed':completed,'sample_updated_at':updated}


def evidence_signal(sample, mode):
    n=sample.get('sample_products',0); known=sample.get('interpretable_sales_products',0)
    coverage=known/n if n else 0
    result={'state':'INSUFFICIENT','label':'证据不足','natural_products':n,
            'sales_coverage_percent':round(coverage*100,1),
            'minimum_products':1 if mode=='recent' else 30,
            'minimum_sales_coverage_percent':0 if mode=='recent' else 80,
            'scope':'当前关键词的自然商品样本','period':'页面销量累计周期未知'}
    if mode=='recent' and n:
        sold=sample.get('with_sales_products',0)
        # Missing sales are unknown, not unsold. Use the worst case on each side.
        lower=sold/n
        upper=(sold+n-known)/n
        result['confirmed_sold_lower_bound_percent']=round(lower*100,1)
        if lower>=.5:
            result.update(state='FAVORABLE' if n>=30 else 'PRELIMINARY',label='有利信号' if n>=30 else '初步有利',
                reason=f'最近上架排序的{n}件自然样本中，至少{sold}件显示已售（至少{round(lower*100,1)}%）；另有{n-known}件销量未显示。这不是90天上架确认或周/月环比')
        elif upper<=.1:
            result.update(state='ADVERSE' if n>=30 else 'PRELIMINARY',label='不利信号' if n>=30 else '初步不利',
                reason=f'最近上架排序的{n}件自然样本中，即使把销量未知的{n-known}件都算作已售，已售比例也不超过{round(upper*100,1)}%；不是90天上架确认或周/月环比')
        else:
            result.update(state='MIXED',label='线索尚不明显',
                reason=f'最近上架排序的{n}件自然样本中，{sold}件显示已售，{n-known}件销量未知；已售比例至少{round(lower*100,1)}%，目前只能作初步参考')
    elif mode=='recent':
        result['reason']='最近上架轮次尚无自然商品样本。'
    elif n<30 or coverage<.8:
        result['reason']=('样本缺少完整广告识别记录，暂不能判断；原始商品仍可在 Excel 查看'
            if n<30 and sample.get('unknown_sponsorship_products',0)>0 else
            '非广告商品不足30件或可读销量不足80%，先补足样本')
    elif mode=='sales':
        share=sample.get('top5_displayed_share_approx')
        if share is not None:
            state,label=('ADVERSE','不利信号') if share>=90 else ('FAVORABLE','有利信号') if share<=50 else ('MIXED','信号一般')
            result.update(state=state,label=label,reason=f'自然样本前5件展示销量约占{share}%；不是全类目份额')
    return result


def build_frontend_market(run_dir):
    """Cache by file versions so the web status poll does not reread unchanged data."""
    root = Path(run_dir)/'temu'/'workers'
    versions = []
    for pattern in ('*/*/products.jsonl', '*/*/progress.json'):
        for path in sorted(root.glob(pattern)):
            try:
                stat = path.stat()
                versions.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
    report = dict(_build(tuple(versions)))
    try:
        plan = json.loads((Path(run_dir)/'temu'/'keyword_plan.json').read_text('utf-8'))
        report['primary_category_query'] = next((r['query'] for r in plan.get('keywords', []) if r.get('purpose') == 'CATEGORY_SAMPLE'), None)
    except (OSError, ValueError, TypeError, KeyError):
        report['primary_category_query'] = None
    report['queries']=[{**row,**{mode:{**row[mode],'signal':evidence_signal(row[mode],mode)} for mode in ('sales','recent')}} for row in report['queries']]
    all_keywords=report['all_keywords']
    report['all_keywords']={mode:{**all_keywords[mode],
        'signal':{**evidence_signal(all_keywords[mode],mode),
            'scope':'所有关键词去重后的自然商品样本'}} for mode in ('sales','recent')}
    report['criteria_evidence']={'same_products':'留待第二部分严格同款分析；观察到不超过20件，不代表全站不超过20件',
        'growth':'最近上架排序只提供新品成交线索，不能计算周/月环比',
        'concentration':'跨关键词按商品去重的搜索样本，仅供动态参考；大类目词另列，广告和未知广告属性单列'}
    return report


@lru_cache(maxsize=4)
def _build(versions):
    groups = {}
    all_rows={'sales':{},'recent':{}}
    all_progress={'sales':[],'recent':[]}
    def group(query):
        return groups.setdefault(query, {'sales': {}, 'recent': {}, 'progress': {'sales': [], 'recent': []}})
    skipped = 0
    for filename, _, _ in sorted(versions, key=lambda version: version[1]):
        path = Path(filename)
        try:
            content = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError):
            skipped += 1
            continue
        if path.name == 'progress.json':
            try:
                progress = json.loads(content)
                sort = progress.get('sales_sort') or {}
                query, mode = progress.get('search_query'), sort.get('requested')
                if query and mode in ('sales', 'recent'):
                    item={'worker': path.parent.parent.name,
                        'status': progress.get('status'), 'sort_state': sort.get('state'),
                        'updated_at': progress.get('updated_at'), 'products': progress.get('unique_goods'),
                        'sampling':progress.get('sampling')}
                    group(str(query))['progress'][mode].append(item)
                    all_progress[mode].append(item)
            except (ValueError, AttributeError):
                skipped += 1
            continue
        for line in content.splitlines():
            try:
                row = json.loads(line)
                sort = row.get('search_sort') or {}
                mode = sort.get('requested')
                if mode not in ('sales', 'recent') or sort.get('state') != 'UI_CONFIRMED' or not row.get('goods_id'):
                    continue
                goods_id=str(row['goods_id'])
                if _prefer_observation(all_rows[mode].get(goods_id),row):
                    all_rows[mode][goods_id]=row
                queries = row.get('source_queries') or [row.get('search_query')]
                if isinstance(queries, str):
                    queries = [queries]
                for query in queries:
                    if query:
                        group(str(query))[mode][str(row['goods_id'])] = row
            except (ValueError, AttributeError, TypeError):
                skipped += 1
    all_sets=[set(value[mode]) for value in groups.values() for mode in ('sales','recent')]
    captured=sum(map(len,all_sets)); unique=len(set().union(*all_sets)) if all_sets else 0
    return {'sampling_efficiency':{'pass_products':captured,'unique_products':unique,
                'duplicate_percent':round(100*(1-unique/captured),1) if captured else None},
            'basis': 'VISIBLE_FRONTEND_APPROXIMATE', 'read_issues': skipped,
            'note': '最近上架排序样本用于观察新品是否卖得动；不代表已确认在90天内上架。销量为页面展示数量级，周期不明，不是周/月环比。',
            'all_keywords':{mode:{**_summary(all_rows[mode],mode,all_progress[mode]),
                'keyword_count':sum(bool(value[mode]) for value in groups.values())} for mode in ('sales','recent')},
            'queries': [{'query': query, **{mode: _summary(value[mode], mode, value['progress'][mode])
                        for mode in ('sales', 'recent')}} for query, value in sorted(groups.items())]}
