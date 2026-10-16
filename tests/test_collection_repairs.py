import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from selection_automation.merge_temu_snapshots import merge
from selection_automation.worker_runtime import WorkerLease, WorkerStopRequested, replacement_guard, run_collector, transport_recovery_allowed

ROOT = Path(__file__).resolve().parents[1]


def test_union_primary_fields_are_one_latest_complete_observation(tmp_path):
    def row(at, price, image, **extra):
        return dict(goods_id="1", title="towel", product_url="https://www.temu.com/1", price_usd=price,
                    main_image_url=image, all_image_urls=[image], observed_at=at, **extra)
    new, old, partial = tmp_path / "a.jsonl", tmp_path / "z.jsonl", tmp_path / "zz.jsonl"
    new.write_text(json.dumps(row("2026-09-22T01:00:00Z", 20, "new.jpg", search_query="new")), "utf-8")
    old.write_text(json.dumps(row("2026-09-21T01:00:00Z", 10, "old.jpg", search_query="old")), "utf-8")
    partial.write_text(json.dumps(row("2026-09-23T01:00:00Z", None, "partial.jpg", search_query="partial")), "utf-8")
    result, summary = merge([partial, old, new])
    primary = result[0]
    assert (primary["price_usd"], primary["main_image_url"], primary["observed_at"]) == (20, "new.jpg", "2026-09-22T01:00:00Z")
    assert primary["all_image_urls"] == ["new.jpg"]
    assert primary["source_queries"] == ["new", "old", "partial"]
    assert primary["primary_observation"]["snapshot"] == str(new.resolve())
    assert {entry["value"] for entry in primary["field_conflicts"]["price_usd"]} == {10, 20}
    assert summary["unique_goods_id"] == 1 and summary["duplicate_rows_merged"] == 2
    assert merge([new, partial, old])[0] == result


def test_worker_lease_excludes_another_process_and_releases(tmp_path):
    code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from selection_automation.worker_runtime import WorkerLease
try:
    with WorkerLease(Path(sys.argv[1]), 'worker-01-new'):
        pass
except RuntimeError:
    sys.exit(17)
