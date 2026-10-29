from datetime import datetime, timedelta, timezone
import pytest
from selection_automation.market_provider import collect, metrics

NOW=datetime(2026,9,26,tzinfo=timezone.utc)


def category(**policy):
    return {'category_key':'fixture','category_name_zh':'电钻','commercial':policy}


class Source:
    def __init__(self):self.calls=[]
    def categories(self,query):
        return {'total':2,'list':[{'catId':1,'catName':'电钻','linkUrl':'https://www.geekbi.com/category/1'},
                                  {'catId':2,'catName':'电钻配件'}]}
    def goods(self,query):
        self.calls.append(query)
        assert query['catIds']==[1] and 'onSaleTimeMin' not in query
        return {'total':2,'list':[{'goodsId':str(query['page']),'catIds':[1],
                                  'onSaleTime':'2026-08-01','updateTime':'2026-09-25'}]}


def test_real_returned_category_ids_pagination_and_local_date_filter():
    source=Source();result=collect(category(),source,now=NOW)
    assert len(source.calls)==2 and result['status']=='COMPLETE'
    assert len(result['records'])==2 and result['categories'][0]['expected']==2


def test_repeated_page_stops_without_claiming_full_coverage():
    source=Source();original=source.goods
    source.goods=lambda query:original({**query,'page':1})
    result=collect(category(),source,now=NOW)
    assert result['status']=='PARTIAL' and len(source.calls)==2


def test_fuzzy_category_is_not_silently_used():
    source=Source()
    assert collect({**category(),'category_name_zh':'投影仪'},source,now=NOW)['status']=='CATEGORY_UNRESOLVED'
    assert not source.calls


def test_recovery_search_terms_do_not_become_accepted_category_aliases():
    class Broader(Source):
        def categories(self,query):
            assert query['keyword']=='电动工具'
            return {'total':1,'list':[{'catId':55,'catName':'电动工具','isLeaf':True}]}
    source=Broader()
    result=collect(category(),source,now=NOW,search_terms=['电动工具'])
    assert result['status']=='CATEGORY_UNRESOLVED' and not source.calls
    assert [row['keyword'] for row in result['category_queries']]==['电动工具']


def test_alias_queries_match_only_actual_returned_exact_english_name():
    class Aliases:
        def __init__(self):self.queries=[];self.goods_calls=[]
        def categories(self,query):
            self.queries.append(query['keyword'])
            return {'total':1,'list':[{'catId':81,'catName':'Luggage-Scales','isLeaf':True}]} if query['keyword']=='luggage scales' else {
                'total':1,'list':[{'catId':99,'catName':'Luggage Accessories','isLeaf':False}]}
        def goods(self,query):
            self.goods_calls.append(query);assert query['catIds']==[81]
            return {'total':0,'list':[]}
    source=Aliases()
    result=collect({**category(),'category_name_zh':'行李秤','temu':{'category_aliases':['luggage scales']}},source,now=NOW)
    assert result['status']=='COMPLETE' and source.queries==['行李秤','luggage scales']
    assert result['categories'][0]['catId']==81
    assert [row['keyword'] for row in result['category_queries']]==source.queries


def test_alias_must_not_widen_to_parent_even_when_model_selects_it():
    class Parents(Source):
        def categories(self,query):
            return {'total':1,'list':[{'catId':1,'catName':'厨房电器','isLeaf':False}]}
    source=Parents()
    result=collect({**category(),'category_name_zh':'电磁炉','temu':{'category_aliases':['厨房电器']}},source,now=NOW,
        choose_categories=lambda *args:{'cat_ids':[1],'confidence':1})
    assert result['status']=='CATEGORY_UNRESOLVED'
    assert result['optional'] and result['blocking'] is False and not source.calls
    assert result['category_candidates'][0]['catId']==1


