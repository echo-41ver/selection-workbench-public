"""Run the real supervisor loop against isolated files and fake child boundaries."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import company_setup
import pipeline
import selection


class LocalRun:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.cat = json.loads((ROOT / 'config/categories/power_drill.json').read_text(encoding='utf-8'))
        self.cat['category_key'] = 'supervisor_fixture'
        self.base = tmp_path / 'runs' / self.cat['category_key']
        self.category = self.base / 'category.json'
        self.config = self.base / 'stage12_config.json'
        self.output = self.base / 'stage12'
        self.now = 100.0
        self.tick = 0
        self.spawns = []
        self.terminated = []
        self.statuses = []
        self.company_calls = []
        self.bootstrap_code = 0
        self.sleep_intervals = []
        self.collectors_finish = False
        self.block_first_api = False
        self.publication_locked = False
        self.final_publications = 0
        self.on_sleep = lambda: None
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(selection, 'ROOT', tmp_path)
        monkeypatch.setattr(selection, 'identity', lambda: 'isolated-machine')
        monkeypatch.setattr(company_setup, 'ensure_company', lambda *, start: self.company_calls.append(start))
        monkeypatch.setattr(pipeline, 'write_stage12_config', lambda cat: self.config)
        monkeypatch.setattr(selection, 'spawn', self.spawn)
        monkeypatch.setattr(selection, 'terminate_owned', lambda child: self.terminated.append(child.name))
        monkeypatch.setattr(selection.subprocess, 'run', self.publish_final)
        monkeypatch.setattr(selection.time, 'monotonic', lambda: self.now)
        monkeypatch.setattr(selection.time, 'sleep', self.sleep)
        original_save = selection.save

        def save(path, value):
            if path == self.base / 'status.json':
                self.statuses.append(json.loads(json.dumps(value)))
            original_save(path, value)

        monkeypatch.setattr(selection, 'save', save)
        selection.save(self.category, self.cat)
        selection.save(self.config, {'output_dir': str(self.output)})
        selection.save(tmp_path / 'config/sessions.json', {
            'machine_id': 'isolated-machine', 'workers': [{'name': 'worker-01'}]})
        self.output.mkdir()
        self.make_finished_analysis()

    def make_finished_analysis(self):
        """Real completion() reads a minimal, complete local N x M fixture."""
        amazon = self.base / 'amazon-source.jsonl'
        temu = self.base / 'temu-source.jsonl'
        amazon.write_text('{"asin":"A"}\n', encoding='utf-8')
        temu.write_text('{"goods_id":"T"}\n', encoding='utf-8')
        with sqlite3.connect(self.output / 'stage12.sqlite3') as con:
            con.execute('CREATE TABLE sources (input_path TEXT, last_error TEXT, last_snapshot_hash TEXT, received_records INTEGER)')
            for source in (amazon, temu):
                con.execute('INSERT INTO sources VALUES (?, NULL, ?, 1)',
                            (str(source), hashlib.sha256(source.read_bytes()).hexdigest()))
            con.execute('CREATE TABLE pairs (status TEXT)')
            con.execute("INSERT INTO pairs VALUES ('TEXT_HARD_EXCLUDE')")
            con.execute('CREATE TABLE candidates (payload_json TEXT)')

    def publish(self):
        selection.save(self.base / 'publish_state.json', {
            'errors': {}, 'pending_views': {'layer123': {'reason': 'fixture Excel open'}}
            if self.publication_locked else {}})

    def publish_final(self, command, **kwargs):
        assert Path(command[1]).name == 'publish_all.py', command
        self.final_publications += 1
        self.publish()
        return SimpleNamespace(returncode=0)

    def spawn(self, command, log, env=None):
        name = log.stem
        self.spawns.append({'name': name, 'key': os.getenv('DEEPSEEK_API_KEY'), 'tick': self.tick})
        code = None
        if name == 'bootstrap':
            assert Path(command[1]).name == 'company_setup.py'
            assert command[2:4] == ['--start', '--new-task']
            assert command[4] == self.cat['category_key']
            return SimpleNamespace(name=name,pid=900000+len(self.spawns),poll=lambda:self.bootstrap_code)
        if name in ('amazon', 'worker-01') and self.collectors_finish:
            code = 0
            if name == 'worker-01':
                from selection_automation import work_dispatch
                while job:=work_dispatch.claim(self.base,'worker-01'):
                    work_dispatch.complete(self.base,'worker-01',job['query'])
                plan = selection.read(self.base / 'temu/keyword_plan.json')
                selection.save(self.base / 'temu/supervisors/worker-01.json', {
                    'status': 'WORKER_COMPLETE', 'completed_queries': [item['query'] for item in plan['keywords']]})
        elif name == 'cleanup':
            code=0
            selection.save(self.base/'cleanup_status.json',{'status':'COMPLETE'})
        elif name == 'excel':
            code = 0
            self.publish()
        elif name == 'market':
            code = 0
            selection.save(self.base/'market/status.json',{'status':'COMPLETE'})
        elif name == 'stage3' and self.block_first_api and self.stage3_count == 1:
            selection.save(self.base / 'stage123/provider_blocked.json', {
                'status': 402,
                'key_identity': hashlib.sha256(os.environ['DEEPSEEK_API_KEY'].encode()).hexdigest()})
            code = 1
        return SimpleNamespace(name=name, pid=900000 + len(self.spawns), returncode=code, poll=lambda: code)

    @property
    def stage3_count(self):
        return sum(item['name'] == 'stage3' for item in self.spawns)

    def sleep(self, seconds):
        self.sleep_intervals.append(seconds)
        self.tick += 1
        assert self.tick <= 8, 'supervisor failed to reach the bounded fixture outcome'
        self.now += 65
        self.on_sleep()

    def command(self, action):
        self.monkeypatch.setattr(sys, 'argv', ['selection', action, '--category-key', self.cat['category_key']])
        selection.main()

    def request_and_run(self, paid=False):
        result = selection.request_start(self.category, self.cat, self.base, paid, background=False)
        assert result['状态'] == '启动中'
        selection.supervise(self.category, paid)


def test_running_supervisor_stop_cleans_owned_children_and_persists_stopped(tmp_path, monkeypatch):
    run = LocalRun(tmp_path, monkeypatch)
    # A stale STOP belongs to the preceding run; request_start clears only this.
    (run.base / 'STOP').write_text('previous stopped run', encoding='utf-8')

    def stop_during_run():
        run.command('stop')
        before = len(run.spawns)
        reply = selection.request_start(run.category, run.cat, run.base, False)
        assert reply['状态'] == '停止中'
        assert len(run.spawns) == before
        assert (run.base / 'STOP').exists()

    run.on_sleep = stop_during_run
    run.request_and_run()
    assert [item['状态'] for item in run.statuses][-2:] == ['停止中', '已停止']
    assert {'amazon', 'worker-01', 'temu_union', 'stage12'} <= set(run.terminated)
    assert selection.read(run.base / 'status.json')['状态'] == '已停止'
    assert not (tmp_path / 'runtime/active.json').exists()
    assert not (tmp_path / 'runtime/starting.json').exists()
    assert all((tmp_path / f'runtime/proxyshare-chains/activity/worker-{worker}/revoked_at').exists()
               for worker in range(1, 5))


@pytest.mark.parametrize('recovery', ['saved_key', 'resume'])
def test_provider_block_then_recovery_relaunches_real_supervisor_service(tmp_path, monkeypatch, recovery):
    run = LocalRun(tmp_path, monkeypatch)
    run.block_first_api = True
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-old-key')
    (run.output / f'layer3_candidates_{run.cat["category_key"]}.jsonl').write_text('', encoding='utf-8')
    selection.save(run.base / 'stage123/paid_enabled.json', {'enabled': True})
    # Credential decoding is the mocked boundary; file changes, refresh logic,
    # provider marker handling and service restart all run unchanged.
    from selection_automation import secrets
    monkeypatch.setattr(secrets, 'load', lambda path: json.loads(path.read_text(encoding='utf-8')))

    def recover_and_stop():
        if run.tick == 2:
            blocked = selection.read(run.base / 'status.json')
            assert blocked['状态'] == '等待人工介入'
            assert blocked['阶段']['stage3']['HTTP状态'] == 402
            assert run.stage3_count == 1
            if recovery == 'saved_key':
                selection.save(tmp_path / 'runtime/secrets/deepseek.dpapi', {'key': 'offline-new-key'})
            else:
                run.command('resume-api')
        if run.tick == 3:
            assert run.stage3_count == 2
            assert not (run.base / 'stage123/provider_blocked.json').exists()
            assert selection.read(run.base / 'status.json')['阶段']['stage3']['状态'] == '运行中'
            run.command('stop')

    run.on_sleep = recover_and_stop
    run.request_and_run(paid=True)
    stage3 = [item for item in run.spawns if item['name'] == 'stage3']
    assert len(stage3) == 2
    assert stage3[-1]['key'] == ('offline-new-key' if recovery == 'saved_key' else 'offline-old-key')
    assert run.company_calls == [False]
    assert 'stage3' in run.terminated
    assert selection.read(run.base / 'status.json')['状态'] == '已停止'


def test_finished_collection_waits_for_locked_excel_then_completes(tmp_path, monkeypatch):
    run = LocalRun(tmp_path, monkeypatch)
    import watch_temu_union
    monkeypatch.setattr(watch_temu_union,'watch_health',lambda base:{'状态':'已更新','阻断':False})
    run.collectors_finish = True
    run.publication_locked = True

    def unlock_after_failed_completion():
        if run.final_publications == 1:
            assert run.final_publications == 1
            assert selection.completion(run.config, False)['缺失或失败'] == 0
            current = selection.read(run.base / 'status.json')
            assert current['状态'] == '运行中', json.dumps(current,ensure_ascii=True)
            assert current['阶段']['excel']['状态'] == '查看快照已更新，固定文件待解锁覆盖'
            assert not any(item['状态'].startswith('完成') for item in run.statuses)
            run.publication_locked = False

    run.on_sleep = unlock_after_failed_completion
    run.request_and_run()
    final = selection.read(run.base / 'status.json')
    assert run.final_publications == 2
    assert final['状态'] == '完成'
    assert final['商品对'] == 1
    assert run.tick == 3
    assert {'temu_union', 'stage12'} <= set(run.terminated)
    assert not (tmp_path / 'runtime/active.json').exists()


def test_slow_bootstrap_progress_and_stop_preserve_created_profiles(tmp_path, monkeypatch):
    run = LocalRun(tmp_path, monkeypatch)
    run.bootstrap_code = None
    (tmp_path / 'config/sessions.json').unlink()
    journal = tmp_path / 'runtime/bootstrap.json'
    selection.save(journal, {'machine_id':'isolated-machine','workers':[]})

    def progress_then_stop():
        current = selection.read(run.base / 'status.json')
        stage = current['阶段']['bootstrap']
        assert current['状态'] == '启动中'
        assert stage['初始化已绑定'] == (1 if run.tick == 3 else 0)
        assert stage['初始化已创建'] == (0 if run.tick == 1 else 1)
        assert 'private-profile-id' not in json.dumps(current)
        if run.tick < 3:
            selection.save(journal, {'machine_id':'isolated-machine','workers':[{
                'profile_id':'private-profile-id',
                'bootstrap_status':'PROFILE_CREATED' if run.tick == 1 else 'BOUND'}]})
        else:
            run.command('stop')

    run.on_sleep = progress_then_stop
    run.request_and_run()
    assert run.company_calls == [False]
    assert [item['name'] for item in run.spawns] == ['bootstrap']
    assert run.terminated == ['bootstrap']
    assert run.sleep_intervals == [1, 1, 1]
    assert selection.read(journal)['workers'][0]['profile_id'] == 'private-profile-id'
    assert selection.read(run.base / 'status.json')['状态'] == '已停止'
    assert not (tmp_path / 'runtime/active.json').exists()
    assert not (tmp_path / 'runtime/starting.json').exists()


def test_bootstrap_failure_is_visible_and_prevents_collection(tmp_path, monkeypatch):
    run = LocalRun(tmp_path, monkeypatch)
    run.bootstrap_code = 7
    with pytest.raises(RuntimeError, match='退出码 7'):
        run.request_and_run()
    final = selection.read(run.base / 'status.json')
    assert final['状态'] == '启动或运行失败'
    assert final['阶段']['bootstrap']['退出码'] == 7
    assert final['阶段']['bootstrap']['日志'].endswith('bootstrap.log')
    assert [item['name'] for item in run.spawns] == ['bootstrap']
    assert not (tmp_path / 'runtime/active.json').exists()


def test_paid_key_missing_waits_without_starting_visual_api(tmp_path, monkeypatch):
    run = LocalRun(tmp_path, monkeypatch)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    (run.output / f'layer3_candidates_{run.cat["category_key"]}.jsonl').write_text('', encoding='utf-8')
    selection.save(run.base / 'stage123/paid_enabled.json', {'enabled': True})
    def stop_after_wait():
        current = selection.read(run.base / 'status.json')
        assert current['阶段']['stage3']['状态'] == '已授权，等待 API Key'
        assert run.stage3_count == 0
        run.command('stop')
    run.on_sleep = stop_after_wait
    run.request_and_run(paid=True)
    assert run.company_calls == [False]
    assert run.stage3_count == 0


def test_stop_during_local_config_read_prevents_bootstrap_spawn(tmp_path, monkeypatch):
    run = LocalRun(tmp_path, monkeypatch)
    monkeypatch.setattr(company_setup,'ensure_company',lambda *,start:run.command('stop'))
    run.request_and_run()
    assert run.spawns == []
    assert selection.read(run.base/'status.json')['状态'] == '已停止'


def retired_checkpoint(run):
    """Isolated evidence of the real cleanup/manifest-removal failure boundary."""
    run.cat['temu']['worker_count'] = 1
    run.cat['amazon']['target_count'] = 1
    selection.save(run.category, run.cat)
    selection.save(run.root/'runtime/machine.json', {'id':'isolated-machine'})
    manifest = {'machine_id':'isolated-machine', 'task_key':run.cat['category_key'],
                'used':True, 'workers':[{'name':'worker-01','profile_id':'retired-owned-profile'}]}
    selection.save(run.root/'runtime/profile_retirement/receipt.json', {
        'machine_id':'isolated-machine', 'new_task_key':run.cat['category_key'],
        'records':[{'path':str(run.root/'config/sessions.json'),'manifest':manifest}],
        'deleted':['retired-owned-profile']})
    (run.root/'config/sessions.json').unlink()
    selection.save(run.base/'temu/keyword_plan.json', {
        'finalized':True, 'keywords':[{'query':'fixture query','max_products':1}]})
    selection.save(run.base/'temu/supervisors/assignments.json', {'jobs':[{
        'query':'fixture query','max_products':1,'owner':'worker-01','complete':True,
        'sort_passes':{order:{'owner':'worker-01','complete':True} for order in ('sales','recent')}}]})
    selection.save(run.base/'temu/supervisors/worker-01.json', {
        'status':'WORKER_COMPLETE','environment_cleanup':'COMPLETE',
        'deleted_profile_id':'retired-owned-profile'})
    source = run.base/'temu/workers/worker-01/fixture/products.jsonl'
    source.parent.mkdir(parents=True)
    source.write_text('{"goods_id":"fixture-real-boundary"}\n', encoding='utf-8')
    selection.save(run.base/'cleanup_status.json', {'status':'COMPLETE','pending_workers':[],'waiting_workers':[]})
    selection.save(run.base/'amazon/amazon_frontend_latest.json', {
        'status':'COMPLETE','details_complete':True,'source_url':run.cat['amazon']['bestsellers_url'],
        'missing_details':[], 'records':[{'asin':'FIXTURE','rank':1}]})
    selection.save(run.base/'stage123/paid_enabled.json', {'controller':'independent'})
    return manifest


@pytest.mark.parametrize('restored_data_only', [False, True])
def test_retired_complete_collection_resumes_downstream_and_stops_without_browser_recreation(tmp_path, monkeypatch, restored_data_only):
    from selection_automation.completed_collection import completed_checkpoint, unchanged
    import watch_temu_union
    run = LocalRun(tmp_path, monkeypatch)
    retired_checkpoint(run)
    if restored_data_only:
        (run.root/'runtime/profile_retirement/receipt.json').unlink()
        selection.save(run.root/'runtime/web-console/state.json', {
            'category_key':run.cat['category_key'], 'explicitly_restored':True})
    monkeypatch.setattr(watch_temu_union,'watch_health',lambda base:{'状态':'已更新','阻断':False})
    proof = completed_checkpoint(run.root, run.base, run.cat)
    assert proof and unchanged(proof)
    before = {path:Path(path).read_bytes() for path in proof['files']}

    def stop_after_real_loop():
        status = selection.read(run.base/'status.json')
        assert status['阶段']['bootstrap']['状态'] == '采集已完成，仅续接汇总、分析和导出'
        assert selection.live_owner()['category_key'] == run.cat['category_key']
        assert {'temu_union','stage12','market','excel'} <= {item['name'] for item in run.spawns}
        run.command('stop')

    run.on_sleep = stop_after_real_loop
    # Execute the actual supervisor control flow twice across the same checkpoint.
    for _ in range(2):
        selection.request_start(run.category, run.cat, run.base, False, background=False, preserve_prepared=not restored_data_only)
        selection.supervise(run.category, False, preserve_prepared=not restored_data_only)
        assert selection.read(run.base/'status.json')['状态'] == '已停止'
        assert not selection.live_owner()
        assert not (run.root/'config/sessions.json').exists()
        assert all(Path(path).read_bytes() == data for path, data in before.items())
    assert not ({'bootstrap','binding_recovery','amazon','worker-01','cleanup','stage3'} & {item['name'] for item in run.spawns})


@pytest.mark.parametrize('missing', ['explicit_restore', 'retired_worker', 'deleted_profile', 'source'])
def test_restored_history_requires_complete_retired_source_data(tmp_path, monkeypatch, missing):
    from selection_automation.completed_collection import completed_checkpoint
    run = LocalRun(tmp_path, monkeypatch)
    retired_checkpoint(run)
    (run.root/'runtime/profile_retirement/receipt.json').unlink()
    selection.save(run.root/'runtime/web-console/state.json', {
        'category_key':run.cat['category_key'], 'explicitly_restored':missing!='explicit_restore'})
    if missing == 'source':
        (run.base/'temu/workers/worker-01/fixture/products.jsonl').unlink()
    elif missing in {'retired_worker', 'deleted_profile'}:
        path=run.base/'temu/supervisors/worker-01.json'
        state=selection.read(path)
        state['environment_cleanup' if missing=='retired_worker' else 'deleted_profile_id']=None
        selection.save(path,state)
    assert completed_checkpoint(run.root, run.base, run.cat) is None


@pytest.mark.parametrize('broken', ['unfinished_plan','unfinished_pass','missing_pass','new_query',
                                  'amazon_details','amazon_rank','amazon_listing','missing_source',
                                  'foreign_machine','foreign_task','foreign_receipt_path','wrong_deleted_profile',
                                  'cleanup_pending','pending_rotation','malformed_dispatch'])
def test_incomplete_or_foreign_checkpoint_never_bypasses_strict_browser_resume(tmp_path, monkeypatch, broken):
    from selection_automation.completed_collection import completed_checkpoint
    run = LocalRun(tmp_path, monkeypatch)
    retired_checkpoint(run)
    paths = {
        'plan':run.base/'temu/keyword_plan.json',
        'dispatch':run.base/'temu/supervisors/assignments.json',
        'amazon':run.base/'amazon/amazon_frontend_latest.json',
        'receipt':run.root/'runtime/profile_retirement/receipt.json',
        'worker':run.base/'temu/supervisors/worker-01.json',
        'cleanup':run.base/'cleanup_status.json'}
    if broken == 'missing_source':
        (run.base/'temu/workers/worker-01/fixture/products.jsonl').unlink()
    elif broken == 'pending_rotation':
        selection.save(run.base/'commands/rotate/pending.json', {
            'status':'QUEUED','created_at':1,'id':'pending'})
    else:
        key = ('plan' if broken in {'unfinished_plan','new_query'} else
               'dispatch' if broken in {'unfinished_pass','missing_pass','malformed_dispatch'} else
               'amazon' if broken.startswith('amazon_') else
               'worker' if broken == 'wrong_deleted_profile' else
               'cleanup' if broken == 'cleanup_pending' else 'receipt')
        value = selection.read(paths[key])
        if broken == 'unfinished_plan': value['finalized'] = False
        elif broken == 'new_query': value['keywords'].append({'query':'new query','max_products':1})
        elif broken == 'unfinished_pass': value['jobs'][0]['sort_passes']['recent']['complete'] = False
        elif broken == 'missing_pass': del value['jobs'][0]['sort_passes']['recent']
        elif broken == 'malformed_dispatch': value['jobs'] = ['invalid']
        elif broken == 'amazon_details': value['details_complete'] = False
        elif broken == 'amazon_rank': value['records'][0]['rank'] = 2
        elif broken == 'amazon_listing': value['source_url'] = 'https://www.amazon.com/zgbs/different/123'
        elif broken == 'foreign_machine': value['machine_id'] = 'other-machine'
        elif broken == 'foreign_task': value['records'][0]['manifest']['task_key'] = 'other_task'
        elif broken == 'foreign_receipt_path': value['records'][0]['path'] = str(run.root/'other/sessions.json')
        elif broken == 'wrong_deleted_profile': value['deleted_profile_id'] = 'someone-elses-profile'
        elif broken == 'cleanup_pending': value['pending_workers'] = ['worker-01']
        selection.save(paths[key], value)
    assert completed_checkpoint(run.root, run.base, run.cat) is None
    if broken != 'pending_rotation':
        # Missing old manifest still fails visibly; no new environment is created.
        with pytest.raises(RuntimeError, match='维护续接必须属于本任务与本安装'):
            selection.supervise(run.category, False, preserve_prepared=True)
        assert not run.spawns


def test_analysis_only_resume_rejects_checkpoint_changes_after_its_owner_started(tmp_path, monkeypatch):
    import watch_temu_union
    run = LocalRun(tmp_path, monkeypatch)
    retired_checkpoint(run)
    monkeypatch.setattr(watch_temu_union,'watch_health',lambda base:{'状态':'已更新','阻断':False})
    run.on_sleep = lambda: selection.save(run.base/'temu/keyword_plan.json', {
        'finalized':False,'keywords':[{'query':'fixture query','max_products':1}]})
    with pytest.raises(RuntimeError, match='续接依据发生变化'):
        selection.supervise(run.category, False, preserve_prepared=True)
    assert selection.read(run.base/'status.json')['状态'] == '启动或运行失败'
    assert not (run.root/'config/sessions.json').exists()
    assert not ({'bootstrap','amazon','worker-01','stage3'} & {item['name'] for item in run.spawns})
