import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_codex_visual_pool as pool


def test_pool_dispatches_different_agents_and_binds_process_ownership(tmp_path,monkeypatch):
    import psutil
    category=tmp_path/'category.json';category.write_text('{}')
    monkeypatch.setattr(pool,'load_category',lambda _:configured())
    monkeypatch.setattr(pool,'_contact_sheets',lambda *a,**kw:None)
    monkeypatch.setattr(pool,'bind_original_images',lambda *a:None)
    class Child:
        pid=12345
        class Stdin:
            def write(self,value):pass
            def close(self):pass
        stdin=Stdin()
    commands=[]
    monkeypatch.setattr(pool.subprocess,'Popen',lambda args,**kw:commands.append((args,kw['cwd'])) or Child())
    monkeypatch.setattr(psutil,'Process',lambda _:SimpleNamespace(create_time=lambda:1.0))
    monkeypatch.setattr('selection_automation.local_agents.analysis_command',lambda agent,job,*args:[agent['id'],str(job)])
    agents=[{'id':'codex','name':'Codex CLI'},{'id':'claude','name':'Claude Code'}]
    worker=pool.Pool(category,{'workers':2,'agent_backends':agents})
    pair,_=fixture()
    worker.launch({'task_id':'one'},[pair])
    worker.launch({'task_id':'two'},[{**pair,'goods_id':'other'}])
    assert [command[0][0] for command in commands]==['codex','claude']
    assert {job['agent_id'] for job in worker.jobs.values()}=={'codex','claude'}
    assert all(str(cwd) in args for args,cwd in commands)
    assert all(job.get('command_marker') for job in worker.jobs.values())


def test_doubao_dispatches_whole_twelve_pair_groups_into_independent_slots(tmp_path,monkeypatch):
    import psutil
    category=tmp_path/'category.json';category.write_text('{}')
    monkeypatch.setattr(pool,'load_category',lambda _:configured())
    bound=[]
    monkeypatch.setattr(pool,'_contact_sheets',lambda *a,**kw:None)
    monkeypatch.setattr(pool,'bind_original_images',lambda job,rows:bound.append(rows))
    class Input:
        def write(self,value):pass
        def close(self):pass
    monkeypatch.setattr(pool.subprocess,'Popen',lambda *a,**kw:SimpleNamespace(pid=12345,stdin=Input()))
    monkeypatch.setattr(psutil,'Process',lambda _:SimpleNamespace(create_time=lambda:1.0))
    monkeypatch.setattr('selection_automation.local_agents.analysis_command',lambda agent,job,*a:['native',str(job)])
    worker=pool.Pool(category,{'workers':10,'agent_backends':[{'id':'doubao','name':'豆包','max_parallel':2,'max_group_size':12}]})
    pair,_=fixture()
    assert worker.launch({'task_id':'one'},[{**pair,'goods_id':str(index)} for index in range(13)])==12
    job=next(iter(worker.jobs.values()))
    assert len(job['identities'])==len(bound[0])==12
    assert worker.launch({'task_id':'two'},[{**pair,'goods_id':'other'}])==1
    with pytest.raises(RuntimeError,match='槽位'):worker.launch({'task_id':'two'},[{**pair,'goods_id':'other'}])
from test_codex_visual_review import fixture
from test_set_inventory_identity import configured


