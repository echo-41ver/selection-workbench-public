import json

import pytest

from selection_automation.frontend_market import build_frontend_market, displayed_sales


def write_pass(base, query, mode, rows, worker='worker-01-new', state='UI_CONFIRMED'):
    path=base/'temu/workers'/worker/(query+'--'+mode)
    path.mkdir(parents=True,exist_ok=True)
    path.joinpath('products.jsonl').write_text('\n'.join(json.dumps({'is_sponsored':False,'search_query':query,
        'search_sort':{'requested':mode,'state':state}, **row}) for row in rows), encoding='utf-8')
    path.joinpath('progress.json').write_text(json.dumps({'is_sponsored':False,'search_query':query,
        'sales_sort':{'requested':mode,'state':state},'status':'KEYWORD_LIMIT_REACHED',
        'unique_goods':len(rows)}), encoding='utf-8')


@pytest.mark.parametrize('raw,expected',[('1K+ sold',1000),('1.5K+ sold',1500),
    ('2,103 sold',2103),('0 sold',0),('',None),('42 reviews',None),('sold',None)])
def test_displayed_quantity_not_review_or_missing(raw,expected):
    assert displayed_sales(raw)==expected


def test_passes_are_separate_and_missing_sales_not_zero(tmp_path):
    rows=[{'goods_id':'a','sales_text':'1K+ sold'}, {'goods_id':'b','sales_text':''},
          {'goods_id':'c','sales_text':'0 sold'}]
    write_pass(tmp_path,'drill','recent',rows)
    write_pass(tmp_path,'drill','recent',rows,worker='worker-02-new')
    write_pass(tmp_path,'drill','sales',[{'goods_id':'a','sales_text':'2K+ sold'}])
    result=build_frontend_market(tmp_path)['queries'][0]
    assert result['recent']['sample_products']==3
    assert result['recent']['interpretable_sales_products']==2
    assert result['recent']['unknown_sales_products']==1
    assert result['recent']['with_sales_percent']==50
    assert result['recent']['displayed_sales_median_approx']==500
    assert result['sales']['displayed_sales_median_approx']==2000
    assert len(result['recent']['progress'])==2


def test_sort_confirmation_and_sample_share(tmp_path):
    rows=[{'goods_id':str(i),'sales_text':f'{i} sold','product_url':'javascript:alert(1)'} for i in range(1,7)]
    write_pass(tmp_path,'drill','sales',rows)
    write_pass(tmp_path,'drill','recent',rows,state='SELECTING')
    result=build_frontend_market(tmp_path)['queries'][0]
    assert result['sales']['top5_displayed_share_approx']==95.2
    assert result['sales']['examples'][0]['url']==''
    assert result['recent']['sample_products']==0
    assert result['recent']['with_sales_percent'] is None
    assert result['recent']['displayed_sales_median_approx'] is None


def test_updates_and_partial_jsonl_do_not_hide_good_rows(tmp_path):
    write_pass(tmp_path,'drill','recent',[{'goods_id':'a','sales_text':'1 sold'}])
    assert build_frontend_market(tmp_path)['queries'][0]['recent']['sample_products']==1
    file=next(tmp_path.glob('temu/workers/*/*/products.jsonl'))
    with file.open('a',encoding='utf-8') as stream:
        stream.write('\n'+json.dumps({'is_sponsored':False,'goods_id':'b','search_query':'drill','sales_text':'3 sold',
            'search_sort':{'requested':'recent','state':'UI_CONFIRMED'}})+'\n{partial')
    report=build_frontend_market(tmp_path)
    assert report['queries'][0]['recent']['sample_products']==2
    assert report['read_issues']==1
    assert build_frontend_market(tmp_path/'empty')['queries']==[]


def test_report_identifies_category_sample_from_saved_plan(tmp_path):
    from selection_automation.frontend_market import build_frontend_market
    import json
    plan=tmp_path/'temu/keyword_plan.json';plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({'keywords':[{'query':'pet food bowl','purpose':'CATEGORY_SAMPLE'}]}))
    assert build_frontend_market(tmp_path)['primary_category_query']=='pet food bowl'


