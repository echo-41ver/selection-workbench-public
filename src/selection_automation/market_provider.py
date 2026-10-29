"""GeekBI source-separated market evidence. Uses the same client/auth as its MCP."""
from datetime import datetime, timedelta, timezone
import math
import re
from statistics import median
import unicodedata

from .commercial import instant, metric, settings


class Provider:
    def __init__(self, *, auth_state_path=None):
        from geekbi_temu_mcp.client import GeekBIClient
        self.client=GeekBIClient(timeout=30)
        if auth_state_path is not None:
            from .market_auth import use_installation_auth
            use_installation_auth(self.client, auth_state_path)

    def categories(self, query):
        from geekbi_temu_mcp.models import CategorySearchInput
        return self.client.categories(CategorySearchInput(**query))['data']

    def goods(self, query):
        from geekbi_temu_mcp.models import GoodsSearchInput
        return self.client.goods(GoodsSearchInput(**query))['data']


def _category_name(value):
    """Normalize formatting only; never turn a parent name into a child name."""
    return re.sub(r'[\s_\-‐‑–—]+', ' ', unicodedata.normalize('NFKC', str(value or '')).casefold()).strip()


def _category_terms(category):
    terms=[category['category_name_zh']]
    heading=category.get('amazon',{}).get('verification',{}).get('heading') or ''
    verified_name=re.fullmatch(r'Best Sellers in\s+(.+)',heading.strip(),re.I)
    if verified_name:terms.append(verified_name.group(1))
    temu=category.get('temu') or {}
    for field in ('category_aliases','aliases','base_queries','include_terms'):
        values=temu.get(field) or []
        if isinstance(values,str):values=[values]
        terms.extend(value for value in values if isinstance(value,str))
    result=[];seen=set()
    for term in terms:
        term=term.strip();key=_category_name(term)
        if key and key not in seen:
            result.append(term);seen.add(key)
        if len(result)>=12:break
    return result


def _exact_categories(rows, terms, requested):
    accepted={_category_name(term) for term in terms}
    selected=[]
    for row in rows:
        if type(row.get('catId')) is not int or row['catId']<=0:continue
        names={_category_name(row.get(field)) for field in ('catName','catNameEn','catNameEnglish','catNameCn')}
        # An alias must not silently widen a narrow request to a parent scope.
        if row.get('isLeaf') is False and _category_name(requested) not in names:continue
        if names & accepted:selected.append(row)
    return selected