"""
    with WorkerLease(tmp_path, "worker-01-new"):
        child = subprocess.run([sys.executable, "-B", "-c", code, str(tmp_path), str(ROOT / "src")], timeout=10)
        assert child.returncode == 17
    child = subprocess.run([sys.executable, "-B", "-c", code, str(tmp_path), str(ROOT / "src")], timeout=10)
    assert child.returncode == 0


def test_replacement_stops_owned_child_and_waits_for_worker_release(tmp_path):
    ready = threading.Event()
    stopped = threading.Event()
    failures = []

    def supervisor():
        try:
            with WorkerLease(tmp_path, "worker-01-new") as lease:
                ready.set()
                with (tmp_path / "child.log").open("w") as stream:
                    run_collector([sys.executable, "-B", "-c", "import time; time.sleep(30)"], lease=lease, cwd=tmp_path, stream=stream)
        except WorkerStopRequested:
            stopped.set()
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=supervisor)
    worker.start()
    assert ready.wait(3)
    with replacement_guard(tmp_path, "worker-01-new"):
        assert stopped.is_set()
        with pytest.raises(RuntimeError, match="already owned"):
            with WorkerLease(tmp_path, "worker-01-new"):
                pass
    worker.join(3)
    assert not worker.is_alive() and not failures
    with WorkerLease(tmp_path, "worker-01-new"):
        pass


def test_replacement_token_cannot_bypass_owner_check(tmp_path):
    with WorkerLease(tmp_path, "worker-01-new"):
        with pytest.raises(RuntimeError, match="owning supervisor"):
            with replacement_guard(tmp_path, "worker-01-new", "forged"):
                pytest.fail("must not enter replacement")


@pytest.mark.parametrize("status", ["STOPPED_HTTP_403", "STOPPED_HTTP_429", "STOPPED_LOGIN_REQUIRED", "WAITING_HUMAN_VERIFICATION", "STOPPED_ACCESS_DENIED", "STOPPED_TRY_AGAIN_STALLED"])
def test_only_confirmed_transport_failure_can_replace(status):
    progress = dict(status=status, automatic_recovery_eligible=True, resume_requires_session_rotation=True)
    assert not transport_recovery_allowed(progress)
    progress["status"] = "STOPPED_PROXY_CONNECTION"
    assert transport_recovery_allowed(progress)
    progress["http_block"] = {"status": 403}
    assert not transport_recovery_allowed(progress)


@pytest.fixture
def pipeline_fixture(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("collection_test_pipeline", ROOT / "scripts" / "pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "cleanup_completed_worker", lambda *args: True)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "interruptible_wait", lambda seconds, lease: lease.check_stop())
    category = {"category_key": "bath_towel_set"}
    sessions = tmp_path / "sessions.json"
    sessions.write_text(json.dumps({"workers": [{"name": "worker-01-new", "webdriver_url": "http://localhost:4451", "session_id_file": "sid", "bidi_url_file": "bidi"}]}), "utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "proxy_policy.json").write_text(json.dumps({"session_rotation_policy": "NEW_ADSPOWER_PROFILE_AND_NEW_PROXY_SESSION", "delete_old_profile_first_via_adspower": True, "worker_chain_ports": [17891]}), "utf-8")
    plan = module.keyword_plan_path(category)
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({"keywords": [{"query": "first", "max_products": 500}, {"query": "second", "max_products": 500}]}), "utf-8")
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
    monkeypatch.setattr(module.socket, "create_connection", lambda *args, **kwargs: Connection())
    return module, category, sessions


def test_all_queries_require_sales_sort_and_unconfirmed_does_not_advance(pipeline_fixture,monkeypatch):
    module,category,sessions=pipeline_fixture
    calls=[]
    def run(command,**kwargs):
        query=command[command.index('--query')+1]
        calls.append((query,command[command.index('--sort')+1]))
        output=Path(command[command.index('--output')+1])
        status='KEYWORD_LIMIT_REACHED' if query=='first' else ('STOPPED_SALES_SORT_UNCONFIRMED' if sum(q=='second' for q,o in calls)<=3 else 'WAITING_CAPTCHA')
        (output/'progress.json').write_text(json.dumps({'status':status}),encoding='utf-8')
        return 0 if query=='first' else 2
    plan=module.keyword_plan_path(category)
    plan.write_text(json.dumps({'keywords':[{'query':q,'max_products':3} for q in ['first','second','third']]}),encoding='utf-8')
    monkeypatch.setattr(module,'run_collector',run)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(module,'interruptible_wait',lambda seconds,lease:lease.check_stop())
    with pytest.raises(RuntimeError,match='incomplete'):
        module.collect_temu(Path('category.json'),category,sessions)
    assert calls==[('first','sales'),('first','recent')]+[('second','sales')]*4
    state=json.loads((module.run_root(category)/'temu/supervisors/worker-01-new.json').read_text('utf-8'))
    assert state['query_status']=='WAITING_CAPTCHA' and state['attempt']==1
    assert state['current_query']=='second' and state['completed_queries']==['first']
    assert state['verify_to_resume'] is True


def test_rate_recovery_history_has_no_cumulative_limit_and_reopens_on_progress(pipeline_fixture):
    module,_,_=pipeline_fixture
    previous={'automatic_rotation_attempted':True,'status':'RECOVERY_NOT_CONFIRMED',
        'unique_goods_before':0,'unique_goods_after':0,'profile_id':'old-profile'}
    allowed,next_record,progressed=module.rate_recovery_budget(previous,0,'new-profile')
    assert allowed is True and progressed is False
    assert next_record['attempts_without_progress']==2
    assert module.rate_recovery_budget(previous,0,'old-profile')[0] is False
    capped={**previous,'attempts_without_progress':3}
    assert module.rate_recovery_budget(capped,0,'third-profile')[0] is True
    recovered={**previous,'status':'RECOVERED_WITH_NEW_PRODUCTS','unique_goods_after':360}
    allowed,next_record,progressed=module.rate_recovery_budget(recovered,360,'new-profile')
    assert allowed is True and progressed is True
    assert next_record['attempts_without_progress']==1


def test_recent_pass_resumes_without_redoing_sales_and_union_deduplicates(pipeline_fixture, monkeypatch):
    module, category, sessions = pipeline_fixture
    calls = []; blocked = [True]
    def collect(command, **kwargs):
        query = command[command.index('--query')+1]
        order = command[command.index('--sort')+1]
        output = Path(command[command.index('--output')+1])
        calls.append((query, order))
        if query == 'first' and order == 'recent' and blocked[0]:
            (output/'progress.json').write_text(json.dumps({'status':'STOPPED_SALES_SORT_UNCONFIRMED' if sum(q=='first' and o=='recent' for q,o in calls)<=3 else 'WAITING_CAPTCHA'}), encoding='utf-8')
            return 2
        evidence = {'requested':order, 'state':'UI_CONFIRMED', 'query':query}
        (output/'products.jsonl').write_text(json.dumps({'goods_id':'shared', 'search_query':query,
            'search_sort':evidence, 'sales_text':'100+ sold'})+'\n', encoding='utf-8')
        status='KEYWORD_COMPLETE' if query=='first' and order=='sales' else 'KEYWORD_LIMIT_REACHED'
        (output/'progress.json').write_text(json.dumps({'status':status,
            'sales_sort':evidence, 'unique_goods':1, 'search_query':query}), encoding='utf-8')
        return 0
    monkeypatch.setattr(module, 'run_collector', collect)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw: SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match='incomplete'):
        module.collect_temu(Path('category.json'), category, sessions)
    assert calls == [('first','sales')]+[('first','recent')]*4
    state_path = module.run_root(category)/'temu/supervisors/worker-01-new.json'
    assert json.loads(state_path.read_text('utf-8'))['completed_queries'] == []
    blocked[0] = False; calls.clear()
    module.collect_temu(Path('category.json'), category, sessions)
    assert calls == [('first','recent'), ('second','sales'), ('second','recent')]
    state = json.loads(state_path.read_text('utf-8'))
    assert state['completed_queries'] == ['first','second'] and len(state['completed_passes']) == 4
    module.merge_temu(category)
    union = module.run_root(category)/'temu/union'
    summary = json.loads((union/'summary.json').read_text('utf-8'))
    row = json.loads((union/'products.jsonl').read_text('utf-8'))
    assert summary['unique_goods_id'] == 1 and summary['source_rows'] == 4
    assert {x['search_sort']['requested'] for x in row['observation_sources']} == {'sales','recent'}


def test_completed_unsorted_checkpoint_is_recollected_and_archived(pipeline_fixture,monkeypatch):
    module,category,sessions=pipeline_fixture
    output=module.run_root(category)/'temu/workers/worker-01-new/first'
    output.mkdir(parents=True)
    old=''.join(json.dumps({'goods_id':str(i),'search_sort':{'requested':'default','state':'NOT_REQUESTED'}})+'\n' for i in range(3))
    (output/'products.jsonl').write_text(old,encoding='utf-8')
    (output/'progress.json').write_text(json.dumps({'status':'KEYWORD_LIMIT_REACHED','unique_goods':3}),encoding='utf-8')
    helper=(ROOT/'scripts/temu_sales_sort.mjs').as_uri()
    calls=[]
    def run(command,**kwargs):
        query=command[command.index('--query')+1];calls.append(query)
        order=command[command.index('--sort')+1]
        assert order in ('sales','recent')
        directory=Path(command[command.index('--output')+1])
        # Exercise the collector's real on-disk checkpoint preparation, offline.
        result=subprocess.run(['node','--input-type=module','-e',
            f'import {{prepareSalesCheckpoint}} from {json.dumps(helper)};console.log(JSON.stringify(await prepareSalesCheckpoint(process.argv[1],process.argv[2],process.argv[3])));',
            str(directory),query,order],capture_output=True,text=True,timeout=10)
        assert result.returncode==0,result.stderr
        if query=='first' and order=='sales':
            record=json.loads(result.stdout)
            assert record['retained_goods']==0 and record['removed_goods']==3
            assert (Path(record['archive'])/'products.jsonl').read_text('utf-8')==old
            assert (directory/'products.jsonl').read_text('utf-8')==''
            assert json.loads((directory/'progress.json').read_text('utf-8'))['keyword_complete'] is False
        evidence={'requested':order,'state':'UI_CONFIRMED','query':query}
        (directory/'products.jsonl').write_text(json.dumps({'goods_id':'new','search_sort':evidence})+'\n',encoding='utf-8')
        (directory/'progress.json').write_text(json.dumps({'status':'KEYWORD_LIMIT_REACHED','unique_goods':1,'sales_sort':evidence}),encoding='utf-8')
        return 0
    monkeypatch.setattr(module,'run_collector',run)
    module.collect_temu(Path('category.json'),category,sessions)
    assert calls==['first','first','second','second']
    # A later invocation skips only the new, confirmed products.
    calls.clear();module.collect_temu(Path('category.json'),category,sessions)
    assert calls==[]


def test_missing_see_more_preserves_current_keyword_for_recovery(pipeline_fixture, monkeypatch):
    module, category, sessions = pipeline_fixture
    output = module.run_root(category) / 'temu/workers/worker-01-new/first'
    output.mkdir(parents=True)
    (output / 'automatic_recovery.json').write_text(json.dumps({'attempts_without_progress':3,'unique_goods_before':0}), encoding='utf-8')
    calls = []
    def fake_collector(command, **kwargs):
        query = command[command.index("--query") + 1]
        calls.append(query)
        output = Path(command[command.index("--output") + 1])
        status = ("STOPPED_SEE_MORE_UNAVAILABLE" if len(calls)<=5 else "WAITING_CAPTCHA") if query == "first" else "KEYWORD_LIMIT_REACHED"
        (output / "progress.json").write_text(json.dumps({"status": status}), "utf-8")
        return 2 if query == "first" else 0
    monkeypatch.setattr(module, "run_collector", fake_collector)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match="incomplete"):
        module.collect_temu(Path("category.json"), category, sessions)
    assert calls == ["first"]*6
    state = json.loads((module.run_root(category) / "temu/supervisors/worker-01-new.json").read_text("utf-8"))
    assert state["status"] == "HUMAN_ACTION_REQUIRED"
    assert state["current_query"] == "first" and state["query_status"] == "WAITING_CAPTCHA"
    assert state["completed_queries"] == []


def test_pipeline_recovers_transport_once_with_owned_lease(pipeline_fixture, monkeypatch):
    module, category, sessions = pipeline_fixture
    calls, recoveries = [], []
    def fake_collector(command, **kwargs):
        query = command[command.index("--query") + 1]
        calls.append(query)
        output = Path(command[command.index("--output") + 1])
        payload = {"status": "KEYWORD_LIMIT_REACHED"} if len(calls) > 1 else {
            "status": "STOPPED_PROXY_CONNECTION", "automatic_recovery_eligible": True, "resume_requires_session_rotation": True,
        }
        (output / "progress.json").write_text(json.dumps(payload), "utf-8")
        return 0 if len(calls) > 1 else 2
    def fake_replacement(command, **kwargs):
        recoveries.append(command)
        assert "--supervisor-token" in command and "--start-collector" not in command
        with pytest.raises(RuntimeError, match="already owned"):
            with WorkerLease(module.ROOT / "runtime/worker-leases", "worker-01-new"):
                pass
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module, "run_collector", fake_collector)
    monkeypatch.setattr(module.subprocess, "run", fake_replacement)
    module.collect_temu(Path("category.json"), category, sessions)
    assert calls == ["first", "first", "first", "second", "second"] and len(recoveries) == 1


def test_node_collection_checkpoint_and_bounded_control_wait(tmp_path):
    helper = (ROOT / "scripts/collection_checkpoint.mjs").as_uri()
    script = f"""
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {{amazonCollectionStatus, detailComplete, resumableAmazonRecords, serializedJsonWriter, missingControlState, terminalCollectionStop}} from {json.dumps(helper)};
import {{completeDetail}} from {json.dumps((ROOT / 'tests/fixtures/amazon_detail.mjs').as_uri())};
const scope = {{target_count: 2, source_url: 'https://www.amazon.com/test'}};
const good = completeDetail({{asin:'A', title:'A', product_url:'https://a'}});
const bad = {{asin:'B', title:'B', product_url:'https://b', detail_status:'详情页失败', image_status:'榜单图回退'}};
const checkpoint = {{schema_version:'amazon-detail-checkpoint-v1', scope, status:'RUNNING', records:[good,bad]}};
assert.deepEqual(resumableAmazonRecords(checkpoint,scope).filter(row=>!detailComplete(row)),[bad]);
assert.equal(amazonCollectionStatus([good,bad]),'PARTIAL_DETAIL_FAILURE');
assert.equal(amazonCollectionStatus([good]),'COMPLETE');
assert.throws(()=>resumableAmazonRecords(checkpoint,{{...scope,target_count:3}}));
const write=serializedJsonWriter(process.argv[1]);
await Promise.all([write({{...checkpoint,records:[bad,bad]}}),write(checkpoint)]);
assert.deepEqual(JSON.parse(await fs.readFile(process.argv[1],'utf8')),checkpoint);
let wait = null;
for(let n=0;n<8;n++) wait=missingControlState(wait,40,n*8000,8,120000);
assert.equal(wait.exhausted,true);
wait=missingControlState(wait,80,65000,8,120000);
assert.equal(wait.exhausted,false);
assert.equal(wait.checks,1);
assert.equal(terminalCollectionStop({{documentURI:'about:neterror',url:'https://www.temu.com/search_result.html'}}).automatic_recovery_eligible,true);
assert.equal(terminalCollectionStop({{url:'https://www.temu.com/login.html'}}).status,'STOPPED_LOGIN_REQUIRED');
assert.equal(terminalCollectionStop({{text:'verify you are human'}}),null);
assert.equal(terminalCollectionStop({{documentURI:'about:neterror'}},{{status:403}}).status,'STOPPED_HTTP_403');
"""
    completed = subprocess.run(["node", "--input-type=module", "-e", script, str(tmp_path / "checkpoint.json")], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize('persistent_login', [False, True])
@pytest.mark.parametrize('blocked_status', ['STOPPED_LOGIN_REQUIRED', 'STOPPED_HTTP_429', 'STOPPED_PROXY_OR_NETWORK_FAILURE', 'STOPPED_SEE_MORE_UNAVAILABLE', 'STOPPED_HTTP_403', 'STOPPED_HTTP_403_NO_LOGIN', 'STOPPED_INITIAL_PRODUCTS_TIMEOUT'])
def test_recovery_preserves_checkpoint_and_distinguishes_access_gates(pipeline_fixture, monkeypatch, persistent_login, blocked_status):
    module,category,sessions=pipeline_fixture
    calls=[]; rotations=[]
    def collector(command,**kwargs):
        query=command[command.index('--query')+1]; calls.append(query)
        output=Path(command[command.index('--output')+1]); checkpoint=output/'products.jsonl'
        if len(calls)==1: checkpoint.write_text('{"goods_id":"kept"}\n',encoding='utf-8')
        if len(calls)>1:
            assert command[command.index('--webdriver-url')+1]=='http://localhost:9999'
            assert command[command.index('--session-id-file')+1].endswith('new_sid')
            if query=='first' and command[command.index('--sort')+1]=='sales': assert 'kept' in checkpoint.read_text(encoding='utf-8')
        status=('WAITING_CAPTCHA' if persistent_login and len(calls)>3 else blocked_status) if len(calls)==1 or persistent_login else 'KEYWORD_LIMIT_REACHED'
        if status == 'STOPPED_HTTP_403_NO_LOGIN': status = 'STOPPED_HTTP_403'
        (output/'progress.json').write_text(json.dumps({'status':status,
            'http_status':403 if blocked_status in {'STOPPED_HTTP_403','STOPPED_HTTP_403_NO_LOGIN'} else None,
            'confirmed_login_redirect':blocked_status == 'STOPPED_HTTP_403'}),encoding='utf-8')
        return 0 if status=='KEYWORD_LIMIT_REACHED' else 2
    def replace(command,**kwargs):
        rotations.append(command)
        payload=json.loads(sessions.read_text('utf-8')); payload['workers'][0].update(webdriver_url='http://localhost:9999',session_id_file='new_sid',bidi_url_file='new_bidi')
        sessions.write_text(json.dumps(payload),encoding='utf-8')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module,'run_collector',collector)
    monkeypatch.setattr(module.subprocess,'run',replace)
    if blocked_status == 'STOPPED_HTTP_403_NO_LOGIN':
        with pytest.raises(RuntimeError,match='incomplete'):
            module.collect_temu(Path('category.json'),category,sessions)
        assert calls==['first'] and rotations==[]
        state=json.loads((module.run_root(category)/'temu/supervisors/worker-01-new.json').read_text('utf-8'))
        assert state['query_status']=='STOPPED_HTTP_403' and state['browser_preserved'] is True
    elif persistent_login:
        with pytest.raises(RuntimeError,match='incomplete'):
            module.collect_temu(Path('category.json'),category,sessions)
        assert calls==['first'] * 4 and len(rotations)==3
        state=json.loads((module.run_root(category)/'temu/supervisors/worker-01-new.json').read_text('utf-8'))
        assert state['query_status']=='WAITING_CAPTCHA' and state['browser_preserved'] is True
        assert state['current_query']=='first' and state['completed_queries']==[]
        # Restarting with an unchanged challenge must preserve its session and data.
        with pytest.raises(RuntimeError,match='incomplete'):
            module.collect_temu(Path('category.json'),category,sessions)
        assert calls==['first'] * 5 and len(rotations)==3
    else:
        module.collect_temu(Path('category.json'),category,sessions)
        assert calls==['first','first','first','second','second'] and len(rotations)==1
    assert 'kept' in (module.run_root(category)/'temu/workers/worker-01-new/first/products.jsonl').read_text('utf-8')


def test_initial_guard_observes_login_page_and_preserves_explicit_http_refusal():
    source=(ROOT/'scripts/temu_flower_collect.mjs').as_posix()
    script=f"""
