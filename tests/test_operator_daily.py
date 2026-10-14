"""Synthetic evidence verifies delivery, prices and missing-data boundaries."""
import json
from datetime import datetime,timezone
import pytest
from selection_automation.operations import price_range,sales,subscription,save_subscription,daily_tick,build_shortlist
import selection_automation.operations as operations


def test_selected_sku_price_is_not_an_all_sku_range():
    result=price_range({'price_usd':12.99,'listing_price_text':'$12.99'})
    assert result['min'] is None and result['max'] is None and result['observed_price']==12.99
    result=price_range({'listing_price_text':'$12.99 - $29.99'})
    assert (result['min'],result['max'])==(12.99,29.99)
    assert price_range({'listing_price_text':'$12.99 Was $29.99'})['min'] is None
    assert price_range({'listing_price_text':'CNY 12.99 - 29.99'})['min'] is None


def test_incomplete_sku_prices_preserve_the_known_subset():
    partial=price_range({'variants':[{'price_usd':10},{'label':'unpriced'}]})
    assert partial['min']==partial['max']==10
    assert '子集' in partial['basis']
    result=price_range({'variants':[{'price_usd':10},{'price_usd':15}]})
    assert result['min']==10 and result['max']==15 and '子集' in result['basis']


def test_sales_does_not_turn_bsr_or_rating_count_into_sales():
    assert sales({'rank':1,'review_count':1000},'amazon')['lower'] is None
    assert sales({'monthly_sales_text':'1K+ bought in past month'},'amazon')['lower']==1000
    assert sales({'sales_text':'200 sold'},'temu')['basis']=='页面显示销量，周期未知'


def test_chinese_sales_lower_bounds_and_approximate_display_counts():
    parsed={'sales_text':'已售1万+件','sales_count':10000,'sales_basis':'页面显示销量，周期未知',
            'sales_precision':'displayed_lower_bound'}
    assert sales(parsed,'temu')['lower']==10000
    assert sales(parsed,'temu')['precision']=='displayed_lower_bound'
    assert sales({'sales_text':'已售\n6,740件'},'temu')['lower']==6740
    assert sales({'sales_text':'已售1.2万+件'},'temu')['lower']==12000
    approximate=sales({**parsed,'sales_text':'已售1万件','sales_precision':'displayed_approximate'},'temu')
    assert approximate['lower'] is None and approximate['displayed_value']==10000
    assert sales({'sales_text':'1K sold'},'temu')['lower'] is None
    assert sales({'sales_text':'已售1,2件'},'temu')['lower'] is None


def test_daily_one_product_is_durable_across_restarts(tmp_path):
    config={'category_key':'mice'};p=tmp_path/'runs/mice/category.json';p.parent.mkdir(parents=True);p.write_text(json.dumps(config))
    save_subscription(tmp_path,{'category_key':'mice','time':'08:30','collect_daily':False})
    now=datetime(2026,10,13,0,30,tzinfo=timezone.utc)
    def snapshot(*a,**kw):return {'status':'READY','message':'ready','products':[{'asin':'A'},{'asin':'B'}]}
    assert daily_tick(tmp_path,now=now,shortlist_builder=snapshot)[0]['product']['asin']=='A'
    assert daily_tick(tmp_path,now=now,shortlist_builder=snapshot)==[]
    next_day=datetime(2026,10,14,0,30,tzinfo=timezone.utc)
    assert daily_tick(tmp_path,now=next_day,shortlist_builder=snapshot)[0]['product']['asin']=='B'


def test_no_qualified_product_never_becomes_a_forced_pick(tmp_path):
    p=tmp_path/'runs/mice/category.json';p.parent.mkdir(parents=True);p.write_text('{"category_key":"mice"}')
    save_subscription(tmp_path,{'category_key':'mice','collect_daily':False})
    empty=lambda *a,**k:{'status':'READY','message':'no evidence','products':[]}
    result=daily_tick(tmp_path,now=datetime(2026,10,13,1,tzinfo=timezone.utc),shortlist_builder=empty)[0]
    assert result['status']=='NO_QUALIFIED_PRODUCT' and result['product'] is None


