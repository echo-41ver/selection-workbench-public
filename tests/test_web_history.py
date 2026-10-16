import http.client
import json
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import web_console as web


def saved_run(root,key,name,status='完成',count=10):
    base=root/'runs'/key
    web.selection.save(base/'category.json',{'category_key':key,'category_name_zh':name,
        'api_key':'must-not-leak','temu':{'worker_count':4},'amazon':{'target_count':count}})
    web.selection.save(base/'status.json',{'状态':status,'Amazon商品数':count,'TEMU商品数':123,
        'private_token':'must-not-leak','阶段':{}})
    (base/'excel').mkdir()
    (base/'excel'/f'Amazon_US_{name}_Best_Sellers.xlsx').write_bytes(key.encode())
    return base


def test_history_is_read_only_and_not_overwritten_by_current_task(tmp_path,monkeypatch):
    saved_run(tmp_path,'drill_a','电钻')
    saved_run(tmp_path,'pen_b','钢笔',count=20)
    console=web.Console(tmp_path);console.key='pen_b'
    monkeypatch.setattr(console,'owner',lambda:{'category_key':'pen_b'})
    listing=console.history()
    assert {r['category_name'] for r in listing['runs']}=={'钢笔','电钻'}
    assert next(r for r in listing['runs'] if r['category_key']=='pen_b')['active'] is True
    result=console.history_status('drill_a')
    assert result['scope']['category']=='电钻'
    assert result['details']['Amazon商品数']==10
    assert result['historical'] and result['read_only'] and not result['active']
    assert result['severity']=='green'
    assert result['excel_files']['amazon']['url']=='/api/history/drill_a/excel/amazon'
    assert console.key=='pen_b'
    assert console.excel_workbook('amazon').read_bytes()==b'pen_b'
    assert 'must-not-leak' not in json.dumps([listing,result])


def test_history_counts_use_saved_products_and_deduplicated_union(tmp_path,monkeypatch):
    base=saved_run(tmp_path,'headphones','耳机',count=1)
    web.selection.save(base/'amazon/amazon_frontend_latest.json',{'records':[{'asin':str(n)} for n in range(20)]})
    web.selection.save(base/'temu/union/summary.json',{'source_rows':16800,'unique_goods_id':5264})
    console=web.Console(tmp_path);monkeypatch.setattr(console,'owner',lambda:None)
    assert console.history()['runs'][0]['counts']=={'amazon':20,'temu':5264}
    web.selection.save(base/'amazon/amazon_frontend_latest.json',{'records':[]})
    web.selection.save(base/'temu/union/summary.json',{'unique_goods_id':0})
    assert console.history()['runs'][0]['counts']=={'amazon':0,'temu':0}


def test_history_counts_keep_missing_unknown_and_support_saved_stage_totals(tmp_path):
    console=web.Console(tmp_path)
    assert console.history_counts(tmp_path,{})=={'amazon':None,'temu':None}
    assert console.history_counts(tmp_path,{'阶段':{'amazon':{'actual_count':12},
        'temu_union':{'unique_goods':345}}})=={'amazon':12,'temu':345}


def test_history_never_uses_current_profiles_or_dead_heartbeat(tmp_path,monkeypatch):
    base=saved_run(tmp_path,'drill_a','电钻','运行中')
    web.selection.save(base/'temu/supervisors/worker-01-new.json',{'status':'WORKER_COMPLETE','profile_serial':'12'})
    web.selection.save(base/'temu/union/watch_state.json',{'status':'PUBLISHED','heartbeat_at':1,'unique_goods':123})
    web.selection.save(tmp_path/'config/sessions.json',{'workers':[{'name':'worker-01-new','profile_serial':'99'}]})
    console=web.Console(tmp_path)
    monkeypatch.setattr(console,'owner',lambda:None)
    result=console.history_status('drill_a')
    assert result['severity']=='neutral'
    assert result['phase']=='历史快照（未记录最终状态）'
    assert result['workers'][0]['profile_serial']=='12'
    assert result['workers'][0]['rotation_control'] is None
    assert result['details']['阶段']['temu_union']['阻断'] is False
    assert '无心跳' not in result['details']['阶段']['temu_union']['状态']


@pytest.mark.parametrize('key',['../secret','drill_a/../secret','%2e%2e','missing','C:\\secret',''])
def test_history_rejects_invalid_task_paths(tmp_path,key):
    console=web.Console(tmp_path)
    with pytest.raises(web.ConsoleError) as error:console.history_status(key)
    assert error.value.status==404


