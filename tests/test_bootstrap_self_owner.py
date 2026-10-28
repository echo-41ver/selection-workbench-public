"""The new supervisor owns startup, but must not block retirement of an ended task."""
import json
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import company_setup
import selection
import retain_proxy_connections
import setup_proxyshare_adspower_workers as setup


def fixture(monkeypatch,tmp_path,owner):
    monkeypatch.setattr(company_setup,'ROOT',tmp_path)
    monkeypatch.setattr(selection,'identity',lambda:'mine')
    monkeypatch.setattr(selection,'read',lambda *args:{'task_key':'old','machine_id':'mine','workers':[]})
    monkeypatch.setattr(selection,'live_owner',lambda:owner)
    monkeypatch.setattr(selection,'save',lambda *args:None)
    monkeypatch.setattr(retain_proxy_connections,'owned_chains',lambda root:{})
    monkeypatch.setattr(retain_proxy_connections,'stop_owned',lambda root:None)
    retired=[]
    monkeypatch.setattr(setup,'retire_for_task',retired.append)
    return retired


def test_new_starting_supervisor_can_retire_ended_previous_task(monkeypatch,tmp_path):
    retired=fixture(monkeypatch,tmp_path,{'category_key':'new'})
    assert company_setup.cleanup_ended_task('old',preparing_task='new') is True
    assert retired==['new']


@pytest.mark.parametrize('owner,preparing',[
    ({'category_key':'old'},'new'),({'category_key':'other'},'new'),
    ({'category_key':'new'},None),({'category_key':'old'},'old'),
])
def test_live_task_remains_protected(monkeypatch,tmp_path,owner,preparing):
    retired=fixture(monkeypatch,tmp_path,owner)
    with pytest.raises(RuntimeError,match='监督器仍在运行'):
        company_setup.cleanup_ended_task('old',preparing_task=preparing)
    assert retired==[]


@pytest.mark.parametrize('machine',['mine','other'])
def test_ended_partial_bootstrap_cleanup_uses_owned_checkpoint(monkeypatch,tmp_path,machine):
    retired=fixture(monkeypatch,tmp_path,{'category_key':'new'})
    checkpoint={'task_key':'old','machine_id':machine,'workers':[],'proxy_sessions_ready':True}
    monkeypatch.setattr(selection,'read',lambda path,default:checkpoint if path.name=='bootstrap.json' else default)
    writes=[]
    monkeypatch.setattr(selection,'save',lambda path,value:writes.append((path,value.copy())))
    if machine=='other':
        with pytest.raises(RuntimeError,match='其他安装'):
            company_setup.cleanup_ended_task('old',preparing_task='new')
        assert not writes and not retired
    else:
        assert company_setup.cleanup_ended_task('old',preparing_task='new') is True
        assert writes==[(tmp_path/'runtime/bootstrap.json',{**checkpoint,'used':True})]
        assert retired==['new']


@pytest.mark.parametrize('status,cleaned',[('已停止',True),('完成',True),('等待人工验证',False)])
def test_new_task_releases_only_ended_partial_bootstrap_before_route_prepare(monkeypatch,tmp_path,status,cleaned):
    from selection_automation.proxy_activity import ProxyActivityLease
    from selection_automation.route_guard import collection_active
    import selection_automation.local_proxy_prepare as routes
    secret=tmp_path/'runtime/secrets/company.dpapi'
    secret.parent.mkdir(parents=True);secret.touch()
    checkpoint=tmp_path/'runtime/bootstrap.json'
    checkpoint.write_text(json.dumps({'task_key':'old','machine_id':'mine','workers':[],
                                      'proxy_sessions_ready':True}))
    state=tmp_path/'runs/old/status.json'
    state.parent.mkdir(parents=True);state.write_text(json.dumps({'状态':status}))
    monkeypatch.setattr(company_setup,'ROOT',tmp_path)
    monkeypatch.setattr(company_setup,'load',lambda _:{'adspower_api_key':'TEST_ONLY'})
    monkeypatch.setattr(selection,'identity',lambda:'mine')
    monkeypatch.setattr(selection,'live_owner',lambda:{'category_key':'new'})
    monkeypatch.setattr(retain_proxy_connections,'owned_chains',lambda root:{})
    events=[]
    import replace_proxyshare_worker_environment as api_module
    api_ready=[False]
    def api(base,path,**kwargs):
        assert base=='http://127.0.0.1:50325'
        if path=='/status':
            events.append('api-status')
            if not api_ready[0]:raise RuntimeError('TEST_ONLY_NOT_RUNNING')
        else:events.append('api-auth')
        return {'code':0}
    def launch(*args,**kwargs):
        events.append('api-launch');api_ready[0]=True
    monkeypatch.setattr(api_module,'api_call',api)
    monkeypatch.setattr(company_setup,'find_adspower_executable',lambda _:str(tmp_path/'AdsPower.exe'))
    monkeypatch.setattr(company_setup.subprocess,'Popen',launch)
    with ProxyActivityLease(tmp_path,1,purpose='manual_verification') as lease:
        def stop_helper(root):
            events.append('stop-helper');lease.__exit__(None,None,None)
        monkeypatch.setattr(retain_proxy_connections,'stop_owned',stop_helper)
        def retire(task):
            assert api_ready[0]
            events.append(('retire',task));checkpoint.unlink()
        monkeypatch.setattr(setup,'retire_for_task',retire)
        def prepare(*args):
            events.append(('prepare',collection_active(tmp_path)))
            raise RuntimeError('test reached route preparation')
        monkeypatch.setattr(routes,'prepare',prepare)
        with pytest.raises(RuntimeError,match='test reached route preparation'):
            company_setup.ensure_company(True,new_task='new',worker_count=1)
    assert events==['api-status','api-launch','api-status','api-auth']+(
        ['stop-helper',('retire','new'),('prepare',False)] if cleaned else [('prepare',True)])
    assert checkpoint.exists() is not cleaned
    assert not (tmp_path/'config/sessions.json').exists()
