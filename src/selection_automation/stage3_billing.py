"""Read-only isolation of uncertain paid requests; never settle provider usage."""
import json
from pathlib import Path


def _rows(path):
    try:
        with Path(path).open(encoding='utf-8-sig') as stream:
            for line in stream:
                if line.strip():
                    row = json.loads(line)
                    if not isinstance(row, dict): raise ValueError('Invalid billing row')
                    yield row
    except FileNotFoundError:
        return


def _v1_retry_authorizations(intent_path, result_path):
    """Validate exact human retry scope without changing the billing journal."""
    path=Path(result_path).with_name('billing_retry_authorizations.json')
    empty={'request_ids':set(),'by_pair':{},'success_pairs':set(),'attempts':{}}
    if not path.exists(): return empty
    document=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(document,dict) or document.get('version')!=1 or not isinstance(document.get('authorizations'),list):
        raise ValueError('Invalid retry authorization document')
    started={r['request_id']:r for r in _rows(intent_path) if r.get('status')=='started'}
    results=list(_rows(result_path));scope={};by_pair={};ids=set();auth_ids=set()
    originals={(r.get('vision_revision_key'),r.get('asin'),r.get('goods_id'),r.get('attempt'))
               for r in results if r.get('billing_state')=='UNKNOWN' and r.get('api_status')=='failed'}
    for auth in document['authorizations']:
        if not isinstance(auth,dict):raise ValueError('Invalid authorization object')
        aid=auth.get('authorization_id')
        if (not isinstance(aid,str) or not aid or aid in auth_ids or
            type(auth.get('authorized_at')) not in (int,float) or auth['authorized_at']<=0 or
            type(auth.get('max_extra_attempts')) is not int or auth['max_extra_attempts']!=1 or
            not isinstance(auth.get('scope'),list) or not auth['scope']):
            raise ValueError('Invalid human retry authorization')
        auth_ids.add(aid)
        for item in auth['scope']:
            if not isinstance(item,dict):raise ValueError('Invalid scope object')
            rid=item.get('original_request_id');key=item.get('vision_revision_key')
            pair=(item.get('asin'),item.get('goods_id'));baseline=item.get('baseline_attempt')
            if (not all(isinstance(v,str) and v for v in (rid,key,*pair)) or
                type(baseline) is not int or baseline<1 or (rid,key) in scope or pair in by_pair):
                raise ValueError('Invalid or duplicate retry scope')
            original=started.get(rid,{})
            if key not in (original.get('vision_revision_keys') or []):
                raise ValueError('Retry scope does not bind original intent')
            if (key,*pair,baseline) not in originals:
                raise ValueError('Retry scope does not bind original unknown result')
            scope[rid,key]=item;by_pair[pair]=item;ids.add(rid)
    for rid in ids:
        row=started[rid];keys=row.get('vision_revision_keys')
        if (not isinstance(keys,list) or not keys or len(keys)!=len(set(keys)) or
            type(row.get('pairs')) is not int or row['pairs']!=len(keys) or
            {key for request,key in scope if request==rid}!=set(keys)):
            raise ValueError('Retry scope must include complete original request')
    success=set();attempts={}
    for row in results:
        pair=(row.get('asin'),row.get('goods_id'))
        if pair not in by_pair: continue
        if row.get('api_status')=='success': success.add(pair)
        attempt=row.get('attempt',0)
        if type(attempt) is not int: raise ValueError('Invalid scoped result attempt')
        attempts[pair]=max(attempts.get(pair,0),attempt)
    return {'request_ids':ids,'by_pair':by_pair,'success_pairs':success,'attempts':attempts}


def retry_authorizations(intent_path,result_path):
    result=_v1_retry_authorizations(intent_path,result_path)
    from .schema_billing_retry import billing_view
    repair=billing_view(Path(result_path).parent)
    if repair is None:return result
    if result['request_ids'] & repair['request_ids'] or set(result['by_pair']) & set(repair['by_pair']):
        raise ValueError('Overlapping retry authorizations')
    return {**result,'request_ids':result['request_ids']|repair['request_ids'],
            'by_pair':{**result['by_pair'],**repair['by_pair']},
            'success_pairs':result['success_pairs']|repair['success_pairs'],
            'attempts':{**result['attempts'],**repair['attempts']},'schema_retry':repair['schema_retry']}


def authorized_retry_attempt(authorization,pair,keys):
    item=authorization['by_pair'].get(pair)
    if not item or pair in authorization['success_pairs'] or not keys or keys[0]!=item['vision_revision_key']:
        return None
    if item.get('schema_retry') and item.get('claimed'):return None
    limit=item['baseline_attempt']+1
    return limit if authorization['attempts'].get(pair,0)<limit else None


def billing_hold(intent_path, result_path):
    """Unmapped uncertainty fails closed; mapped pairs stay held across revisions."""
    empty = {'request_ids': set(), 'keys': set(), 'pairs': set(), 'fail_closed': False}
    try:
        started = {}; settled = set()
        for row in _rows(intent_path):
            rid = row.get('request_id')
            if not isinstance(rid, str) or not rid: raise ValueError('Invalid intent ID')
            if row.get('status') == 'started':
                if rid in started and started[rid] != row: raise ValueError('Conflicting intent')
                started[rid] = row
            elif row.get('status') == 'settled':
                if row.get('billing_state') not in ('KNOWN', 'NO_CHARGE'):
                    raise ValueError('Uncertain settlement')
                settled.add(rid)
            else: raise ValueError('Invalid intent status')
        authorization=retry_authorizations(intent_path,result_path)
        unresolved = set(started)-settled-authorization['request_ids']
        if not unresolved: return empty
        keys = set()
        for rid in unresolved:
            row = started[rid]; group = row.get('vision_revision_keys')
            if (not isinstance(group, list) or not group or
                any(not isinstance(k, str) or not k for k in group) or
                len(set(group)) != len(group) or type(row.get('pairs')) is not int or
                row['pairs'] != len(group)):
                raise ValueError('Unmapped intent keys')
            keys.update(group)
        mapped = {}
        for row in _rows(result_path):
            key = row.get('vision_revision_key')
            if key not in keys: continue
            pair = (row.get('asin'), row.get('goods_id'))
            if not all(isinstance(v, str) and v for v in pair):
                raise ValueError('Invalid intent pair')
            if key in mapped and mapped[key] != pair: raise ValueError('Conflicting key binding')
            mapped[key] = pair
        if set(mapped) != keys: raise ValueError('Missing intent pair binding')
        return {'request_ids': unresolved, 'keys': keys, 'pairs': set(mapped.values()), 'fail_closed': False}
    except (ValueError, OSError, TypeError, KeyError):
        return {**empty, 'fail_closed': True}
