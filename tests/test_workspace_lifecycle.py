import json
from pathlib import Path
import sys
from types import SimpleNamespace as N
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import web_console as web


def task(root,key='old'):
    base=root/'runs'/key;base.mkdir(parents=True)
    web.selection.save(base/'category.json',{'category_key':key,'category_name_zh':'历史测试'})
    web.selection.save(base/'status.json',{'状态':'已停止'})
    web.selection.save(root/'runtime/last_run.json',{'category_key':key})
    (base/'kept-data.json').write_text('{"preserved":true}')
    return base


def test_finished_tasks_do_not_return_until_explicit_restore(tmp_path,monkeypatch):
    base=task(tmp_path)
    web.selection.save(tmp_path/'runtime/web-console/state.json',{'category_key':'old'})
    console=web.Console(tmp_path);monkeypatch.setattr(console,'owner',lambda:None)
    assert console.current_run_key()==(None,False)
    monkeypatch.setattr(console,'invoke',lambda *a,**k:(_ for _ in ()).throw(AssertionError('restore must not launch work')))
    assert console.restore_history({'category_key':'old'})['category_key']=='old'
    assert console.current_run_key()==('old',False)
    assert (base/'kept-data.json').read_text()=='{"preserved":true}'


def test_new_draft_and_live_state_do_not_inherit_old_run(tmp_path,monkeypatch):
    task(tmp_path);console=web.Console(tmp_path);console.key='old';console.draft={'id':'new-draft'}
    monkeypatch.setattr(console,'owner',lambda:None)
    assert console.current_run_key()==(None,False)
    assert console.live_status()=={'ok':True,'category_key':None}
    state=console.status()
    assert state['category_key'] is None and state['details']=={}
    assert state['draft']['id']=='new-draft' and not state['active']


def test_confirmed_stop_clears_workspace_but_keeps_history_and_results(tmp_path,monkeypatch):
    base=task(tmp_path);console=web.Console(tmp_path);console.key='old';console.draft_used=True
    monkeypatch.setattr(console,'owner',lambda:None)
    monkeypatch.setattr(console,'invoke',lambda *a,**k:N(returncode=0))
    console.action('stop',{})
    state=console.status()
    assert state['category_key'] is None and state['details']=={} and state['draft'] is None
    assert json.loads((tmp_path/'runtime/web-console/state.json').read_text())['category_key'] is None
    assert (base/'kept-data.json').exists()
    assert web.Console(tmp_path).key is None


def stopped_workspace(tmp_path,monkeypatch):
    import selection_automation.stage3_progress as stage3
    base=task(tmp_path)
    web.selection.request_task_stop(base)
    web.selection.save(base/'status.json',{'状态':'停止中','阶段':{'excel':{'状态':'已更新'}},'证据':'保留'})
    console=web.Console(tmp_path);console.key='old';console.draft_used=True
    console.workspace_stop_pending=True
    monkeypatch.setattr(console,'owner',lambda:None)
    monkeypatch.setattr(console,'workspace_writers_stopped',lambda base:(True,{'temu_union':False,'stage12':False}))
    monkeypatch.setattr(stage3,'controller_alive',lambda base:False)
    monkeypatch.setattr(web,'codex_review_control',lambda *a,**kw:{'running':False})
    return base,console,stage3


def test_verified_stop_persists_terminal_after_stale_cli_owner_overwrite(tmp_path,monkeypatch):
    base,console,_=stopped_workspace(tmp_path,monkeypatch)
    def stale_cli_stop(*args,**kwargs):
        # The supervisor can publish its terminal state after STOP, before a
        # CLI with the earlier live_owner result writes "stopping" over it.
        saved=web.selection.read(base/'status.json')
        web.selection.save(base/'status.json',{**saved,'状态':'已停止'})
        web.selection.save(base/'status.json',{**saved,'状态':'停止中'})
        return N(returncode=0)
    monkeypatch.setattr(console,'invoke',stale_cli_stop)
    console.action('stop',{})
    assert web.selection.read(base/'status.json')['状态']=='停止中'
    console.finish_workspace_stop(None)
    saved=web.selection.read(base/'status.json')
    assert saved['状态']=='已停止'
    assert saved['阶段']['excel']['状态']=='已更新' and saved['证据']=='保留'
    assert console.key is None and not console.workspace_stop_pending
    assert (base/'kept-data.json').exists()
    assert web.selection.read(tmp_path/'runtime/web-console/state.json')['category_key'] is None


@pytest.mark.parametrize('remaining',['owner','busy','stage3_busy','stage12','unknown_writer','controller','codex'])
def test_stop_does_not_persist_terminal_before_all_existing_gates_close(tmp_path,monkeypatch,remaining):
    base,console,stage3=stopped_workspace(tmp_path,monkeypatch)
    owner={'category_key':'old'} if remaining=='owner' else None
    console.busy=remaining=='busy';console.stage3_busy=remaining=='stage3_busy'
    if remaining in ('stage12','unknown_writer'):
        value=True if remaining=='stage12' else None
        monkeypatch.setattr(console,'workspace_writers_stopped',lambda base:(False,{'stage12':value,'temu_union':False}))
    if remaining=='controller':monkeypatch.setattr(stage3,'controller_alive',lambda base:True)
    if remaining=='codex':monkeypatch.setattr(web,'codex_review_control',lambda *a,**kw:{'running':True})
    before=(base/'status.json').read_bytes()
    console.finish_workspace_stop(owner)
    assert (base/'status.json').read_bytes()==before
    assert console.key=='old' and console.workspace_stop_pending
