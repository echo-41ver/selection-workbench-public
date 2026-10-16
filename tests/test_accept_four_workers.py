import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from accept_four_workers import report


def write(path,value,lines=False):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(row)+'\n' for row in value) if lines else json.dumps(value),encoding='utf-8')


def evidence(tmp_path):
    before=[];after=[]
    for i in range(4):
        name=f'worker-{i+1:02d}-new'; old=f'old{i}';new=f'new{i}'
        before.append({'name':name,'profile_id':old,'proxy_session_fingerprint':f'fixture-digest-{i}'})
        after.append({'name':name,'profile_id':new})
        for phase,rounds in [('before',2),('after',1)]:
            root=tmp_path/name/phase
            events=[{'type':'IMAGE_BLOCK_GATE_PASSED','navigation_performed':False},{'type':'BLANK_BOOTSTRAP_DWELL'},
                    {'type':'BLANK_BOOTSTRAP_NAVIGATION'},{'type':'EMPTY_SEARCH_QUERY_SUBMITTED'}]
            events += [{'type':'ROUND_COMPLETE','new_goods':2} for _ in range(rounds)]
            write(root/'events.jsonl',events,True)
            write(root/'products.jsonl',[{'goods_id':'kept'}]+([{'goods_id':'new'}] if phase=='after' else []),True)
            write(root/'progress.json',{'status':'RUNNING'})
        write(tmp_path/name/'replacement.json',{'phase':'REPLACED','old_profile_id':old,'new_profile_id':new,'new_proxy_session':True})
    write(tmp_path/'before_manifest.json',{'workers':before});write(tmp_path/'after_manifest.json',{'workers':after})
    return {'workers':after}


def test_complete_fixture_passes(tmp_path):
    assert report(tmp_path,evidence(tmp_path))['status']=='PASSED'


def test_product_totals_cannot_replace_round_evidence(tmp_path):
    manifest=evidence(tmp_path)
    write(tmp_path/'worker-01-new'/'before'/'events.jsonl',[],True)
    assert report(tmp_path,manifest)['status']=='PARTIAL'


def test_captcha_remains_waiting(tmp_path):
    manifest=evidence(tmp_path)
    write(tmp_path/'worker-02-new'/'after'/'progress.json',{'status':'WAITING_HUMAN_VERIFICATION'})
    assert report(tmp_path,manifest)['status']=='WAITING_CAPTCHA'


def test_unknown_proxy_identity_cannot_pass(tmp_path):
    manifest=evidence(tmp_path)
    path=tmp_path/'before_manifest.json'; value=json.loads(path.read_text())
    for worker in value['workers']:worker.pop('proxy_session_fingerprint')
    write(path,value)
    result=report(tmp_path,manifest)
    assert result['status']=='PARTIAL'
    assert not result['checks']['four_distinct_initial_proxy_sessions']


def test_existing_qa_splits_cumulative_events_at_rotation(tmp_path):
    from accept_four_workers import report_existing_qa
    directory=tmp_path/'runs/qa/temu/workers/worker-01-new/keyword'
    phase=[{'type':'IMAGE_BLOCK_GATE_PASSED','navigation_performed':False},
           {'type':'BLANK_BOOTSTRAP_DWELL'},{'type':'BLANK_BOOTSTRAP_NAVIGATION'},
           {'type':'EMPTY_SEARCH_QUERY_SUBMITTED'},{'type':'ROUND_COMPLETE','new_goods':40,'unique_goods':80}]
    write(directory/'events.jsonl',[{**event,'at':'2026-09-25T02:00:00Z'} for event in phase],True)
    write(tmp_path/'qa-before_rotation-proof.json',{'started_at':0,'workers':{'worker-01-new':{'profile_id':'old','session_sha256':'digest','two_page_progress':{'output_directory':str(directory),'unique_goods':80}}}})
    write(tmp_path/'runtime/proxyshare-chains/replacement-worker-1.json',{'phase':'REPLACED','old_profile_id':'old','new_profile_id':'new','new_proxy_session':True,'replaced_at':'2026-09-25T03:00:00Z'})
    result=report_existing_qa(tmp_path,{'workers':[{'name':'worker-01-new','profile_id':'new'}]})
    checks=result['workers'][0]['checks']
    assert checks['before_two_pages'] is True
    assert checks['after_two_pages'] is False
    assert checks['post_rotation_added_goods'] is False
    assert result['status']=='PARTIAL'


def test_latest_replacement_matches_after_multiple_rotations(tmp_path):
    from accept_four_workers import report_existing_qa
    directory=tmp_path/'runs/qa/temu/workers/worker-03-new/query'
    write(tmp_path/'qa-before_rotation-proof.json',{'workers':{'worker-03-new':{'profile_id':'initial','session_sha256':'digest','two_page_progress':{}}}})
    write(tmp_path/'qa-third-two-pages-proof.json',{'before':{'output_directory':str(directory),'unique_goods':80,'started_at':'2026-09-25T02:00:00Z'}})
    write(tmp_path/'runtime/proxyshare-chains/replacement-worker-3.json',{'phase':'REPLACED','old_profile_id':'intermediate','new_profile_id':'latest','new_proxy_session':True,'replaced_at':'2026-09-25T03:00:00Z'})
    result=report_existing_qa(tmp_path,{'workers':[{'name':'worker-03-new','profile_id':'latest'}]})
    worker=result['workers'][0]
    assert worker['checks']['latest_replacement_matches_current'] is True
    assert worker['checks']['initial_to_latest_replacement_chain_verified'] is False
    assert worker['evidence_gaps']
    assert worker['before_goods']==80
    assert worker['before_source']=='qa-third-two-pages-proof.json'
    assert worker['status']=='PARTIAL'
