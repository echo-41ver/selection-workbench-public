"""Compact matching selection with embedded pictures and complete collected fields."""
from __future__ import annotations
import json
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from selection_automation.selection_delivery import build_delivery
from selection_automation.commercial import jsonl
from selection_workflow.excel_images import cached_thumbnail


def export(root, category, report):
    from export_category_workbooks import _formats, _publish_formatted, _write_value
    data=build_delivery(root,category,report)
    name=data['category_name'];base=Path(root)/'runs'/category['category_key']
    destination=base/'excel'/f'{name}_选品结果.xlsx'
    note=(f"最终同款与类似款合计严格小于 {data['limit']}；每个商品对一行，同一商品ID只计一次。"
          '待核验不是零竞争；图片直接嵌入。销量与规格价格带均保留实际采集口径。')
    prefix=['Amazon ASIN','Amazon标题','Amazon图片','TEMU图片','同款+类似款数量','Amazon商品链接','TEMU商品链接']
    summary_headers=prefix+['同款数','类似款数','TEMU商品ID','最终关系','待分析对数','待补证对数',
        'Amazon当前价(USD)','Amazon最低价(USD)','Amazon最高价(USD)','Amazon价格带口径',
        'Amazon评分','Amazon评论数','Amazon销量原文','Amazon销量口径','Amazon榜单排名',
        '选品建议','结论置信度','综合分析依据','数据缺口','Amazon采集时间','分析时间','数量筛选门槛']
    parameter_headers=['当前价(USD)','最低价(USD)','最高价(USD)','价格带口径','评分','评论数',
        '销量原文','销量口径','真实上架时间','上架时间来源','采集时间','采集来源']
    pair_headers=prefix+['TEMU商品ID','TEMU标题']+['Amazon'+h for h in parameter_headers]+['TEMU'+h for h in parameter_headers]+[
        '最终关系','判断依据','视觉证据','分析模型','同款决策概率','辅助评分','商业选品建议','综合分析依据']
    pairs_by_asin=defaultdict(list)
    for pair in data['pairs']:pairs_by_asin[pair['asin']].append(pair)
    summary_rows=[(item,pair) for item in data['products'] for pair in (pairs_by_asin[item['asin']] or [None])]
    selected_asins={item['asin'] for item in data['products']}
    needed_goods={pair['goods_id'] for pair in data['pairs']}
    local_images={}
    for row in jsonl(base/f"stage123/export_frozen/layer3_candidates_{category['category_key']}.jsonl"):
        for side,id_key,wanted in [('amazon','asin',selected_asins),('temu','goods_id',needed_goods)]:
            identity=str(row.get(id_key) or '')
            path=row.get(side+'_image_path')
            if identity in wanted and path and Path(path).is_file():local_images.setdefault((side,identity),path)
    assets={('amazon',item['asin']):(item['amazon'],item['raw']) for item in data['products']}
    assets.update({('temu',pair['goods_id']):(pair['temu'],pair['temu_raw']) for pair in data['pairs'] if pair['temu_raw'] is not None})
    def thumbnail(entry):
        identity,(card,raw)=entry
        return identity,cached_thumbnail({'asin':identity[1],'main_image_url':card['image'],
            'local_image_path':local_images.get(identity) or raw.get('local_image_path')},base/'excel'/'embedded_image_cache')
    with ThreadPoolExecutor(max_workers=4) as pool:images=dict(pool.map(thumbnail,assets.items()))
    def fields(card):
        if card is None:return ['未取得']*len(parameter_headers)
        price,sales=card['price'],card['sales']
        return [price['observed_price'],price['min'],price['max'],price['basis'],card['rating'],card['review_count'],
                sales['text'] or '未取得',sales['basis'],card['listed_at'],card['listed_at_source'],card['observed_at'],card['source']]
    def first_values(asin,amazon,temu,total):
        return [asin,amazon['title'],'','',total,amazon['url'],temu['url'] if temu else '尚无最终同款或类似款']
    def build(book):
        f=_formats(book)
        def sheet(title,headers):
            ws=book.add_worksheet(title);ws.hide_gridlines(2)
            ws.merge_range(0,0,0,len(headers)-1,name+'｜'+title,f['title'])
            ws.merge_range(1,0,1,len(headers)-1,note,f['note'])
            ws.set_row(0,28);ws.set_row(1,38);ws.set_row(3,38)
            ws.write_row(3,0,headers,f['header']);ws.freeze_panes(4,1)
            for col,header in enumerate(headers):
                width=62 if '依据' in header or '证据' in header else 42 if '标题' in header else 28 if '口径' in header or '建议' in header else 24 if header in ('Amazon图片','TEMU图片') else 20
                ws.set_column(col,col,width)
            return ws
        def emit(ws,index,headers,values):
            for col,(header,value) in enumerate(zip(headers,values)):
                style=f['money'] if '(USD)' in header else f['text']
                _write_value(ws,index,col,value if value is not None else '未取得',style,f,header,native_links=True)
            ws.set_row(index,100)
        def pictures(ws,index,asin,gid):
            for side,identity,col in [('amazon',asin,2),('temu',gid,3)]:
                path=images.get((side,identity)) if identity else None
                if path:
                    with Image.open(path) as image:scale=min(120/image.width,120/image.height)
                    ws.insert_image(index,col,str(path),{'x_scale':scale,'y_scale':scale,'x_offset':4,'y_offset':4,'object_position':1})
                else:ws.write(index,col,'尚无最终匹配' if side=='temu' and not gid else '图片未取得',f['text'])
        summary=sheet('选品结论',summary_headers)
        for index,(item,pair) in enumerate(summary_rows,4):
            card,match,advice=item['amazon'],item['match'],item['advice'];price,sales=card['price'],card['sales']
            temu=pair['temu'] if pair else None
            emit(summary,index,summary_headers,[*first_values(item['asin'],card,temu,item['same_count']),
                item['confirmed_count'],item['similar_count'],pair['goods_id'] if pair else '',pair['relation'] if pair else advice['conclusion'],
                match.get('pending',0),match.get('possible',0),price['observed_price'],price['min'],price['max'],price['basis'],
                card['rating'],card['review_count'],sales['text'] or '未取得',sales['basis'],item['raw'].get('rank'),advice['conclusion'],
                advice['confidence'],advice['reason'],'；'.join(advice['missing']) or '当前关键字段已取得',card['observed_at'],data['as_of'],f"合计严格小于 {data['limit']}"])
            pictures(summary,index,item['asin'],pair['goods_id'] if pair else None)
        summary.autofilter(3,0,max(3,len(summary_rows)+3),len(summary_headers)-1)
        detail=sheet('同款商品明细',pair_headers)
        for index,item in enumerate(data['pairs'],4):
            evidence,advice=item['evidence'],item['advice']
            emit(detail,index,pair_headers,[*first_values(item['asin'],item['amazon'],item['temu'],item['same_count']),
                item['goods_id'],item['temu']['title'] if item['temu'] else '未取得',*fields(item['amazon']),*fields(item['temu']),
                item['relation'],evidence.get('reason','已通过当前版本最终关系判断'),evidence.get('evidence'),evidence.get('model'),
                evidence.get('probability'),evidence.get('score'),advice['conclusion'],advice['reason']])
            pictures(detail,index,item['asin'],item['goods_id'])
        detail.autofilter(3,0,max(3,len(data['pairs'])+3),len(pair_headers)-1)
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
        return '选品结论', 4, len(summary_rows)
    delivered = _publish_formatted(destination, build)
    return {**delivered, '交付格式': '精简选品结果', '入选Amazon数': len(data['products']),
            '确认同款对数':sum(pair['relation']=='确认同款' for pair in data['pairs']), '类似款对数':sum(pair['relation']=='相似款' for pair in data['pairs']), '同款数门槛': data['limit']}