import fs from 'node:fs'; import vm from 'node:vm'; import assert from 'node:assert/strict';
import {{terminalCollectionStop}} from {json.dumps((ROOT/'scripts/collection_checkpoint.mjs').as_uri())};
import {{classifyPageState as classify}} from {json.dumps((ROOT/'scripts/temu_flower_recovery_policy.mjs').as_uri())};
import {{supersededByVerification}} from {json.dumps((ROOT/'scripts/temu_network_evidence.mjs').as_uri())};
import {{observeHttpPageTransition}} from {json.dumps((ROOT/'scripts/temu_http_transition.mjs').as_uri())};
const source=fs.readFileSync({json.dumps(source)},'utf8');
const guard=source.slice(source.indexOf('class InitialProductsStopped'),source.indexOf('async function navigateConfirmed'));
for (const http of [null,403,429]) {{
 const snapshots=[];
 const context=vm.createContext({{terminalCollectionStop,classify,supersededByVerification,observeHttpPageTransition,URL,humanVerificationActive:false,httpBlock:http?{{status:http}}:null, finalStatus:null,
 appendEvent:async()=>{{}},writeSnapshot:async(status,details)=>snapshots.push({{status,...details}})}});
 vm.runInContext(guard,context);
 await vm.runInContext("(async()=>{{try{{await guardInitialProducts({{url:'https://www.temu.com/login.html'}},'test');}}catch(e){{if(!(e instanceof InitialProductsStopped))throw e;}}}})()",context);
 assert.equal(context.finalStatus,'STOPPED_LOGIN_REQUIRED');
 assert.equal(snapshots[0].automatic_retry,true);
 assert.equal(snapshots[0].automatic_recovery_eligible,true);
 assert.equal(snapshots[0].manual_action_required,false);
 assert.equal(snapshots[0].resume_requires_session_rotation,true);
 if(http) {{
  await vm.runInContext("(async()=>{{try{{await guardInitialProducts({{url:'https://www.temu.com/search_result.html',text:'Access denied'}},'test');}}catch(e){{if(!(e instanceof InitialProductsStopped))throw e;}}}})()",context);
  assert.equal(context.finalStatus,'STOPPED_HTTP_'+http);
  assert.equal(snapshots[1].automatic_retry,false);
 }}
 assert.equal(snapshots.length,http?2:1);
}}
"""
    completed=subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True,timeout=15)
    assert completed.returncode==0,completed.stderr


def test_bootstrap_network_failure_rotates_unless_explicit_http_block():
    assert transport_recovery_allowed({'status': 'STOPPED_PROXY_OR_NETWORK_FAILURE'})
    for code in (403, 429):
        assert not transport_recovery_allowed({'status': 'STOPPED_PROXY_OR_NETWORK_FAILURE', 'http_status': code})
        assert not transport_recovery_allowed({'status': 'STOPPED_PROXY_OR_NETWORK_FAILURE', 'http_block': {'status': code}})


def test_background_scroll_stall_uses_instant_and_reaches_bottom():
    script = r"""
