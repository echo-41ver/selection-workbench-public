"""Normal startup must not leave an existing web without its verified watchdog."""
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import persistent_web_service as persistent
import restart_web as service
from selection_automation.process_lock import ProcessLock

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows independent startup')


class Process:
    def __init__(self, pid, root, script, created, *, active=True):
        self.pid=pid;self.root=root;self.created=created;self.active=active
        self.args=['python',str(root/'scripts'/script)]
        self.info={'pid':pid,'name':'python.exe'}
        if script=='persistent_web_service.py':self.args+=['--host','--port','8765']
    def cwd(self):return str(self.root)
    def name(self):return self.info['name']
    def cmdline(self):return self.args
    def create_time(self):return self.created
    def terminate(self):pytest.fail('Adoption must not terminate any process')


class Adoption:
    def __init__(self, root, monkeypatch, prior='exited'):
        self.root=root;self.port=8765;self.calls=[];self.clock=0;self.health_ready=True
        self.web=Process(900001,root,'web_console.py',100)
        self.old_host=Process(900002,root,'persistent_web_service.py',90,active=False)
        self.new_host=Process(900003,root,'persistent_web_service.py',110)
        self.processes={self.web.pid:self.web}
        self.marker=root/'runtime/web-console/persistent-service-8765.json'
        self.marker.parent.mkdir(parents=True)
        if prior=='exited':
            self.processes[self.old_host.pid]=self.old_host;self.write(self.old_host)
        elif prior=='missing_process':self.write(self.old_host)
        self.business=root/'runs/original_task'
        self.business.mkdir(parents=True)
        for name in ('STOP','category.json','requests.jsonl','request_intents.jsonl','results.jsonl'):
            (self.business/name).write_text('unchanged original '+name,encoding='utf-8')
        self.before={path:path.read_bytes() for path in self.business.iterdir()}
        monkeypatch.setattr(service,'listener',lambda port:self.web)
        monkeypatch.setattr(service.psutil,'Process',self.process)
        monkeypatch.setattr(service.psutil,'process_iter',lambda *args:list(self.processes.values()))
        monkeypatch.setattr(service,'native_process_info',self.native)
        monkeypatch.setattr(persistent,'healthy',lambda port:self.health_ready)
        monkeypatch.setattr(persistent,'start',self.start)
        monkeypatch.setattr(service.subprocess,'Popen',lambda *a,**k:pytest.fail('No caller-owned child may start'))
        monkeypatch.setattr(service,'stop_console',lambda *a:pytest.fail('Existing web must remain running'))
        monkeypatch.setattr(service.time,'monotonic',lambda:self.clock)
        monkeypatch.setattr(service.time,'sleep',lambda seconds:setattr(self,'clock',self.clock+1))
    def process(self,pid):
        if pid not in self.processes:raise psutil.NoSuchProcess(pid)
        return self.processes[pid]
    def native(self,pid):
        process=self.process(pid)
        return {'active':process.active,'created':process.created,
                'exit_code':259 if process.active else 0xC000013A,'exited':None if process.active else 109}
    def write(self,host,**extra):
        self.marker.write_text(json.dumps({'root':str(self.root),'port':self.port,'pid':host.pid,
                                          'created':host.created,'launch_mode':'WINDOWS_TASK_SCHEDULER',**extra}),encoding='utf-8')
    def start(self,port,root):
        self.calls.append((port,root));self.processes[self.new_host.pid]=self.new_host;self.write(self.new_host)
        return {'task_name':'same-installation-manual-task','launch_mode':'WINDOWS_TASK_SCHEDULER','automatic_trigger':False}
    def intact(self):
        assert all(path.read_bytes()==data for path,data in self.before.items())
        assert self.web.pid==900001 and self.web.created==100


@pytest.mark.parametrize('prior',['exited','missing_registration','missing_process'])
def test_normal_start_adopts_existing_web_and_second_start_reuses_same_host(tmp_path,monkeypatch,prior):
    fixture=Adoption(tmp_path,monkeypatch,prior)
    result=service.ensure_running(8765,tmp_path)
    assert result['persistent_ready'] and result['host_recovered']
    assert result['pid']==fixture.web.pid and result['persistent_host']=={'pid':900003,'created':110}
    record=json.loads(Path(result['host_recovery_record']).read_text('utf-8'))
    assert record['state']=='ready' and record['previous_host']['state'] in {'exited','missing_registration','process_missing'}
    if prior=='exited':assert record['previous_host']['native_exit_code']==0xC000013A
    second=service.ensure_running(8765,tmp_path)
    assert second['persistent_ready'] and not second['host_recovered']
    assert len(fixture.calls)==1
    fixture.intact()


@pytest.mark.parametrize('bad',['corrupt','wrong_root','wrong_port','invalid_pid','invalid_created',
                               'pid_reuse','unrelated_live_command','unregistered_live_host','host_lock'])
def test_unknown_host_identity_fails_closed_without_duplicate_or_web_stop(tmp_path,monkeypatch,bad):
    fixture=Adoption(tmp_path,monkeypatch)
    if bad=='corrupt':fixture.marker.write_text('{incomplete',encoding='utf-8')
    elif bad=='wrong_root':fixture.write(fixture.old_host,root=str(tmp_path/'foreign'))
    elif bad=='wrong_port':fixture.write(fixture.old_host,port=8766)
    elif bad=='invalid_pid':fixture.write(fixture.old_host,pid=True)
    elif bad=='invalid_created':fixture.write(fixture.old_host,created=float('nan'))
    elif bad=='pid_reuse':fixture.old_host.created+=1
    elif bad=='unrelated_live_command':fixture.old_host.active=True;fixture.old_host.args=['python','foreign.py']
    elif bad=='unregistered_live_host':
        fixture.old_host.active=True;fixture.marker.unlink()
    if bad=='host_lock':
        with ProcessLock(tmp_path/'runtime/web-console/persistent-service-8765.lock'):
            with pytest.raises(RuntimeError,match='常驻宿主'):service.ensure_running(8765,tmp_path)
    else:
        with pytest.raises(RuntimeError,match='常驻宿主'):service.ensure_running(8765,tmp_path)
    assert not fixture.calls
    fixture.intact()


