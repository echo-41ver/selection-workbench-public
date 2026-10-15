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


def conclusion_label(value):
    text=str(value or '').strip()
    if text in {'DIFFERENT','NOT_SAME'} or text.startswith('不同款'):return '不同款'
    if text in {'SIMILAR','相似款','类似款'} or text.startswith(('相似款（','类似款（')):return '类似款'
    if text in {'SAME','CONFIRMED_SAME','确认同款','同款'} or text.startswith(('确认同款（','同款（')):return '同款'
    if text in {'UNKNOWN','无法判断','待补充证据','待补证','重点复核'} or text.startswith(('待补充证据','重点复核')):return '无法判断'
    return '未分析'


PARAMETER_NAMES={'brand':'品牌','seller':'卖家','availability':'库存提示','specifications':'规格参数',
    'variants':'已采集变体','skus':'已采集SKU','sku':'SKU','best_sellers_rank':'详情页榜单排名',
    'rank':'榜单排名','price_usd':'当前价格(USD)','price_range':'已采集规格价格带','rating':'评分',
    'review_count':'评论数','review_count_text':'评论数原文','sales_text':'销量原文','sales_count':'页面销量',
    'monthly_sales_text':'页面月销量原文','sales_basis':'销量口径','sales_precision':'销量精度',
    'listed_at':'上架时间','listed_at_source':'上架时间来源','observed_at':'采集时间',
    'negative_reviews':'已采集差评','shipping':'配送信息','description':'商品描述','bullet_points':'商品卖点'}


def collected_parameters(raw):
    import json
    excluded={'asin','goods_id','title','main_image_url','image_url','local_image_path','product_url',
        'api_key','password','access_token','proxy_password','cookies','headers','raw_record','card_rect','card_metrics'}
    return {PARAMETER_NAMES.get(key,key):json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else value
            for key,value in (raw or {}).items() if key not in excluded and value is not None and value not in ('',[],{})
            and not any(word in key.lower() for word in ('token','password','cookie','secret'))}


def short_reason(value,limit=140):
    import re
    text=re.sub(r'\s+',' ',str(value or '')).strip()
    return text if len(text)<=limit else text[:limit-1]+'…'


def build_delivery(root, category, report):
    root = Path(root)
    base = root / 'runs' / category['category_key']
    limit = settings(category.get('commercial'))['same_limit']
    source = read_json(base / 'amazon/amazon_frontend_latest.json')
    amazon = {str(r['asin']): r for r in source.get('records', [])}
    # Strictly less than N, including historical runs whose older operator was <=.
    selected = [p for p in report.get('products', []) if p['match']['confirmed']+p['match'].get('similar',0) < limit]
    needed = {str(g) for p in report.get('products',[]) for g in p.get('confirmed_goods_ids', [])+p.get('similar_goods_ids',[])}
    temu = {str(r['goods_id']): r for r in jsonl(base / 'temu/union/products.jsonl') if str(r['goods_id']) in needed}
    products, pairs = [], []
    selected_ids={str(entry['asin']) for entry in selected}
    for entry in report.get('products',[]):
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
        if asin in selected_ids:products.append(item)
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
