from copy import deepcopy
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation.stage3_runtime import update_control,dispatch_allowed,read_control
from selection_automation.stage3_provider import apply,settings
from selection_automation.vision import compatible_vision_keys


def test_pause_one_agent_preserves_other_dispatch_and_saved_state(tmp_path):
    update_control(tmp_path,paused_agents=['acciowork'])
    assert not dispatch_allowed(tmp_path,'acciowork')
    assert dispatch_allowed(tmp_path,'doubao')
    update_control(tmp_path,paused=True)
    assert not dispatch_allowed(tmp_path,'doubao')
    update_control(tmp_path,paused=False)
    assert read_control(tmp_path)['paused_agents']==['acciowork']
    assert dispatch_allowed(tmp_path,'doubao')
    update_control(tmp_path,switch_pending=True)
    assert not dispatch_allowed(tmp_path,'doubao')


def test_manual_provider_concurrency_has_no_fixed_ceiling():
    for mode in ('local_agent','liquid_d1','multimodal_api','local_model'):
        provider=settings({'mode':mode,'workers':1024,'concurrency_mode':'manual'})
        assert provider['workers']==1024 and provider['concurrency_mode']=='manual'


def test_live_concurrency_does_not_require_model_catalog_or_release_pause(tmp_path,monkeypatch):
    import web_console as web
    from selection_automation import local_agents,agent_models
    category={'category_key':'fixture','stage3':{'modules':{'design':100}}}
    provider=settings({'mode':'local_agent','agent_ids':['doubao'],'agent_models':{'doubao':'豆包 专家'},'workers':2,'concurrency_mode':'manual'})
    apply(category,provider);base=tmp_path/'runs/fixture';base.mkdir(parents=True)
    web.selection.save(base/'category.json',category)
    web.selection.save(base/'stage123/codex_pool_config.json',{'workers':2,'enabled':True,'agent_ids':['doubao'],
        'agent_models':provider['agent_models'],'group_size':12,'agent_backends':[{'id':'doubao','max_parallel':2}]})
    update_control(base/'stage123',paused=True,paused_agents=['acciowork'],agent_blocks={'doubao':{'code':'HUMAN_VERIFICATION_REQUIRED'}})
    original=read_control(base/'stage123')
    console=web.Console(tmp_path);monkeypatch.setattr(console,'current_run_key',lambda:('fixture',True))
    monkeypatch.setattr(local_agents,'ready_agents',lambda *a,**kw:pytest.fail('Model probe started'))
    monkeypatch.setattr(agent_models,'validate_selection',lambda *a:pytest.fail('Model catalog required'))
    for width in (400,1):
        result=console.set_stage3_concurrency({'workers':width,'concurrency_mode':'manual'})
        assert result['provider']['workers']==width
        pool=web.selection.read(base/'stage123/codex_pool_config.json')
        assert pool['enabled'] and pool['workers']==pool['agent_backends'][0]['max_parallel']==width
        assert pool['agent_ids']==['doubao'] and pool['agent_models']==provider['agent_models'] and pool['group_size']==12
        assert read_control(base/'stage123')==original and not (base/'stage123/STOP').exists()


@pytest.mark.parametrize('mode',['liquid_d1','multimodal_api','local_model'])
@pytest.mark.parametrize('paused',[False,True])
def test_live_api_concurrency_drains_known_requests_and_preserves_pause(tmp_path,monkeypatch,mode,paused):
    import web_console as web
    from selection_automation import stage3_progress
    category={'category_key':'fixture','stage3':{'modules':{'design':100}}}
    apply(category,settings({'mode':mode,'workers':2,'concurrency_mode':'manual'}))
    base=tmp_path/'runs/fixture';base.mkdir(parents=True);web.selection.save(base/'category.json',category)
    update_control(base/'stage123',paused=paused)
    console=web.Console(tmp_path);monkeypatch.setattr(console,'current_run_key',lambda:('fixture',True))
    monkeypatch.setattr(stage3_progress,'controller_alive',lambda base:True)
    calls=[];monkeypatch.setattr(console,'queue_stage3_switch',lambda key,base,p,**kw:calls.append((p,kw)))
    result=console.set_stage3_concurrency({'workers':400,'concurrency_mode':'manual'})
    assert result['switch_pending'] and calls[0][0]['workers']==400
    assert calls[0][1]['configure_only']==paused
    assert (base/'stage123/STOP').exists()
    assert web.selection.read(base/'category.json')['stage3']['provider']['workers']==2
    assert read_control(base/'stage123')['pending_provider']['model']==category['stage3']['model']


