from selection_automation.recovery_policy import recovery_budget,recovery_kind,rate_limit_wait,site_access_wait,sort_retry_wait,automatic_retry_stopped
from selection_automation.worker_runtime import transport_recovery_allowed


def test_failed_bootstrap_navigation_reaches_supervisor_transport_recovery():
    progress = {'status':'STOPPED_BOOTSTRAP_NAVIGATION',
                'automatic_recovery_eligible':True,'resume_requires_session_rotation':True}
    assert recovery_kind(progress['status'], progress, transport_recovery_allowed(progress)) == 'TRANSPORT'
    for key in ['automatic_recovery_eligible','resume_requires_session_rotation']:
        incomplete = {k:v for k,v in progress.items() if k != key}
        assert recovery_kind(incomplete['status'], incomplete, transport_recovery_allowed(incomplete)) is None
    for code in [403,429]:
        rejected = {**progress,'http_block':{'status':code},'http_status':code}
        assert not transport_recovery_allowed(rejected)
        assert recovery_kind(rejected['status'],rejected,transport_recovery_allowed(rejected)) == ('ACCESS' if code == 403 else 'RATE_LIMIT')
    login = {**progress,'confirmed_login_redirect':True}
    assert recovery_kind(login['status'],login,False) == 'LOGIN'
    assert recovery_kind('WAITING_SEARCH_VERIFICATION',progress,True) is None


def test_login_requests_session_replacement_even_with_legacy_manual_flags():
    flags = {'manual_action_required': True, 'automatic_retry': False}
    assert recovery_kind('STOPPED_LOGIN_REQUIRED', flags, False) == 'LOGIN'
    assert recovery_kind('STOPPED_HTTP_403', {**flags, 'confirmed_login_redirect': True}, False) == 'LOGIN'
    assert recovery_kind('STOPPED_PROXY_OR_NETWORK_FAILURE', {'confirmed_login_redirect': True}, True) == 'LOGIN'


def test_site_refusal_does_not_reset_no_progress_on_failure_type_change():
    previous = {'kind': 'TRANSPORT', 'attempts_without_progress': 3, 'unique_goods_before': 0}
    allowed, record = recovery_budget(previous, 0, 'profile-1', 'LOGIN')
    assert allowed and record['attempts_without_progress'] == 4
    allowed, record = recovery_budget(record, 0, 'profile-2', 'RATE_LIMIT')
    assert allowed and record['attempts_without_progress'] == 5
    assert site_access_wait('ACCESS',record,2)==7211
    assert recovery_budget(record, 1, 'profile-3', 'LOGIN')[1]['attempts_without_progress']==1
    allowed, transport = recovery_budget(record, 0, 'profile-3', 'TRANSPORT')
    assert allowed


def test_session_replacements_have_no_cumulative_limit_and_captcha_is_preserved():
    previous = {'kind':'ACCESS','attempts_without_progress':50,'unique_goods_before':10}
    for kind in ('LOGIN','ACCESS','RATE_LIMIT','CAPTCHA','TRANSPORT','INITIAL_PRODUCTS','SORT','CONTROL'):
        allowed, record = recovery_budget(previous,10,'new-profile',kind)
        assert allowed and record['limit'] is None
        allowed, progressed = recovery_budget(record,11,'newer-profile',kind)
        assert allowed and progressed['attempts_without_progress'] == 1
    assert recovery_kind('WAITING_CAPTCHA',{'scroll_collection_started':True},True) is None
    assert recovery_kind('WAITING_CAPTCHA',{},True) is None


def test_site_access_backoff_staggers_workers_and_does_not_block_captcha():
    for attempts in (0,2,6,100):
        assert site_access_wait('LOGIN',{'attempts_without_progress':attempts},1)==0
        assert site_access_wait('LOGIN',{'attempts_without_progress':attempts},32)==0
    assert site_access_wait('ACCESS',{'attempts_without_progress':2},4)==633
    assert site_access_wait('RATE_LIMIT',{'attempts_without_progress':3},1)==0
    assert site_access_wait(None,{},1)==0


