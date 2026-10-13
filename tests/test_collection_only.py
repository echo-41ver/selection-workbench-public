import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import selection
import pipeline
from selection_automation.category_config import load_category
from selection_automation.collection_contract import source_completion
from test_supervisor_control_flow import LocalRun


def test_prepare_collection_without_any_analysis_rules(tmp_path, monkeypatch):
    cat=json.loads((ROOT/'config/categories/power_drill.json').read_text('utf-8'))
    cat['execution_mode']='collection'
    for name in ('stage1','stage2','stage3'): cat.pop(name)
    path=tmp_path/'input.json';selection.save(path,cat)
    monkeypatch.setattr(selection,'ROOT',tmp_path)
    monkeypatch.setattr(pipeline,'write_stage12_config',lambda *a: (_ for _ in ()).throw(AssertionError('analysis called')))
    frozen,loaded,base=selection.prepare(path)
    assert load_category(frozen)==cat
    assert not (base/'stage12_config.json').exists()
    assert pipeline.build_category_keyword_plan([],cat)


def test_collection_supervisor_never_launches_analysis(tmp_path, monkeypatch):
    run=LocalRun(tmp_path,monkeypatch)
    run.cat['execution_mode']='collection'
    selection.save(run.category,run.cat)
    selection.save(run.base/'stage123/paid_enabled.json',{'enabled':True})
    run.on_sleep=lambda: (run.base/'STOP').write_text('test complete')
    selection.supervise(run.category,False)
    assert not {'stage12','stage3'} & {s['name'] for s in run.spawns}
    assert {'amazon','temu_union'} <= {s['name'] for s in run.spawns}
    # Amazon remains running, yet TEMU starts in the very same supervisor cycle.
    assert next(s['tick'] for s in run.spawns if s['name']=='amazon')==next(s['tick'] for s in run.spawns if s['name']=='worker-01')


def test_completion_requires_current_union_but_no_analysis_db(tmp_path):
    from watch_temu_union import snapshot_signature
    cat={'amazon':{'target_count':1}}
    selection.save(tmp_path/'amazon/amazon_frontend_latest.json',{'records':[{'asin':'A','rank':1}],'status':'COMPLETE'})
    source=tmp_path/'temu/workers/w/q/products.jsonl';selection.save(source,{'goods_id':'T'})
    selection.save(tmp_path/'temu/union/products.jsonl',{'goods_id':'T'})
    selection.save(tmp_path/'temu/union/watch_state.json',{'status':'PUBLISHED','signature':snapshot_signature([source]),'unique_goods':1})
    assert source_completion(tmp_path,cat)['缺失或失败']==0
    source.write_text('{"goods_id":"T2"}')
    assert source_completion(tmp_path,cat) is None


def test_worker_exit_does_not_write_analysis_config(tmp_path,monkeypatch):
    cat=json.loads((ROOT/'config/categories/power_drill.json').read_text('utf-8'))
    cat['execution_mode']='collection'
    for name in ('stage1','stage2','stage3'): cat.pop(name)
    path=tmp_path/'category.json';selection.save(path,cat)
    monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    monkeypatch.setattr(pipeline,'collect_temu',lambda *a:None)
    monkeypatch.setattr(pipeline,'write_stage12_config',lambda *a: (_ for _ in ()).throw(AssertionError('analysis called')))
    monkeypatch.setattr(sys,'argv',['pipeline','collect-temu','--category',str(path),'--sessions',str(tmp_path/'sessions.json')])
    pipeline.main()


def test_web_collection_start_bypasses_rule_compiler_and_preserves_rules(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import web_console as web
    cat=json.loads((ROOT/'config/categories/power_drill.json').read_text('utf-8'))
    cat['amazon']['target_count']=1
    console=web.Console(tmp_path)
    console.draft_path=tmp_path/'runtime/web-console/category.json'
    selection.save(console.draft_path,cat)
    console.draft_amazon_path=console.draft_path.with_suffix('.amazon.json')
    selection.save(console.draft_amazon_path,{'records':[{'asin':'B000000001','rank':1,'title':'Drill'}],
        'source_url':cat['amazon']['bestsellers_url'],'status':'LISTING_COMPLETE'})
    console.draft_plan_path=console.draft_path.with_suffix('.keywords.json')
    selection.save(console.draft_plan_path,{'keywords':[{'query':'power drill','source_asins':['B000000001']}]})
    calls=[]
    def invoke(argv,**kwargs):
        calls.append(argv)
        assert Path(argv[0]).name=='selection.py' and argv[1]=='start'
        frozen=selection.read(Path(argv[-1]))
        selection.save(tmp_path/'runs'/frozen['category_key']/'category.json',frozen)
        # A successful launch must register the supervisor, not just return zero.
        selection.save(tmp_path/'runtime/active.json',
                       {'category_key':frozen['category_key'],'pid':123,'created':1})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(console,'invoke',invoke)
    monkeypatch.setattr(console,'owner',lambda:{'category_key':console.key,'pid':123,'created':1})
    console._run_draft({'execution_mode':'collection','worker_count':8})
    assert len(calls)==1 and console.draft_used
    frozen=selection.read(Path(calls[0][-1]))
    assert frozen['execution_mode']=='collection' and frozen['temu']['worker_count']==8
    assert all(frozen[name]==cat[name] for name in ('stage1','stage2','stage3'))