def collect(category, provider, progress=lambda value:None, *, now=None, max_pages=50, choose_categories=None, search_terms=None):
    now=now or datetime.now(timezone.utc)
    name=category['category_name_zh'].strip()
    terms=_category_terms(category);queries=[];candidates={};search_complete=True
    selected=[]
    for term in terms if search_terms is None else search_terms:
        for page in range(1,4):
            data=provider.categories({'keyword':term,'siteId':48,'page':page,'size':200})
            rows=data.get('list') or []
            queries.append({'keyword':term,'page':page,'returned':len(rows),'total':data.get('total')})
            for row in rows:
                if type(row.get('catId')) is int and row['catId']>0:candidates[row['catId']]=row
            selected=_exact_categories(candidates.values(),terms,name)
            if selected:break
            total=data.get('total')
            if not rows or (type(total) is int and page*200>=total) or (total is None and len(rows)<200):break
            if page==3:search_complete=False
        if selected:break
    queried={'list':list(candidates.values())}
    # A fuzzy keyword response is not a category boundary. Only actual returned
    # exact names are accepted automatically; different taxonomy paths stay visible.
    selection_method='提供方真实类目名称与本任务中英文名称或别名精确匹配'
    # Match the reviewed taxonomy path, taking IDs from this live response.
    heading=str(category.get('amazon',{}).get('verification',{}).get('heading') or '').casefold()
    if not selected and heading=='best sellers in computer keyboards':
        selected=[r for r in queried['list'] if r.get('catName')=='普通键盘'
                  and [p.get('catName') for p in r.get('parentCatItems',[])][:4]
                  == ['电子','电脑及配件','外设产品','电脑鼠标、键盘、输入设备']]
        selection_method='已核验 Amazon 电脑键盘榜单对应极鲸云普通键盘类目路径'
    if not selected and search_terms is None and heading=='best sellers in barrel & hand fuel pumps':
        queried=provider.categories({'keyword':'抽油泵','siteId':48,'page':1,'size':200})
        queries.append({'keyword':'抽油泵','page':1,'returned':len(queried['list']),'total':queried.get('total')})
        selected=[r for r in queried['list'] if r.get('catName')=='桶形和手摇燃油泵'
                  and [p.get('catName') for p in r.get('parentCatItems',[])][:3]
                  == ['汽车用品','维修工具','车辆维修车间设备']]
        selection_method='已核验 Amazon 榜单对应极鲸云完整类目路径'
    if not selected and choose_categories:
        eligible=[r for r in queried['list'] if r.get('isLeaf') is not False or _category_name(r.get('catName'))==_category_name(name)]
        chosen=choose_categories(category,eligible) if eligible else {}
        ids=chosen.get('cat_ids',[]) if isinstance(chosen,dict) and number(chosen.get('confidence')) is not None and chosen['confidence']>=.9 else []
        allowed={r.get('catId') for r in eligible}
        if 1<=len(ids)<=3 and all(type(i) is int and i in allowed for i in ids):
            selected=[r for r in queried['list'] if r.get('catId') in ids]
            selection_method='模型从提供方真实类目候选中匹配；详见完整类目路径'
    if not selected:
        unresolved=bool(candidates or queried['list']) or not search_complete
        return {'status':'CATEGORY_UNRESOLVED' if unresolved else 'CATEGORY_UNSUPPORTED',
                'message':('极鲸云类目边界尚未完成核实；采集与分析可继续' if unresolved
                           else '极鲸云本次未匹配到相关类目；采集与分析可继续'),
                'source':'GEEKBI','queried_at':now.isoformat(),'category_key':category['category_key'],
                'optional':True,'blocking':False,'category_queries':queries,
                'category_candidates':[{'catId':r.get('catId'),'name':r.get('catName'),'link':r.get('linkUrl'),
                    'path':' > '.join(p.get('catName','') for p in r.get('parentCatItems',[]))} for r in queried['list'][:40]],'records':[]}
    gathered={};pages=[];scopes=[]
    for cat in selected:
        seen=set();total=None;complete=False
        for page in range(1,max_pages+1):
            query={'catIds':[cat['catId']],'siteId':48,'page':page,'size':200,'sort':'onSaleTime','order':'desc'}
            data=provider.goods(query);rows=data['list'];total=data['total']
            pages.append({'query':query,'returned':len(rows),'total':total})
            before=len(seen)
            for row in rows:
                if not row.get('goodsId') or cat['catId'] not in (row.get('catIds') or []): continue
                gid=str(row['goodsId']);seen.add(gid)
                old=gathered.get(gid)
                if old is None or (instant(row.get('updateTime')) or now-timedelta(days=9999)) > (instant(old.get('updateTime')) or now-timedelta(days=9999)):
                    gathered[gid]=row
            progress({'status':'RUNNING','message':'正在读取极鲸云类目商品与上架时间',
                      'pages':len(pages),'products':len(gathered),'total':total})
            if len(seen)>=total: complete=True;break
            if not rows or len(seen)==before: break
        scopes.append({'catId':cat['catId'],'name':cat['catName'],'link':cat.get('linkUrl'),
                       'path':' > '.join(x['catName'] for x in cat.get('parentCatItems',[])),
                       'expected':total,'retrieved':len(seen),'complete':complete,
                       'updated_at':cat.get('updateTime')})
    return {'status':'COMPLETE' if all(c['complete'] for c in scopes) else 'PARTIAL',
            'message':'极鲸云数据已读取；数据提供方收录范围，不是TEMU全站',
            'source':'GEEKBI','site_id':48,'currency':'USD','category_key':category['category_key'],
            'queried_at':now.isoformat(),'records':list(gathered.values()),'categories':scopes,'pages':pages,
            'selection_method':selection_method,
            'category_queries':queries,
            'date_filter':'按上架时间降序分页后，本地按真实上架日期筛选；规避上游日期参数序列化错误'}


def number(value):
    return float(value) if type(value) in (float,int) and math.isfinite(value) else None