def test_one_operator_with_two_categories_still_gets_one_daily_product(tmp_path):
    for key in ('mice','computers'):
        p=tmp_path/f'runs/{key}/category.json';p.parent.mkdir(parents=True);p.write_text(json.dumps({'category_key':key}))
        save_subscription(tmp_path,{'category_key':key,'label':'数码运营','collect_daily':False})
    snapshot=lambda root,cat,**kw:{'status':'READY','message':'ready',
        'products':[{'asin':cat['category_key'],'temu':{'sales':{'lower':200 if cat['category_key']=='mice' else 300}}}]}
    result=daily_tick(tmp_path,now=datetime(2026,10,13,1,tzinfo=timezone.utc),shortlist_builder=snapshot)
    assert len(result)==1 and result[0]['product']['asin']=='computers'
    assert daily_tick(tmp_path,now=datetime(2026,10,13,2,tzinfo=timezone.utc),shortlist_builder=snapshot)==[]


@pytest.mark.parametrize('patch',[{'time':'24:00'},{'same_limit':True},{'min_sales':0},{'enabled':'yes'}])
def test_subscription_validates_configuration(patch):
    with pytest.raises(ValueError):subscription({'category_key':'mice',**patch})


def test_strict_n_and_new_listing_use_confirmed_goods_and_real_date(tmp_path,monkeypatch):
    category={'category_key':'mice','commercial':{'same_limit':2}};base=tmp_path/'runs/mice'
    (base/'amazon').mkdir(parents=True);(base/'temu/union').mkdir(parents=True)
    (base/'amazon/amazon_frontend_latest.json').write_text(json.dumps({'records':[{'asin':'A','title':'Mouse'}]}))
    report={'evidence_signature':'current','products':[{'asin':'A','rank':1,'confirmed_goods_ids':['T'],
              'match':{'confirmed':1,'expected':1,'pending':0,'possible':0}}]}
    (base/'commercial_report.json').write_text(json.dumps(report))
    row={'goods_id':'T','sales_text':'200 sold','listed_at':'2026-10-12T00:00:00Z','listed_at_source':'detail page'}
    p=base/'temu/union/products.jsonl';p.write_text(json.dumps(row)+'\n')
    monkeypatch.setattr(operations,'report_signature',lambda *a:'current')
    now=datetime(2026,10,13,tzinfo=timezone.utc)
    assert len(build_shortlist(tmp_path,category,now=now)['products'])==1
    assert build_shortlist(tmp_path,category,same_limit=1,now=now)['products']==[]
    row['listed_at_source']=None;row['first_seen_at']='2026-10-12T00:00:00Z';p.write_text(json.dumps(row)+'\n')
    assert build_shortlist(tmp_path,category,now=now)['products']==[]


def evidence_fixture(root,monkeypatch,*,row=None,match=None):
    category={'category_key':'mice','commercial':{'same_limit':5}}
    base=root/'runs/mice';(base/'amazon').mkdir(parents=True);(base/'temu/union').mkdir(parents=True)
    amazon={'asin':'A','title':'Mouse','product_url':'https://www.amazon.com/dp/A',
            'observed_at':'2026-10-13T00:00:00Z','source':'VISIBLE_AMAZON_US_FRONTEND'}
    (base/'amazon/amazon_frontend_latest.json').write_text(json.dumps({'records':[amazon]}))
    report={'evidence_signature':'current','products':[{'asin':'A','rank':1,'confirmed_goods_ids':['T'],
        'match':match if match is not None else {'confirmed':1,'expected':1,'pending':0,'possible':0}}]}
    (base/'commercial_report.json').write_text(json.dumps(report))
    temu={'goods_id':'T','sales_text':'200 sold','listed_at':'2026-10-12T00:00:00Z','listed_at_source':'detail page',
          'product_url':'https://www.temu.com/goods.html?goods_id=T','observed_at':'2026-10-13T00:00:00Z',**(row or {})}
    (base/'temu/union/products.jsonl').write_text(json.dumps(temu)+'\n')
    monkeypatch.setattr(operations,'report_signature',lambda *a:'current')
    return category,base


