from selection_automation.profile_capacity import reclaim_one, list_profiles
from urllib.parse import parse_qs, urlsplit
import json
import pytest


def profile(pid='old', **extras):
    return {'user_id':pid,'name':'TEMU-machine123-W1-20260101-000000',
            'remark':'Portable TEMU worker; local ProxyShare chain; never configure ProxyShare directly',
            'multi_device_mode':False,'password':'must-not-save',**extras}


@pytest.mark.parametrize('replacement',[False,True])
def test_reclaim_archives_redacted_metadata_rechecks_and_deletes(tmp_path,replacement):
    calls=[]; snapshots=[]
    row=profile(name='TEMU-machine123-W1-replacement-20260101-000000',remark='NEW profile + NEW ProxyShare chained session; automatic replacement') if replacement else profile()
    def api(base,path,**kwargs):
        calls.append(path)
        if '/user/list' in path: return {'data':{'list':[row]}}
        if 'cloud-active' in path: return {'data':[]}
        if '/active?' in path: return {'data':{'status':'Inactive'}}
        assert path.endswith('/delete'); assert kwargs['body']=={'profile_id':['old']}
        assert snapshots[-1]['status']=='DELETE_PENDING'
        return {'code':0}
    def atomic(path,value): snapshots.append(dict(value))
    assert reclaim_one(api,'fixture','machine123-any',set(),tmp_path,atomic)=='old'
    assert sum('/active?' in p for p in calls)==2
    assert sum('cloud-active' in p for p in calls)==2
    assert 'must-not-save' not in json.dumps(snapshots)
    assert snapshots[-1]['status']=='DELETED'


@pytest.mark.parametrize('case',['protected','foreign','active','remote_active','unknown','mode_unknown','mode_enabled','race'])
def test_reclaim_protects_unsafe_profiles(tmp_path,case):
    calls=[]; checks=[0]; row=profile()
    if case=='foreign': row['name']='ordinary-browser'
    if case=='mode_unknown': row.pop('multi_device_mode')
    if case=='mode_enabled': row['multi_device_mode']=True
    def api(base,path,**kwargs):
        calls.append(path)
        if '/user/list' in path:return {'data':{'list':[row]}}
        if 'cloud-active' in path:return {'data':[{'user_id':'old'}] if case=='remote_active' else []}
        if '/active?' in path:
            checks[0]+=1
            return {'data':{'status': 'Unknown' if case=='unknown' else 'Active' if case=='active' or (case=='race' and checks[0]>1) else 'Inactive'}}
        pytest.fail('unsafe delete')
    assert reclaim_one(api,'fixture','machine123-any',{'old'} if case=='protected' else set(),tmp_path,lambda *_:None) is None
    assert not any('delete' in p for p in calls)


def test_profile_list_paginates():
    pages=[]
    def api(base,path,**kwargs):
        page=int(parse_qs(urlsplit(path).query)['page'][0]); pages.append(page)
        return {'data':{'list':[profile(str(n)) for n in range(100)] if page==1 else [profile('last')]}}
    assert len(list_profiles(api,'fixture'))==101
    assert pages==[1,2]