def test_observing_an_existing_native_turn_does_not_spend_another_model_retry(tmp_path,monkeypatch):
    import psutil
    category=tmp_path/'category.json';category.write_text('{}')
    monkeypatch.setattr(pool,'load_category',lambda _:configured())
    monkeypatch.setattr(pool,'_contact_sheets',lambda *args,**kw:None)
    monkeypatch.setattr(pool,'bind_original_images',lambda *args:None)
    class Input:
        def write(self,data):pass
        def close(self):pass
    monkeypatch.setattr(pool.subprocess,'Popen',lambda *args,**kw:SimpleNamespace(pid=12345,stdin=Input()))
    monkeypatch.setattr(psutil,'Process',lambda pid:SimpleNamespace(create_time=lambda:1.0))
    monkeypatch.setattr('selection_automation.local_agents.analysis_command',lambda agent,job,*args:['native',str(job)])
    worker=pool.Pool(category,{'workers':1,'agent_backends':[{'id':'doubao','name':'豆包','selected_model':'TEST_MODEL'}]})
    pair,_=fixture();digest=pool._group_digest([pair]);old=worker.folder/'jobs/old';old.mkdir(parents=True)
    pool._write_jsonl(old/'manifest.jsonl',[pair]);(old/'reviews.jsonl').touch()
    pool.save(old/'job.json',{'digest':digest,'state':'retry','identities':[list(pool.key(pair))],'agent_id':'doubao','attempt':1})
    pool.save(old/'native-session.json',{'agent':'doubao','model':'TEST_MODEL','state':'send_intended','result_job':str(old),'conversation_id':'TEST_ORIGINAL_TURN'})
    worker.attempts[digest]={'count':3,'schema_revision':pool.POOL_SCHEMA_REVISION}
    worker.launch({'task_id':'recovery','required_agent':'doubao'},[pair])
    job=next(iter(worker.jobs.values()))
    assert job['observation_only'] and job['attempt']==1
    assert worker.attempts[digest]['count']==1
    assert pool.read(Path(job['folder'])/'native-session.json')['conversation_id']=='TEST_ORIGINAL_TURN'


def test_inventory_guide_template_matches_the_strict_decoder():
    from selection_automation.set_inventory import validate_inventory
    cat = configured()
    guide = pool.pool_facts_guide(cat)
    inventory = guide['facts_output_template']['feature_comparisons']['body_type']['inventory']
    validate_inventory(inventory, ['steak', 'chef', 'scissors'])
    assert guide['inventory_requirements'] == cat['stage3']['set_inventory_features']
    assert inventory == {side: {'complete': False, 'groups': []} for side in ('amazon', 'temu')}
    text = pool.prompt(Path('job'), 1, 'gpt-6.1-sol', Path('category.json'))
    assert 'inventory_requirements' in text and '双侧inventory' in text


def test_inventory_guide_does_not_add_fields_to_other_features():
    cat = configured()
    cat['stage3']['set_inventory_features'] = {}
    guide = pool.pool_facts_guide(cat)
    assert not guide['inventory_requirements']
    assert all(set(row) == {'relation', 'amazon', 'temu'}
               for row in guide['facts_output_template']['feature_comparisons'].values())


def test_ready_facts_require_complete_rows_and_unchanged_original_images(tmp_path):
    from test_set_inventory_identity import facts
    cat = configured()
    pair, row = fixture()
    for side in ('amazon', 'temu'):
        image = tmp_path / f'{side}.jpg'
        from PIL import Image
        Image.new('RGB',(80,60),'red' if side=='amazon' else 'blue').save(image)
        pair[f'{side}_image_path'] = str(image)
    row.update(visual_facts=facts(cat, a=6, t=8))
    pool._write_jsonl(tmp_path / 'manifest.jsonl', [pair])
    pool.bind_original_images(tmp_path, [pair])
    job = {'folder': str(tmp_path), 'task_id': 'complete-facts'}
    pool._write_jsonl(tmp_path / 'reviews.jsonl', [])
    assert not pool.job_reviews_ready(cat, job)
    pool._write_jsonl(tmp_path / 'reviews.jsonl', [row])
    assert pool.job_reviews_ready(cat, job)
    Path(pair['temu_image_path']).write_bytes(b'replaced image')
    assert not pool.job_reviews_ready(cat, job)


def test_finished_facts_import_even_while_cli_summary_is_still_running(tmp_path, monkeypatch):
    worker = stub_pool(tmp_path, monkeypatch)
    pair, _ = fixture()
    task = {'task_id': 'ready', 'rows': [pair]}
    job = {'task_id': 'ready', 'folder': str(tmp_path / 'job'),
           'identities': [list(pool.key(pair))], 'digest': 'digest'}
    (tmp_path / 'job').mkdir()
    worker.jobs = {'job': job}
    worker.processes = {'job': SimpleNamespace(poll=lambda: None)}
    monkeypatch.setattr(pool, 'tasks', lambda _: iter([task]))
    monkeypatch.setattr(pool, 'job_reviews_ready', lambda *a: True)
    stopped = []
    monkeypatch.setattr(pool, 'stop_owned_job', lambda j: stopped.append(j) or True)
    monkeypatch.setattr(pool, 'import_job', lambda *a: (1, []))
    monkeypatch.setattr(pool, 'pending_rows', lambda *a: [])
    worker.tick()
    assert stopped == [job] and not worker.jobs
    assert pool.read(tmp_path / 'job/job.json')['state'] == 'complete'


