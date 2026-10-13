import json
import pytest
from selection_automation.local_proxy_prepare import prepare, reuse_prepared
from selection_automation.network_readiness import NetworkNotReady


COMPANY={'proxy_architecture':'LOCAL_VPN','preproxy_mode':'AUTO'}
HTTP={'host':'127.0.0.1','port':7890,'protocol':'HTTP'}


def read(path,default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')


@pytest.fixture
def configured(tmp_path):
    runtime=tmp_path/'runtime'
    save(runtime/'local-proxy-choice.json',{**HTTP,'protocol':'SOCKS5'})
    save(runtime/'local-proxy-next-choice.json',HTTP)
    save(runtime/'secrets/proxyshare.settings.json',{
        'proxy_architecture':'LOCAL_VPN','preproxy_mode':'AUTO',
        'preproxy_host':'127.0.0.1','preproxy_port':7890,'preproxy_protocol':'SOCKS5',
        'local_proxy_owner':{'pid':101,'created':100,'name':'clash-meta.exe'},
        'worker_count':4,'chain_base_port':17891})
    return tmp_path


def observation(protocol='MIXED'):
    return {'status':'READY','candidates':[{'host':'127.0.0.1','port':7890,
        'protocol':protocol,'pid':101,'created':100,'name':'clash-meta.exe'}]}


def adopt(root,**kwargs):
    options={'observation':observation,'business_active':lambda root:False,'check_owner':lambda owner:True}
    options.update(kwargs)
    return prepare(root,COMPANY,read,save,**options)


def test_pending_http_does_not_change_existing_task_route(configured):
    transport,status=reuse_prepared(configured,COMPANY,read,check_owner=lambda owner:True)
    assert transport['preproxy_protocol']=='SOCKS5' and status['route_reused']
    assert read(configured/'runtime/local-proxy-next-choice.json',{})==HTTP
    assert read(configured/'runtime/local-proxy-choice.json',{})['protocol']=='SOCKS5'


def test_idle_new_task_adopts_http_and_keeps_it_for_reuse_and_later_tasks(configured):
    transport,_=adopt(configured)
    assert transport['preproxy_protocol']=='HTTP'
    saved=read(configured/'runtime/secrets/proxyshare.settings.json',{})
    assert saved['worker_count']==4 and saved['chain_base_port']==17891
    assert saved['preproxy_protocol']=='HTTP'
    assert read(configured/'runtime/local-proxy-choice.json',{})==HTTP
    assert read(configured/'runtime/local-proxy-next-choice.json',None)=={}
    assert reuse_prepared(configured,COMPANY,read,check_owner=lambda owner:True)[0]['preproxy_protocol']=='HTTP'
    assert adopt(configured)[0]['preproxy_protocol']=='HTTP'


@pytest.mark.parametrize('active_at_start',[True,False])
def test_pending_choice_is_not_adopted_while_task_is_active(configured,active_at_start):
    checks=iter([active_at_start,True])
    with pytest.raises(NetworkNotReady):
        adopt(configured,business_active=lambda root:next(checks))
    assert read(configured/'runtime/local-proxy-next-choice.json',{})==HTTP
    assert read(configured/'runtime/local-proxy-choice.json',{})['protocol']=='SOCKS5'
    assert read(configured/'runtime/secrets/proxyshare.settings.json',{})['preproxy_protocol']=='SOCKS5'


def test_unavailable_http_keeps_pending_and_current_route(configured):
    with pytest.raises(NetworkNotReady):adopt(configured,observation=lambda:observation('SOCKS5'))
    assert read(configured/'runtime/local-proxy-next-choice.json',{})==HTTP
    assert read(configured/'runtime/local-proxy-choice.json',{})['protocol']=='SOCKS5'
    assert read(configured/'runtime/secrets/proxyshare.settings.json',{})['preproxy_protocol']=='SOCKS5'


def test_interrupted_choice_commit_keeps_pending_for_retry(configured):
    def fail_choice(path,value):
        if path.name=='local-proxy-choice.json':raise OSError('fixture interrupted write')
        save(path,value)
    with pytest.raises(OSError):
        prepare(configured,COMPANY,read,fail_choice,observation=observation,
                business_active=lambda root:False,check_owner=lambda owner:True)
    assert read(configured/'runtime/local-proxy-next-choice.json',{})==HTTP
    assert adopt(configured)[0]['preproxy_protocol']=='HTTP'
    assert reuse_prepared(configured,COMPANY,read,check_owner=lambda owner:True)[0]['preproxy_protocol']=='HTTP'