def test_explicit_continue_clears_selected_verification_but_preserves_manual_pauses(tmp_path):
    from selection_automation.stage3_runtime import block_agent,resume_dispatch
    update_control(tmp_path,paused=True,paused_agents=['acciowork','codex'])
    block_agent(tmp_path,'doubao','HUMAN_VERIFICATION_REQUIRED','verification')
    block_agent(tmp_path,'claude','LOGIN_REQUIRED','login')
    value=resume_dispatch(tmp_path,['doubao','claude','codex'])
    assert not value['paused'] and dispatch_allowed(tmp_path,'doubao')
    assert set(value['paused_agents'])=={'acciowork','codex','claude'}
    assert set(value['agent_blocks'])=={'claude'}


def test_global_continue_route_releases_the_selected_agent_gate(tmp_path,monkeypatch):
    import web_console as web
    from selection_automation.stage3_runtime import block_agent
    cat={'category_key':'fixture','stage3':{'modules':{'design':100}}}
    apply(cat,settings({'mode':'local_agent','agent_ids':['doubao']}))
    base=tmp_path/'runs/fixture';base.mkdir(parents=True);web.selection.save(base/'category.json',cat)
    update_control(base/'stage123',paused=True,paused_agents=['acciowork'])
    block_agent(base/'stage123','doubao','HUMAN_VERIFICATION_REQUIRED','verification')
    console=web.Console(tmp_path);monkeypatch.setattr(console,'current_run_key',lambda:('fixture',True))
    monkeypatch.setattr(web,'codex_review_control',lambda base:{'running':True})
    result=console.control_stage3({'action':'resume'})
    assert result['ok'] and dispatch_allowed(base/'stage123','doubao')
    assert read_control(base/'stage123')['paused_agents']==['acciowork']


def test_continue_start_route_releases_existing_agent_gate_without_new_controller(tmp_path,monkeypatch):
    import web_console as web
    from selection_automation.stage3_runtime import block_agent
    cat={'category_key':'fixture','stage3':{'modules':{'design':100}}}
    provider=settings({'mode':'local_agent','agent_ids':['doubao']});apply(cat,provider)
    base=tmp_path/'runs/fixture';base.mkdir(parents=True);web.selection.save(base/'category.json',cat)
    block_agent(base/'stage123','doubao','HUMAN_VERIFICATION_REQUIRED','verification')
    console=web.Console(tmp_path);monkeypatch.setattr(console,'current_run_key',lambda:('fixture',True))
    monkeypatch.setattr(web,'codex_review_control',lambda base:{'running':True})
    result=console.start_stage3({'provider':provider})
    assert result['ok'] and dispatch_allowed(base/'stage123','doubao')


def test_explicit_provider_switch_reuses_unchanged_evidence_only(tmp_path):
    from test_identity_policy import category
    from PIL import Image
    cat=category();picture=tmp_path/'image.png';Image.new('RGB',(20,20),'red').save(picture)
    pair={'asin':'TEST_A','goods_id':'TEST_T','pair_revision':'p','amazon_image_path':str(picture),'temu_image_path':str(picture)}
    apply(cat,settings({'mode':'local_agent','agent_ids':['acciowork'],'agent_models':{'acciowork':'native-one'}}))
    old=compatible_vision_keys(cat,pair)[0]
    apply(cat,settings({'mode':'local_agent','agent_ids':['doubao'],'agent_models':{'doubao':'native-two'}}))
    keys=compatible_vision_keys(cat,pair)
    assert old in keys and keys[0]!=old
    changed=deepcopy(cat);changed['stage3']['visual_instructions']='TEST_ONLY_NEW_IDENTITY_RULE'
    assert old not in compatible_vision_keys(changed,pair)
    Image.new('RGB',(20,20),'blue').save(picture)
    assert old not in compatible_vision_keys(cat,pair)


def test_previous_decision_result_survives_switch_to_agent():
    from selection_automation.rules import score
    cat={'stage3':{'modules':{'design':100}}}
    apply(cat,settings({'mode':'liquid_d1','confidence':.9}))
    facts={'decision_api':{'choice':'different','probabilities':{'same':.01,'similar':.01,'different':.98}}}
    assert score(cat,facts)['state']=='不同款'
    apply(cat,settings({'mode':'local_agent','agent_ids':['doubao']}))
    assert score(cat,facts)['state']=='不同款'