def test_ready_job_cannot_stop_a_process_without_current_ownership(monkeypatch):
    monkeypatch.setattr(pool, 'alive_job', lambda _: False)
    assert not pool.stop_owned_job({'pid': 1})


def test_locked_owner_is_published_before_slow_initialization(tmp_path, monkeypatch):
    import os
    import psutil

    category = tmp_path / 'category.json'
    category.write_text('{}', encoding='utf-8')
    folder = tmp_path / 'stage123/codex_pool'
    folder.mkdir(parents=True)
    pool.save(folder / 'pool.json', {'pid':-1, 'created':0, 'completed_groups':7})

    def initialization(path, config):
        state = pool.read(folder / 'pool.json')
        assert state['pid'] == os.getpid()
        assert state['created'] == psutil.Process().create_time()
        assert state['completed_groups'] == 7
        assert state['phase'] == '初始化看图队列，结果保留'
        raise RuntimeError('stop after checking owner')

    monkeypatch.setattr(pool, 'Pool', initialization)
    with pytest.raises(RuntimeError, match='stop after checking owner'):
        pool.watch(category)


def test_cli_group_is_sol_medium_standard_ephemeral_and_attaches_actual_contact(tmp_path):
    args = pool.command('codex.exe', tmp_path)
    assert '--ephemeral' in args and '--ignore-user-config' in args
    assert args[args.index('-m')+1] == 'gpt-6.1-sol'
    assert 'model_reasoning_effort="medium"' in args and 'service_tier="default"' in args
    assert args[args.index('-i')+1] == str(tmp_path / 'contacts/contact-001.jpg')
    assert 'priority' not in ' '.join(args)


