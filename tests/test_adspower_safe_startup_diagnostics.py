import io,json,pathlib,sys,pytest
from types import SimpleNamespace
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import replace_proxyshare_worker_environment as replacement
from selection_automation.adspower_diagnostics import provider_error,AdsPowerAPIError
@pytest.mark.parametrize('message,category',[
 ('permission denied; key=SECRET','AUTHORIZATION'),
 ('Firefox kernel not installed; proxy=http://TEST_ONLY_USER:TEST_ONLY_PASSWORD@proxy.example/?key=SECRET','KERNEL_UNAVAILABLE'),
 ('proxy configuration invalid: http://TEST_ONLY_USER:TEST_ONLY_PASSWORD@proxy.example/?key=SECRET','PROXY_CONFIGURATION'),
 ('unrecognized condition key=SECRET user=USER password=PASS','PROVIDER_REJECTED'),
])
def test_provider_error_keeps_code_and_category_without_raw_sensitive_fields(message,category):
 error=provider_error('/api/v1/browser/start?user_id=PRIVATE&api_key=SECRET',-1,message)
 assert error.diagnostic['provider_code']==-1 and error.diagnostic['category']==category
 assert error.diagnostic['operation']=='BROWSER_START'
 serialized=json.dumps(error.diagnostic)+str(error)
 assert all(secret not in serialized for secret in ('SECRET','USER','PASS','PRIVATE','http://','api_key='))
def test_untrusted_string_code_is_not_reflected():
 error=provider_error('/unknown?key=SECRET','SECRET','failure')
 assert error.diagnostic['provider_code'] is None and 'SECRET' not in str(error)
@pytest.mark.parametrize('status',[502,503,504])
def test_wrapped_upstream_failure_preserves_uncertain_post_outcome(status):
 error=provider_error('/api/v2/browser-profile/delete',-1,f'Request failed with status code {status}; key=SECRET')
 assert error.diagnostic=={'operation':'PROFILE_DELETE','category':'CONNECTIVITY','provider_code':-1,'http_status':status,'retryable':False}
 assert 'SECRET' not in str(error)
def test_nonretryable_kernel_rejection_stops_existing_profile_without_recreate(monkeypatch):
 calls=[]
 def api(base,path,**kw):calls.append(path);raise provider_error(path,-1,'Firefox kernel not installed')
 with pytest.raises(AdsPowerAPIError):replacement.start_profile_with_retry('fixture','owned-profile',api=api)
 assert len(calls)==1 and '/browser/start?' in calls[0]
def test_bootstrap_failure_checkpoint_reuses_created_profile(tmp_path,monkeypatch):
 import setup_proxyshare_adspower_workers as setup,selection
 monkeypatch.setattr(setup,'ROOT',tmp_path);monkeypatch.setattr(setup,'SESSIONS',tmp_path/'config/sessions.json')
 monkeypatch.setattr(selection,'identity',lambda:'TEST_INSTALLATION');monkeypatch.setattr(sys,'argv',['setup'])
 monkeypatch.setattr(setup,'api_base',lambda:'fixture');monkeypatch.setattr(setup,'powershell',lambda:'fixture-powershell')
 monkeypatch.setattr(setup,'run_background_launcher',lambda *a,**k:SimpleNamespace(returncode=0,stdout='',stderr=''))
 policy=tmp_path/'runtime/proxy_policy.json';policy.parent.mkdir();policy.write_text(json.dumps({'worker_count':1,'worker_chain_ports':[22001],'webdriver_ports':[23001]}),encoding='utf-8')
 calls=[]
 def api(base,path,**kw):
  calls.append(path)
  if '/user/list' in path:return {'code':0,'data':{'list':[]}}
  if 'create' in path:return {'code':0,'data':{'profile_id':'owned-profile'}}
  raise provider_error(path,-1,'Firefox kernel not installed; key=SECRET')
 monkeypatch.setattr(setup,'api_call',api)
 for attempt in range(2):
  with pytest.raises(AdsPowerAPIError):setup._main()
 journal=json.loads((tmp_path/'runtime/bootstrap.json').read_text(encoding='utf-8'))
 assert sum('create' in p for p in calls)==1
 assert journal['workers'][0]['profile_id']=='owned-profile'
 assert journal['workers'][0]['bootstrap_status']=='PROFILE_CREATED'
 assert journal['workers'][0]['startup_error']['category']=='KERNEL_UNAVAILABLE'
 assert 'SECRET' not in json.dumps(journal) and not setup.SESSIONS.exists()

@pytest.mark.parametrize('readback',['empty','exists','unknown','malformed'])
@pytest.mark.parametrize('category',['CONNECTIVITY','PROFILE_IN_USE'])
def test_lost_delete_response_requires_exact_profile_absence(readback,monkeypatch,category):
 calls=[]
 def api(base,path,**kw):
  calls.append((path,kw))
  if 'delete' in path:
   raise AdsPowerAPIError(path,category,'request outcome unconfirmed')
  assert '/user/list?' in path and 'user_id=owned-profile' in path
  if readback=='unknown':raise AdsPowerAPIError(path,'CONNECTIVITY','unavailable')
  if readback=='malformed':return {'data':{}}
  return {'data':{'list':[] if readback=='empty' else [{'user_id':'owned-profile'}]}}
 monkeypatch.setattr(replacement,'api_call',api)
 monkeypatch.setattr(replacement.time,'sleep',lambda _:None)
 if readback=='empty':replacement.delete_profile_after_release('fixture','owned-profile',attempts=1)
 else:
  with pytest.raises(RuntimeError):replacement.delete_profile_after_release('fixture','owned-profile',attempts=1)
 assert len(calls)==2 and sum('delete' in path for path,_ in calls)==1

@pytest.mark.parametrize('profiles,confirmed',[
 ([],True),([{'user_id':'owned-profile'}],False),
 ([{'profile_id':'owned-profile'}],False),([{'id':'owned-profile'}],False),
 ([{'user_id':'other-profile'}],True),([{'id':'other-profile'}],True),
 ([{}],False),([None],False),(['other-profile'],False),
 ([{'user_id':''}],False),([{'id':'   '}],False),
 ([{'profile_id':None}],False),([{'id':True}],False),
 ([{'user_id':[]}],False),([{'user_id':'other-profile'},{}],False),
])
def test_delete_readback_requires_every_element_to_have_valid_id(profiles,confirmed,monkeypatch):
 calls=[]
 def api(base,path,**kw):
  calls.append(path)
  if 'delete' in path:raise AdsPowerAPIError(path,'CONNECTIVITY','outcome unconfirmed')
  return {'data':{'list':profiles}}
 monkeypatch.setattr(replacement,'api_call',api)
 monkeypatch.setattr(replacement.time,'sleep',lambda _:None)
 if confirmed:replacement.delete_profile_after_release('fixture','owned-profile',attempts=1)
 else:
  with pytest.raises(RuntimeError):replacement.delete_profile_after_release('fixture','owned-profile',attempts=1)
 assert len(calls)==2