def metrics(category, snapshot, now=None):
    now=now or datetime.now(timezone.utc);policy=settings(category.get('commercial'))
    queried=instant(snapshot.get('queried_at'))
    if snapshot.get('status') not in ('COMPLETE','PARTIAL') or not queried or not timedelta(0)<=now-queried<=timedelta(days=1):
        return None
    records=snapshot.get('records',[])
    def fresh(row):
        stamp=instant(row.get('updateTime'))
        return stamp is not None and timedelta(0)<=now-stamp<=timedelta(days=7) and bool(row.get('linkUrl'))
    new=[r for r in records if instant(r.get('onSaleTime')) and timedelta(0)<=now-instant(r['onSaleTime'])<=timedelta(days=policy['new_days'])]
    periods={}
    for period,days in [('week',7),('month',30)]:
        values=[]
        eligible=[r for r in new if (now-instant(r['onSaleTime'])).total_seconds()>=days*2*86400]
        for row in eligible:
            rate=number(row.get(period+'SoldRate'));sales=number(row.get(period+'Sold'))
            # An entirely new product has no full preceding cycle; provider null
            # and zero-base growth are not converted to infinite opportunity.
            if (fresh(row)
                    and rate is not None and rate>=-1 and sales is not None and sales>=0
                    and ((rate==-1 and sales==0) or (rate>-1 and sales>0))):
                values.append(rate*100)
        coverage=len(values)/len(eligible) if eligible else 0
        enough=len(values)>=policy['min_new_products'] and coverage>=policy['min_sales_coverage']
        value=median(values) if values else None
        periods[period]=metric('SAMPLE_PASS' if enough and value>policy['growth_min_percent'] else 'FAIL' if enough else 'UNKNOWN',
            '极鲸云上架日期与销量环比；采用提供方周/月口径，可能包含估算' if enough else
            '有上架日期，但有效销量环比、完整周期或更新时效不足；缺失字段不当作零',
            value=value,valid_products=len(values),new_products=len(new),eligible_products=len(eligible),
            too_young_products=len(new)-len(eligible),coverage=coverage,
            rising_products=sum(v>policy['growth_min_percent'] for v in values),days=days)
    selected=[periods[policy['growth_period']]] if policy['growth_period']!='either' else list(periods.values())
    state='SAMPLE_PASS' if any(p['state']=='SAMPLE_PASS' for p in selected) else 'FAIL' if all(p['state']=='FAIL' for p in selected) else 'UNKNOWN'
    growth=metric(state,'来源为极鲸云；近期限上架商品的销量环比中位数，未混用销售额增长',
                  periods=periods,threshold=policy['growth_min_percent'],scope='极鲸云已匹配类目收录样本')
    sales=[number(r.get('monthSold')) for r in records if fresh(r) and number(r.get('monthSold')) is not None and number(r.get('monthSold'))>=0]
    total=sum(sales);coverage=len(sales)/len(records) if records else 0
    enough=len(sales)>=max(policy['min_market_products'],policy['top_n']+1) and coverage>=policy['min_sales_coverage'] and total>0
    share=sum(sorted(sales,reverse=True)[:policy['top_n']])/total*100 if total else None
    concentration=metric('SAMPLE_PASS' if enough and share<policy['top_share_max_percent'] else 'FAIL' if enough else 'UNKNOWN',
        '极鲸云商品月销量前N名 / 有有效月销量的类目采样商品；独立于TEMU网页展示销量，不混算分母',
        value_percent=round(share,2) if enough else None,
        known_sample_percent=round(share,2) if share is not None else None,
        valid_products=len(sales),sample_products=len(records),
        coverage=coverage,top_n=policy['top_n'],threshold_percent=policy['top_share_max_percent'],scope='极鲸云类目收录样本')
    return {'growth':growth,'concentration':concentration,'new_products':len(new),'products':len(records),
            'source':'GEEKBI','queried_at':snapshot['queried_at'],'categories':snapshot.get('categories',[]),
            'pages':len(snapshot.get('pages',[])),'complete':snapshot['status']=='COMPLETE',
            'confidence':'提供方数据；可能含估算，非平台全量审计',
            'evidence_rows':[{k:r.get(k) for k in ('goodsId','goodsName','goodsNameCn','linkUrl','onSaleTime',
                               'updateTime','sold','weekSold','monthSold','weekSoldRate','monthSoldRate','catIds')}
                              for r in records]}