@pytest.mark.parametrize('failure',['scheduler','no_registration','wrong_new_identity','web_changed','unhealthy'])
def test_failed_host_recovery_preserves_old_control_exit_and_does_not_claim_ready(tmp_path,monkeypatch,failure):
    fixture=Adoption(tmp_path,monkeypatch)
    def start(port,root):
        fixture.calls.append((port,root))
        if failure=='scheduler':raise RuntimeError('scheduler launch failed')
        if failure!='no_registration':
            fixture.processes[fixture.new_host.pid]=fixture.new_host
            fixture.write(fixture.new_host)
        if failure=='wrong_new_identity':fixture.new_host.args=['python','wrong.py']
        if failure=='web_changed':monkeypatch.setattr(service,'listener',lambda port:Process(900004,tmp_path,'web_console.py',111))
        if failure=='unhealthy':fixture.health_ready=False
        return {'task_name':'same-manual-task'}
    monkeypatch.setattr(persistent,'start',start)
    with pytest.raises(RuntimeError,match='恢复未确认'):service.ensure_running(8765,tmp_path)
    records=list((tmp_path/'runtime/web-console').glob('host-recovery-*.json'))
    assert len(records)==1
    record=json.loads(records[0].read_text('utf-8'))
    assert record['state']=='failed' and record['previous_host']['native_exit_code']==0xC000013A
    assert 'host' not in record and len(fixture.calls)==1
    fixture.intact()


def test_concurrent_start_and_unhealthy_existing_web_never_dispatch_new_host(tmp_path,monkeypatch):
    fixture=Adoption(tmp_path,monkeypatch)
    with ProcessLock(tmp_path/'runtime/web-console/restart-8765.lock'):
        with pytest.raises(RuntimeError,match='Another supervisor'):service.ensure_running(8765,tmp_path)
    monkeypatch.setattr(persistent,'healthy',lambda port:False)
    with pytest.raises(RuntimeError,match='健康检查未通过'):service.ensure_running(8765,tmp_path)
    assert not fixture.calls
    fixture.intact()


def test_attached_diagnostic_mode_keeps_its_existing_nonpersistent_semantics(tmp_path,monkeypatch):
    fixture=Adoption(tmp_path,monkeypatch)
    result=service.ensure_running(8765,tmp_path,independent=False)
    assert result['ok'] and 'persistent_ready' not in result and not fixture.calls
    fixture.intact()


def test_web_appearing_between_locked_checks_is_reused_without_restart(tmp_path,monkeypatch):
    fixture=Adoption(tmp_path,monkeypatch)
    checks=[None,fixture.web]
    monkeypatch.setattr(service,'listener',lambda port:checks.pop(0) if checks else fixture.web)
    monkeypatch.setattr(service,'status',lambda port:pytest.fail('Existing web does not need status/report or restart'))
    result=service.ensure_running(8765,tmp_path)
    assert result['persistent_ready'] and result['pid']==900001 and len(fixture.calls)==1
    fixture.intact()


def test_web_identity_changed_while_reading_new_host_never_returns_ready(tmp_path,monkeypatch):
    fixture=Adoption(tmp_path,monkeypatch)
    original=service.registered_persistent_host
    def registration(root,port):
        host,prior=original(root,port)
        if host:
            monkeypatch.setattr(service,'listener',lambda port:Process(900004,tmp_path,'web_console.py',111))
        return host,prior
    monkeypatch.setattr(service,'registered_persistent_host',registration)
    with pytest.raises(RuntimeError,match='网页身份发生变化'):service.ensure_running(8765,tmp_path)
    record=json.loads(next((tmp_path/'runtime/web-console').glob('host-recovery-*.json')).read_text('utf-8'))
    assert record['state']=='failed' and 'host' not in record
    fixture.intact()


def test_related_unreadable_host_is_not_hidden_by_an_earlier_unrelated_python(tmp_path,monkeypatch):
    fixture=Adoption(tmp_path,monkeypatch,'missing_registration')
    unrelated=Process(900005,tmp_path,'mcp_server.py',20)
    denied=Process(900006,tmp_path,'persistent_web_service.py',21)
    def denied_cwd():raise psutil.AccessDenied(denied.pid)
    denied.cwd=denied_cwd
    monkeypatch.setattr(service.psutil,'process_iter',lambda *args:[unrelated,denied])
    with pytest.raises(RuntimeError,match='身份暂不可读'):service.ensure_running(8765,tmp_path)
    assert not fixture.calls
    fixture.intact()


def test_real_windows_kernel_distinguishes_retained_exited_process_from_active_identity():
    # Keep Popen's process handle so the exact exited PID remains queryable.
    child=subprocess.Popen([sys.executable,'-c','raise SystemExit(7)'],creationflags=subprocess.CREATE_NO_WINDOW)
    assert child.wait(timeout=15)==7
    info=service.native_process_info(child.pid)
    assert info['active'] is False and info['exit_code']==7
    assert info['created'] and info['exited']>=info['created']
    assert service.native_process_active(child.pid) is False