def test_ads_and_legacy_unknowns_never_inflate_natural_market_signal(tmp_path):
    rows=[{'goods_id':str(i),'sales_text':'1 sold','is_sponsored':False} for i in range(10)]
    rows += [{'goods_id':'ad','sales_text':'999K+ sold','is_sponsored':True},
             {'goods_id':'legacy-ad','sales_text':'100K+ sold','is_sponsored':None,'card_text':'AD\nA bowl'},
             {'goods_id':'unknown','sales_text':'100K+ sold','is_sponsored':None}]
    write_pass(tmp_path,'bowl','sales',rows)
    sample=build_frontend_market(tmp_path)['queries'][0]['sales']
    assert sample['captured_products']==13 and sample['sample_products']==10
    assert sample['top5_displayed_share_approx']==50
    assert sample['sponsored_products']==2 and sample['unknown_sponsorship_products']==1


def test_signal_requires_coverage_and_never_calls_recent_sort_growth():
    from selection_automation.frontend_market import evidence_signal
    sample={'sample_products':100,'interpretable_sales_products':79,'with_sales_products':75,'top5_displayed_share_approx':5}
    assert evidence_signal(sample,'sales')['state']=='INSUFFICIENT'
    sample['interpretable_sales_products']=80
    assert evidence_signal(sample,'sales')['state']=='FAVORABLE'
    sample['top5_displayed_share_approx']=91
    assert evidence_signal(sample,'sales')['state']=='ADVERSE'
    recent=evidence_signal(sample,'recent')
    assert recent['state']=='FAVORABLE' and '不是90天' in recent['reason']


def test_recent_signal_uses_conservative_sold_bounds_when_some_sales_are_missing():
    from selection_automation.frontend_market import evidence_signal
    sample={'sample_products':269,'interpretable_sales_products':209,
            'with_sales_products':209,'unknown_sponsorship_products':0}
    signal=evidence_signal(sample,'recent')
    assert signal['state']=='FAVORABLE'
    assert signal['confirmed_sold_lower_bound_percent']==77.7
    assert '至少209件' in signal['reason'] and '60件销量未显示' in signal['reason']
    sample['with_sales_products']=0
    assert evidence_signal(sample,'recent')['state']=='MIXED'
    sample={'sample_products':100,'interpretable_sales_products':95,'with_sales_products':1}
    assert evidence_signal(sample,'recent')['state']=='ADVERSE'
    sample={'sample_products':29,'interpretable_sales_products':29,'with_sales_products':29}
    assert evidence_signal(sample,'recent')['state']=='PRELIMINARY'


def test_all_keyword_market_updates_and_deduplicates(tmp_path):
    write_pass(tmp_path,'remote control','sales',
               [{'goods_id':str(i),'sales_text':'10 sold'} for i in range(6)])
    first=build_frontend_market(tmp_path)['all_keywords']['sales']
    assert first['keyword_count']==1
    assert first['sample_products']==6
    assert first['top5_displayed_share_approx']==83.3

    write_pass(tmp_path,'tv remote','sales',[
        {'goods_id':'0','sales_text':'','is_sponsored':True},
        {'goods_id':'6','sales_text':'20 sold'},
        {'goods_id':'7','sales_text':'20 sold'}],worker='worker-02-new')
    report=build_frontend_market(tmp_path)
    combined=report['all_keywords']['sales']
    assert combined['keyword_count']==2
    assert combined['captured_products']==8
    assert combined['sample_products']==8
    assert combined['interpretable_sales_products']==8
    assert combined['top5_displayed_share_approx']==70
    assert report['queries'][0]['sales']['sample_products']==6


def test_recent_market_uses_early_samples_without_calling_them_growth(tmp_path):
    write_pass(tmp_path,'new remote','recent',[
        {'goods_id':'a','sales_text':'12 sold'},
        {'goods_id':'b','sales_text':''}])
    recent=build_frontend_market(tmp_path)['all_keywords']['recent']
    assert recent['keyword_count']==1
    assert recent['sample_products']==2
    assert recent['signal']['label']=='初步有利'
    assert '不是90天' in recent['signal']['reason']
    assert build_frontend_market(tmp_path)['all_keywords']['sales']['signal']['state']=='INSUFFICIENT'


def test_market_sample_reports_completed_pass_and_its_observation_time(tmp_path):
    write_pass(tmp_path,'remote control','sales',
               [{'goods_id':str(i),'sales_text':f'{i} sold'} for i in range(6)])
    progress=next(tmp_path.glob('temu/workers/*/*/progress.json'))
    value=json.loads(progress.read_text())
    value['updated_at']='2026-09-29T10:24:45.638Z'
    progress.write_text(json.dumps(value))
    sample=build_frontend_market(tmp_path)['queries'][0]['sales']
    assert sample['pass_completed'] is True
    assert sample['sample_updated_at']=='2026-09-29T10:24:45.638Z'
