import json
from selection_automation.merge_temu_snapshots import merge


def test_frontend_wins_all_fields_provider_only_adds_missing_ids(tmp_path):
    frontend=tmp_path/'products.jsonl'
    original={'goods_id':'1','title':'frontend','price_usd':10,'product_url':'https://www.temu.com/1.html','main_image_url':'https://img/1','rating':None}
    frontend.write_text(json.dumps(original)+'\n',encoding='utf-8')
    provider=tmp_path/'geekbi.json'
    provider.write_text(json.dumps({'status':'PARTIAL','site_id':48,'records':[
        {'goodsId':'1','goodsName':'provider conflicting','price':99,'goodsScore':5},
        {'goodsId':'2','goodsName':'extra','price':12,'thumbnail':'https://img/2','sold':0},
        {'goodsId':'2','goodsName':'duplicate'}]}),encoding='utf-8')
    rows,summary=merge([frontend],provider)
    by_id={r['goods_id']:r for r in rows}
    assert len(rows)==2 and by_id['1']['title']=='frontend' and by_id['1']['price_usd']==10
    assert by_id['1']['rating'] is None
    assert by_id['2']['source']=='GEEKBI_SUPPLEMENT' and by_id['2']['sales_text']=='0'
    assert by_id['2']['price_usd']==12
    assert summary['frontend_unique_goods']==summary['provider_supplement_goods']==1


def test_failed_or_different_site_provider_is_not_imported(tmp_path):
    provider=tmp_path/'geekbi.json'
    for status,site in [('FAILED',48),('COMPLETE',1)]:
        provider.write_text(json.dumps({'status':status,'site_id':site,'records':[{'goodsId':'2'}]}),encoding='utf-8')
        assert merge([],provider)[0]==[]