@pytest.mark.parametrize('name,is_leaf', [('Spatulas',True),('Spatulas',False),('Spatula Accessories',True)])
def test_verified_amazon_heading_queries_real_category_without_widening(name,is_leaf):
    class HeadingSource:
        def __init__(self):self.queries=[];self.goods_calls=[]
        def categories(self,query):
            self.queries.append(query['keyword'])
            return {'total':1,'list':[{'catId':81,'catNameEn':name,'catName':'\u522e\u5200','isLeaf':is_leaf}]} if query['keyword']=='Spatulas' else {
                'total':0,'list':[]}
        def goods(self,query):
            self.goods_calls.append(query)
            assert query['catIds']==[81]
            return {'total':1,'list':[{'goodsId':'live-product','catIds':[81]}]}
    def forbidden(*args):raise AssertionError('heading discovery must not require model mapping')
    source=HeadingSource()
    cat={**category(),'category_name_zh':'\u7845\u80f6\u522e\u5200','temu':{'category_aliases':['silicone spatula']},
         'amazon':{'verification':{'heading':'Best Sellers in Spatulas','title':'Amazon Best Sellers: Best Spatulas'}}}
    result=collect(cat,source,now=NOW,choose_categories=forbidden if name=='Spatulas' and is_leaf else None)
    matched=name=='Spatulas' and is_leaf
    assert result['status']==('COMPLETE' if matched else 'CATEGORY_UNRESOLVED')
    assert source.queries[:2]==['\u7845\u80f6\u522e\u5200','Spatulas']
    assert len(source.goods_calls)==int(matched)
    assert [row['goodsId'] for row in result['records']]==(['live-product'] if matched else [])


def test_product_title_is_not_used_as_a_category_query():
    class Empty(Source):
        def __init__(self):super().__init__();self.queries=[]
        def categories(self,query):
            self.queries.append(query['keyword']);return {'total':0,'list':[]}
    source=Empty()
    cat={**category(),'amazon':{'verification':{'heading':'Silicone Spatula Product','title':'Best Sellers in Spatulas'}}}
    result=collect(cat,source,now=NOW)
    assert result['status']=='CATEGORY_UNSUPPORTED' and source.queries==['\u7535\u94bb']
    assert not source.calls


def test_exact_category_can_be_found_beyond_first_fuzzy_page():
    class Paged(Source):
        def categories(self,query):
            return {'total':201,'list':[{'catId':1000+i,'catName':f'电钻配件{i}'} for i in range(200)]} if query['page']==1 else {
                'total':201,'list':[{'catId':1,'catName':'电钻','isLeaf':True}]}
    source=Paged();result=collect(category(),source,now=NOW)
    assert result['status']=='COMPLETE' and len(result['category_queries'])==2


def test_incomplete_taxonomy_search_does_not_claim_unsupported():
    class Many(Source):
        def categories(self,query):
            return {'total':1000,'list':[{'catId':query['page'],'catName':'其它类目'}]}
    result=collect(category(),Many(),now=NOW)
    assert result['status']=='CATEGORY_UNRESOLVED' and len(result['category_queries'])==3


def test_provider_failure_is_not_misreported_as_category_unsupported():
    class Broken(Source):
        def categories(self,query):raise TimeoutError('network')
    with pytest.raises(TimeoutError):collect(category(),Broken(),now=NOW)


def test_verified_keyboard_path_uses_live_id_without_login_or_model_mapping():
    class Keyboards:
        def categories(self, query):
            return {'list': [{'catId': 3894, 'catName': '普通键盘',
                'parentCatItems': [{'catName': n} for n in ['电子','电脑及配件','外设产品','电脑鼠标、键盘、输入设备','普通键盘']]},
                {'catId': 18165, 'catName': '键盘、钢琴', 'parentCatItems': [{'catName': '乐器'}]}]}
        def goods(self, query):
            assert query['catIds'] == [3894]
            return {'list': [], 'total': 0}
    def forbidden(*args):
        raise AssertionError('verified taxonomy must not request a category model')
    cat={**category(), 'category_name_zh':'键盘', 'amazon':{'verification':{'heading':'Best Sellers in Computer Keyboards'}}}
    result=collect(cat,Keyboards(),choose_categories=forbidden,now=NOW)
    assert result['status']=='COMPLETE'
    assert result['categories'][0]['catId']==3894


