"""Actual console entry; injected child failures, no API/browser/process launches."""
import json,sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import web_console as web

@pytest.mark.parametrize('failure',['generation','amazon-preparation'])
def test_failed_new_generation_keeps_previous_draft_visible_after_restart(tmp_path,monkeypatch,failure):
    console=web.Console(tmp_path)
    folder=tmp_path/'runtime/web-console';folder.mkdir(parents=True)
    old=folder/'old.json';old.write_text('{"old":true}',encoding='utf8')
    console.draft={'id':'old-draft','category_name':'已审阅旧标准'};console.draft_path=old
    console.draft_key='old-key-never-persisted';console.persist_draft()
    before=old.read_bytes();saved=console.draft.copy()
    monkeypatch.setattr(console,'owner',lambda:None)
    def invoke(argv,**kwargs):
        if failure=='generation': return SimpleNamespace(returncode=1)
        output=Path(argv[argv.index('--output')+1]);output.write_text('{"temu":{}}',encoding='utf8')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    def preparation(*args): raise web.ConsoleError('AMAZON_FAILED','离线模拟榜单准备失败')
    monkeypatch.setattr(console,'collect_amazon_and_plan',preparation)
    pending=[]
    class InlineThread:
        def __init__(self,*,target,args,daemon):self.target,self.args=target,args
        def start(self):pending.append(self)
    monkeypatch.setattr(web.threading,'Thread',InlineThread)
    console.start({'category_name':'另一个新品类','amazon_count':1,'per_keyword':2,'api_key':'new-placeholder'})
    job=pending.pop();job.target(*job.args)  # Run after start releases its lock.
    assert console.draft==saved and console.draft_path==old and console.draft_key is None
    assert old.read_bytes()==before and not console.busy
    restored=web.Console(tmp_path)
    assert restored.draft==saved and restored.draft_path==old
    assert restored.generation_error and restored.pending_scope['category']=='另一个新品类'
    persisted=(folder/'resume-draft.json').read_text('utf8')
    assert 'old-key-never-persisted' not in persisted and 'new-placeholder' not in persisted

@pytest.mark.parametrize('explanation_fails',[False,True])
def test_new_usable_draft_is_not_replaced_by_previous_when_explanation_fails(tmp_path,monkeypatch,explanation_fails):
    from test_generic_agent_entry import authored_case
    from selection_automation.identity_policy import compile_policy
    console=web.Console(tmp_path);console.busy=True
    old=tmp_path/'old.json';old.write_text('old reviewed bytes',encoding='utf8')
    previous={'draft':{'id':'old'},'draft_path':old,'draft_amazon_path':None,'draft_plan_path':None}
    new=compile_policy(authored_case('new_cap','新杯盖','threaded join','snap join'))
    def invoke(argv,**kwargs):
        if 'generate_category.py' in argv[0]:
            Path(argv[argv.index('--output')+1]).write_text(json.dumps(new),encoding='utf8')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    amazon=tmp_path/'a.json';amazon.write_text('{}',encoding='utf8')
    plan=tmp_path/'p.json';plan.write_text('{"keywords":[],"coverage":{}}',encoding='utf8')
    monkeypatch.setattr(console,'collect_amazon_and_plan',lambda *a:(amazon,plan))
    def explanation(source,*args):
        if explanation_fails:raise web.ConsoleError('EXPLANATION_FAILED','离线模拟解释失败')
        return source
    monkeypatch.setattr(console,'explain_file',explanation)
    console._start({'category_name':'新杯盖','amazon_count':1,'per_keyword':2,'api_key':'new-placeholder'},previous)
    assert console.draft['category_name']=='新杯盖' and console.draft['id']!='old'
    generated=json.loads(console.draft_path.read_text('utf8'))
    assert console.draft_path!=old and generated['category_key'].startswith('new_cap_')
    assert all(generated[layer]==new[layer] for layer in ('stage1','stage2','stage3'))
    assert old.read_text('utf8')=='old reviewed bytes'
    assert not console.busy
