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
