"""Export only selected Amazon products and their confirmed TEMU counterparts."""
from __future__ import annotations

import json
from pathlib import Path

from selection_automation.selection_delivery import build_delivery


def export(root, category, report):
    from export_category_workbooks import _formats, _publish_formatted, _write_value
    data = build_delivery(root, category, report)
    name = data['category_name']
    destination = Path(root)/'runs'/category['category_key']/'excel'/f'{name}_选品结果.xlsx'
    note = (f"按首页条件：当前采样确认同款数严格小于 {data['limit']}；只展示入选Amazon及已确认同款。"
            '待分析不是零同款；销量保留页面口径；价格带只包含实际取得的规格价格，单个当前价不冒充完整价格带。')
    summary_headers = ['Amazon ASIN', 'Amazon标题', '确认同款数', '待分析对数', '待补证对数',
        'Amazon当前价(USD)', 'Amazon最低价(USD)', 'Amazon最高价(USD)', 'Amazon价格带口径',
        'Amazon评分', 'Amazon评论数', 'Amazon销量原文', 'Amazon销量口径',
        'Amazon榜单排名', '选品建议', '结论置信度', '综合分析依据', '数据缺口',
        'Amazon商品链接', 'Amazon主图链接', 'Amazon采集时间', '分析时间', '同款数筛选门槛']
    product_headers = ['标题', '商品链接', '主图链接', '当前价(USD)', '最低价(USD)', '最高价(USD)',
                       '价格带口径', '评分', '评论数', '销量原文', '销量口径', '真实上架时间', '上架时间来源', '采集时间', '采集来源']
    pair_headers = ['Amazon ASIN', 'TEMU商品ID', 'Amazon确认同款数'] + ['Amazon'+h for h in product_headers] + ['TEMU'+h for h in product_headers] + [
        '同款判断', '判断依据', '视觉证据', '分析模型', '同款决策概率', '辅助评分', '商业选品建议', '综合分析依据']

    def fields(card):
        if card is None:
            return ['当前采集快照缺失'] + [None]*(len(product_headers)-1)
        price, sales = card['price'], card['sales']
        return [card['title'], card['url'], card['image'], price['observed_price'], price['min'], price['max'],
                price['basis'], card['rating'], card['review_count'], sales['text'] or '未取得', sales['basis'],
                card['listed_at'], card['listed_at_source'], card['observed_at'], card['source'] or '未取得来源字段']

    def build(book):
        f = _formats(book)
        def sheet(title, headers):
            ws = book.add_worksheet(title)
            ws.hide_gridlines(2)
            ws.merge_range(0, 0, 0, len(headers)-1, name+'｜'+title, f['title'])
            ws.merge_range(1, 0, 1, len(headers)-1, note, f['note'])
            ws.set_row(0, 28); ws.set_row(1, 38); ws.set_row(3, 38)
            ws.write_row(3, 0, headers, f['header']); ws.freeze_panes(4, 1)
            for col, header in enumerate(headers):
                ws.set_column(col, col, 62 if '依据' in header or '证据' in header else 42 if '标题' in header else 28 if '口径' in header or '建议' in header else 20)
            return ws

        def emit(ws, index, headers, values):
            for col, (header, value) in enumerate(zip(headers, values)):
                style = f['money'] if '(USD)' in header else f['text']
                _write_value(ws, index, col, value if value is not None else '未取得', style, f, header, native_links=True)
            ws.set_row(index, 78)

        summary = sheet('选品结论', summary_headers)
        for index, item in enumerate(data['products'], 4):
            card, match, advice = item['amazon'], item['match'], item['advice']
            price, sales = card['price'], card['sales']
            emit(summary, index, summary_headers, [item['asin'], card['title'], item['same_count'], match.get('pending', 0),
                match.get('possible', 0), price['observed_price'], price['min'], price['max'], price['basis'], card['rating'],
                card['review_count'], sales['text'] or '未取得', sales['basis'], item['raw'].get('rank'), advice['conclusion'],
                advice['confidence'], advice['reason'], '；'.join(advice['missing']) or '当前关键字段已取得', card['url'], card['image'],
                card['observed_at'], data['as_of'], f"严格小于 {data['limit']}"])
        summary.autofilter(3, 0, max(3, len(data['products'])+3), len(summary_headers)-1)

        detail = sheet('同款商品明细', pair_headers)
        for index, item in enumerate(data['pairs'], 4):
            evidence, advice = item['evidence'], item['advice']
            emit(detail, index, pair_headers, [item['asin'], item['goods_id'], item['same_count'], *fields(item['amazon']), *fields(item['temu']),
                evidence.get('state', '确认同款'), evidence.get('reason', '已通过当前版本第三层核验'), evidence.get('evidence'),
                evidence.get('model'), evidence.get('probability'), evidence.get('score'), advice['conclusion'], advice['reason']])
        detail.autofilter(3, 0, max(3, len(data['pairs'])+3), len(pair_headers)-1)

        # Preserve every collected field once per selected product, never every Cartesian pair.
        extra_headers = ['平台', '商品ID', '采集字段', '已采集内容', '内容分段', '商品来源链接']
        extra = sheet('商品完整字段', extra_headers)
        raw_products = {('Amazon', item['asin']): item['raw'] for item in data['products']}
        raw_products.update({('TEMU', p['goods_id']): p['temu_raw'] for p in data['pairs'] if p['temu_raw'] is not None})
        labels = {'title': '标题', 'specifications': '规格', 'variants': '变体', 'skus': 'SKU规格', 'brand': '品牌',
                  'seller': '卖家', 'availability': '库存提示', 'best_sellers_rank': '详情页榜单排名', 'review_count': '评论数',
                  'rating': '评分', 'price_usd': '当前价格', 'price_range': '已采集规格价格带', 'negative_reviews': '真实差评',
                  'sales_text': '销量原文', 'monthly_sales_text': '页面月销量原文', 'observed_at': '采集时间'}
        count = 0
        for (site, identity), raw in raw_products.items():
            for key, value in raw.items():
                if key.lower() in {'api_key', 'password', 'access_token', 'proxy_password'}:
                    continue
                text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value) if value is not None else '未取得'
                chunks = [text[i:i+32000] for i in range(0, len(text), 32000)] or ['']
                for part, chunk in enumerate(chunks, 1):
                    emit(extra, count+4, extra_headers, [site, identity, labels.get(key, key), chunk,
                         f'{part}/{len(chunks)}', raw.get('product_url')]); count += 1
        extra.set_column(3, 3, 90)
        extra.autofilter(3, 0, max(3, count+3), len(extra_headers)-1)
        return '选品结论', 4, len(data['products'])
    delivered = _publish_formatted(destination, build)
    return {**delivered, '交付格式': '精简选品结果', '入选Amazon数': len(data['products']),
            '确认同款对数': len(data['pairs']), '同款数门槛': data['limit']}
