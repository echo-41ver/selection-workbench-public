import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('generate_category', Path(__file__).resolve().parents[1]/'scripts/generate_category.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def test_search_login_uses_public_category_navigation_without_repeating_search(monkeypatch):
    import subprocess
    root = 'https://www.amazon.com/Best-Sellers/zgbs'
    home = 'https://www.amazon.com/Best-Sellers-Home-Kitchen/zgbs/home-garden'
    bags = 'https://www.amazon.com/Best-Sellers-Laundry-Bags/zgbs/home-garden/3744371'
    pages = {
        root: {'url':root, 'heading':'Best Sellers', 'rank_count':30,
               'links':[{'url':home, 'text':'Home & Kitchen'}]},
        home: {'url':home, 'heading':'Home & Kitchen', 'rank_count':30,
               'links':[{'url':bags, 'text':'Laundry Bags'}]},
        bags: {'url':bags, 'heading':'Best Sellers in Laundry Bags', 'rank_count':30, 'links':[]},
    }
    answers = iter([{'search_query':'laundry bag'}, {'next_url':home}, {'next_url':bags}, {'matches':True}])
    monkeypatch.setattr(generator, 'model_json', lambda *a, **kw:next(answers))
    searches, reads = [], []
    def search(query):
        searches.append(query)
        raise generator.CategoryGenerationError('search login', 'AMAZON_LISTING_READ_FAILED') from subprocess.CalledProcessError(
            1, ['node'], stderr='{"code":"AMAZON_LOGIN_REQUIRED"}')
    monkeypatch.setattr(generator, 'search_listing_candidates', search)
    monkeypatch.setattr(generator, 'read_listing', lambda url:(reads.append(url) or pages[url]))
    assert generator.resolve_listing('洗衣袋', lambda **kw:None) == pages[bags]
    assert searches == ['laundry bag'] and reads == [root, home, bags]


@pytest.mark.parametrize('code', ['AMAZON_HTTP_429', 'AMAZON_VERIFICATION_REQUIRED'])
def test_category_navigation_does_not_hide_search_verification_or_rate_limit(monkeypatch, code):
    import json, subprocess
    monkeypatch.setattr(generator, 'model_json', lambda *a, **kw:{'search_query':'laundry bag'})
    def search(query):
        raise generator.CategoryGenerationError('original gate') from subprocess.CalledProcessError(
            1, ['node'], stderr=json.dumps({'code':code}))
    monkeypatch.setattr(generator, 'search_listing_candidates', search)
    monkeypatch.setattr(generator, 'read_listing', lambda _:pytest.fail('gate was hidden by another route'))
    with pytest.raises(generator.CategoryGenerationError, match='original gate'):
        generator.resolve_listing('洗衣袋', lambda **kw:None)


@pytest.mark.parametrize('choice', ['https://www.amazon.com/zgbs/home-garden/999999999',
                                   'https://www.amazon.com/Best-Sellers/zgbs'])
def test_category_navigation_rejects_unobserved_or_revisited_url(monkeypatch, choice):
    import subprocess
    answers = iter([{'search_query':'laundry bag'}, {'next_url':choice}])
    monkeypatch.setattr(generator, 'model_json', lambda *a, **kw:next(answers))
    def search(query):
        raise generator.CategoryGenerationError('search login') from subprocess.CalledProcessError(
            1, ['node'], stderr='{"code":"AMAZON_LOGIN_REQUIRED"}')
    monkeypatch.setattr(generator, 'search_listing_candidates', search)
    reads = []
    def read(url):
        reads.append(url)
        return {'url':url, 'heading':'Best Sellers', 'rank_count':30,
                'links':[{'url':'https://www.amazon.com/zgbs/home-garden', 'text':'Home & Kitchen'}]}
    monkeypatch.setattr(generator, 'read_listing', read)
    with pytest.raises(generator.CategoryGenerationError) as error:
        generator.resolve_listing('洗衣袋', lambda **kw:None)
    assert error.value.code == 'CATEGORY_UNRESOLVED'
    assert reads == ['https://www.amazon.com/Best-Sellers/zgbs']


@pytest.mark.parametrize('arguments', [['--query', 'laundry bag'], ['https://www.amazon.com/zgbs/home-garden/900001']])
@pytest.mark.parametrize('direct_succeeds', [True, False])
def test_category_proxy_login_tries_direct_once_and_preserves_evidence(monkeypatch, tmp_path, arguments, direct_succeeds):
    import json
    import subprocess
    from types import SimpleNamespace
    from selection_automation import browser_transport
    monkeypatch.setattr(generator, 'ROOT', tmp_path)
    monkeypatch.setattr(browser_transport, 'amazon_environment', lambda _: {
        'AMAZON_BROWSER_ROUTE_MODE': 'AUTO', 'AMAZON_BROWSER_PROXY': 'http://127.0.0.1:7890'})
    calls = []
    def run(command, **options):
        calls.append(dict(options['env']))
        if len(calls) == 1 or not direct_succeeds:
            raise subprocess.CalledProcessError(1, command, stderr='{"code":"AMAZON_LOGIN_REQUIRED"}')
        return SimpleNamespace(stdout=json.dumps({'url': 'https://www.amazon.com/zgbs/home-garden/900001'}))
    monkeypatch.setattr(generator.subprocess, 'run', run)
    if direct_succeeds:
        generator._read_amazon_category(arguments, timeout=100)
    else:
        with pytest.raises(subprocess.CalledProcessError):
            generator._read_amazon_category(arguments, timeout=100)
    assert len(calls) == 2
    assert calls[0]['AMAZON_BROWSER_PROXY'] == 'http://127.0.0.1:7890'
    assert calls[1]['AMAZON_BROWSER_ROUTE_MODE'] == 'DIRECT'
    assert calls[1]['AMAZON_BROWSER_PROXY'] == ''
    record = json.loads(next((tmp_path/'runtime/web-console').glob('amazon-category-proxy-login-*.json')).read_text('utf-8'))
    assert record['recovered'] is direct_succeeds
    assert record['failures'][0]['stderr'] == '{"code":"AMAZON_LOGIN_REQUIRED"}'


@pytest.mark.parametrize('mode,proxy,code', [
    ('DIRECT', '', 'AMAZON_LOGIN_REQUIRED'), ('AUTO', '', 'AMAZON_LOGIN_REQUIRED'),
    ('AUTO', 'http://127.0.0.1:7890', 'AMAZON_CAPTCHA_REQUIRED'),
    ('AUTO', 'http://127.0.0.1:7890', 'AMAZON_HTTP_429')])
def test_category_browser_does_not_retry_other_routes_or_gates(monkeypatch, tmp_path, mode, proxy, code):
    import json
    import subprocess
    from selection_automation import browser_transport
    monkeypatch.setattr(generator, 'ROOT', tmp_path)
    monkeypatch.setattr(browser_transport, 'amazon_environment', lambda _: {
        'AMAZON_BROWSER_ROUTE_MODE': mode, 'AMAZON_BROWSER_PROXY': proxy})
    calls = []
    def run(command, **options):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command, stderr=json.dumps({'code': code}))
    monkeypatch.setattr(generator.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        generator._read_amazon_category(['--query', 'laundry bag'], timeout=100)
    assert len(calls) == 1


@pytest.mark.parametrize('recover', [True, False])
@pytest.mark.parametrize('stage,kind,message',[
    ('page_navigation','TimeoutError','page.goto: Timeout 45000ms exceeded'),
    ('page_navigation','Error','page.evaluate: Execution context was destroyed, most likely because of a navigation'),
    ('page_content','Error','page.evaluate: Execution context was destroyed, most likely because of a navigation')])
def test_listing_transient_failure_retained_with_one_retry(monkeypatch, tmp_path, recover,stage,kind,message):
    import json
    import subprocess
    from types import SimpleNamespace
    monkeypatch.setattr(generator, 'ROOT', tmp_path)
    calls = []
    url = 'https://www.amazon.com/zgbs/electronics/172541'
    stderr = json.dumps({'stage':stage,'exception_type':kind,'message':message})
    def run(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1 or not recover:
            raise subprocess.CalledProcessError(1, ['node'], output='', stderr=stderr)
        return SimpleNamespace(stdout=json.dumps({'url': url, 'rank_count': 30}))
    monkeypatch.setattr(generator.subprocess, 'run', run)
    if recover:
        assert generator.read_listing(url)['rank_count'] == 30
    else:
        with pytest.raises(generator.CategoryGenerationError, match='榜单.*失败') as error:
            generator.read_listing(url)
        assert error.value.code == 'AMAZON_LISTING_READ_FAILED'
    assert len(calls) == 2
    saved = json.loads(next((tmp_path/'runtime/web-console').glob('amazon-listing-error-*.json')).read_text('utf-8'))
    assert saved['recovered'] is recover
    assert saved['failures'][0]['stderr'] == stderr
    assert 'CalledProcessError' in saved['failures'][0]['traceback']


@pytest.mark.parametrize('stage,status,blocked', [('page_content', None, True),
    ('page_navigation', 403, False), ('page_navigation', 429, False), ('page_content',403,False),
    ('page_content',429,False), ('browser_launch', None, False)])
def test_listing_gates_and_dependencies_not_retried(monkeypatch, tmp_path, stage, status, blocked):
    import json
    import subprocess
    monkeypatch.setattr(generator, 'ROOT', tmp_path)
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, ['node'], stderr=json.dumps({
            'stage': stage, 'http_status': status, 'blocked': blocked, 'message': 'original failure'}))
    monkeypatch.setattr(generator.subprocess, 'run', run)
    with pytest.raises(generator.CategoryGenerationError, match='original failure'):
        generator.read_listing('https://www.amazon.com/zgbs/electronics/172541')
    assert len(calls) == 1


def fixture_config():
    c = generator.category_contract()['template']
    c.update(category_key='drill', category_name_zh='电钻')
    c['temu'].update(base_queries=['power drill'], include_terms=['drill'], keyword_limit=4)
    c['temu'].update(category_aliases=['电钻','電鑽','power drill'],
        subject_filter={'mode':'TARGET_BODY_ONLY','reject_mixed_subjects':True,'exclude_subject_terms':['nail drill','磨甲机']})
    c['stage1'].update(rule_version='v1', attributes=[{'name':'power','hard_conflict':True,'values':{'corded':['corded'], 'cordless':['cordless']}}])
    c['stage2'].update(candidate_threshold=54,threshold_reason_zh='电钻拍摄角度和背景变化较大，采用较低门槛减少漏检。')
    c['stage3'].update(modules={'identity':100}, module_descriptions={'identity':'双边外观'}, visual_instructions='缺失为UNKNOWN', rule_version='v1', prompt_version='v1', schema_version='v1')
    c['stage3']['identity_policy']={'version':'category-identity-policy-v2-effects','product_scope':'比较电钻本体',
        'features':[{'id':'power','label':'供电结构','role':'identity','evidence':'both','scope':'机身与供电接口',
                     'rule':'有线机身与电池接口结构不同排除','modules':['identity'],'required_visual':True,
                     'region':'body','effects':['connection'],'preserves':[]}]}
    return c


@pytest.mark.parametrize('heading', ['Best Sellers in Manual Can Openers', 'Best Sellers in Jar Openers'])
def test_bottle_opener_rejects_other_container_openers(heading):
    assert 'bottle opener' in generator.category_intent('开瓶器').lower()
    with pytest.raises(generator.CategoryGenerationError) as error:
        generator.validate_listing_intent('开瓶器', {'heading': heading})
    assert error.value.code == 'CATEGORY_SCOPE_MISMATCH'


def test_bottle_and_can_openers_keep_their_own_scope():
    generator.validate_listing_intent('开瓶器', {'heading': 'Best Sellers in Bottle Openers'})
    generator.validate_listing_intent('开罐器', {'heading': 'Best Sellers in Manual Can Openers'})


def test_discovery_follows_actual_category_navigation_to_correct_leaf(monkeypatch):
    can = 'https://www.amazon.com/zgbs/kitchen/289757'
    parent = 'https://www.amazon.com/zgbs/kitchen/289754'
    bar = 'https://www.amazon.com/zgbs/home-garden/760384'
    corkscrew = 'https://www.amazon.com/zgbs/kitchen/289734'
    bottle = 'https://www.amazon.com/zgbs/kitchen/3109475011'
    pages = {
        can: {'url':can, 'heading':'Best Sellers in Manual Can Openers', 'rank_count':30,
              'links':[{'url':parent, 'text':'Kitchen Utensils & Gadgets'}]},
        parent: {'url':parent, 'heading':'Best Sellers in Kitchen Utensils & Gadgets', 'rank_count':30,
                 'links':[{'url':bar, 'text':'Bar & Wine Tools'}]},
        bar: {'url':bar, 'heading':'Best Sellers in Bar & Wine Tools', 'rank_count':30,
                 'links':[{'url':corkscrew, 'text':'Corkscrews & Openers'}]},
        corkscrew: {'url':corkscrew, 'heading':'Best Sellers in Corkscrews & Openers', 'rank_count':30,
                 'links':[{'url':bottle, 'text':'Bottle Openers'}]},
        bottle: {'url':bottle, 'heading':'Best Sellers in Bottle Openers', 'rank_count':30},
    }
    visited = []
    def read(url):
        visited.append(url)
        return pages[url]
    monkeypatch.setattr(generator, 'read_listing', read)
    monkeypatch.setattr(generator, 'search_listing_candidates', lambda _: {
        'candidates':[{'url':can}], 'summary':'INITIAL_SEARCH_ONLY'})
    responses = iter([{'search_query':'bottle opener'}, {'url':can}, {'url':parent},
                      {'matches':False}, {'url':bar}, {'matches':False}, {'url':corkscrew},
                      {'matches':False}, {'url':bottle}, {'matches':True}])
    prompts = []
    def model(prompt, **kwargs):
        prompts.append(prompt)
        return next(responses)
    monkeypatch.setattr(generator, 'model_json', model)
    assert generator.resolve_listing('开瓶器', lambda **kwargs:None)['url'] == bottle
    assert visited == [can, parent, bar, corkscrew, bottle]
    assert parent in prompts[2] and bar in prompts[4] and bottle in prompts[8]
    assert 'INITIAL_SEARCH_ONLY' in prompts[1] and 'INITIAL_SEARCH_ONLY' not in prompts[2]


@pytest.mark.parametrize('manual_url', [False, True])
def test_transfer_pump_rejects_engine_parts_before_rule_generation(monkeypatch, manual_url):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-only')
    monkeypatch.setattr('selection_automation.node_runtime.ensure_node_runtime', lambda _,**kw: None)
    evidence = {'url': 'https://www.amazon.com/zgbs/automotive/15726131',
                'heading': 'Best Sellers in Automotive Replacement Oil Pumps', 'rank_count': 50, 'links': []}
    monkeypatch.setattr(generator, 'read_listing', lambda _: evidence)
    monkeypatch.setattr(generator, 'search_listing_candidates', lambda _: {'candidates':[{'url':evidence['url']}]})
    prompts = []
    def decide(prompt, **kwargs):
        prompts.append(prompt)
        assert 'Exclude engine-internal oil pump replacement parts' in prompt
        return {'url': evidence['url']}
    monkeypatch.setattr(generator, 'model_json', decide)
    with pytest.raises(generator.CategoryGenerationError, match='发动机内部机油泵配件') as error:
        generator.generate_category('抽油泵', 50, 400, bestsellers_url=evidence['url'] if manual_url else '')
    assert error.value.code == 'CATEGORY_SCOPE_MISMATCH'
    assert len(prompts) == (0 if manual_url else 2)


def test_transfer_scope_allows_correct_leaf_and_explicit_engine_request():
    generator.validate_listing_intent('抽油泵', {'heading': 'Best Sellers in Barrel & Hand Fuel Pumps'})
    generator.validate_listing_intent('发动机内部抽油泵', {'heading': 'Best Sellers in Automotive Oil Pumps'})


def test_policy_repair_is_once_and_preserves_collection_configuration():
    original = fixture_config()
    broken = fixture_config()
    broken['stage3']['identity_policy']['features'][0].pop('required_visual')
    calls = []
    def repair(prompt, **kwargs):
        calls.append(kwargs)
        return {'identity_policy': original['stage3']['identity_policy'], 'attributes': original['stage1']['attributes']}
    result = generator.compile_generated_policy(broken, model=repair)
    assert len(calls) == 1 and calls[0]['retry_truncated'] is False
    assert result['stage3']['identity_policy_generation']['repair_used'] is True
    assert result['amazon'] == original['amazon']
    assert result['temu'] == original['temu']
    assert 'required_visual' not in broken['stage3']['identity_policy']['features'][0]


def test_saved_policy_repair_compiles_without_repeating_paid_request():
    original=fixture_config();broken=fixture_config()
    broken['stage3']['identity_policy']=None
    saved={'identity_policy':original['stage3']['identity_policy'],'attributes':original['stage1']['attributes']}
    result=generator.compile_generated_policy(broken,policy_repair=saved,
        model=lambda *a,**kw:pytest.fail('completed policy repair was billed again'))
    assert result['amazon']==original['amazon'] and result['temu']==original['temu']
    assert result['stage3']['identity_policy_generation']['repair_used'] is True


def test_rule_repair_survives_new_output_name_without_repeating_paid_call(monkeypatch,tmp_path):
    import json
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-secret')
    original=fixture_config();broken=fixture_config()
    broken['stage3']['identity_policy']['features'][0].pop('required_visual')
    repair={'identity_policy':original['stage3']['identity_policy'],'attributes':original['stage1']['attributes']}
    evidence={'url':'https://www.amazon.com/zgbs/tools/123','title':'Best Sellers',
              'heading':'Best Sellers in Power Drills','rank_count':50,'links':[]}
    monkeypatch.setattr(generator,'resolve_listing',lambda *a,**kw:evidence)
    monkeypatch.setattr(generator,'approved_category',lambda *a:None)
    answers=iter([broken,repair])
    monkeypatch.setattr(generator,'model_json',lambda *a,**kw:next(answers))
    write=generator.atomic_write
    def interrupt_after_saved_repair(path,text):
        write(path,text)
        if 'policy_repair' in json.loads(text):raise OSError('interrupted after saved model reply')
    monkeypatch.setattr(generator,'atomic_write',interrupt_after_saved_repair)
    with pytest.raises(generator.CategoryGenerationError):
        generator.generate_category('电钻',2,120,draft_store=tmp_path)
    assert json.loads(next(tmp_path.glob('*.json')).read_text('utf-8'))['policy_repair']==repair
    monkeypatch.setattr(generator,'atomic_write',write)
    monkeypatch.setattr(generator,'model_json',lambda *a,**kw:pytest.fail('saved repair was billed again'))
    result=generator.generate_category('电钻',2,120,draft_store=tmp_path,reuse_approved=True)
    assert result['stage3']['identity_policy_generation']['repair_used'] is True
    assert result['amazon']['target_count']==2


def test_unusable_policy_repair_stops_after_bounded_retry_without_fallback():
    broken = fixture_config(); broken['stage3']['identity_policy'] = None
    calls = []
    def repair(*args, **kwargs):
        calls.append(1)
        return {'identity_policy': None, 'attributes': broken['stage1']['attributes']}
    with pytest.raises(ValueError):
        generator.compile_generated_policy(broken, model=repair)
    assert calls == [1,1]


def setup(monkeypatch, config=None, decision=None):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    evidence = {'url':'https://www.amazon.com/zgbs/tools/123', 'title':'Best Sellers', 'heading':'Best Sellers in Power Drills','rank_count':50,'links':[]}
    monkeypatch.setattr(generator, 'read_listing', lambda url:evidence)
    monkeypatch.setattr(generator, 'search_listing_candidates',lambda _: {'candidates':[{'url':evidence['url']}]})
    responses=iter([{'search_query':'power drill'}, decision or {'url':evidence['url']}, {'matches':True}, fixture_config() if config is None else config])
    monkeypatch.setattr(generator, 'model_json', lambda prompt, **kwargs: next(responses))


def test_verified_configuration_overrides_model_endpoint_and_counts(monkeypatch):
    c=fixture_config(); c['stage3']['base_url']='https://evil.invalid'
    setup(monkeypatch,c)
    result=generator.generate_category('电钻',3,40)
    assert result['stage3']['base_url']=='https://api.deepseek.com'
    assert result['amazon']['target_count']==3
    assert result['temu']['max_products_per_keyword']==40
    assert result['amazon']['verification']['rank_count']==50


@pytest.mark.parametrize('threshold',[43,58])
def test_rules_and_recall_threshold_share_one_configured_api_generation(monkeypatch,threshold):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only-generic-key')
    monkeypatch.setenv('SELECTION_RULES_BASE_URL','https://configured.example/v1')
    monkeypatch.setenv('SELECTION_RULES_MODEL','configured-model')
    evidence={'url':'https://www.amazon.com/zgbs/tools/123','title':'Best Sellers',
              'heading':'Best Sellers in Power Drills','rank_count':50,'links':[]}
    monkeypatch.setattr(generator,'read_listing',lambda url:evidence)
    calls=[]
    def model(prompt,**kwargs):
        calls.append(prompt)
        config=fixture_config();config['stage2']['candidate_threshold']=threshold
        return config
    monkeypatch.setattr(generator,'model_json',model)
    out=generator.generate_category('电钻',3,40,bestsellers_url=evidence['url'])
    assert len(calls)==1 and 'threshold_reason_zh' in calls[0] and 'compact-category-v1' in calls[0]
    assert out['stage2']['candidate_threshold']==threshold
    assert out['stage2']['threshold_source']=='configured_rule_api'
    assert out['stage1']['attributes']


@pytest.mark.parametrize('category,node,proposed,expected',[
    ('削皮器','16439871',60,55),('削皮器','16439871',43,43),
    ('开瓶器','3109475011',68,55),('开瓶器','3109475011',43,43),
    ('开瓶器','123',68,68),
    ('削皮器','123',60,60),('其他品类','16439871',60,60)])
def test_reviewed_recall_ceiling_applies_only_to_exact_category_and_listing(monkeypatch,category,node,proposed,expected):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only')
    evidence={'url':'https://www.amazon.com/zgbs/kitchen/'+node,'title':'Best Sellers',
              'heading':'Best Sellers in Peelers','rank_count':30,'links':[]}
    monkeypatch.setattr(generator,'resolve_listing',lambda *a,**kw:evidence)
    config=fixture_config();config.update(category_name_zh=category,category_key='peeler')
    config['stage2']['candidate_threshold']=proposed
    monkeypatch.setattr(generator,'model_json',lambda *a,**kw:config)
    out=generator.generate_category(category,1,40)
    assert out['stage2']['candidate_threshold']==expected
    assert out['stage2']['model_recommended_threshold']==proposed
    assert out['stage2']['threshold_source']==('reviewed_category_recall' if expected!=proposed else 'configured_rule_api')
    assert out['stage3']['identity_policy']==config['stage3']['identity_policy']


def test_reviewed_recall_does_not_override_an_explicit_manual_threshold():
    config=fixture_config();config['category_name_zh']='削皮器'
    config['amazon']['bestsellers_url']='https://www.amazon.com/zgbs/kitchen/16439871'
    config['stage2'].update(candidate_threshold=60,threshold_source='manual')
    assert generator.apply_reviewed_recall_aliases(config)['stage2']['candidate_threshold']==60


@pytest.mark.parametrize('change', ['empty_attributes','bad_weights','python'])
def test_invalid_rules_fail_without_template_fallback(monkeypatch,change):
    c=fixture_config()
    if change=='empty_attributes': c['stage1']['attributes']=[]
    if change=='bad_weights': c['stage3']['modules']['identity']=99
    if change=='python': c['stage1']['text_gate']='python'
    setup(monkeypatch,c)
    with pytest.raises(generator.CategoryGenerationError,match='不合法'):
        generator.generate_category('电钻',3,40)


def test_invented_link_not_followed(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only')
    reads=[]
    def read(url):
        reads.append(url)
        assert url=='https://www.amazon.com/zgbs/tools/123'
        return {'url':url,'heading':'Best Sellers in Other Tools','rank_count':30,'links':[]}
    monkeypatch.setattr(generator,'read_listing',read)
    monkeypatch.setattr(generator,'search_listing_candidates',lambda _: {'candidates':[{'url':'https://www.amazon.com/zgbs/tools/123'}]})
    monkeypatch.setattr(generator,'model_json',lambda *a,**kw:{'url':'https://www.amazon.com/zgbs/invented'})
    with pytest.raises(generator.CategoryGenerationError,match='真实'):
        generator.generate_category('电钻',3,40)
    assert reads==['https://www.amazon.com/zgbs/tools/123']


def test_missing_key_no_network(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    monkeypatch.setattr(generator,'read_listing',lambda _:pytest.fail('network'))
    with pytest.raises(generator.CategoryGenerationError,match='API key'):
        generator.generate_category('电钻',3,40)


def test_bad_json_redacted(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','sensitive')
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return b'not JSON sensitive'
    monkeypatch.setattr(generator,'open_model_url',lambda *a,**k:Response())
    with pytest.raises(generator.CategoryGenerationError) as error:
        generator.model_json('test')
    assert 'sensitive' not in str(error.value)


def test_non_amazon_url_never_launched(monkeypatch):
    monkeypatch.setattr(generator.subprocess,'run',lambda *a,**k:pytest.fail('launched'))
    with pytest.raises(generator.CategoryGenerationError):
        generator.read_listing('https://evil.invalid/zgbs/tools')


def test_generation_progress_is_structured_and_secret_free(monkeypatch):
    setup(monkeypatch)
    events=[]
    generator.generate_category('电钻',3,40,progress=lambda **x:events.append(x))
    stages=[x['stage'] for i,x in enumerate(events) if i==0 or x['stage']!=events[i-1]['stage']]
    assert stages==['discovering','validating','generating','validating']
    assert next(x for x in events if x['stage']=='discovering')['depth']==1
    assert 'test-secret' not in str(events)


def test_cli_failure_status_without_partial_config(monkeypatch,tmp_path):
    import json
    output=tmp_path/'category.json'; status=tmp_path/'status.json'
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    monkeypatch.setattr(generator,'approved_category',lambda *args:None)
    monkeypatch.setattr(generator.sys,'argv',['generate_category.py','--category-name','电钻','--amazon-count','2','--per-keyword','40','--output',str(output),'--status-file',str(status)])
    assert generator.main()==1
    result=json.loads(status.read_text('utf-8'))
    assert result['stage']=='error'
    assert result['failed_stage']=='input'
    assert not output.exists()


def test_truncated_output_never_silently_repeats_paid_request(monkeypatch):
    import json
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-secret')
    budgets=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return json.dumps({'choices':[{'finish_reason':'length','message':{'content':''}}]}).encode()
    def request(req,**kwargs):
        budgets.append(json.loads(req.data)['max_tokens'])
        return Response()
    monkeypatch.setattr(generator,'open_model_url',request)
    with pytest.raises(generator.CategoryGenerationError) as error:
        generator.model_json('test')
    assert error.value.code=='MODEL_OUTPUT_TRUNCATED'
    assert budgets==[8000]


@pytest.mark.parametrize('url', [
    'https://www.amazon.com/Best-Sellers-Remote/zgbs/electronics/123/ref=nav?x=1',
    'https://www.amazon.com/gp/bestsellers/electronics/123/ref=nav',
    'https://amazon.com/zgbs/electronics/123',
])
def test_listing_identity_ignores_navigation_tracking(url):
    assert generator.listing_identity(url)=='https://www.amazon.com/zgbs/electronics/123'


@pytest.mark.parametrize('url', ['https://evil.com/zgbs/electronics/123',
    'https://www.amazon.com@evil.com/zgbs/electronics/123',
    'https://user@www.amazon.com/zgbs/electronics/123',
    'https://www.amazon.com:8443/zgbs/electronics/123',
    'https://www.amazon.com/dp/B000000001', 'https://www.amazon.com/zgbsfake'])
def test_manual_source_rejects_non_listing_or_unsafe_urls(url):
    assert not generator.valid_url(url)


def test_manual_listing_is_verified_before_generating(monkeypatch):
    setup(monkeypatch)
    calls=[]
    monkeypatch.setattr(generator,'model_json',lambda prompt,**kw:(calls.append(prompt) or fixture_config()))
    result=generator.generate_category('电钻',3,40,bestsellers_url='https://www.amazon.com/gp/bestsellers/tools/123')
    assert len(calls)==1
    assert result['amazon']['verification']['rank_count']==50
    monkeypatch.setattr(generator,'read_listing',lambda url:{'url':url,'rank_count':0,'heading':'Best Sellers','links':[]})
    with pytest.raises(generator.CategoryGenerationError,match='排名商品'):
        generator.generate_category('电钻',3,40,bestsellers_url='https://www.amazon.com/zgbs/tools/123')
    assert len(calls)==1


def test_listing_scope_uses_real_products_instead_of_shared_storage_heading(monkeypatch):
    bad='https://www.amazon.com/zgbs/office-products/1069646'
    good='https://www.amazon.com/zgbs/office-products/900001'
    bags=[{'rank':1,'title':'Mesh Zipper Pouch Bags 24 Pack'}]
    boxes=[{'rank':1,'title':'Desktop Storage Organizer Box with Drawers'}]
    pages={bad:{'url':bad,'heading':'Best Sellers in Supply Organizers','rank_count':30,
                'ranked_products':bags,'links':[{'url':good,'text':'Desk Organizers'}]},
           good:{'url':good,'heading':'Best Sellers in Desk Organizers','rank_count':30,
                 'ranked_products':boxes,'links':[]}}
    answers=iter([{'search_query':'desktop storage box'},{'url':bad},{'matches':False},
                  {'url':good},{'matches':True}])
    prompts=[];reads=[]
    monkeypatch.setattr(generator,'search_listing_candidates',lambda _:{'candidates':[{'url':bad}]})
    monkeypatch.setattr(generator,'read_listing',lambda url:(reads.append(url) or pages[url]))
    def model(prompt,**kw):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr(generator,'model_json',model)
    assert generator.resolve_listing('桌面收纳盒',lambda **kw:None)==pages[good]
    assert reads==[bad,good] and len(prompts)==5
    assert bags[0]['title'] in prompts[2] and 'physical products' in prompts[2]
    assert bags[0]['title'] in prompts[3] and boxes[0]['title'] in prompts[4]


def test_unobserved_model_url_reads_live_candidate_and_still_checks_product_scope(monkeypatch):
    live='https://www.amazon.com/gp/bestsellers/home-garden/16353541/ref=observed'
    invented='https://www.amazon.com/zgbs/home-garden/16353551'
    page={'url':live,'heading':'Best Sellers in Skirt Hangers','rank_count':30,
          'ranked_products':[{'rank':1,'title':'Clip clothes hangers for skirts'}],'links':[]}
    answers=iter([{'search_query':'clothes hangers'},{'url':invented},{'matches':True}])
    reads=[]
    monkeypatch.setattr(generator,'search_listing_candidates',lambda _:{'candidates':[{'url':live}]})
    monkeypatch.setattr(generator,'read_listing',lambda url:(reads.append(url) or page))
    monkeypatch.setattr(generator,'model_json',lambda *a,**k:next(answers))
    assert generator.resolve_listing('衣架',lambda **k:None)==page
    assert reads==[live]


def test_mismatched_category_can_follow_observed_department_without_accepting_it(monkeypatch):
    bad='https://www.amazon.com/zgbs/office-products/1069646'
    department='https://www.amazon.com/Best-Sellers-Office-Products/zgbs/office-products'
    good='https://www.amazon.com/zgbs/office-products/900001'
    pages={bad:{'url':bad,'heading':'Best Sellers in Supply Organizers','rank_count':30,
                'links':[{'url':department,'text':'Office Products'}]},
           department:{'url':department,'heading':'Best Sellers in Office Products','rank_count':30,
                       'links':[{'url':good,'text':'Desk Organizers'}]},
           good:{'url':good,'heading':'Best Sellers in Desk Organizers','rank_count':30,'links':[]}}
    answers=iter([{'search_query':'desktop storage box'},{'url':bad},{'matches':False},
                  {'url':department},{'url':good},{'matches':True}])
    reads=[]
    monkeypatch.setattr(generator,'search_listing_candidates',lambda _:{'candidates':[{'url':bad}]})
    monkeypatch.setattr(generator,'read_listing',lambda url:(reads.append(url) or pages[url]))
    monkeypatch.setattr(generator,'model_json',lambda *a,**kw:next(answers))
    assert generator.resolve_listing('桌面收纳盒',lambda **kw:None)==pages[good]
    assert reads==[bad,department,good]


def test_observed_branch_can_recover_within_the_public_navigation_budget(monkeypatch):
    wrong='https://www.amazon.com/zgbs/sporting-goods/100001'
    root='https://www.amazon.com/Best-Sellers/zgbs'
    department='https://www.amazon.com/Best-Sellers-Home-Kitchen/zgbs/home-garden'
    parent='https://www.amazon.com/zgbs/home-garden/100002'
    child='https://www.amazon.com/zgbs/home-garden/100003'
    leaf='https://www.amazon.com/zgbs/home-garden/100004'
    urls=[wrong,root,department,parent,child,leaf]
    pages={url:{'url':url,'heading':'Best Sellers in '+heading,'rank_count':30,
                'ranked_products':[{'rank':1,'title':title}],
                'links':[{'url':urls[i+1],'text':'Observed next category'}] if i+1<len(urls) else []}
           for i,(url,heading,title) in enumerate(zip(urls,
               ['Specialist Implements','Beauty','Home & Kitchen','Household Supplies','Cleaning Tools','Cleaning Implements'],
               ['Specialist maintenance item','Beauty item','Household item','Mixed household products','Mixed cleaning tools','General cleaning implement']))}
    answers=iter([{'search_query':'cleaning implement'},{'url':'https://www.amazon.com/zgbs/home-garden/999999'},
        {'matches':False},{'url':root},{'url':department},
        {'url':parent},{'matches':False},{'url':child},{'matches':False},{'url':leaf},{'matches':True}])
    reads=[];searches=[]
    monkeypatch.setattr(generator,'search_listing_candidates',
        lambda query:(searches.append(query) or {'candidates':[{'url':wrong}]}))
    monkeypatch.setattr(generator,'read_listing',lambda url:(reads.append(url) or pages[url]))
    monkeypatch.setattr(generator,'model_json',lambda *a,**k:next(answers))
    assert generator.resolve_listing('测试清洁工具',lambda **k:None)==pages[leaf]
    assert reads==urls and len(set(reads))==len(reads) and searches==['cleaning implement']


def test_category_correction_stops_at_eight_even_with_more_observed_links(monkeypatch):
    urls=['https://www.amazon.com/zgbs/tools/'+str(200000+i) for i in range(9)]
    reads=[];searches=[]
    monkeypatch.setattr(generator,'search_listing_candidates',
        lambda query:(searches.append(query) or {'candidates':[{'url':urls[0]}]}))
    def read(url):
        reads.append(url)
        return {'url':url,'heading':'Best Sellers in Other Implements','rank_count':30,
                'links':[{'url':urls[len(reads)],'text':'Observed child'}]}
    def model(prompt,**kwargs):
        if prompt.startswith('Translate the requested'):return {'search_query':'test implement'}
        if prompt.startswith('Check whether'):return {'matches':False}
        return {'url':urls[len(reads)]}
    monkeypatch.setattr(generator,'read_listing',read)
    monkeypatch.setattr(generator,'model_json',model)
    with pytest.raises(generator.CategoryGenerationError) as error:
        generator.resolve_listing('测试工具',lambda **k:None)
    assert error.value.code=='CATEGORY_UNRESOLVED'
    assert reads==urls[:8] and searches==['test implement']


def test_direct_api_link_corrected_without_directory_walk_or_revisiting(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','fixture-secret')
    wrong='https://www.amazon.com/zgbs/pc/172493'
    child='https://www.amazon.com/zgbs/electronics/123/ref=two'
    pages={wrong:{'url':wrong,'heading':'Best Sellers in Computer Input Devices','rank_count':50,'links':[]},
           child:{'url':child,'heading':'Best Sellers in Remote Controls','rank_count':50,'title':'Remote Controls','links':[]}}
    reads=[];prompts=[]
    monkeypatch.setattr(generator,'search_listing_candidates',lambda _: {'candidates':[{'url':wrong},{'url':child}]})
    monkeypatch.setattr(generator,'read_listing',lambda url:(reads.append(url) or pages[url]))
    responses=iter([{'search_query':'remote control'},{'url':wrong},{'matches':False},{'url':'https://www.amazon.com/zgbs/electronics/172493'},
                    {'matches':True},fixture_config()])
    def model(prompt,**kw):
        prompts.append(prompt)
        return next(responses)
    monkeypatch.setattr(generator,'model_json',model)
    result=generator.generate_category('遥控器',10,500)
    assert reads==[wrong,child]
    assert result['amazon']['bestsellers_url']==child
    assert 'Requested category:' in prompts[4]


@pytest.mark.parametrize('category,heading,node', [('鼠标垫','Mouse Pads','49080'),
    ('浴帘','Shower Curtains','13749881'),('电钻','Power Drills','123'),('便携洗衣机','Portable Clothes Washing Machines','9709422011')])
def test_direct_api_category_links_use_real_page_without_sidebar_links(monkeypatch,category,heading,node):
    url='https://www.amazon.com/zgbs/home-garden/'+node
    responses=iter([{'search_query':heading},{'url':url},{'matches':True}]);calls=[]
    def model(prompt,**kw):
        calls.append((prompt,kw))
        return next(responses)
    monkeypatch.setattr(generator,'model_json',model)
    monkeypatch.setattr(generator,'search_listing_candidates',lambda _: {'candidates':[{'url':url}]})
    evidence={'url':url,'heading':'Best Sellers in '+heading,'rank_count':40,'links':[]}
    reads=[]
    monkeypatch.setattr(generator,'read_listing',lambda value:(reads.append(value) or evidence))
    assert generator.resolve_listing(category,lambda **kw:None)==evidence
    assert reads==[url] and len(calls)==3
    assert category in calls[0][0] and heading in calls[2][0]
    assert [options['max_tokens'] for _,options in calls]==[200,800,400]


@pytest.mark.parametrize('failure', ['not_found','unranked'])
def test_direct_api_repairs_missing_or_unranked_candidate(monkeypatch,failure):
    bad='https://www.amazon.com/zgbs/office-products/99999999'
    good='https://www.amazon.com/zgbs/office-products/49080'
    responses=iter([{'search_query':'mouse pad'},{'url':bad},{'url':good},{'matches':True}]);reads=[]
    monkeypatch.setattr(generator,'model_json',lambda *a,**kw:next(responses))
    monkeypatch.setattr(generator,'search_listing_candidates',lambda _: {'candidates':[{'url':bad},{'url':good,'text':'Mouse Pads'}]})
    def read(url):
        reads.append(url)
        if url==bad and failure=='not_found':
            raise generator.CategoryGenerationError('榜单不存在','INVALID_LISTING_CANDIDATE')
        return {'url':url,'heading':'Best Sellers in Mouse Pads','rank_count':40 if url==good else 0}
    monkeypatch.setattr(generator,'read_listing',read)
    assert generator.resolve_listing('鼠标垫',lambda **kw:None)['url']==good
    assert reads==[bad,good]


def test_direct_api_uses_live_rank_links_once_to_correct_unknown_node(monkeypatch):
    good='https://www.amazon.com/gp/bestsellers/office-products/705323011'
    answers=iter([{'url':'https://www.amazon.com/zgbs/pc/999','search_query':'mouse pad'},
                  {'url':good},{'matches':True}])
    prompts=[];searches=[]
    def model(prompt,**kw):prompts.append(prompt);return next(answers)
    monkeypatch.setattr(generator,'model_json',model)
    def search(query):
        searches.append(query)
        return {'candidates':[{'url':good,'text':'Mouse Pads','product':{'title':'Mouse Pad','asin':'B09DV56338'}}]}
    monkeypatch.setattr(generator,'search_listing_candidates',search)
    def read(url):
        if url!=good:raise generator.CategoryGenerationError('404','INVALID_LISTING_CANDIDATE')
        return {'url':url,'heading':'Best Sellers in Mouse Pads','rank_count':30}
    monkeypatch.setattr(generator,'read_listing',read)
    assert generator.resolve_listing('鼠标垫',lambda **kw:None)['url']==good
    assert searches==['mouse pad'] and good in prompts[1]


def test_direct_api_preserves_network_failure_without_spending_on_link_repairs(monkeypatch):
    calls=[]
    monkeypatch.setattr(generator,'model_json',lambda *a,**kw:(calls.append(1) or {'url':'https://www.amazon.com/zgbs/office-products/49080'}))
    monkeypatch.setattr(generator,'search_listing_candidates',lambda _: {'candidates':[{'url':'https://www.amazon.com/zgbs/office-products/49080'}]})
    def read(url):raise generator.CategoryGenerationError('TLS connection failed','AMAZON_LISTING_READ_FAILED')
    monkeypatch.setattr(generator,'read_listing',read)
    with pytest.raises(generator.CategoryGenerationError,match='TLS') as error:
        generator.resolve_listing('鼠标垫',lambda **kw:None)
    assert error.value.code=='AMAZON_LISTING_READ_FAILED' and calls==[1,1]


def test_short_real_node_retries_amazons_working_rank_link_path(monkeypatch,tmp_path):
    import json,subprocess
    monkeypatch.setattr(generator,'ROOT',tmp_path)
    reads=[]
    def read(url):
        reads.append(url)
        if len(reads)==1:
            raise subprocess.CalledProcessError(1,['node'],stderr=json.dumps({'http_status':404}))
        return {'url':url,'heading':'Best Sellers in Mouse Pads','rank_count':30}
    monkeypatch.setattr(generator,'_read_listing_once',read)
    result=generator.read_listing('https://www.amazon.com/zgbs/office-products/705323011')
    assert reads==['https://www.amazon.com/zgbs/office-products/705323011',
                   'https://www.amazon.com/gp/bestsellers/office-products/705323011']
    assert result['rank_count']==30


def test_api_cannot_open_an_invented_node_even_if_url_looks_valid(monkeypatch):
    monkeypatch.setattr(generator,'model_json',lambda *a,**kw:{'url':'https://www.amazon.com/zgbs/pc/99999999999'})
    monkeypatch.setattr(generator,'search_listing_candidates',lambda _: {'candidates':[{'url':'https://www.amazon.com/zgbs/office-products/705323011'}]})
    reads=[]
    def read(url):
        reads.append(url)
        assert url=='https://www.amazon.com/zgbs/office-products/705323011'
        return {'url':url,'heading':'Best Sellers in Other Products','rank_count':30,'links':[]}
    monkeypatch.setattr(generator,'read_listing',read)
    with pytest.raises(generator.CategoryGenerationError) as error:
        generator.resolve_listing('鼠标垫',lambda **kw:None)
    assert reads==['https://www.amazon.com/zgbs/office-products/705323011']
    assert error.value.code=='CATEGORY_UNRESOLVED'


def test_search_reads_official_api_and_accepts_only_us_nodes(monkeypatch):
    from selection_automation import deepseek_web
    monkeypatch.setenv('SELECTION_RULES_BASE_URL','https://api.deepseek.com')
    monkeypatch.setenv('SELECTION_RULES_MODEL','deepseek-flash')
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-only')
    monkeypatch.setattr(deepseek_web,'search',lambda *a,**kw:[
        {'url':'https://www.amazon.com/Mouse-Pads/b?node=705323011','title':'Mouse Pads'},
        {'url':'https://www.amazon.de/Mouse-Pads/b?node=123','title':'Mauspads'},
        {'url':'https://www.amzn.com/Best-Sellers-Mouse-Pads/zgbs/office-products/705323011','title':'Best Mouse Pads'}])
    monkeypatch.setattr(generator,'_read_amazon_category',lambda *a,**kw:pytest.fail('unnecessary browser search'))
    result=generator.search_listing_candidates('mouse pad')
    assert [item['node'] for item in result['browse_nodes']]==['705323011']
    assert result['candidates'][0]['url']=='https://www.amazon.com/Best-Sellers-Mouse-Pads/zgbs/office-products/705323011'


@pytest.mark.parametrize('status,code',[(404,None),(410,None),(200,'AMAZON_LISTING_NOT_RANKED')])
def test_listing_missing_candidate_is_distinct_from_transport_failure(monkeypatch,tmp_path,status,code):
    import json,subprocess
    monkeypatch.setattr(generator,'ROOT',tmp_path)
    def run(*a,**kw):
        raise subprocess.CalledProcessError(1,['node'],stderr=json.dumps({'http_status':status,'code':code}))
    monkeypatch.setattr(generator.subprocess,'run',run)
    with pytest.raises(generator.CategoryGenerationError) as error:
        generator.read_listing('https://www.amazon.com/zgbs/office-products/49080')
    assert error.value.code=='INVALID_LISTING_CANDIDATE'



def test_manual_url_preserves_full_observed_path(monkeypatch):
    setup(monkeypatch)
    url='https://www.amazon.com/Best-Sellers-Remote-Controls/zgbs/electronics/10967581'
    reads=[]
    monkeypatch.setattr(generator,'read_listing',lambda value:(reads.append(value) or {'url':value,'title':'Remote Controls','heading':'Best Sellers in Remote Controls','rank_count':30}))
    monkeypatch.setattr(generator,'model_json',lambda *args,**kw:fixture_config())
    generator.generate_category('遥控器',100,500,bestsellers_url=url)
    assert reads==[url]


@pytest.mark.parametrize('url',['https://www.amazon.com/zgbs/office-products/705323011',
    'https://www.amazon.com/gp/bestsellers/office-products/705323011/ref=pd_zg_hrsr_office-products',
    'https://www.amazon.com/Best-Sellers-Mouse-Pads/zgbs/office-products/705323011'])
def test_verified_rank_url_passes_both_matching_and_collection_contracts(url):
    from selection_automation.rules import validate
    from selection_automation.collection_contract import validate_collection
    category=generator.compile_generated_policy(fixture_config());category['amazon'].update(bestsellers_url=url,target_count=6)
    category['temu']['max_products_per_keyword']=400
    validate(category)
    validate_collection(category)