def test_history_http_download_is_explicit_run_and_does_not_republish(tmp_path,monkeypatch):
    saved_run(tmp_path,'drill_a','电钻')
    saved_run(tmp_path,'pen_b','钢笔')
    console=web.Console(tmp_path);console.key='pen_b'
    monkeypatch.setattr(console,'owner',lambda:None)
    monkeypatch.setattr(console,'refresh_excel',lambda kind:pytest.fail('Historical download must not republish'))
    server=web.LocalConsoleServer(('127.0.0.1',0),web.Handler);server.console=console
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=http.client.HTTPConnection('127.0.0.1',server.server_port)
        def snapshot(path):
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                client.request('GET',path);response=client.getresponse();data=json.loads(response.read())
                assert response.status==200
                if not data.get('history_loading'):return data
                time.sleep(.01)
            pytest.fail('History background snapshot did not complete')
        assert len(snapshot('/api/history')['runs'])==2
        assert snapshot('/api/history/drill_a')['category_key']=='drill_a'
        client.request('GET','/api/history/drill_a/excel/amazon');response=client.getresponse()
        assert response.status==200 and response.read()==b'drill_a'
        for path in ['/api/history/../secret','/api/history/drill_a/excel/../../secret',
                     '/api/history/drill_a/excel/layer123']:
            client.request('GET',path);response=client.getresponse()
            assert response.status==404;response.read()
        assert console.key=='pen_b'
        client.close()
    finally:server.shutdown();server.server_close();thread.join()


def test_history_http_stays_responsive_during_slow_list_and_detail_reads(tmp_path,monkeypatch):
    saved_run(tmp_path,'drill_a','电钻')
    console=web.Console(tmp_path);started=threading.Event();release=threading.Event();calls=[]
    def slow(kind):
        calls.append(kind);started.set();release.wait(3)
        return {'ok':True,'runs':[]} if kind=='list' else {'ok':True,'category_key':'drill_a'}
    monkeypatch.setattr(console,'history',lambda:slow('list'))
    # The monitor must resolve the current method, as real Console construction does.
    console.history_monitor.loader=console.history
    monkeypatch.setattr(console,'history_status',lambda key:slow('detail'))
    server=web.LocalConsoleServer(('127.0.0.1',0),web.Handler);server.console=console
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=http.client.HTTPConnection(*server.server_address,timeout=1)
        for path in ['/api/history','/api/history','/api/history/drill_a','/api/history/drill_a']:
            client.request('GET',path);response=client.getresponse()
            assert response.status==200 and json.loads(response.read())['history_loading']
        assert started.is_set() and sorted(calls)==['detail','list']
        client.close()
    finally:release.set();server.shutdown();server.server_close();thread.join()


def complete_collection(root):
    base=saved_run(root,'collection_a','测试','完成（存在待补证或失败项）',count=1)
    category=web.selection.read(base/'category.json')
    web.selection.save(base/'category.json',{**category,'execution_mode':'collection'})
    web.selection.save(base/'amazon/amazon_frontend_latest.json',{'status':'COMPLETE','details_complete':True,'records':[{'asin':'A'}]})
    passes=[{'query':'test','sort':'sales'},{'query':'test','sort':'recent'}]
    web.selection.save(base/'temu/supervisors/assignments.json',{'workers':[{'name':'worker-01-new','passes':passes}]})
    web.selection.save(base/'temu/supervisors/worker-01-new.json',{'status':'WORKER_COMPLETE','completed_passes':passes})
    web.selection.save(base/'publish_state.json',{'errors':{'commercial':'市场待补证'}})
    (base/'excel/TEMU_US_测试_关键词采集明细.xlsx').write_bytes(b'temu')
    return base


def test_history_collection_completion_keeps_original_market_status(tmp_path,monkeypatch):
    complete_collection(tmp_path)
    console=web.Console(tmp_path);monkeypatch.setattr(console,'owner',lambda:None)
    row=console.history()['runs'][0]
    assert row['status']=='采集已完成'
    assert row['original_status']=='完成（存在待补证或失败项）'
    result=console.history_status('collection_a')
    assert result['phase']=='采集已完成' and result['severity']=='green'
    assert result['commercial_error']
    assert result['details']['状态']==result['original_status']


@pytest.mark.parametrize('failure',['amazon','worker','pass','excel','publish','stage'])
def test_history_does_not_hide_incomplete_collection(tmp_path,monkeypatch,failure):
    base=complete_collection(tmp_path)
    if failure=='amazon':web.selection.save(base/'amazon/amazon_frontend_latest.json',{'status':'COMPLETE','details_complete':False,'records':[{}]})
    if failure=='worker':web.selection.save(base/'temu/supervisors/worker-01-new.json',{'status':'FAILED'})
    if failure=='pass':web.selection.save(base/'temu/supervisors/worker-01-new.json',{'status':'WORKER_COMPLETE','completed_passes':[]})
    if failure=='excel':(base/'excel/TEMU_US_测试_关键词采集明细.xlsx').unlink()
    if failure=='publish':web.selection.save(base/'publish_state.json',{'errors':{'temu':'导出失败'}})
    if failure=='stage':web.selection.save(base/'status.json',{'状态':'存在阻断','阶段':{'temu_union':{'阻断':True}}})
    console=web.Console(tmp_path);monkeypatch.setattr(console,'owner',lambda:None)
    assert console.history()['runs'][0]['status']!='采集已完成'
    assert console.history_status('collection_a')['phase']!='采集已完成'
