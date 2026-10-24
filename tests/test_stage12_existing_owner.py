"""Keep the exact task's recovered local writer; never infer success from a PID."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import selection as s


def fixture(tmp_path,*,owner=None,child=None,attempts=2,finished=False):
    log=tmp_path/'logs';log.mkdir()
    launches=[]
    args=dict(children={'stage12':child} if child is not None else {},
        finished={'stage12'} if finished else set(),attempts={'stage12':attempts},next_at={},
        state={'stage12':{'状态':'失败，等待有界恢复','退出码':75}},
        launch=lambda *values:launches.append(values),log=log,
        existing_owner=lambda base:owner)
    return args,launches


def test_verified_recovery_prevents_duplicate_launch_and_preserves_exit_budget(tmp_path):
    args,launches=fixture(tmp_path,owner={'pid':123,'created':1000},attempts=3,finished=True)
    row=s.stage12_service_step(tmp_path,['watch'],**args)
    assert not launches and row['PID']==123 and row['external_service']
    assert row['状态']=='运行中，沿用本任务已恢复的第一二层后台程序'
    assert row['阻断'] is False and '退出码' not in row
    assert args['attempts']['stage12']==3 and 'stage12' in args['finished']
    assert row['previous_exit']['returncode']==75


def test_dead_duplicate_native_exit_is_recorded_before_reusing_recovered_owner(tmp_path):
    child=SimpleNamespace(pid=321,poll=lambda:75,returncode=75)
    args,launches=fixture(tmp_path,owner={'pid':123,'created':1000},child=child)
    row=s.stage12_service_step(tmp_path,['watch'],**args)
    assert not launches and row['PID']==123 and not args['children']
    event=json.loads((tmp_path/'logs/stage12_lifecycle.jsonl').read_text())
    assert event['event']=='PROCESS_EXIT_OBSERVED' and event['pid']==321 and event['returncode']==75
    assert row['previous_exit']['returncode']==75 and args['attempts']['stage12']==3


def test_unknown_owner_does_not_launch_or_hide_existing_failure_budget(tmp_path):
    args,launches=fixture(tmp_path,owner=None,attempts=3,finished=True)
    row=s.stage12_service_step(tmp_path,['watch'],**args)
    assert not launches and row['阻断'] and row['owner_verification_pending']
    assert args['attempts']['stage12']==3 and 'stage12' in args['finished']


def test_live_owned_child_keeps_normal_supervision_without_external_owner_probe(tmp_path):
    child=SimpleNamespace(pid=321,poll=lambda:None)
    args,launches=fixture(tmp_path,child=child)
    args['existing_owner']=lambda base:pytest.fail('The current child is still running')
    s.stage12_service_step(tmp_path,['watch'],**args)
    assert not launches and args['children']['stage12'] is child


def test_verified_owner_exit_is_not_left_advertised_as_running(tmp_path):
    args,launches=fixture(tmp_path,owner={'pid':123,'created':1000},attempts=3,finished=True)
    s.stage12_service_step(tmp_path,['watch'],**args)
    args['existing_owner']=lambda base:False
    row=s.stage12_service_step(tmp_path,['watch'],**args)
    assert not launches and row['阻断'] and row['recovery_exhausted']
    assert 'PID' not in row and '已退出' in row['状态']


def test_absent_owner_uses_original_bounded_launch_logic(tmp_path):
    args,launches=fixture(tmp_path,owner=False)
    s.stage12_service_step(tmp_path,['watch'],**args)
    assert launches==[('stage12',['watch'])]


@pytest.mark.parametrize('failure',['none','wrong_root','wrong_task','wrong_mode','no_process','reused_pid','denied','changed_record','dead_native'])
def test_adoption_checks_exact_task_script_native_liveness_ctime_and_record(tmp_path,monkeypatch,failure):
    import psutil,restart_web
    root=tmp_path/'root';base=root/'runs/task';path=base/'stage12/stage12_watch_owner.json';path.parent.mkdir(parents=True)
    config=base/'stage12_config.json';config.write_text(json.dumps({'output_dir':str(path.parent)}))
    owner={'pid':123,'created':1000,'config':str(config)};path.write_text(json.dumps(owner))
    monkeypatch.setattr(s,'ROOT',root)
    script=root/'scripts/run_incremental_stage12.py';target=config
    if failure=='wrong_root':script=tmp_path/'other/scripts/run_incremental_stage12.py'
    if failure=='wrong_task':target=root/'runs/another/stage12_config.json'
    class Process:
        def create_time(self):return 1001 if failure=='reused_pid' else 1000
        def cmdline(self):
            if failure=='denied':raise psutil.AccessDenied(123)
            if failure=='changed_record':path.write_text(json.dumps({**owner,'created':1002}))
            return ['python',str(script),'once' if failure=='wrong_mode' else 'watch','--config',str(target),
                    *(['--no-process'] if failure=='no_process' else [])]
    monkeypatch.setattr(psutil,'Process',lambda pid:Process())
    monkeypatch.setattr(restart_web,'native_process_active',lambda pid:failure!='dead_native')
    actual=s.stage12_existing_owner(base)
    if failure=='none':assert actual==owner
    else:assert actual is (None if failure in ('denied','changed_record') else False)


def test_unknown_owner_exception_does_not_create_second_writer(tmp_path):
    args,launches=fixture(tmp_path,owner=False)
    def denied(base):raise PermissionError('cannot inspect this process')
    args['existing_owner']=denied
    row=s.stage12_service_step(tmp_path,['watch'],**args)
    assert not launches and row['owner_verification_pending']
    assert row['owner_verification_error_type']=='PermissionError'


@pytest.mark.parametrize('bad',['config_shape','output_type','owner_shape','owner_identity'])
def test_malformed_ownership_records_are_unknown_instead_of_restart_permission(tmp_path,monkeypatch,bad):
    root=tmp_path/'root';base=root/'runs/task';out=base/'stage12';out.mkdir(parents=True)
    config=base/'stage12_config.json'
    config.write_text(json.dumps([] if bad=='config_shape' else {'output_dir':None if bad=='output_type' else str(out)}))
    owner=[] if bad=='owner_shape' else {'pid':True,'created':1000,'config':str(config)}
    (out/'stage12_watch_owner.json').write_text(json.dumps(owner))
    monkeypatch.setattr(s,'ROOT',root)
    assert s.stage12_existing_owner(base) is None