import fs from 'node:fs'; import vm from 'node:vm'; import assert from 'node:assert/strict';
const source=fs.readFileSync('scripts/temu_flower_collect.mjs','utf8');
const code=source.slice(source.indexOf('async function visibleScrollToBottom'),source.indexOf('async function rememberSeeMoreBottomOffset'));
let y=0; const events=[]; const behaviors=[];
const context=vm.createContext({classify:()=>null,scan:async()=>({}),
 lightPageState:async()=>({scrollY:y,scrollHeight:4000,innerHeight:800}),rand:()=>300,
 sleep:async()=>{},appendEvent:async(name)=>events.push(name),
 execute:async(script,args)=>{behaviors.push(args[1]);if(args[1]==='instant')y=Math.min(3200,y+args[0]);}});
vm.runInContext(code,context);
await vm.runInContext('visibleScrollToBottom()',context);
assert.ok(y>=3170);
assert.ok(events.includes('SCROLL_STALL_RECOVERY'));
assert.ok(behaviors.includes('smooth')&&behaviors.includes('instant'));
"""
    result = subprocess.run(['node','--input-type=module','-e',script],cwd=ROOT,capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('ancestor_matches', [True, False])
def test_replacement_owner_accepts_windows_launcher_ancestor_only(tmp_path, monkeypatch, ancestor_matches):
    import psutil
    (tmp_path / 'worker-01-new.owner.json').write_text(json.dumps({
        'pid': 123, 'token': 'owned-token', 'worker': 'worker-01-new', 'purpose': 'collector'
    }), encoding='utf-8')
    monkeypatch.setattr(psutil, 'Process', lambda: SimpleNamespace(parents=lambda: [SimpleNamespace(pid=456), SimpleNamespace(pid=123 if ancestor_matches else 789)]))
    if ancestor_matches:
        with replacement_guard(tmp_path, 'worker-01-new', 'owned-token'):
            pass
    else:
        with pytest.raises(RuntimeError, match='owning supervisor'):
            with replacement_guard(tmp_path, 'worker-01-new', 'owned-token'):
                pass


@pytest.mark.parametrize('recovered', [True, False])
def test_missing_local_proxy_is_restarted_under_owned_lease(pipeline_fixture, monkeypatch, recovered):
    module, category, sessions = pipeline_fixture
    probes=[];launches=[]
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
    def connect(*args, **kwargs):
        probes.append(args)
        if len(probes)==1 or not recovered: raise OSError('not listening')
        return Connection()
    def launch(command, **kwargs):
        launches.append(command)
        with pytest.raises(RuntimeError,match='already owned'):
            with WorkerLease(module.ROOT/'runtime/worker-leases','worker-01-new'):pass
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.socket,'create_connection',connect)
    monkeypatch.setattr(module,'run_background_launcher',launch)
    monkeypatch.setattr(module.shutil,'which',lambda name:'pwsh')
    with WorkerLease(module.ROOT/'runtime/worker-leases','worker-01-new') as lease:
        if recovered: assert module.ensure_proxy_chain(1,17891,lease)
        else:
            with pytest.raises(RuntimeError,match='still unavailable'):module.ensure_proxy_chain(1,17891,lease)
    assert len(launches)==1 and launches[0][launches[0].index('-Worker')+1]=='1'


def test_login_error_page_recovers_even_when_transport_budget_was_used(pipeline_fixture, monkeypatch):
    module, category, sessions = pipeline_fixture
    output=module.run_root(category)/'temu/workers/worker-01-new/first'
    output.mkdir(parents=True)
    (output/'transport_recovery.json').write_text('{}',encoding='utf-8')
    calls=[];rotations=[]
    def collect(command,**kwargs):
        calls.append(command)
        out=Path(command[command.index('--output')+1])
        status='STOPPED_PROXY_OR_NETWORK_FAILURE' if len(calls)==1 else 'KEYWORD_LIMIT_REACHED'
        (out/'progress.json').write_text(json.dumps({'status':status,'confirmed_login_redirect':len(calls)==1}),encoding='utf-8')
        return 2 if len(calls)==1 else 0
    monkeypatch.setattr(module,'run_collector',collect)
    monkeypatch.setattr(module.subprocess,'run',lambda command,**kwargs: rotations.append(command) or SimpleNamespace(returncode=0))
    module.collect_temu(Path('category.json'),category,sessions)
    assert len(rotations)==1 and (output/'automatic_recovery.json').exists()


@pytest.mark.parametrize('old_status', ['STOPPED_HTTP_429', 'STOPPED_LOGIN_REQUIRED'])
@pytest.mark.parametrize('exit_code', [0, 1])
def test_previous_run_progress_never_drives_current_recovery_or_success(pipeline_fixture, monkeypatch, old_status, exit_code):
    module,category,sessions=pipeline_fixture
    output=module.run_root(category)/'temu/workers/worker-01-new/first'
    output.mkdir(parents=True)
    (output/'progress.json').write_text(json.dumps({'status':old_status}),encoding='utf-8')
    calls=[]
    def collect(command,**kwargs):
        query=command[command.index('--query')+1];calls.append(query)
        if len(calls)==1:return exit_code
        out=Path(command[command.index('--output')+1])
        (out/'progress.json').write_text(json.dumps({'status':'KEYWORD_LIMIT_REACHED'}),encoding='utf-8')
        return 0
    monkeypatch.setattr(module,'run_collector',collect)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:pytest.fail('stale status must not replace profile'))
    module.collect_temu(Path('category.json'),category,sessions)
    assert calls==['first','first','first','second','second']


def test_login_recovery_budget_resets_only_on_real_progress(pipeline_fixture):
    module, _, _ = pipeline_fixture
    allowed, record, progressed = module.login_recovery_budget({'automatic_rotation_attempted': True}, 99, 'old')
    assert allowed and not progressed and record['attempts_without_progress'] == 2
    allowed, record, _ = module.login_recovery_budget(record, 99, 'new')
    assert allowed and record['attempts_without_progress'] == 3
    assert module.login_recovery_budget(record, 99, 'next')[0]
    allowed, record, progressed = module.login_recovery_budget(record, 100, 'next')
    assert allowed and progressed and record['attempts_without_progress'] == 1


@pytest.mark.parametrize('progresses, expected_rotations', [(True, 5), (False, 5)])
@pytest.mark.parametrize('failure', ['STOPPED_HTTP_403','STOPPED_HTTP_429','STOPPED_SALES_SORT_UNCONFIRMED'])
def test_nonhuman_session_recovery_continues_past_three_and_retains_same_output(pipeline_fixture, monkeypatch, progresses, expected_rotations, failure):
    module, category, sessions = pipeline_fixture
    calls=[]; rotations=[]
    def collect(command, **kwargs):
        query=command[command.index('--query')+1]
        out=Path(command[command.index('--output')+1])
        calls.append((query,str(out)))
        count=sum(q=='first' for q,o in calls)
        blocked=query=='first' and count<=5
        (out/'progress.json').write_text(json.dumps({
            'status':failure if blocked else 'KEYWORD_LIMIT_REACHED',
            'confirmed_login_redirect':blocked and failure!='STOPPED_SALES_SORT_UNCONFIRMED',
            'unique_goods':count*10 if progresses else 10,
        }),encoding='utf-8')
        return 2 if blocked else 0
    monkeypatch.setattr(module,'run_collector',collect)
    monkeypatch.setattr(module.subprocess,'run',lambda command,**kwargs: rotations.append(command) or SimpleNamespace(returncode=0))
    module.collect_temu(Path('category.json'),category,sessions)
    assert len(rotations)==expected_rotations
    state=json.loads((module.run_root(category)/'temu/supervisors/worker-01-new.json').read_text('utf-8'))
    assert state['status']=='WORKER_COMPLETE'
    assert len({o for q,o in calls[:len(rotations)+1]})==1


@pytest.mark.parametrize('finalized', [False, True])
def test_provisional_plan_does_not_delete_reusable_lane(pipeline_fixture, monkeypatch, finalized):
    module, category, sessions = pipeline_fixture
    path=module.keyword_plan_path(category)
    payload=json.loads(path.read_text('utf-8'));payload['finalized']=finalized
    path.write_text(json.dumps(payload),encoding='utf-8')
    deleted=[]
    monkeypatch.setattr(module,'cleanup_completed_worker',lambda *args: deleted.append(args) or True)
    def collect(command,**kwargs):
        output=Path(command[command.index('--output')+1])
        (output/'progress.json').write_text(json.dumps({'status':'KEYWORD_LIMIT_REACHED'}),encoding='utf-8')
        return 0
    monkeypatch.setattr(module,'run_collector',collect)
    module.collect_temu(Path('category.json'),category,sessions)
    assert not deleted  # only parent final-queue cleanup may delete