@pytest.mark.parametrize(('patch','outcome'),[
    ({'sales_text':None},'DATA_INCOMPLETE'),
    ({'listed_at_source':None},'DATA_INCOMPLETE'),
    ({'listed_at':'not a date'},'DATA_INCOMPLETE'),
    ({'listed_at':'2027-01-01T00:00:00Z'},'DATA_INCOMPLETE'),
    ({'sales_text':'20 sold','listed_at_source':None},'NO_MATCH'),
    ({'listed_at':'2025-01-01T00:00:00Z','sales_text':None},'NO_MATCH'),
])
def test_missing_evidence_and_known_failed_criteria_have_distinct_outcomes(tmp_path,monkeypatch,patch,outcome):
    category,_=evidence_fixture(tmp_path,monkeypatch,row=patch)
    result=build_shortlist(tmp_path,category,now=datetime(2026,10,13,tzinfo=timezone.utc))
    assert result['status']=='READY' and result['outcome']==outcome and result['products']==[]
    assert result['evidence_complete'] is (outcome=='NO_MATCH')


@pytest.mark.parametrize('patch',[{'confirmed':None},{'expected':None},{'pending':None},{'possible':None},
                                  {'confirmed':1.5},{'pending':1},{'possible':1}])
def test_unknown_or_unfinished_matching_is_not_zero_competition(tmp_path,monkeypatch,patch):
    category,_=evidence_fixture(tmp_path,monkeypatch,match={'confirmed':1,'expected':1,'pending':0,'possible':0,**patch})
    assert build_shortlist(tmp_path,category)['outcome']=='DATA_INCOMPLETE'


@pytest.mark.parametrize('missing',['amazon','temu','details','report'])
def test_missing_snapshot_or_match_details_is_incomplete(tmp_path,monkeypatch,missing):
    category,base=evidence_fixture(tmp_path,monkeypatch)
    if missing=='amazon':(base/'amazon/amazon_frontend_latest.json').write_text('{"records":[]}')
    elif missing=='temu':(base/'temu/union/products.jsonl').write_text('')
    else:
        report=json.loads((base/'commercial_report.json').read_text())
        if missing=='details':report['products'][0]['confirmed_goods_ids']=[]
        else:report['products']=[]
        (base/'commercial_report.json').write_text(json.dumps(report))
    assert build_shortlist(tmp_path,category)['outcome']=='DATA_INCOMPLETE'


def test_zero_fully_reviewed_matches_is_a_known_failure_of_rival_sales_rule(tmp_path,monkeypatch):
    category,base=evidence_fixture(tmp_path,monkeypatch,match={'confirmed':0,'expected':1,'pending':0,'possible':0})
    report=json.loads((base/'commercial_report.json').read_text());report['products'][0]['confirmed_goods_ids']=[]
    (base/'commercial_report.json').write_text(json.dumps(report))
    result=build_shortlist(tmp_path,category)
    assert result['outcome']=='NO_MATCH' and result['products']==[]


def test_decision_card_exposes_missing_provenance_and_optional_evidence(tmp_path,monkeypatch):
    category,_=evidence_fixture(tmp_path,monkeypatch,row={'price_usd':12.99,'rating':4.6})
    result=build_shortlist(tmp_path,category,now=datetime(2026,10,13,tzinfo=timezone.utc))
    assert result['outcome']=='RECOMMENDED'
    card=result['products'][0];temu=card['temu']
    assert card['source_run_key']=='mice' and card['why'] and card['next_actions']
    assert temu['site']=='temu' and temu['source'] is None and temu['source_status']=='MISSING'
    assert temu['evidence']['price']['status']=='AVAILABLE'
    assert temu['evidence']['price_range']['status']=='MISSING'
    assert temu['evidence']['negative_reviews']['status']=='MISSING'
    assert temu['evidence']['sales']['status']=='AVAILABLE'
    assert temu['observed_at_source']=='observed_at'
    assert 'Amazon' not in str(temu['source'])
    assert any('价格区间' in action for action in card['next_actions'])