def test_background_codex_inherits_explicit_proxy_without_api_keys(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','unused-secret')
    monkeypatch.setenv('DEEPSEEK_API_KEY','unused-secret')
    env=pool.job_environment({'proxy_url':'http://127.0.0.1:7890'})
    assert env['HTTPS_PROXY']==env['HTTP_PROXY']=='http://127.0.0.1:7890'
    assert 'OPENAI_API_KEY' not in env and 'DEEPSEEK_API_KEY' not in env


def test_incomplete_group_preserves_prior_without_changing_group_membership(tmp_path):
    pair, row = fixture()
    pair2 = {**pair, 'goods_id':'second'}
    task = {'task_id':'test-group', 'rows':[pair,pair2],
            'output':tmp_path/'normalized.jsonl', 'raw_reviews':tmp_path/'raw.jsonl'}
    pool.import_reviews(configured(), task['rows'], [{**row,'goods_id':'second'}], task['output'], task['task_id'])
    job = tmp_path/'job';job.mkdir()
    pool._write_jsonl(job/'manifest.jsonl',[pair])
    pool._write_jsonl(job/'reviews.jsonl',[row,{**row,'goods_id':'not-in-manifest'}])
    imported, errors = pool.import_job(configured(),task,job)
    assert imported == 0 and errors == ['review absent from group manifest']
    assert {value['goods_id'] for value in pool.read_jsonl(task['output'])} == {'second'}
    assert [value['goods_id'] for value in pool.pending_rows(configured(),task)] == [pair['goods_id']]
    assert len(list(pool.read_jsonl(task['raw_reviews']))) == 1


def test_import_writes_specific_strict_fact_validation_errors(tmp_path, monkeypatch):
    pair, row = fixture()
    task = {'task_id':'test-group', 'rows':[pair], 'output':tmp_path/'normalized.jsonl',
            'raw_reviews':tmp_path/'raw.jsonl'}
    job = tmp_path/'job'; job.mkdir()
    pool._write_jsonl(job/'manifest.jsonl', [pair])
    pool._write_jsonl(job/'reviews.jsonl', [row])
    category = configured()
    category['stage3']['codex_require_module_facts'] = True
    monkeypatch.setattr(pool, 'checked_facts', lambda *a: (_ for _ in ()).throw(
        ValueError('invalid bilateral identity evidence')))
    imported, errors = pool.import_job(category, task, job)
    diagnostics = json.loads((job/'validation_errors.json').read_text(encoding='utf-8'))
    assert imported == 0 and errors == ['invalid bilateral identity evidence']
    assert diagnostics[0]['line'] == 1 and diagnostics[0]['error'] == errors[0]


def test_launch_copies_failed_reviews_into_new_job_without_overwriting_old(tmp_path, monkeypatch):
    category_path = tmp_path/'category.json'
    category_path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(pool, 'load_category', lambda _:configured())
    monkeypatch.setattr(pool, '_contact_sheets', lambda *a, **kw:None)
    monkeypatch.setattr(pool, 'bind_original_images', lambda *a:None)
    monkeypatch.setattr(pool, 'codex_executable', lambda _: 'codex.exe')
    class Child:
        pid = 12345
        class Stdin:
            def write(self, _): pass
            def close(self): pass
        stdin = Stdin()
    monkeypatch.setattr(pool.subprocess, 'Popen', lambda *a, **kw:Child())
    import psutil
    monkeypatch.setattr(psutil, 'Process', lambda _:SimpleNamespace(create_time=lambda:1.0))
    worker = pool.Pool(category_path, {'workers':1,'group_size':12})
    pair, row = fixture()
    digest = pool._group_digest([pair])
    old = worker.folder/'jobs'/f'{digest}-old'; old.mkdir(parents=True)
    (old/'reviews.jsonl').write_text(json.dumps(row, ensure_ascii=False)+'\n', encoding='utf-8')
    (old/'validation_errors.json').write_text('[{"line":1,"error":"invalid bilateral identity evidence"}]', encoding='utf-8')
    pool.save(old/'job.json', {'digest':digest,'state':'retry','identities':[list(pool.key(pair))]})
    worker.launch({'task_id':'test','rows':[pair]}, [pair])
    launched = Path(next(iter(worker.jobs.values()))['folder'])
    assert launched != old
    assert (launched/'reviews.jsonl').read_text(encoding='utf-8') == (old/'reviews.jsonl').read_text(encoding='utf-8')
    assert json.loads((launched/'validation_errors.json').read_text(encoding='utf-8'))[0]['error'] == 'invalid bilateral identity evidence'
    assert worker.jobs[launched.name]['reused_reviews_from'] == str(old)


def test_schema_revision_resets_old_retry_exhaustion():
    assert pool._strict_attempt({'count':3,'next_retry_at':9999999999,'schema_revision':1}) == {
        'count':0,'next_retry_at':0,'schema_revision':pool.POOL_SCHEMA_REVISION}


def test_desktop_reviewer_ownership_is_kept_until_actual_turn_end(tmp_path):
    path=tmp_path/'rollout.jsonl'
    path.write_text(json.dumps({'type':'event_msg','payload':{'type':'task_started'}})+'\n')
    assert pool.owner_active(path)
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'event_msg','payload':{'type':'task_complete'}})+'\n')
    assert not pool.owner_active(path)
    assert pool.owner_active(tmp_path/'missing')


def stub_pool(tmp_path, monkeypatch):
    category_path=tmp_path/'category.json'
    category_path.write_text('{}')
    monkeypatch.setattr(pool,'load_category',lambda _:configured())
    worker=pool.Pool(category_path,{'workers':2,'group_size':12})
    monkeypatch.setattr(pool,'ensure_publisher',lambda *a,**kw:None)
    monkeypatch.setattr(pool,'pending_rows',lambda _,task:task['rows'])
    monkeypatch.setattr(pool,'prepare_batch',lambda *a,**kw:{'selected':0})
    def launch(task,rows):
        name=str(len(worker.jobs))
        worker.jobs[name]={'identities':[list(pool.key(row)) for row in rows]}
        worker.processes[name]=SimpleNamespace(poll=lambda:None)
    monkeypatch.setattr(worker,'launch',launch)
    return worker


def test_finished_first_batch_automatically_prepares_and_starts_next(tmp_path,monkeypatch):
    worker=stub_pool(tmp_path,monkeypatch)
    queue=[];preparations=[]
    monkeypatch.setattr(pool,'tasks',lambda _:iter(queue))
    def prepare(*a,**kw):
        preparations.append(kw)
        queue.extend({'task_id':f'new-{i}','rows':[{'asin':str(i),'goods_id':'T','vision_revision_key':'v'}]}
                     for i in range(2))
        return {'selected':2}
    monkeypatch.setattr(pool,'prepare_batch',prepare)
    state=worker.tick()
    assert state['running']==2 and len(preparations)==1
    assert preparations[0]['skip_empty'] and preparations[0]['per_shard']==96


