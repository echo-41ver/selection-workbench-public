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
    from offline_preparation import completed_amazon_snapshot,extract_complete_visual
    console=web.Console(tmp_path);console.busy=True
    old=tmp_path/'old.json';old.write_text('old reviewed bytes',encoding='utf8')
    previous={'draft':{'id':'old'},'draft_path':old,'draft_amazon_path':None,'draft_plan_path':None}
    new=compile_policy(authored_case('new_cap','新杯盖','threaded join','snap join'))
    calls=[];before_explanation={}
    def invoke(argv,**kwargs):
        calls.append(argv)
        if 'generate_category.py' in argv[0]:
            Path(argv[argv.index('--output')+1]).write_text(json.dumps(new),encoding='utf8')
        elif len(argv)>1 and 'collect_amazon_search_frontend.mjs' in argv[1]:
            category=web.selection.read(Path(argv[argv.index('--config')+1]))
            web.selection.save(Path(argv[argv.index('--output')+1]),completed_amazon_snapshot(category,'Fixture cup lid'))
        elif 'extract_match_criteria.py' in argv[0]:
            extract_complete_visual(argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    def explanation(source,*args):
        before_explanation.update(web.selection.read(source))
        if explanation_fails:raise web.ConsoleError('EXPLANATION_FAILED','离线模拟解释失败')
        return source
    monkeypatch.setattr(console,'explain_file',explanation)
    console._start({'category_name':'新杯盖','amazon_count':1,'per_keyword':2,'api_key':'new-placeholder'},previous)
    assert console.draft['category_name']=='新杯盖' and console.draft['id']!='old'
    generated=json.loads(console.draft_path.read_text('utf8'))
    assert console.draft_path!=old and generated['category_key'].startswith('new_cap_')
    assert all(generated[layer]==before_explanation[layer] for layer in ('stage1','stage2','stage3'))
    assert all(generated[layer]==new[layer] for layer in ('stage1','stage2'))
    assert all(generated['stage3'][key]==value for key,value in new['stage3'].items())
    assert web.Console.visual_preparation_complete(generated,web.selection.read(console.draft_amazon_path))
    assert sum('generate_category.py' in row[0] for row in calls)==1
    assert sum('extract_match_criteria.py' in row[0] for row in calls)==1
    assert old.read_text('utf8')=='old reviewed bytes'
    assert not console.busy


@pytest.mark.parametrize('failure',['nonzero','incomplete'])
def test_unfinished_visual_preparation_keeps_previous_and_never_explains(tmp_path,monkeypatch,failure):
    from test_generic_agent_entry import authored_case
    from selection_automation.identity_policy import compile_policy
    from offline_preparation import completed_amazon_snapshot,extract_complete_visual
    console=web.Console(tmp_path);console.busy=True
    old=tmp_path/'old.json';old.write_text('reviewed old rules',encoding='utf-8')
    saved={'id':'previous-reviewed','category_name':'旧品类'}
    previous={'draft':saved,'draft_path':old,'draft_amazon_path':None,'draft_plan_path':None}
    category=compile_policy(authored_case('new_cap','新杯盖','threaded join','snap join'))
    def invoke(argv,**kwargs):
        if 'generate_category.py' in argv[0]:
            web.selection.save(Path(argv[argv.index('--output')+1]),category)
        elif len(argv)>1 and 'collect_amazon_search_frontend.mjs' in argv[1]:
            current=web.selection.read(Path(argv[argv.index('--config')+1]))
            web.selection.save(Path(argv[argv.index('--output')+1]),completed_amazon_snapshot(current,'Fixture cup lid'))
        elif 'extract_match_criteria.py' in argv[0]:
            if failure=='nonzero':return SimpleNamespace(returncode=2)
            partial=extract_complete_visual(argv)
            partial['match_criteria']['image_review']['completed_images']=0
            web.selection.save(Path(argv[argv.index('--category')+1]),partial)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    monkeypatch.setattr(console,'explain_file',lambda *a:pytest.fail('unfinished images cannot reach explanation'))
    console._start({'category_name':'新杯盖','amazon_count':1,'per_keyword':2,'api_key':'test-placeholder'},previous)
    assert console.draft==saved and console.draft_path==old
    assert console.generation_error['code']=='VISUAL_PREPARATION_INCOMPLETE'
    assert console.preparation_path and console.preparation_path!=old
    assert old.read_text('utf-8')=='reviewed old rules' and not console.busy
