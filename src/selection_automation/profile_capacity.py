"""Reclaim only inactive, machine-owned portable TEMU profiles on account limits.

Official API contracts: /api/v1/user/list, /api/v1/browser/active,
/api/v1/browser/cloud-active, /api/v2/browser-profile/delete.
"""
import re
from datetime import datetime, timezone
from urllib.parse import urlencode


def list_profiles(api, base):
    seen=set(); result=[]
    for page in range(1, 101):
        payload=api(base, '/api/v1/user/list?'+urlencode({'page':page,'page_size':100}), timeout=30)
        batch=(payload.get('data') or {}).get('list')
        if not isinstance(batch,list):
            raise RuntimeError('ADSPOWER_PROFILE_LIST_UNKNOWN')
        fresh=[row for row in batch if str(row.get('user_id') or row.get('profile_id') or '') not in seen]
        if batch and not fresh:
            raise RuntimeError('ADSPOWER_PROFILE_PAGINATION_STALLED')
        for row in fresh:
            identifier=str(row.get('user_id') or row.get('profile_id') or '')
            if identifier:
                seen.add(identifier); result.append(row)
        if len(batch)<100:
            return result
    raise RuntimeError('ADSPOWER_PROFILE_PAGINATION_LIMIT')


def inactive(api, base, identifier):
    try:
        local=api(base,'/api/v1/browser/active?'+urlencode({'user_id':identifier}),timeout=30)
        if (local.get('data') or {}).get('status')!='Inactive':
            return False
        remote=api(base,'/api/v1/browser/cloud-active',method='POST',body={'user_ids':identifier},timeout=30)
        return remote.get('code',0)==0 and remote.get('data')==[]
    except Exception:
        return False


def reclaim_one(api, base, machine_id, protected, archive_dir, atomic_json, multi_device_mode=None):
    """No stop calls, no broad account cleanup, no raw profile credential snapshots."""
    owned_name=re.compile(r'^TEMU-'+re.escape(machine_id[:10])+r'-W[1-4]-(?:replacement-)?\d{8}-\d{6}$')
    for row in list_profiles(api,base):
        identifier=str(row.get('user_id') or row.get('profile_id') or '')
        if not identifier or identifier in protected or not owned_name.fullmatch(str(row.get('name',''))):
            continue
        if not str(row.get('remark','')).startswith(('Portable TEMU worker; local ProxyShare chain;', 'NEW profile + NEW ProxyShare chained session;')):
            continue
        # Multi-device mode makes cloud-active report a false inactive result.
        if str(row.get('multi_device',row.get('multi_device_mode',multi_device_mode))).lower() not in ('0','false','off'):
            continue
        if not inactive(api,base,identifier):
            continue
        import hashlib
        archive=archive_dir/(hashlib.sha256(identifier.encode()).hexdigest()+'.json')
        metadata={'profile_id':identifier,'name':row['name'],'machine_id':machine_id,
                  'reason':'inactive_owned_profile_capacity_reclaim','status':'DELETE_PENDING',
                  'checked_at':datetime.now(timezone.utc).isoformat()}
        atomic_json(archive,metadata)
        # Recheck immediately before deletion, after disk operations.
        if not inactive(api,base,identifier):
            metadata['status']='SKIPPED_ACTIVITY_CHANGED'; atomic_json(archive,metadata)
            continue
        api(base,'/api/v2/browser-profile/delete',method='POST',body={'profile_id':[identifier]},timeout=30)
        metadata['status']='DELETED'; atomic_json(archive,metadata)
        return identifier
    return None
