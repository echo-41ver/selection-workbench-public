"""Evidence-backed operator shortlist and idempotent local daily delivery."""
from datetime import datetime,timedelta,timezone
import json,math,re
from pathlib import Path
from .commercial import instant,read_json,jsonl,report_signature
from .merge_temu_snapshots import atomic_write
from .process_lock import ProcessLock

BEIJING=timezone(timedelta(hours=8))


def operator_id(label):
    import hashlib
    return 'operator_'+hashlib.sha256(label.encode('utf-8')).hexdigest()[:16]


def number(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:return None
    return value


def price_range(row):
    """One selected price is never relabeled as the minimum/maximum of every SKU."""
    observed=number(row.get('price_usd'))
    declared=row.get('price_range')
    if isinstance(declared,dict) and declared.get('currency')=='USD':
        lo,hi=number(declared.get('min')),number(declared.get('max'))
        if lo is not None and hi is not None and 0<lo<=hi:
            return {'min':lo,'max':hi,'currency':'USD','basis':declared.get('basis') or '页面显示价格区间','observed_price':observed}
    for field in ('detail_price_range_text','listing_price_text'):
        text=str(row.get(field) or '').strip()
        match=re.fullmatch(r'(?:US\s*\$|USD\s*|\$)\s*([\d,]+(?:\.\d{1,2})?)\s*[-–—至]\s*(?:US\s*\$|USD\s*|\$)?\s*([\d,]+(?:\.\d{1,2})?)',text,re.I)
        if match:
            lo,hi=(float(v.replace(',','')) for v in match.groups())
            if 0<lo<=hi:return {'min':lo,'max':hi,'currency':'USD','basis':'页面明确显示区间','observed_price':observed}
    skus=row.get('skus') or row.get('variants') or []
    if isinstance(skus,list):
        prices=[number(s.get('price_usd')) for s in skus if isinstance(s,dict) and s.get('available',True) is not False]
        valid=[p for p in prices if p is not None and p>0]
        if valid:
            complete=row.get('all_variants_priced') is True and len(valid)==len(prices)
            return {'min':min(valid),'max':max(valid),'currency':'USD',
                    'basis':'全部已核验可售规格' if complete else '已采集有价格的可售规格子集；其余规格价格未取得',
                    'observed_price':observed}
    return {'min':None,'max':None,'currency':'USD','basis':'未取得规格价格区间','observed_price':observed}


def sales(row,site):
    text=str(row.get('sales_text') or row.get('monthly_sales_text') or '').strip()
    value=number(row.get('sales_count'))
    precision=row.get('sales_precision')
    if value is not None and (not isinstance(value,float) or value.is_integer()):
        if row.get('sales_basis') and precision in {'exact','displayed','displayed_lower_bound'}:
            return {'text':text,'lower':value,'basis':row['sales_basis'],'precision':precision}
        if row.get('sales_basis') and precision=='displayed_approximate':
            return {'text':text,'lower':None,'displayed_value':value,
                    'basis':row['sales_basis']+'；缩写近似值，不视为销量下限','precision':precision}
    quantity=r'([\d,]+(?:\.\d+)?)\s*([KM万萬千]?)\s*(\+?)'
    match=(re.search(r'(?:已售|售出|销量|銷量)\s*[:：]?\s*'+quantity,text,re.I)
           or re.search(quantity+r'\s*(?:sold|sales?|bought in past month|(?:件|个|個)?\s*(?:已售|售出))',text,re.I))
    if match:
        if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?',match[1]):
            return {'text':text or None,'lower':None,'basis':'未取得可靠销量口径','precision':'unknown'}
        value=float(match[1].replace(',',''))*{'':1,'K':1000,'M':1000000,'万':10000,'萬':10000,'千':1000}[match[2].upper()]
        basis='最近一月页面显示' if 'past month' in text.lower() else '页面显示销量，周期未知'
        if value.is_integer():
            if match[2] and not match[3]:
                return {'text':text,'lower':None,'displayed_value':value,'basis':basis+'；缩写近似值，不视为销量下限',
                        'precision':'displayed_approximate'}
            return {'text':text,'lower':value,'basis':basis,'precision':'displayed_lower_bound' if match[3] else 'displayed'}
    return {'text':text or None,'lower':None,'basis':'未取得可靠销量口径','precision':'unknown'}


def product_card(row,site,*,snapshot_metadata=None):
    negative=[]
    for review in row.get('negative_reviews') or []:
        if isinstance(review,dict) and review.get('text') and review.get('source_url') and number(review.get('rating')) is not None and review['rating']<=2:
            negative.append({'text':str(review['text'])[:1000],'source_url':review['source_url'],'rating':review['rating']})
    rating=number(row.get('rating'))
    observed_at=row.get('observed_at');observed_at_source='observed_at' if observed_at else None
    if not observed_at and isinstance(row.get('primary_observation'),dict):
        observed_at=row['primary_observation'].get('observed_at')
        observed_at_source='primary_observation.observed_at' if observed_at else None
    snapshot_metadata=snapshot_metadata or {}
    if not observed_at and snapshot_metadata.get('observed_at'):
        observed_at=snapshot_metadata['observed_at'];observed_at_source='snapshot.observed_at'
    source=row.get('source') or snapshot_metadata.get('source') or snapshot_metadata.get('source_name')
    source_recorded_in='row.source' if row.get('source') else ('snapshot' if source else None)
    card={'id':str(row.get('asin') if site=='amazon' else row.get('goods_id')),
            'title':row.get('title'),'url':row.get('product_url'),'image':row.get('main_image_url'),
            'price':price_range(row),'sales':sales(row,site),'rating':rating if rating is not None and rating<=5 else None,
            'review_count':row.get('review_count') if row.get('review_count') is not None else row.get('review_count_text'),
            'listed_at':row.get('listed_at'),'listed_at_source':row.get('listed_at_source'),
            'negative_reviews':negative,'negative_review_status':'有原文与来源' if negative else '尚未采集真实差评',
            'site':site,'observed_at':observed_at,'observed_at_source':observed_at_source,
            'source':source,'source_status':'AVAILABLE' if source else 'MISSING','source_recorded_in':source_recorded_in}
    available={
        'url':bool(card['url']),'image':bool(card['image']),
        'price':card['price']['observed_price'] is not None or card['price']['min'] is not None,
        'price_range':card['price']['min'] is not None and card['price']['max'] is not None,
        'sales':card['sales']['lower'] is not None,'rating':card['rating'] is not None,
        'listed_at':bool(card['listed_at_source'] and instant(card['listed_at'])),
        'negative_reviews':bool(negative),'observed_at':instant(observed_at) is not None,'source':bool(source),
    }
    card['evidence']={name:{'status':'AVAILABLE' if present else 'MISSING',
        'label':'已取得' if present else '未取得','source':source,'source_url':card['url'],
        'observed_at':observed_at} for name,present in available.items()}
    card['evidence']['listed_at']['source']=card['listed_at_source']
    card['evidence']['negative_reviews']['source_urls']=[review['source_url'] for review in negative]
    card['evidence']['observed_at']['basis']=observed_at_source
    if observed_at_source=='snapshot.observed_at':
        card['evidence']['observed_at']['note']='快照记录时间，不代表所有商品字段分别复核的时间。'
    return card


def build_shortlist(root,category,*,same_limit=None,min_sales=100,new_days=90,require_new=True,now=None):
    root=Path(root);base=root/'runs'/category['category_key'];now=now or datetime.now(timezone.utc)
    report=read_json(base/'commercial_report.json')
    if not report or report.get('evidence_signature')!=report_signature(root,category):
        return {'status':'WAITING_CURRENT_ANALYSIS','outcome':'DATA_INCOMPLETE','evidence_complete':False,
                'category_key':category['category_key'],'source_run_key':category['category_key'],
                'products':[],'message':'等待当前采集与第三层核验的有效结果，暂不使用旧报告。'}
    limit=category.get('commercial',{}).get('same_limit',5) if same_limit is None else same_limit
    amazon_snapshot=read_json(base/'amazon/amazon_frontend_latest.json')
    amazon={str(r['asin']):r for r in amazon_snapshot.get('records',[])}
    temu={str(r['goods_id']):r for r in jsonl(base/'temu/union/products.jsonl')}
    # Validated commercial counts may include API decisions; reconcile these rather
    # than treating an unavailable decision file as a zero-match opportunity.
    selected=[];rejected={};rejected_by_outcome={'DATA_INCOMPLETE':{},'NO_MATCH':{}}
    def reject(reason,outcome):
        rejected[reason]=rejected.get(reason,0)+1
        bucket=rejected_by_outcome[outcome];bucket[reason]=bucket.get(reason,0)+1
    report_products=report.get('products',[])
    if not report_products:reject('尚无已核验的商品报告','DATA_INCOMPLETE')
    for asin in set(amazon)-{str(item.get('asin')) for item in report_products}:
        reject('Amazon商品尚无当前同款核验结果','DATA_INCOMPLETE')
    for item in report_products:
        match=item.get('match',{});asin=str(item['asin']);reason=None;failure='DATA_INCOMPLETE'
        confirmed=number(match.get('confirmed'));expected=number(match.get('expected'))
        count_values=[number(match.get(field)) for field in ('confirmed','expected','pending','possible')]
        if (any(value is None or int(value)!=value for value in count_values)
                or match.get('pending') or match.get('possible') or not expected or confirmed>expected):reason='同款核验未完成'
        elif confirmed>=limit:reason='同款数不满足严格少于N';failure='NO_MATCH'
        rivals=set(str(gid) for gid in item.get('confirmed_goods_ids',[]))
        if reason is None and len(rivals)!=confirmed:reason='已确认同款商品明细尚未齐全'
        cards=[product_card(temu[gid],'temu') for gid in rivals if gid in temu]
        if reason is None and len(cards)!=len(rivals):reason='同款商品当前快照缺失'
        if reason is None and asin not in amazon:reason='Amazon商品当前快照缺失'
        viable=[];criteria_unknown=False
        for card in cards:
            listed=instant(card['listed_at']) if card['listed_at_source'] else None
            age=(now-listed).total_seconds() if listed is not None else None
            known_old=age is not None and age>new_days*86400
            fresh=age is not None and 0<=age<=new_days*86400
            lower=card['sales']['lower']
            # An explicit failed criterion suffices; absent evidence never becomes zero.
            if (lower is not None and lower<min_sales) or (require_new and known_old):continue
            if lower is not None and lower>=min_sales and (fresh or not require_new):viable.append(card)
            else:criteria_unknown=True
        if reason is None and not viable:
            failure='DATA_INCOMPLETE' if criteria_unknown else 'NO_MATCH'
            reason='同款销量或真实上架时间证据未齐' if criteria_unknown else '已核验同款均未满足销量和新品条件'
        if reason:
            reject(reason,failure);continue
        best=max(viable,key=lambda c:(c['sales']['lower'],c['rating'] or 0))
        why=[f"当前采样确认{int(confirmed)}个同款，严格少于{limit}",
             f"所选TEMU同款页面销量{best['sales']['text'] or best['sales']['lower']}，达到{min_sales}门槛（{best['sales']['basis']}）"]
        if require_new:why.append(f"有来源的上架时间位于最近{new_days}天内")
        amazon_card=product_card(amazon[asin],'amazon',snapshot_metadata=amazon_snapshot)
        next_actions=['核对两侧相同规格和包装数量，再向供应商询价及核算成本。']
        if any(card['evidence']['price_range']['status']=='MISSING' for card in (amazon_card,best)):
            next_actions.append('补齐可售规格价格区间；当前单价不能代表全部规格。')
        if any(card['evidence']['negative_reviews']['status']=='MISSING' for card in (amazon_card,best)):
            next_actions.append('补采真实差评原文与来源，再判断可改进的产品问题。')
        if any(card['evidence']['observed_at']['status']=='MISSING' or card['evidence']['source']['status']=='MISSING' for card in (amazon_card,best)):
            next_actions.append('补齐页面采集来源和时间，再确认数据时效。')
        selected.append({'asin':asin,'amazon':amazon_card,'source_run_key':category['category_key'],
                         'temu':best,'same_count':match['confirmed'],'same_limit':limit,'same_operator':'lt',
                         'all_confirmed_matches':cards,'rank':item.get('rank'),'scope':'当前采样与三层候选规则内，非TEMU全站',
                         'why':why,'next_actions':next_actions,
                         'reason':f"当前采样确认{match['confirmed']}个同款，严格少于{limit}；所选同款达到配置销量"})
    selected.sort(key=lambda x:(-x['temu']['sales']['lower'],x['same_count'],x['rank'] or 999))
    complete=not rejected_by_outcome['DATA_INCOMPLETE']
    outcome='RECOMMENDED' if selected else ('NO_MATCH' if complete else 'DATA_INCOMPLETE')
    return {'status':'READY','outcome':outcome,'evidence_complete':complete,
            'category_key':category['category_key'],'source_run_key':category['category_key'],'category_name':category.get('category_name_zh'),
            'as_of':now.isoformat(),'same_limit':limit,'min_sales':min_sales,'new_days':new_days,'require_new':require_new,
            'products':selected,'rejected':rejected,'rejected_by_outcome':rejected_by_outcome,
            'evidence_signature':report['evidence_signature'],
            'message':'符合条件的运营候选' if selected else ('当前已核验范围内没有满足条件的商品。' if complete else '当前采集或同款证据未齐，暂时无法推荐。')}


def subscription(body):
    if not isinstance(body,dict):raise ValueError('订阅配置无效')
    key=body.get('category_key')
    if not isinstance(key,str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}',key):raise ValueError('请选择有效品类任务')
    at=body.get('time','08:30')
    if not isinstance(at,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',at):raise ValueError('推送时间应为北京时间 HH:MM')
    out={'category_key':key,'time':at,'label':str(body.get('label') or '运营')[:60]}
    for name,default,maximum in [('same_limit',5,10000),('min_sales',100,100000000),('new_days',90,365),('repeat_days',30,365),('lead_minutes',120,720)]:
        value=body.get(name,default)
        if type(value) is not int or not 1<=value<=maximum:raise ValueError(name+'超出范围')
        out[name]=value
    for name,default in [('enabled',True),('require_new',True),('collect_daily',True)]:
        value=body.get(name,default)
        if type(value) is not bool:raise ValueError(name+'必须为开关')
        out[name]=value
    return out


def save_subscription(root,body):
    row=subscription(body);base=Path(root)/'runtime/operations';base.mkdir(parents=True,exist_ok=True)
    with ProcessLock(base/'subscriptions.lock'):
        rows=read_json(base/'subscriptions.json',{'subscriptions':[]})['subscriptions']
        if any(r['label']==row['label'] and r['category_key']!=row['category_key'] and r['time']!=row['time'] for r in rows):
            raise ValueError('同一运营的多个品类订阅应使用相同推荐时间')
        rows=[r for r in rows if (r['category_key'],r['label'])!=(row['category_key'],row['label'])]+[row]
        atomic_write(base/'subscriptions.json',json.dumps({'subscriptions':rows},ensure_ascii=False,indent=2))
    return row


def ensure_daily_service(root):
    """One process per shared runtime; a restart does not duplicate deliveries."""
    import subprocess,sys,os,psutil
    root=Path(root);folder=root/'runtime/operations';folder.mkdir(parents=True,exist_ok=True)
    with ProcessLock(folder/'service-start.lock'):
        record=read_json(folder/'service-owner.json')
        try:
            process=psutil.Process(record['pid'])
            if (process.is_running() and abs(process.create_time()-record['created'])<.01
                    and any(Path(arg).name=='operator_daily.py' for arg in process.cmdline())):return record
        except (psutil.Error,KeyError):pass
        with (folder/'service.log').open('ab') as output:
            child=subprocess.Popen([sys.executable,str(root/'scripts/operator_daily.py'),'--watch'],cwd=root,
                                   stdout=output,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        record={'pid':child.pid,'created':psutil.Process(child.pid).create_time(),'root':str(root)}
        atomic_write(folder/'service-owner.json',json.dumps(record));return record


def daily_tick(root,*,now=None,shortlist_builder=build_shortlist):
    root=Path(root);now=now or datetime.now(timezone.utc);local=now.astimezone(BEIJING)
    base=root/'runtime/operations';base.mkdir(parents=True,exist_ok=True);results=[]
    with ProcessLock(base/'delivery.lock'):
        groups={}
        for row in read_json(base/'subscriptions.json',{'subscriptions':[]})['subscriptions']:
            if row['enabled']:groups.setdefault(row['label'],[]).append(row)
        for label,rows in groups.items():
            if local.strftime('%H:%M')<rows[0]['time']:continue
            folder=base/'deliveries'/operator_id(label);folder.mkdir(parents=True,exist_ok=True)
            path=folder/(local.date().isoformat()+'.json');prior=read_json(path)
            if prior.get('status')=='DELIVERED':continue
            snapshots=[];pool=[]
            for row in rows:
                source_key=row['category_key'];category_key=source_key;job={};snapshot=None;category={}
                source_category=read_json(root/'runs'/source_key/'category.json')
                if row.get('collect_daily',True):
                    from .daily_collection import collection_status,STARTED
                    job=collection_status(root,category_key,now)
                    if job.get('status') not in STARTED or not job.get('run_key'):
                        snapshot={'status':'WAITING_DAILY_COLLECTION','outcome':'DATA_INCOMPLETE',
                                  'evidence_complete':False,'products':[],
                                  'message':job.get('message') or '今日新采集尚未启动，没有当天证据。'}
                        category_key=None
                    else:category_key=job['run_key']
                if snapshot is None:
                    category=source_category if category_key==source_key else read_json(root/'runs'/category_key/'category.json')
                    snapshot=shortlist_builder(root,category,same_limit=row['same_limit'],min_sales=row['min_sales'],
                        new_days=row['new_days'],require_new=row['require_new'],now=now) if category else {
                            'status':'NO_TASK','outcome':'DATA_INCOMPLETE','evidence_complete':False,
                            'products':[],'message':'品类任务尚未准备，没有可核验的商品数据。'}
                snapshot={**snapshot,'source_category_key':source_key,'source_run_key':category_key,
                          'collection_status':job.get('status'),
                          'category_name':category.get('category_name_zh') or source_category.get('category_name_zh') or '未命名品类'}
                # READY was historically used even for missing evidence. An old
                # snapshot without an outcome must never certify a negative finding.
                snapshot.setdefault('outcome','RECOMMENDED' if snapshot.get('products') else 'DATA_INCOMPLETE')
                if row.get('collect_daily',True) and job.get('status')!='COMPLETE':
                    snapshot['evidence_complete']=False
                    if not snapshot.get('products'):
                        snapshot['outcome']='DATA_INCOMPLETE'
                        if snapshot['status']=='READY':snapshot['message']=job.get('message') or '今日新采集尚未完成，当前样本不足以判断是否存在合格商品。'
                snapshots.append(snapshot)
                pool.extend(({**product,'source_run_key':category_key},row,snapshot) for product in snapshot.get('products',[]))
            seen=set()
            for old in folder.glob('*.json'):
                day=instant(old.stem+'T00:00:00+08:00')
                if day and 0<=(now-day).days<max(r['repeat_days'] for r in rows):
                    data=read_json(old)
                    if data.get('status')=='DELIVERED':seen.add(data['product']['asin'])
            from .operator_feedback import read_feedback,excluded_from_new_recommendations
            handled={asin for asin,feedback in read_feedback(root,label).items() if excluded_from_new_recommendations(feedback)}
            pool.sort(key=lambda item:(-(item[0].get('temu',{}).get('sales',{}).get('lower') or 0),
                                      item[0].get('same_count',0),item[0].get('rank') or 999))
            chosen=next((item for item in pool if item[0]['asin'] not in seen and item[0]['asin'] not in handled),None)
            category_outcomes=[]
            for snapshot in snapshots:
                products=snapshot.get('products',[])
                unseen=sum(product['asin'] not in seen and product['asin'] not in handled for product in products)
                handled_count=sum(product['asin'] in handled for product in products)
                recent_count=sum(product['asin'] in seen and product['asin'] not in handled for product in products)
                outcome=snapshot['outcome'];category_message=snapshot.get('message')
                if products and not unseen:
                    outcome=('DATA_INCOMPLETE' if snapshot.get('evidence_complete') is False else
                             'ALREADY_HANDLED' if handled_count==len(products) else 'ALL_RECENTLY_RECOMMENDED')
                    category_message=('已核验候选均已推荐或进入跟进流程，其余采集或证据尚未完成。' if outcome=='DATA_INCOMPLETE'
                                      else '候选均已进入跟进流程或标记暂缓、不做。' if outcome=='ALREADY_HANDLED'
                                      else '候选均已近期推荐或进入跟进流程，暂不重复推荐。')
                category_outcomes.append({
                    'source_category_key':snapshot['source_category_key'],'source_run_key':snapshot['source_run_key'],
                    'category_name':snapshot['category_name'],
                    'source_status':snapshot['status'],'collection_status':snapshot['collection_status'],
                    'outcome':outcome,'evidence_complete':snapshot.get('evidence_complete'),
                    'candidate_count':len(products),'not_recently_recommended_count':unseen,'available_new_count':unseen,
                    'recently_recommended_count':recent_count,'handled_count':handled_count,
                    'message':category_message,'evidence_signature':snapshot.get('evidence_signature'),
                    'rejected':snapshot.get('rejected',{}),'rejected_by_outcome':snapshot.get('rejected_by_outcome',{}),
                })
            product,row,snapshot=chosen if chosen else (None,rows[0],None)
            incomplete=any(item['outcome']=='DATA_INCOMPLETE' for item in category_outcomes)
            outcome='RECOMMENDED' if product else ('DATA_INCOMPLETE' if incomplete or not category_outcomes else
                    'ALREADY_HANDLED' if pool and all(item[0]['asin'] in handled for item in pool) else
                    'ALL_RECENTLY_RECOMMENDED' if pool else 'NO_MATCH')
            if outcome=='RECOMMENDED':message='今日一个运营候选已送达工作台'
            elif outcome=='ALL_RECENTLY_RECOMMENDED':message='候选均已近期推荐或进入跟进流程，暂不重复推荐。'
            elif outcome=='ALREADY_HANDLED':message='候选均已进入跟进流程或标记暂缓、不做，暂不作为新推荐。'
            elif outcome=='NO_MATCH':message='今日已核验的订阅品类样本内，没有满足条件的商品。'
            else:
                details=list(dict.fromkeys(item['message'] for item in category_outcomes
                    if item['outcome']=='DATA_INCOMPLETE' and item.get('message')))
                message='今日数据未采齐或核验未完成，暂时无法推荐。'+'；'.join(details)
            source_statuses={item['source_status'] for item in category_outcomes}
            source_status=snapshot['status'] if snapshot else (next(iter(source_statuses)) if len(source_statuses)==1 else 'MULTIPLE_CATEGORY_STATES')
            data={'status':'DELIVERED' if product else 'NO_QUALIFIED_PRODUCT','outcome':outcome,'date':local.date().isoformat(),
                  'first_checked_at':prior.get('first_checked_at') or now.isoformat(),'checked_at':now.isoformat(),
                  'delivered_at':now.isoformat() if product else None,'channel':'工作台本地日报','subscription':row,
                  'product':product,'operator':label,'subscribed_categories':[r['category_key'] for r in rows],
                  'message':message,'category_outcomes':category_outcomes,
                  'source_run_key':product.get('source_run_key') if product else None,
                  'source_status':source_status,'evidence_signature':snapshot.get('evidence_signature') if snapshot else None}
            if prior!=data:atomic_write(path,json.dumps(data,ensure_ascii=False,indent=2))
            results.append(data)
    return results
