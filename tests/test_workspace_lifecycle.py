import json
from types import SimpleNamespace as N
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