def test_observation_source_and_negative_review_links_are_preserved():
    card=operations.product_card({'goods_id':'T','source':'VISIBLE_TEMU_FRONTEND_KEYWORD_UNION',
        'primary_observation':{'observed_at':'2026-10-13T00:00:00Z'},
        'negative_reviews':[{'text':'Broken button','rating':1,'source_url':'https://example.test/review/1'},
                            {'text':'Unsupported claim','rating':1}]},'temu')
    assert card['observed_at_source']=='primary_observation.observed_at'
    assert card['evidence']['observed_at']['status']=='AVAILABLE'
    assert card['evidence']['negative_reviews']['source_urls']==['https://example.test/review/1']
    assert len(card['negative_reviews'])==1


def test_actual_snapshot_provenance_is_distinguished_from_per_product_observation():
    card=operations.product_card({'asin':'A'},'amazon',snapshot_metadata={
        'source':'VISIBLE_AMAZON_US_FRONTEND','observed_at':'2026-10-13T00:00:00Z'})
    assert card['source']=='VISIBLE_AMAZON_US_FRONTEND' and card['source_recorded_in']=='snapshot'
    assert card['observed_at_source']=='snapshot.observed_at'
    assert '不代表' in card['evidence']['observed_at']['note']
    empty=operations.product_card({'goods_id':'T'},'temu')
    assert empty['source'] is None and empty['observed_at'] is None
    assert empty['evidence']['observed_at']['status']=='MISSING'


def test_all_subscribed_category_outcomes_survive_including_missing_task(tmp_path):
    for key in ('mice','missing'):
        save_subscription(tmp_path,{'category_key':key,'label':'数码运营','collect_daily':False})
    p=tmp_path/'runs/mice/category.json';p.parent.mkdir(parents=True);p.write_text('{"category_key":"mice","category_name_zh":"鼠标"}',encoding='utf-8')
    snapshot=lambda *a,**kw:{'status':'READY','outcome':'NO_MATCH','evidence_complete':True,'products':[],'message':'已核验无匹配'}
    result=daily_tick(tmp_path,now=datetime(2026,10,13,1,tzinfo=timezone.utc),shortlist_builder=snapshot)[0]
    assert result['outcome']=='DATA_INCOMPLETE' and result['status']=='NO_QUALIFIED_PRODUCT'
    outcomes=result['category_outcomes']
    assert [(row['source_category_key'],row['outcome']) for row in outcomes]==[('mice','NO_MATCH'),('missing','DATA_INCOMPLETE')]
    assert result['source_status']=='MULTIPLE_CATEGORY_STATES' and result['evidence_signature'] is None


def test_known_no_match_and_all_recently_recommended_are_separate(tmp_path):
    p=tmp_path/'runs/mice/category.json';p.parent.mkdir(parents=True);p.write_text('{"category_key":"mice"}')
    save_subscription(tmp_path,{'category_key':'mice','collect_daily':False})
    empty=lambda *a,**kw:{'status':'READY','outcome':'NO_MATCH','evidence_complete':True,'products':[],'message':'已核验无匹配'}
    now=datetime(2026,10,13,1,tzinfo=timezone.utc)
    assert daily_tick(tmp_path,now=now,shortlist_builder=empty)[0]['outcome']=='NO_MATCH'
    candidate=lambda *a,**kw:{'status':'READY','outcome':'RECOMMENDED','evidence_complete':True,'products':[{'asin':'A'}],'message':'有候选'}
    assert daily_tick(tmp_path,now=now,shortlist_builder=candidate)[0]['outcome']=='RECOMMENDED'
    result=daily_tick(tmp_path,now=datetime(2026,10,14,1,tzinfo=timezone.utc),shortlist_builder=candidate)[0]
    assert result['outcome']=='ALL_RECENTLY_RECOMMENDED'
    assert result['category_outcomes'][0]['recently_recommended_count']==1
    assert result['product'] is None


