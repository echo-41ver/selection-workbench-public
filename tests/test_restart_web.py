from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import restart_web as service


def test_rejects_other_installation_before_termination(tmp_path, monkeypatch):
    process = SimpleNamespace(cwd=lambda: str(tmp_path), cmdline=lambda: ['python', 'other.py'])
    monkeypatch.setattr(service, 'listener', lambda port: process)
    with pytest.raises(RuntimeError, match='其他程序'):
        service.restart(8765, tmp_path)


@pytest.mark.parametrize('state', [
    {'ok': True, 'active': True},
    {'ok': True, 'active': False, 'market_busy': True},
    {'ok': True},
    {'ok': False, 'active': False},
])
def test_does_not_stop_busy_or_unknown_state(tmp_path, monkeypatch, state):
    process = SimpleNamespace(cwd=lambda: str(tmp_path),
                              cmdline=lambda: ['python', str(tmp_path / 'scripts/web_console.py')])
    monkeypatch.setattr(service, 'listener', lambda port: process)
    monkeypatch.setattr(service, 'status', lambda port: state)
    with pytest.raises(RuntimeError, match='任务正在运行或状态无法确认'):
        service.restart(8765, tmp_path)


class MaintenanceProcess:
    def __init__(self,pid,root,script,created=101):
        self.pid=pid;self.root=root;self.script=script;self.created=created
        self.running=True;self.terminated=False;self.ancestors=[]
        self.descendants=[]
        self.args=['python',str(root/'scripts'/script)]
    def cwd(self):return str(self.root)
    def cmdline(self):return self.args
    def create_time(self):return self.created
    def is_running(self):return self.running
    def status(self):return 'running'
    def parents(self):return self.ancestors
    def children(self,recursive=False):return self.descendants
    def terminate(self):self.terminated=True;self.running=False
    def wait(self,timeout):return 0


def maintenance_fixture(root,monkeypatch):
    previous=MaintenanceProcess(10,root,'web_console.py')
    owner=MaintenanceProcess(20,root,'selection.py')
    base=root/'runs/fixture';base.mkdir(parents=True)
    category=base/'category.json';category.write_text('{}',encoding='utf-8')
    owner.args+=['run','--prepared-start','--category',str(category)]
    record={'pid':20,'created':101,'category_key':'fixture'}
    runtime=root/'runtime';runtime.mkdir()
    (runtime/'active.json').write_text(json.dumps(record),encoding='utf-8')
    state={'ok':True,'active':True,'console_busy':False,'market_busy':False,
           'stage3_control':{'running':False},'generation':{},'category_key':'fixture'}
    monkeypatch.setattr(service.psutil,'Process',lambda pid:owner)
    return previous,owner,state


def test_explicit_maintenance_restarts_only_console_and_preserves_independent_task(tmp_path,monkeypatch):
    previous,owner,state=maintenance_fixture(tmp_path,monkeypatch)
    current=MaintenanceProcess(30,tmp_path,'web_console.py')
    listeners=iter([previous,None,current])
    monkeypatch.setattr(service,'listener',lambda port:next(listeners))
    monkeypatch.setattr(service,'status',lambda port:state)
    child=SimpleNamespace(pid=30,poll=lambda:None)
    monkeypatch.setattr(service.subprocess,'Popen',lambda *args,**kwargs:child)
    result=service.restart(8765,tmp_path,keep_task=True)
    assert previous.terminated and not owner.terminated and owner.is_running()
    assert result['pid']==30 and result['task_preserved']=={'pid':20,'created':101,'category_key':'fixture'}
    assert not (tmp_path/'runs/fixture/STOP').exists()
    assert json.loads((tmp_path/'runtime/active.json').read_text('utf-8'))==result['task_preserved']


