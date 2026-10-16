import json,sys,zipfile,copy,os
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from selection_automation.company_bundle import normalize,import_bundle,apply_model_settings,SCHEMA
from selection_automation.secrets import store,load
from package_company_wizard import write_bundle
def fixture():
    return {'schema_version':SCHEMA,'adspower_api_key':'TEST_ONLY_ADSPOWER','proxy':{'host':'proxy.example','port':5959,'username':'TEST_ONLY_USER_session-fixture','password':'TEST_ONLY_PASSWORD'},'deepseek':{'key':'TEST_ONLY_DEEPSEEK','base_url':'https://api.deepseek.com','model':'deepseek-flash'},'unrelated_secret':'MUST_NOT_EXPORT','proxy_extra':{'token':'MUST_NOT_EXPORT'}}
def test_whitelist_complete_schema_and_process_only_settings():
    value=normalize(fixture(),require_model=True)
    assert set(value)=={'schema_version','adspower_api_key','adspower_api_url','proxy_architecture','proxy','preproxy_candidates','deepseek'}
    assert value['proxy_architecture']=='LOCAL_CHAIN_PROXYSHARE'
    assert 'MUST_NOT_EXPORT' not in json.dumps(value)
    env={'UNRELATED':'unchanged'};apply_model_settings(value['deepseek'],env)
    assert env['DEEPSEEK_API_KEY']=='TEST_ONLY_DEEPSEEK' and env['OPENAI_BASE_URL']=='https://api.deepseek.com' and env['OPENAI_VISION_MODEL']=='deepseek-flash' and env['UNRELATED']=='unchanged'
@pytest.mark.parametrize('field',['adspower_api_key','proxy'])
def test_missing_complete_bundle_fails_before_any_write(tmp_path,field):
    value=fixture();value.pop(field);writes=[]
    with pytest.raises(ValueError):import_bundle(tmp_path,value,lambda *a:writes.append(a))
    assert not writes and not (tmp_path/'runtime').exists()
def test_dummy_bundle_to_current_user_encrypted_import_and_legacy_compatibility(tmp_path):
    base=tmp_path/'base.zip';output=tmp_path/'complete.zip'
    with zipfile.ZipFile(base,'w') as z:z.writestr('BUILD_INFO.json',json.dumps({'channel':'internal','commit':'dummy','contains_company_credentials':False}));z.writestr('deploy.cmd','dummy fixture')
    original=base.read_bytes();write_bundle(base,output,fixture());assert base.read_bytes()==original
    with zipfile.ZipFile(output) as z:
        value=json.loads(z.read('company.local.json'));assert json.loads(z.read('BUILD_INFO.json'))['company_config_schema']==SCHEMA
    target=tmp_path/'isolated-install';import_bundle(target,value,store)
    assert load(target/'runtime/secrets/deepseek.dpapi')['key']=='TEST_ONLY_DEEPSEEK'
    assert load(target/'runtime/secrets/company.dpapi')['proxy']['password']=='TEST_ONLY_PASSWORD'
    assert b'TEST_ONLY_DEEPSEEK' not in (target/'runtime/secrets/deepseek.dpapi').read_bytes()
    old_model=(target/'runtime/secrets/deepseek.dpapi').read_bytes();legacy=fixture();legacy.pop('schema_version');legacy.pop('deepseek');import_bundle(target,legacy,store)
    assert (target/'runtime/secrets/deepseek.dpapi').read_bytes()==old_model
    with pytest.raises(ValueError):write_bundle(base,output,fixture())
def test_import_store_failure_restores_both_previous_encrypted_files(tmp_path):
    root=tmp_path/'install';import_bundle(root,fixture(),store);folder=root/'runtime/secrets';before={name:(folder/name).read_bytes() for name in ('company.dpapi','deepseek.dpapi')};calls=[]
    def broken(path,value):
        calls.append(path)
        if len(calls)==2:raise RuntimeError('simulated disk failure')
        store(path,value)
    with pytest.raises(RuntimeError,match='restored'):import_bundle(root,fixture(),broken)
    assert all((folder/name).read_bytes()==data for name,data in before.items())
    assert not list(folder.glob('*.tmp'))