def test_cross_protocol_switch_reuses_paid_key_only_when_identity_rules_and_images_match(tmp_path):
    from test_identity_policy import category
    from PIL import Image
    cat=category();picture=tmp_path/'original.png';Image.new('RGB',(20,20),'red').save(picture)
    pair={'asin':'TEST_A','goods_id':'TEST_T','pair_revision':'p','amazon_image_path':str(picture),'temu_image_path':str(picture)}
    apply(cat,settings({'mode':'liquid_d1'}));old=compatible_vision_keys(cat,pair)[0]
    apply(cat,settings({'mode':'local_agent','agent_ids':['doubao']}))
    assert old in compatible_vision_keys(cat,pair)
    changed=deepcopy(cat);changed['stage3']['visual_instructions']='TEST_CHANGED_IDENTITY_RULE'
    assert old not in compatible_vision_keys(changed,pair)
    Image.new('RGB',(20,20),'blue').save(picture)
    assert old not in compatible_vision_keys(cat,pair)


def test_zero_placeholder_with_clear_facts_is_retryable_not_unknown():
    import pytest
    from test_set_inventory_identity import configured,facts as make_facts
    from selection_automation.codex_visual_review import checked_facts
    cat=configured();facts=make_facts(cat);facts['overall_confidence']=0
    with pytest.raises(ValueError,match='占位值'):checked_facts(cat,facts,'TEST_T')


def test_screening_alone_never_counts_as_different():
    from selection_automation.commercial import match_metric,settings
    result=match_metric({'confirmed':2,'similar':1,'different':3,'possible':1,'pending':2,'screened_out':4},13,settings())
    assert result['different']==3 and result['unanalysed']==6
    assert sum(result[k] for k in ('confirmed','similar','different','possible','unanalysed'))==13


def test_live_switch_drains_before_replacing_config_and_preserves_checkpoint(tmp_path,monkeypatch):
    import threading,json
    import web_console as web
    import selection_automation.stage3_progress as progress
    cat={'category_key':'fixture','stage3':{'modules':{'design':100}}}
    first=settings({'mode':'multimodal_api','model':'TEST_MODEL_ONE'});apply(cat,first)
    target=settings({'mode':'multimodal_api','model':'TEST_MODEL_TWO'})
    base=tmp_path/'runs/fixture';base.mkdir(parents=True);web.selection.save(base/'category.json',cat)
    console=web.Console(tmp_path);console.stage3_keys[('fixture',first['base_url'])]='TEST_ONLY_KEY'
    monkeypatch.setattr(console,'current_run_key',lambda:('fixture',True))
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':12,'pending':12})
    alive=[True];monkeypatch.setattr(progress,'controller_alive',lambda base:alive[0])
    monkeypatch.setattr(web,'codex_review_control',lambda base:{'running':False})
    done=threading.Event();started=[];control_at_launch=[]
    def launch(*args):
        control_at_launch.append(read_control(base/'stage123'))
        started.append(args);done.set()
    monkeypatch.setattr(console,'launch_stage3_controller',launch)
    result=console.start_stage3({'provider':target})
    assert result['switch_pending'] and read_control(base/'stage123')['switch_pending']
    assert web.selection.read(base/'category.json')['stage3']['model']=='TEST_MODEL_ONE'
    assert not started
    alive[0]=False;assert done.wait(5)
    saved=web.selection.read(base/'category.json')
    assert saved['stage3']['model']=='TEST_MODEL_TWO'
    assert any(stage.get('model')=='TEST_MODEL_ONE' for stage in saved['stage3_checkpoint_sources'])
    assert control_at_launch[0]['pending_provider'] is None
    assert not read_control(base/'stage123')['switch_pending']
    assert 'TEST_ONLY_KEY' not in json.dumps(saved)