@pytest.mark.parametrize('actual_child', [False, True])
def test_maintenance_refuses_current_owned_readonly_launcher_or_interpreter(tmp_path, monkeypatch, actual_child):
    previous, owner, state = maintenance_fixture(tmp_path, monkeypatch)
    reader = MaintenanceProcess(40, tmp_path, 'unused.py', created=202)
    reader.args = ['python', '-m', 'selection_automation.stage3_progress', '--base', str(tmp_path/'runs/fixture')]
    reader.ancestors = [previous]
    previous.descendants = [reader]
    processes = {20: owner, 40: reader}
    if actual_child:
        child = MaintenanceProcess(41, tmp_path, 'unused.py', created=203)
        child.args = reader.args[:];child.ancestors = [reader, previous]
        reader.running = False  # The interpreter still owns useful work.
        previous.descendants.append(child);processes[41] = child
    monkeypatch.setattr(service.psutil, 'Process', lambda pid: processes[pid])
    monkeypatch.setattr(service, 'native_process_active', lambda pid: True)
    monkeypatch.setattr(service, 'listener', lambda port: previous)
    monkeypatch.setattr(service, 'status', lambda port: state)
    monkeypatch.setattr(service.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Must not restart'))
    with pytest.raises(RuntimeError, match='READONLY_READER_BUSY'):
        service.restart(8765, tmp_path, keep_task=True)
    assert not previous.terminated and not owner.terminated and owner.is_running()


@pytest.mark.parametrize('reason', ['base', 'cwd', 'module', 'parent', 'terminated', 'dead', 'reused'])
def test_readonly_guard_ignores_foreign_terminated_or_stale_identity(tmp_path, monkeypatch, reason):
    previous, owner, state = maintenance_fixture(tmp_path, monkeypatch)
    reader = MaintenanceProcess(40, tmp_path, 'unused.py', created=202)
    reader.args = ['python', '-m', 'selection_automation.stage3_progress', '--base', str(tmp_path/'runs/fixture')]
    reader.ancestors = [previous];previous.descendants = [reader]
    current = reader
    if reason == 'base': reader.args[-1] = str(tmp_path/'runs/foreign')
    if reason == 'cwd': reader.root = tmp_path/'foreign'
    if reason == 'module': reader.args[2] = 'other_module'
    if reason == 'parent': reader.ancestors = [owner]
    if reason == 'dead': reader.running = False
    if reason == 'reused': current = MaintenanceProcess(40, tmp_path, 'other.py', created=303)
    monkeypatch.setattr(service.psutil, 'Process', lambda pid: current)
    monkeypatch.setattr(service, 'native_process_active', lambda pid: reason != 'terminated')
    service.validate_readonly_idle(previous, tmp_path, state)
    assert not previous.terminated and not owner.terminated and owner.is_running()


def test_readonly_guard_refuses_unreadable_process_tree_without_stopping_task(tmp_path, monkeypatch):
    previous, owner, state = maintenance_fixture(tmp_path, monkeypatch)
    def denied(**kwargs):
        raise service.psutil.AccessDenied(previous.pid)
    monkeypatch.setattr(previous, 'children', denied)
    with pytest.raises(RuntimeError, match='无法确认网页只读核对进程状态'):
        service.validate_readonly_idle(previous, tmp_path, state)
    assert not previous.terminated and not owner.terminated


@pytest.mark.parametrize('busy',['console','market','stage3','generation','unknown'])
def test_maintenance_never_terminates_busy_console(tmp_path,monkeypatch,busy):
    previous,owner,state=maintenance_fixture(tmp_path,monkeypatch)
    if busy=='console':state['console_busy']=True
    if busy=='market':state['market_busy']=True
    if busy=='stage3':state['stage3_control']['running']=True
    if busy=='generation':state['generation']={'stage':'generating'}
    if busy=='unknown':state.pop('console_busy')
    monkeypatch.setattr(service,'listener',lambda port:previous)
    monkeypatch.setattr(service,'status',lambda port:state)
    with pytest.raises(RuntimeError,match='控制台存在'):
        service.restart(8765,tmp_path,keep_task=True)
    assert not previous.terminated and not owner.terminated


@pytest.mark.parametrize('foreign',['cwd','command','creation','category','descendant','dead','registration'])
def test_maintenance_rejects_foreign_or_dependent_owner(tmp_path,monkeypatch,foreign):
    previous,owner,state=maintenance_fixture(tmp_path,monkeypatch)
    if foreign=='cwd':owner.root=tmp_path/'another-installation'
    if foreign=='command':owner.args=['python','foreign.py','run']
    if foreign=='creation':owner.created=102
    if foreign=='category':owner.args[-1]=str(tmp_path/'runs/other/category.json')
    if foreign=='descendant':owner.ancestors=[previous]
    if foreign=='dead':owner.running=False
    if foreign=='registration':state['category_key']='other'
    monkeypatch.setattr(service,'listener',lambda port:previous)
    monkeypatch.setattr(service,'status',lambda port:state)
    with pytest.raises(RuntimeError,match='独立运行任务'):
        service.restart(8765,tmp_path,keep_task=True)
    assert not previous.terminated and not owner.terminated