@pytest.mark.parametrize('kind',['public','already-sensitive'])
def test_cannot_repackage_public_or_sensitive_base(tmp_path,kind):
    base=tmp_path/'base.zip';output=tmp_path/'new.zip'
    with zipfile.ZipFile(base,'w') as z:z.writestr('BUILD_INFO.json',json.dumps({'channel':'public' if kind=='public' else 'internal','contains_company_credentials':kind=='already-sensitive'}))
    with pytest.raises(ValueError):write_bundle(base,output,fixture())
    assert not output.exists() and not list(tmp_path.glob('*.tmp'))
def test_provider_change_rejected_before_import(tmp_path):
    value=fixture();value['deepseek']['base_url']='https://other.example'
    with pytest.raises(ValueError):import_bundle(tmp_path,value,store)
    assert not (tmp_path/'runtime').exists()
def test_mid_publication_failure_rolls_back_both_files(tmp_path,monkeypatch):
    root=tmp_path/'install';import_bundle(root,fixture(),store);folder=root/'runtime/secrets';before={name:(folder/name).read_bytes() for name in ('company.dpapi','deepseek.dpapi')}
    replace=os.replace;failed=[]
    def fail_second(source,target):
        if Path(target).name=='deepseek.dpapi' and not failed:
            failed.append(True);raise OSError('simulated rename failure')
        return replace(source,target)
    monkeypatch.setattr(os,'replace',fail_second)
    with pytest.raises(RuntimeError,match='restored'):import_bundle(root,fixture(),store)
    assert all((folder/name).read_bytes()==data for name,data in before.items())
def test_cancel_before_local_confirmation_never_reads_saved_credentials(monkeypatch,tmp_path):
    import types
    from package_company_wizard import wizard
    class Window:
        def withdraw(self):pass
    fake=types.ModuleType('tkinter');fake.Tk=Window;fake.messagebox=types.SimpleNamespace(askyesno=lambda *a,**k:False);fake.filedialog=types.SimpleNamespace()
    monkeypatch.setitem(sys.modules,'tkinter',fake)
    monkeypatch.setattr('selection_automation.secrets.load',lambda *a:pytest.fail('credential read before human confirmation'))
    wizard(tmp_path,tmp_path/'base.zip',tmp_path/'out.zip');assert not list(tmp_path.iterdir())
def test_installer_transaction_restores_company_and_model_with_dummy_files(tmp_path):
    import subprocess
    script=tmp_path/'rollback-proof.ps1'
    script.write_text('''param([string]$Guard,[string]$Fixture)
$ErrorActionPreference='Stop'
. $Guard
$folder=Join-Path $Fixture 'runtime/secrets'
[void](New-Item -ItemType Directory -Path $folder -Force)
[IO.File]::WriteAllText((Join-Path $folder 'company.dpapi'),'TEST_ONLY_OLD_COMPANY')
[IO.File]::WriteAllText((Join-Path $folder 'deepseek.dpapi'),'TEST_ONLY_OLD_MODEL')
[IO.File]::WriteAllText((Join-Path $folder 'liquid_d1.dpapi'),'TEST_ONLY_OLD_D1')
$transaction=Start-ProjectDeployment -Root $Fixture -ImportCompany $true
[IO.File]::WriteAllText((Join-Path $folder 'company.dpapi'),'TEST_ONLY_NEW_COMPANY')
[IO.File]::WriteAllText((Join-Path $folder 'deepseek.dpapi'),'TEST_ONLY_NEW_MODEL')
[IO.File]::WriteAllText((Join-Path $folder 'liquid_d1.dpapi'),'TEST_ONLY_NEW_D1')
Restore-ProjectDeployment $transaction
if ([IO.File]::ReadAllText((Join-Path $folder 'liquid_d1.dpapi')) -ne 'TEST_ONLY_OLD_D1') { throw 'D1 rollback failed' }
if ([IO.File]::ReadAllText((Join-Path $folder 'company.dpapi')) -ne 'TEST_ONLY_OLD_COMPANY') { throw 'company rollback failed' }
if ([IO.File]::ReadAllText((Join-Path $folder 'deepseek.dpapi')) -ne 'TEST_ONLY_OLD_MODEL') { throw 'model rollback failed' }
Write-Output 'DUMMY_ROLLBACK_OK'
''',encoding='utf-8')
    result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),'-Guard',str(Path(__file__).resolve().parents[1]/'scripts/deployment_guard.ps1'),'-Fixture',str(tmp_path/'install')],capture_output=True,text=True)
    assert result.returncode==0 and 'DUMMY_ROLLBACK_OK' in result.stdout
