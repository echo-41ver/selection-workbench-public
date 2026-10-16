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
    'negative_reviews':'已采集差评','shipping':'配送信息','description':'商品描述','bullet_points':'商品卖点',
    'detail_price_text':'详情页价格','listing_price_text':'榜单页价格','detail_price_range_text':'详情页规格价格带',
    'currency':'币种','source':'采集来源','source_queries':'采集关键词','search_query':'当前采集关键词',
    'search_sort':'搜索排序','site':'站点','source_url':'来源页面','is_sponsored':'是否广告商品',
    'sponsorship_observed_at':'广告标记采集时间','long_detail':'补充说明','material':'材质','color':'颜色',
    'size':'尺寸','dimensions':'外形尺寸','weight':'重量','package_contents':'包装内容','warranty':'保修信息',
    'country_of_origin':'原产地','return_policy':'退货政策','coupon':'优惠信息',
    'asin':'Amazon ASIN','goods_id':'TEMU商品ID','title':'商品标题','product_url':'商品链接','main_image_url':'主图链接'}
PARAMETER_NAMES.update(sales_text='页面销量',monthly_sales_text='近一月页面销量',sales_count='页面销量数值')

# Collector diagnostics remain in the source snapshots, rather than product columns.
INTERNAL_PARAMETERS={'raw_record','raw_card_text','card_text','card_rect','card_metrics','listing_image_url',
    'image_source','image_status','detail_image_attempts','detail_status','detail_error','image_candidates',
    'image_width_hint','image_height_hint','detail_quality','merge_policy','provider_evidence','category_relevance',
    'sales_precision','review_count_precision','metric_evidence','thumbnail_url','all_image_urls',
    'card_metrics_version','card_ancestor_depth','first_seen_round','first_seen_ordinal','primary_observation',
    'observation_sources','field_conflicts','observed_image_urls','sponsorship_observation','local_image_path'}

SPECIFICATION_NAMES=dict(zip(
    ['Additional Features','Antenna Location','Batteries','Best Sellers Rank','Brand','Built-In Media',
     'Button Quantity','Color','Compatible Amazon Tablet Models','Compatible Devices','Compatible Operating System Family',
     'Compatible Tablet Computer Models','Connectivity Technology','Customer Reviews','Date First Available',
     'EU Spare Part Availability Duration','Enclosure Material','External Testing Certification','Generation',
     'Global Trade Identification Number','Hand Orientation','Item Dimensions L x W x H','Item Highlight',
     'Item Type Name','Item Weight','Item model number','Keyboard Backlighting Color Support','Keyboard Description',
     'Keyboard Layout','Keyboard Plate Material','Language','Manufacturer','Mechanical Keyboard Switch Model',
     'Mfr Part Number','Minimum Required Operating System Version','Model Name','Model Number','Number of Batteries Required',
     'Number of Items','Number of Keys','Number of Packs','Number of Sections','Power Source','Processor Count',
     'Product Dimensions','Series Number','Style Name','Switch Type','Theme','UPC','Unit Count','Warranty Description',
     'Water Resistance Level','Material','Size','Weight','Dimensions'],
    ['附加功能','天线位置','电池信息','畅销榜排名','品牌','包装内含配件','按键数量','颜色','兼容亚马逊平板型号',
     '兼容设备','兼容操作系统','兼容平板型号','连接方式','用户评价','首次上架日期','欧盟备件供应期限',
     '外壳材质','检测认证','产品代数','全球贸易商品编号','适用手型','外形尺寸（长×宽×高）','商品特点',
     '商品类型','商品重量','商品型号','键盘背光颜色','键盘类型','键盘布局','键盘定位板材质','语言','制造商',
     '机械键盘轴体型号','制造商部件编号','最低操作系统版本','产品名称','型号','所需电池数量','商品数量',
     '按键数','包装数量','分区数量','供电方式','处理器数量','产品尺寸','系列编号','款式','开关类型',
     '主题','通用商品条码','计量数量','保修说明','防水等级','材质','尺寸','重量','外形尺寸']))

DISPLAY_VALUES={'true':'是','false':'否','available':'可用','missing':'未取得','unknown':'未知',
    'complete':'已完成','unavailable':'不可用','usd':'美元','amazon_frontend':'Amazon网页采集',
    'temu_frontend':'TEMU网页采集','temu-frontend':'TEMU网页采集','frontend':'网页采集',
    'sales':'销量排序','recent':'最近上架排序','relevance':'综合排序','us':'美国站',
    'black':'黑色','white':'白色','grey':'灰色','gray':'灰色','graphite':'石墨色','blue':'蓝色','red':'红色',
    'english':'英语','wireless':'无线','wired':'有线','corded electric':'有线供电','battery powered':'电池供电',
    'membrane':'薄膜式','ambidextrous':'左右手通用','keyboard':'键盘','keyboard with numeric keys':'带数字区键盘',
    'combo':'组合套装','see available options':'查看可选规格','usb cable':'USB线','multimedia keyboard':'多媒体键盘',
    'pc':'电脑','office':'办公','accessible':'无障碍型','plastic':'塑料','cotton':'棉',
    'label':'选项','value':'内容','min':'最低价','max':'最高价','currency':'币种','basis':'口径',
    'rating':'评分','text':'内容','source_url':'来源链接','asin':'Amazon ASIN','goods_id':'TEMU商品ID',
    'name':'名称','url':'链接','link':'链接','count':'数量','type':'类型','status':'状态','source':'来源',
    'description':'说明','price':'价格','id':'编号','title':'标题'}