@pytest.mark.parametrize('correct_path', [True, False])
def test_verified_fuel_transfer_taxonomy_never_selects_engine_parts(correct_path):
    class FuelSource:
        def __init__(self):self.calls=[]
        def categories(self,query):
            if query['keyword']!='抽油泵':return {'total':0,'list':[]}
            path=['汽车用品','维修工具','车辆维修车间设备'] if correct_path else ['汽车用品','汽车零件','汽车燃料供应']
            return {'total':2,'list':[{'catId':20094,'catName':'桶形和手摇燃油泵',
                'parentCatItems':[{'catName':p} for p in path]}, {'catId':21034,'catName':'汽车更换电动燃油泵'}]}
        def goods(self,query):
            self.calls.append(query);assert query['catIds']==[20094]
            return {'total':0,'list':[]}
    source=FuelSource()
    cat={**category(),'category_name_zh':'便携式抽油泵／液体输送泵',
         'amazon':{'verification':{'heading':'Best Sellers in Barrel & Hand Fuel Pumps'}}}
    result=collect(cat,source,now=NOW)
    assert result['status']==('COMPLETE' if correct_path else 'CATEGORY_UNRESOLVED')
    assert len(source.calls)==int(correct_path)


def test_category_mapper_may_only_choose_real_high_confidence_ids():
    cat={**category(),'category_name_zh':'无线电钻'}
    for ids,confidence in [([999],1),([1],.5)]:
        result=collect(cat,Source(),now=NOW,choose_categories=lambda *a:{'cat_ids':ids,'confidence':confidence})
        assert result['status']=='CATEGORY_UNRESOLVED'
    result=collect(cat,Source(),now=NOW,choose_categories=lambda *a:{'cat_ids':[1],'confidence':.95})
    assert result['status']=='COMPLETE' and result['categories'][0]['catId']==1


def test_complete_empty_category_search_is_unsupported_without_model_request():
    class Empty(Source):
        def categories(self,query):return {'total':0,'list':[]}
    source=Empty()
    def forbidden(*args):raise AssertionError('no candidate requires no model request')
    result=collect(category(),source,now=NOW,choose_categories=forbidden)
    assert result['status']=='CATEGORY_UNSUPPORTED' and result['category_candidates']==[]
    assert not source.calls


def snapshot(rates=(.3,.4), sales=(90,10)):
    return {'status':'COMPLETE','queried_at':NOW.isoformat(),'records':[
        {'goodsId':str(i),'onSaleTime':(NOW-timedelta(days=70)).isoformat(),
         'updateTime':(NOW-timedelta(days=1)).isoformat(),'linkUrl':'https://www.geekbi.com/product/'+str(i),
         'weekSoldRate':rate,'monthSoldRate':rate,'weekSold':10,'monthSold':amount}
        for i,(rate,amount) in enumerate(zip(rates,sales))]}


def test_rates_are_fractional_sales_not_revenue_and_concentration_separate():
    report=metrics(category(min_new_products=2,top_n=1,min_market_products=2),snapshot(),NOW)
    assert report['growth']['state']=='SAMPLE_PASS'
    assert report['growth']['periods']['month']['value']==35
    assert report['concentration']['value_percent']==90 and report['concentration']['state']=='FAIL'


@pytest.mark.parametrize('mutation', ['missing','stale','new','revenue_only','zero_baseline'])
def test_incomplete_or_invalid_growth_is_unknown(mutation):
    data=snapshot()
    for row in data['records']:
        if mutation=='missing':row['weekSoldRate']=None;row['monthSoldRate']=None
        if mutation=='stale':row['updateTime']='2026-01-01'
        if mutation=='new':row['onSaleTime']='2026-09-25'
        if mutation=='revenue_only':row.pop('weekSoldRate');row.pop('monthSoldRate');row['weekSalesRate']=10
        if mutation=='zero_baseline':row['weekSold']=0;row['monthSold']=0
    result=metrics(category(min_new_products=1),data,NOW)
    assert result['growth']['state']=='UNKNOWN'


def test_snapshot_expiry_and_source_failure_do_not_show_old_positive():
    data=snapshot();data['queried_at']='2026-09-20'
    assert metrics(category(),data,NOW) is None
    data=snapshot();data['status']='FAILED'
    assert metrics(category(),data,NOW) is None