def test_fresh_running_job_cannot_certify_no_match_from_partial_snapshot(tmp_path):
    save_subscription(tmp_path,{'category_key':'mice'})
    category=tmp_path/'runs/daily_mice/category.json';category.parent.mkdir(parents=True)
    category.write_text('{"category_key":"daily_mice"}')
    job=tmp_path/'runtime/operations/collection/2026-10-13/mice.json';job.parent.mkdir(parents=True)
    job.write_text('{"status":"RUNNING","run_key":"daily_mice"}')
    snapshot=lambda *a,**kw:{'status':'READY','outcome':'NO_MATCH','evidence_complete':True,'products':[],'message':'已核验无匹配'}
    result=daily_tick(tmp_path,now=datetime(2026,10,13,1,tzinfo=timezone.utc),shortlist_builder=snapshot)[0]
    assert result['outcome']=='DATA_INCOMPLETE'
    assert result['category_outcomes'][0]['source_run_key']=='daily_mice'
    assert result['category_outcomes'][0]['collection_status']=='RUNNING'
    assert '尚未完成' in result['message']


@pytest.mark.parametrize('state',['INQUIRY','SAMPLE','HOLD','REJECT'])
def test_operator_feedback_suppresses_new_recommendation_until_returned_to_new(tmp_path,state):
    from selection_automation.operator_feedback import save_feedback
    p=tmp_path/'runs/mice/category.json';p.parent.mkdir(parents=True);p.write_text('{"category_key":"mice"}')
    save_subscription(tmp_path,{'category_key':'mice','label':'数码运营','collect_daily':False})
    snapshot=lambda *a,**kw:{'status':'READY','outcome':'RECOMMENDED','evidence_complete':True,'products':[{'asin':'A'}],'message':'有候选'}
    save_feedback(tmp_path,'数码运营',{'asin':'A','source_run_key':'mice','state':state,'reason':'已决定先跟进'})
    now=datetime(2026,10,13,1,tzinfo=timezone.utc)
    result=daily_tick(tmp_path,now=now,shortlist_builder=snapshot)[0]
    assert result['outcome']=='ALREADY_HANDLED' and result['product'] is None
    assert result['category_outcomes'][0]['handled_count']==1
    assert result['category_outcomes'][0]['recently_recommended_count']==0
    save_feedback(tmp_path,'数码运营',{'asin':'A','source_run_key':'mice','state':'NEW','reason':''})
    resumed=daily_tick(tmp_path,now=now,shortlist_builder=snapshot)[0]
    assert resumed['outcome']=='RECOMMENDED' and resumed['product']['asin']=='A'
    assert daily_tick(tmp_path,now=now,shortlist_builder=snapshot)==[]


def test_feedback_filters_only_its_operator_and_selects_another_candidate(tmp_path):
    from selection_automation.operator_feedback import save_feedback
    p=tmp_path/'runs/mice/category.json';p.parent.mkdir(parents=True);p.write_text('{"category_key":"mice"}')
    save_subscription(tmp_path,{'category_key':'mice','label':'甲','collect_daily':False})
    save_subscription(tmp_path,{'category_key':'mice','label':'乙','collect_daily':False})
    snapshot=lambda *a,**kw:{'status':'READY','outcome':'RECOMMENDED','evidence_complete':True,
                           'products':[{'asin':'A'},{'asin':'B'}],'message':'有候选'}
    save_feedback(tmp_path,'甲',{'asin':'A','source_run_key':'mice','state':'HOLD','reason':'待询价'})
    results=daily_tick(tmp_path,now=datetime(2026,10,13,1,tzinfo=timezone.utc),shortlist_builder=snapshot)
    assert {row['operator']:row['product']['asin'] for row in results}=={'甲':'B','乙':'A'}
