"""Colleague-facing shortlist from confirmed identities and collected product data."""
from __future__ import annotations

from pathlib import Path

from .commercial import jsonl, read_json, settings
from .operations import product_card


def recommendation(amazon, matches, match, market):
    missing = []
    if match.get('pending') or match.get('possible') or match.get('state') == 'UNKNOWN':
        missing.append('候选核验未完成')
    if amazon['price']['observed_price'] is None:
        missing.append('Amazon价格未取得')
    if amazon['sales']['text'] is None:
        missing.append('Amazon销量未取得')
    for field,label in [('rating','评分'),('review_count','评论数')]:
        if amazon[field] is None:missing.append('Amazon'+label+'未取得')
        if any(card[field] is None for card in matches):missing.append('部分TEMU同款'+label+'未取得')
    if amazon['price']['min'] is None or amazon['price']['max'] is None:
        missing.append('Amazon规格价格带未取得')
    if any(card['price']['min'] is None or card['price']['max'] is None for card in matches):
        missing.append('部分TEMU同款规格价格带未取得')
    if any(card['price']['observed_price'] is None for card in matches):
        missing.append('部分TEMU同款价格未取得')
    if any(card['sales']['text'] is None for card in matches):
        missing.append('部分TEMU同款销量未取得')
    confirmed = match.get('confirmed', 0)
    similar = match.get('similar', 0)
    if len(matches) != confirmed + similar:
        missing.append('部分同款商品不在当前采集快照中')
    demand = [c['sales']['lower'] for c in matches if c['sales']['lower'] is not None]
    demand = max(demand) if demand else None
    if match.get('state') == 'UNKNOWN' or match.get('pending') or match.get('possible'):
        conclusion = '待完成同款核验'
        confidence = '未知'
    elif not matches:
        conclusion = '采样内未发现同款，补充市场需求证据'
        confidence = '中'
    elif demand is None or demand <= 0:
        conclusion = '同款较少，待核实需求'
        confidence = '中'
    elif any(c['rating'] is not None and c['rating'] < 3.5 for c in matches):
        conclusion = '有销量信号，优先排查质量与差评'
        confidence = '中'
    elif missing:
        conclusion = '有选品线索，补齐数据后核算成本'
        confidence = '中'
    else:
        conclusion = '进入成本核算与供应链询价'
        confidence = '高'
    demand_text = '；'.join(f"{c['id']}：{c['sales']['text']}（{c['sales']['basis']}）" for c in matches if c['sales']['text']) or '当前未取得同款销量'
    prices = [c['price']['observed_price'] for c in matches if c['price']['observed_price'] is not None]
    amazon_price = amazon['price']['observed_price']
    spread = None
    if prices and amazon_price is not None:
        spread = round(amazon_price - min(prices), 2)
    reasons = [f'本次采样确认同款 {confirmed} 件、类似款 {similar} 件，合计 {confirmed+similar} 件；待分析 {match.get("pending", 0)} 对，待补证 {match.get("possible", 0)} 对',
               '需求证据：' + demand_text]
    if spread is not None:
        reasons.append(f'Amazon当前价减已采集TEMU同款最低当前价为 ${spread:.2f}；该页面价差未扣除采购、运费、平台费与退货成本')
    for field, label in [('growth', '新品增长'), ('concentration', '销量集中度')]:
        metric = market.get(field) or {}
        if metric:
            reasons.append(label + '：' + metric.get('label', '证据不足') + '；' + metric.get('reason', ''))
    if missing:
        reasons.append('数据缺口：' + '、'.join(dict.fromkeys(missing)))
    reasons.append('完成询价和履约成本核算前不作利润结论')
    return {'conclusion': conclusion, 'confidence': confidence, 'reason': '。'.join(reasons),
            'price_spread': spread, 'missing': list(dict.fromkeys(missing))}


def build_delivery(root, category, report):
    root = Path(root)
    base = root / 'runs' / category['category_key']
    limit = settings(category.get('commercial'))['same_limit']
    source = read_json(base / 'amazon/amazon_frontend_latest.json')
    amazon = {str(r['asin']): r for r in source.get('records', [])}
    # Strictly less than N, including historical runs whose older operator was <=.
    selected = [p for p in report.get('products', []) if p['match']['confirmed']+p['match'].get('similar',0) < limit]
    needed = {str(g) for p in selected for g in p.get('confirmed_goods_ids', [])+p.get('similar_goods_ids',[])}
    temu = {str(r['goods_id']): r for r in jsonl(base / 'temu/union/products.jsonl') if str(r['goods_id']) in needed}
    products, pairs = [], []
    for entry in selected:
        asin = str(entry['asin'])
        if asin not in amazon:
            raise ValueError('选品报告引用的Amazon商品缺少当前来源记录')
        raw = amazon[asin]
        card = product_card(raw, 'amazon', snapshot_metadata=source)
        same_ids={str(g) for g in entry.get('confirmed_goods_ids', [])}
        similar_ids={str(g) for g in entry.get('similar_goods_ids', [])}
        if same_ids & similar_ids:raise ValueError('同一商品同时记为同款和类似款，拒绝发布')
        gids = sorted(same_ids | similar_ids)
        rivals = [product_card(temu[g], 'temu') for g in gids if g in temu]
        advice = recommendation(card, rivals, entry['match'], report.get('market') or {})
        item = {'asin': asin, 'amazon': card, 'raw': raw, 'match': entry['match'],
                'same_count': len(gids), 'confirmed_count':len(same_ids),'similar_count':len(similar_ids),
                'advice': advice, 'temu_goods_ids': gids}
        if len(same_ids)!=entry['match']['confirmed'] or len(similar_ids)!=entry['match'].get('similar',0):
            raise ValueError('同款或类似款数量与去重商品ID不一致，拒绝发布')
        products.append(item)
        evidence = {str(p['goods_id']): p for p in entry.get('confirmed_pairs', [])+entry.get('similar_pairs',[])}
        for gid in gids:
            pairs.append({'asin': asin, 'goods_id': gid, 'amazon': card,
                          'temu': product_card(temu[gid], 'temu') if gid in temu else None,
                          'amazon_raw': raw, 'temu_raw': temu.get(gid), 'same_count': len(gids),
                          'relation':'相似款' if gid in similar_ids else '确认同款',
                          'advice': advice, 'evidence': evidence.get(gid, {})})
    return {'products': products, 'pairs': pairs, 'limit': limit,
            'category_name': category['category_name_zh'], 'as_of': report.get('as_of'),
            'amazon_count': len(amazon), 'excluded': len(amazon)-len(products)}