def test_one_active_group_cannot_be_dispatched_twice(tmp_path,monkeypatch):
    worker=stub_pool(tmp_path,monkeypatch)
    row={'asin':'A','goods_id':'T','vision_revision_key':'v'}
    monkeypatch.setattr(pool,'tasks',lambda _:iter([
        {'task_id':'one','rows':[row]},{'task_id':'duplicate-old-allocation','rows':[row]}]))
    state=worker.tick()
    assert state['running']==1


def test_current_completed_shards_do_not_end_enabled_continuous_observer(tmp_path,monkeypatch):
    watcher=importlib.import_module('watch_codex_visual_reviews')
    monkeypatch.setattr(watcher,'ROOT',tmp_path)
    monkeypatch.setattr(watcher,'load_category',lambda _:{'category_key':'test'})
    folder=tmp_path/'runs/test/stage123';folder.mkdir(parents=True)
    (folder/'codex_pool_config.json').write_text('{"enabled":true}')
    count=[]
    monkeypatch.setattr(watcher,'tick',lambda *a:count.append(1) or {'done':True})
    def stop(_):raise InterruptedError('test loop stop')
    monkeypatch.setattr(watcher.time,'sleep',stop)
    import pytest
    with pytest.raises(InterruptedError):watcher.watch(tmp_path/'category.json')
    assert count==[1] and (folder/'codex_review/consumer.json').is_file()


def test_network_failure_cools_down_then_restarts_instead_of_permanent_stop(tmp_path,monkeypatch):
    worker=stub_pool(tmp_path,monkeypatch)
    row={'asin':'A','goods_id':'T','vision_revision_key':'v'}
    digest=pool.hashlib.sha256(json.dumps([pool.key(row)]).encode()).hexdigest()[:16]
    job=tmp_path/'failed-network';job.mkdir()
    (job/'stderr.log').write_text('request timed out; stream disconnected')
    worker.jobs['failed']={'task_id':'one','folder':str(job),'identities':[list(pool.key(row))],'digest':digest}
    worker.processes['failed']=SimpleNamespace(poll=lambda:1)
    worker.attempts[digest]={'count':1,'updated_at':0}
    monkeypatch.setattr(pool,'tasks',lambda _:iter([{'task_id':'one','rows':[row]}]))
    monkeypatch.setattr(pool,'import_job',lambda *a:(0,[]))
    state=worker.tick()
    assert state['running']==0 and state['pending']==1 and state['errors']
    assert worker.attempts[digest]['count']==0 and worker.attempts[digest]['next_retry_at']>pool.time.time()
    worker.attempts[digest]['next_retry_at']=0
    assert worker.tick()['running']==1


def test_actual_originals_are_attached_and_changed_image_is_rejected(tmp_path):
    pair, _ = fixture('SAME')
    for side in ('amazon','temu'):
        image = tmp_path / (side + '.jpg')
        from PIL import Image
        Image.new('RGB',(80,60),'red' if side=='amazon' else 'blue').save(image)
        pair[side + '_image_path'] = str(image)
    pool.bind_original_images(tmp_path, [pair])
    args = pool.command('codex.exe', tmp_path)
    assert args.count('-i') == 2
    assert pair['amazon_image_path'] in args and str(tmp_path/'attachments/temu-numbered-grid.jpg') in args
    pool.check_original_binding(tmp_path, pair)
    Path(pair['amazon_image_path']).write_bytes(b'different source')
    with pytest.raises(ValueError, match='binding changed|发生变化'):
        pool.check_original_binding(tmp_path, pair)


def test_new_same_job_without_originals_receipt_is_rejected(tmp_path):
    pair, _ = fixture('SAME')
    pool.save(tmp_path/'job.json', {'schema_revision':3})
    with pytest.raises(ValueError, match='no attached original'):
        pool.check_original_binding(tmp_path, pair)


def test_groups_share_one_amazon_and_preserve_every_pair():
    rows = [{'asin':asin, 'goods_id':str(i)} for i,asin in enumerate(['A','B','A','A','B'])]
    groups = pool.same_amazon_groups(rows, 2)
    assert all(len({r['asin'] for r in group}) == 1 and len(group) <= 2 for group in groups)
    assert sorted(r['goods_id'] for group in groups for r in group) == ['0','1','2','3','4']