DISPLAY_VALUES.update(visible_temu_frontend_keyword_union='TEMU网页关键词去重汇总',
    visible_temu_frontend='TEMU网页采集',
    geekbi_supplement='极鲸云补充数据',geekbi='极鲸云',temu_us='TEMU美国站',
    ui_confirmed='页面已确认',**{'页面显��销量，周期未知':'页面显示销量，周期未知'})
DISPLAY_VALUES.update({'sort by: most recent':'最近上架排序','sort by: top sales':'销量最高排序'})
DISPLAY_VALUES.update({'in stock':'有货','out of stock':'暂无库存','currently unavailable':'暂不可售',
    'not water resistant':'不防水','laptop':'笔记本电脑','single color':'单色','linear':'线性轴',
    'bluetooth':'蓝牙'})


def display_text(value,*,translate=True):
    """Readable labels and observed values, never translate brands or model IDs."""
    import html,re
    text=html.unescape(str(value or ''))
    text=re.split(r'/\*|<style\b|<script\b',text,flags=re.I)[0]
    text=re.split(r'\b(?:var|let|const)\s+[A-Za-z_$][\w$]*\s*(?:[;=])|\bP\.when\s*\(|\(function\s*\(',text)[0]
    text=re.split(r'\{\s*"(?:isInternal|isRobot|showInsightsHub|schema_version|merchantId)"\s*:',text)[0]
    text=re.sub(r'<[^>]+>',' ',text)
    text=re.sub(r'\s+',' ',text).strip()
    if re.search(r'(?i)(?:[a-z]:\\|\\\\[^\\]+\\)',text):return '内部采集记录'
    if not translate:return text
    if text.lower() in DISPLAY_VALUES:return DISPLAY_VALUES[text.lower()]
    review=re.fullmatch(r'(?:[\d.]+\s+)?([\d.]+)\s+out of\s+([\d.]+)\s+stars\s*\(([\d,]+)\)',text,re.I)
    if review:return f'评分：{review[1]}（满分{review[2]}星），{review[3]}条评价'
    quantity=r'([\d,.]+)\s*([KM]?)\s*(\+?)'
    match=re.fullmatch(quantity+r'\s*(bought in past month|sold|sales)',text,re.I)
    if match:
        from decimal import Decimal,InvalidOperation
        try:
            amount=Decimal(match[1].replace(',',''))*{'':1,'K':1000,'M':1000000}[match[2].upper()]
            number=f'{int(amount):,}' if amount==amount.to_integral_value() else format(amount,',f').rstrip('0').rstrip('.')
            return ('近一月购买：' if match[4].lower()=='bought in past month' else '页面已售：')+number+match[3]+' 件'
        except InvalidOperation:pass
    pack=re.fullmatch(r'(?:(Combo)\s*\(Pack of\s*)?(\d+)\s*(?:pack)?\)?',text,re.I)
    if pack and ('pack' in text.lower()):return ('组合套装，' if pack[1] else '')+pack[2]+'件装'
    return SPECIFICATION_NAMES.get(text,text)


def display_parameter(value):
    if value is None:return None
    if isinstance(value,bool):return '是' if value else '否'
    if isinstance(value,(int,float)):return value
    if isinstance(value,list):
        parts=[display_parameter(item) for item in value]
        return '\n'.join(dict.fromkeys(str(part) for part in parts if part not in (None,'')))
    if isinstance(value,dict):
        if 'label' in value and 'value' in value:
            label=display_text(value['label'])
            content=display_text(value['value'],translate=False) if label in ('品牌','制造商','产品名称','型号','商品型号') else display_parameter(value['value'])
            return label+'：'+str(content if content is not None else '')
        if 'label' in value and 'asin' in value:
            label=display_text(value['label'])
            if label in ('','←','→') or label.isdigit():return ''
            return label+'：'+str(value['asin'])
        return '\n'.join(display_text(PARAMETER_NAMES.get(str(key),str(key)))+'：'+str(display_parameter(item))
            for key,item in value.items() if item is not None and item not in ('',[],{})
            and not any(word in str(key).lower() for word in ('token','password','cookie','secret','api_key')))
    return display_text(value)


def collected_parameters(raw,*,include_core=False):
    excluded={'asin','goods_id','title','main_image_url','image_url','local_image_path','product_url',
        'api_key','password','access_token','proxy_password','cookies','headers','raw_record','card_rect','card_metrics'}
    if include_core:excluded-=set(('asin','goods_id','title','main_image_url','product_url'))
    result={}
    for key,value in (raw or {}).items():
        if key in excluded or key in INTERNAL_PARAMETERS or value is None or value in ('',[],{}):continue
        if any(word in key.lower() for word in ('token','password','cookie','secret','api_key')):continue
        label=PARAMETER_NAMES.get(key)
        if label is None:
            import re
            if not re.search(r'[\u4e00-\u9fff]',key):continue
            label=key
        if key=='search_sort' and isinstance(value,dict):
            requested=display_text(value.get('requested','unknown'))
            formatted=requested+('（页面已确认）' if value.get('state')=='UI_CONFIRMED' else '（尚未确认）')
        else:
            formatted=display_text(value,translate=False) if key in ('brand','seller','title') and isinstance(value,str) else display_parameter(value)
        if formatted not in (None,''):result[label]=formatted
    return result


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