def test_pool_keeps_twelve_candidates_together_across_old_shards(tmp_path,monkeypatch):
    import run_codex_visual_pool as pool
    from test_codex_visual_pool import configured,fixture
    category=tmp_path/'category.json';category.write_text('{}')
    monkeypatch.setattr(pool,'load_category',lambda _:configured())
    pair,_=fixture();rows=[{**pair,'goods_id':str(i)} for i in range(12)]
    tasks=[{'task_id':f'task{i}','rows':[row],'output':str(tmp_path/f'{i}.jsonl')} for i,row in enumerate(rows)]
    monkeypatch.setattr(pool,'tasks',lambda base:iter(tasks))
    monkeypatch.setattr(pool,'pending_rows',lambda category,task:task['rows'])
    monkeypatch.setattr(pool,'ensure_publisher',lambda *a,**kw:None)
    worker=pool.Pool(category,{'workers':1,'group_size':12});worker.last_prepare=pool.time.monotonic()
    dispatched=[]
    monkeypatch.setattr(worker,'launch',lambda task,group:dispatched.append((task,group)) or len(group))
    worker.tick()
    assert len(dispatched)==1 and len(dispatched[0][1])==12
    assert len(dispatched[0][0]['members'])==12


@pytest.mark.parametrize('workers',[2,24,400])
@pytest.mark.parametrize('saved_first',[False,True])
def test_native_agent_change_applies_to_new_groups_while_old_jobs_settle(tmp_path,monkeypatch,workers,saved_first):
    import web_console as web
    import run_codex_visual_pool as pool
    import selection_automation.stage3_progress as progress
    from selection_automation import local_agents,agent_models
    cat={'category_key':'fixture','stage3':{'modules':{'design':100}}}
    apply(cat,settings({'mode':'local_agent','agent_ids':['acciowork','doubao'],'concurrency_mode':'manual','workers':2}))
    base=tmp_path/'runs/fixture';base.mkdir(parents=True);web.selection.save(base/'category.json',cat)
    console=web.Console(tmp_path)
    monkeypatch.setattr(console,'current_run_key',lambda:('fixture',True))
    monkeypatch.setattr(console,'stage3_snapshot',lambda key:{'total':24,'pending':12})
    monkeypatch.setattr(progress,'controller_alive',lambda base:False)
    monkeypatch.setattr(web,'codex_review_control',lambda base:{'running':True})
    agents=[{'id':'doubao','installed':True,'max_parallel':6}]
    monkeypatch.setattr(local_agents,'discover',lambda root:agents)
    monkeypatch.setattr(local_agents,'ready_agents',lambda *args,**kw:agents)
    monkeypatch.setattr(agent_models,'validate_selection',lambda *args:None)
    starts=[];monkeypatch.setattr(pool,'ensure_pool',lambda *args:starts.append(args))
    target=settings({'mode':'local_agent','agent_ids':['doubao'],'agent_models':{'doubao':'TEST_FAST_MODEL'},'workers':workers,'concurrency_mode':'manual'})
    if saved_first:
        apply(cat,target);web.selection.save(base/'category.json',cat)
        web.selection.save(base/'stage123/codex_pool_config.json',{'workers':2,'agent_ids':['acciowork','doubao']})
    result=console.start_stage3({'provider':target})
    assert not result.get('switch_pending') and len(starts)==1
    assert not (base/'stage123/STOP').exists()
    assert web.selection.read(base/'stage123/codex_pool_config.json')['agent_ids']==['doubao']
    saved=web.selection.read(base/'stage123/codex_pool_config.json')
    assert saved['workers']==saved['agent_backends'][0]['max_parallel']==workers
    assert web.selection.read(base/'category.json')['stage3']['provider']==target


def test_new_model_does_not_reallocate_an_unfinished_old_model_pair(tmp_path,monkeypatch):
    import prepare_codex_visual_batch as prepare
    from test_set_inventory_identity import configured
    category=tmp_path/'category.json';category.write_text('{}')
    monkeypatch.setattr(prepare,'load_category',lambda path:configured())
    monkeypatch.setattr(prepare,'_stage12_candidates',lambda *args:iter([{'asin':'A','goods_id':'T','facts':{}}]))
    monkeypatch.setattr(prepare,'admitted',lambda *args:True)
    monkeypatch.setattr(prepare,'_image_status',lambda row:[])
    monkeypatch.setattr(prepare,'compatible_vision_keys',lambda *args,**kw:('NEW_MODEL_KEY','OLD_MODEL_KEY'))
    monkeypatch.setattr(prepare,'_existing_current_results',lambda *args:set())
    monkeypatch.setattr(prepare,'_unfinished_allocations',lambda base:{('A','T','OLD_MODEL_KEY')})
    assert prepare.prepare_batch(category,skip_empty=True)['selected']==0
