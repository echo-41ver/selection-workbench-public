import json
import pytest
from selection_automation.analysis_concurrency import recommendation,record_sample,profile_path,stable_best
from selection_automation.stage3_provider import settings


def sample(width,seconds=10,failed=0):
    return {'workers':width,'completed':max(8,width*2),'failed':failed,'seconds':seconds,'saturated':True}


def test_highest_stable_throughput_wins_not_highest_width():
    rows=[sample(10),sample(10),sample(20,40),sample(20,40),sample(200,1),sample(200,1,failed=1)]
    assert stable_best(rows)['best_width']==10
    assert stable_best([sample(10)]) is None
    assert stable_best([sample(10),sample(10),sample(10,failed=1)]) is None
    assert stable_best([None,sample(10,seconds=float('nan'))]) is None
    assert stable_best([{**sample(10),'saturated':False}]*2) is None
    assert stable_best([{k:v for k,v in sample(10).items() if k!='saturated'}]*2) is None


def test_profiles_separate_agents_models_and_api_providers(tmp_path):
    codex={'mode':'local_agent','agent_ids':['codex'],'agent_models':{'codex':'native-model'}}
    for _ in range(2):record_sample(codex,10,30,0,10,root=tmp_path)
    assert recommendation(codex,tmp_path)['workers']==10
    assert not recommendation({**codex,'agent_ids':['doubao']},tmp_path)['measured']
    assert not recommendation({**codex,'agent_models':{'codex':'other-model'}},tmp_path)['measured']
    api=settings({'mode':'multimodal_api'})
    for _ in range(2):record_sample(api,200,500,0,60,root=tmp_path)
    assert recommendation(api,tmp_path)['workers']==200
    assert not recommendation({**api,'base_url':'https://another.example'},tmp_path)['measured']


def test_default_does_not_replace_manual_values_or_trust_stale_best_rate(tmp_path,monkeypatch):
    provider=settings({'mode':'liquid_d1'})
    path=tmp_path/'runs/task/stage123/concurrency.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'provider':{k:provider[k] for k in ('mode','base_url','model','protocol')},
        'best_width':200,'best_rate':100,'samples':[sample(200,failed=1)]}))
    assert recommendation(provider,tmp_path)=={'workers':4,'measured':False,'message':'默认 4 路：暂无足够稳定实测，沿用起始值；可手动修改。'}
    path.write_text(json.dumps({'provider':{k:provider[k] for k in ('mode','base_url','model','protocol')},'samples':[sample(12),sample(12)]}))
    assert recommendation(provider,tmp_path)['workers']==12
    record_sample(provider,4,8,0,100,root=tmp_path)
    assert recommendation(provider,tmp_path)['workers']==12  # Imported history survives the first new sample.
    monkeypatch.setattr('selection_automation.analysis_concurrency.recommended',lambda *a,**kw:200)
    assert settings({'workers':997,'concurrency_mode':'manual'})['workers']==997
    assert settings({})['workers']==200


def test_only_saturated_windows_are_used_and_statistics_do_not_change_jobs(tmp_path):
    provider={'mode':'local_agent','agent_ids':['codex']}
    record_sample(provider,999,2000,0,1,saturated=False,root=tmp_path)
    assert not profile_path(provider,tmp_path).exists()
    assert not recommendation(provider,tmp_path)['measured']


def test_restart_preserves_failed_auto_width_and_does_not_repeat_exploration():
    from selection_automation.analysis_concurrency import restore_tuner
    prior={'workers':1,'best_width':2,'best_rate':999,'exploring':False,
           'samples':[{'workers':2,'completed':0,'failed':12,'seconds':600,'saturated':True}]}
    tuner=restore_tuner(2,4,prior)
    assert tuner.width==1 and tuner.best_width==1 and tuner.best_rate==0
    assert tuner.observe(12,0,20)==1 and not tuner.exploring
    assert restore_tuner(2,4,{}).width==2


@pytest.mark.parametrize('width',[0,True,'1',999])
def test_invalid_backoff_state_cannot_change_dispatch_width(width):
    from selection_automation.analysis_concurrency import restore_tuner
    tuner=restore_tuner(2,4,{'workers':width,'exploring':False,'samples':[sample(2,failed=1)]})
    assert tuner.width==2


def test_clean_completed_samples_still_choose_the_fastest_stable_width():
    from selection_automation.analysis_concurrency import restore_tuner
    tuner=restore_tuner(4,100,{'workers':1,'exploring':False,'samples':[sample(10),sample(10)]})
    assert tuner.width==10