def test_all_requested_site_gates_reach_automatic_recovery():
    for code in ['STOPPED_HTTP_403','STOPPED_ACCESS_DENIED']:
        assert recovery_kind(code,{},True) == 'ACCESS'
    assert recovery_kind('STOPPED_HTTP_429',{},True) == 'RATE_LIMIT'
    for code in ['WAITING_CAPTCHA','CAPTCHA_REQUIRED','WAITING_SEARCH_VERIFICATION']:
        assert recovery_kind(code,{'scroll_collection_started':True},False) is None
    assert recovery_kind('STOPPED_PROXY_OR_NETWORK_FAILURE',{'automatic_recovery_eligible':False},True) is None
    assert recovery_kind('STOPPED_PROXY_OR_NETWORK_FAILURE',{},True)=='TRANSPORT'


def test_cooldown_is_durable_bounded_and_respects_server_delay():
    p={'status':'STOPPED_HTTP_429','http_block':{'retry_after_seconds':180}}
    first=rate_limit_wait(p,{},now=1000)
    assert first['wait_seconds']==180
    second=rate_limit_wait(p,first,now=1010)
    assert second['attempts']==2 and second['wait_seconds']>=170
    assert rate_limit_wait(p,second,now=2000)['wait_seconds']==240
    assert rate_limit_wait(p,{'attempts':20},now=2000)['wait_seconds']==900
    assert rate_limit_wait({**p,'http_block':{'retry_after_seconds':3600}},{'attempts':20},now=2000)['wait_seconds']==3600
    assert rate_limit_wait({**p,'confirmed_login_redirect':True},{},now=0)['wait_seconds']==180
    assert rate_limit_wait({'status':'STOPPED_HTTP_429','http_block':None},{},now=0)['wait_seconds']==60
    assert rate_limit_wait({'status':'STOPPED_HTTP_429','unique_goods':10},
                           {'attempts':3,'unique_goods_before':9},now=0)['attempts']==1


def test_sort_retries_are_local_bounded_and_ignore_login_budget():
    p={'status':'STOPPED_SALES_SORT_UNCONFIRMED','unique_goods':40}
    record={}
    for delay in [5,15,30]:
        record=sort_retry_wait(p,record)
        assert record['wait_seconds']==delay
    assert sort_retry_wait(p,record) is None
    assert sort_retry_wait({**p,'unique_goods':80},record)['attempts']==1
    assert sort_retry_wait({**p,'http_block':{'status':403}},{}) is None


def test_try_again_exhaustion_rotates_automatically_without_losing_progress():
    p={'status':'STOPPED_TRY_AGAIN_STALLED','unique_goods':311,
       'consecutive_try_again_no_progress':3,'automatic_recovery_eligible':True}
    assert recovery_kind(p['status'],p,False)=='TRANSPORT'
    assert p['unique_goods']==311

def test_search_and_verification_429_both_request_rotation():
    p={'automatic_retry':False,'manual_action_required':True,
       'http_block':{'document_endpoint':'https://www.temu.com/search_result.html'}}
    assert recovery_kind('STOPPED_HTTP_429',p,False) == 'RATE_LIMIT'
    p['http_block']['document_endpoint']='https://www.temu.com/bgn_verification.html'
    assert recovery_kind('STOPPED_HTTP_429',p,False) == 'RATE_LIMIT'


def test_startup_captcha_remains_manual_even_with_old_checkpoint_and_transport_flags():
    for status in ('WAITING_CAPTCHA','CAPTCHA_REQUIRED','WAITING_SEARCH_VERIFICATION',
                   'WAITING_EMPTY_SEARCH_VERIFICATION','WAITING_HUMAN_VERIFICATION'):
        for marker in (None,False,1,'true'):
            progress={'status':status,'scroll_collection_started':marker,'unique_goods':1504,'round':37,
                      'automatic_recovery_eligible':True,'automatic_retry':True,
                      'http_block':{'status':429},'confirmed_login_redirect':True}
            assert recovery_kind(status,progress,True) is None
            assert automatic_retry_stopped(progress)
            progress['scroll_collection_started']=True
            assert recovery_kind(status,progress,False)is None
            assert automatic_retry_stopped(progress)
