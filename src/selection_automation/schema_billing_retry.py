"""One explicit, task-bound retry of the historical empty-q HTTP400 defect.

The original UNKNOWN journal and provider block are never settled or removed.
This is a narrow exception, not a general provider/billing reset mechanism.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from .merge_temu_snapshots import atomic_write
from .process_lock import ProcessLock

FILE = 'billing_schema_retry_authorization.json'
POLICY = 'empty-selected-q-schema-repair-v1'
ERROR = 'An object with no properties is not allowed.'


def _read(path):
    value=json.loads(Path(path).read_text('utf-8-sig'))
    if not isinstance(value,dict):raise ValueError('Invalid retry evidence object')
    return value


def _rows(path):
    path=Path(path)
    if not path.exists():return []
    values=[json.loads(line) for line in path.read_text('utf-8-sig').splitlines() if line.strip()]
    if any(not isinstance(row,dict) for row in values):raise ValueError('Invalid retry evidence')
    return values


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def _hex(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def _empty_objects(value):
    if isinstance(value,dict):
        return (int(value.get('type')=='object' and not value.get('properties'))+
                sum(_empty_objects(v) for v in value.values()))
    return sum(_empty_objects(v) for v in value) if isinstance(value,list) else 0


def protocol_binding(category,pair):
    """Reconstruct ONLY the exact old empty-q wire; never send that old wire.

    Matching its real old revision proves the same images, product evidence,
    questions and provider. The normal revision function computes both keys.
    """
    from . import vision
    from .match_criteria import enabled,selected
    if not enabled(category) or selected(category,'visual'):
        raise ValueError('This repair only supports no selected visual conditions')
    prompt,schema=vision.protocol(category,1)
    if _empty_objects(schema):raise ValueError('The current response schema is not repaired')
    old=deepcopy(schema)
    item=old['properties']['candidates']['items']
    if 'q' in item['properties'] or 'q' in item['required']:
        raise ValueError('Unexpected repaired wire shape')
    item['properties']['q']={'type':'object','properties':{},'required':[],'additionalProperties':False}
    item['required'].insert(item['required'].index('c'),'q')
    replacements={
        '本次没有额外勾选图片条件，不输出q字段。':'q仅核实以下勾选图片条件，r用S一致/D明确不同/U不可辨认，a/t写具体双边事实。',
        '本次没有额外强制一致条件，不输出q字段。':'q仅回答额外强制一致条件，r用S/D/U及双边事实；没有额外条件时q={}。',
    }
    present=[value for value in replacements if value in prompt]
    if len(present)!=1:raise ValueError('Unrecognized empty-q repair protocol')
    old_prompt=prompt.replace(present[0],replacements[present[0]])
    context=vision.VisionRevisionContext(category)
    # A private, per-call template copy; no global protocol monkeypatch/cache.
    context._current._fields['protocol']=json.dumps((old_prompt,old),ensure_ascii=False,sort_keys=True)
    old_key=vision.compatible_vision_keys(category,pair,context=context)[0]
    new_key=vision.compatible_vision_keys(category,pair)[0]
    if old_key==new_key:raise ValueError('The real protocol revision did not change')
    return {'original_key':old_key,'retry_key':new_key,
            'original_protocol_sha256':_digest((old_prompt,old)),
            'retry_protocol_sha256':_digest((prompt,schema)),
            'image_sha256':{side:vision.image_digest(pair[side+'_image_path']) for side in ('amazon','temu')}}


def read_authorization(folder):
    """Validate persisted scope; malformed evidence never falls back to dispatch."""
    folder=Path(folder);path=folder/FILE
    if not path.exists():return None
    auth=_read(path)
    if (not isinstance(auth,dict) or auth.get('version')!=2 or auth.get('policy')!=POLICY
            or not re.fullmatch('[0-9a-f]{32}',str(auth.get('authorization_id','')))
            or type(auth.get('authorized_at')) not in (int,float) or not math.isfinite(auth['authorized_at']) or auth['authorized_at']<=0
            or type(auth.get('max_extra_attempts')) is not int or auth['max_extra_attempts']!=1 or auth.get('task_key')!=folder.parent.name
            or not isinstance(auth.get('scope'),list) or len(auth['scope'])!=1):
        raise ValueError('Invalid schema retry authorization')
    item=auth['scope'][0]
    if (not isinstance(item,dict) or not all(isinstance(item.get(k),str) and item[k]
            for k in ('asin','goods_id','pair_revision','original_request_id','original_vision_revision_key','retry_vision_revision_key'))
            or type(item.get('baseline_attempt')) is not int or item['baseline_attempt']<1
            or item['original_vision_revision_key']==item['retry_vision_revision_key']
            or any(not _hex(auth.get(k)) for k in ('block_sha256','key_identity','original_protocol_sha256','retry_protocol_sha256'))
            or auth['original_protocol_sha256']==auth['retry_protocol_sha256']
            or not isinstance(auth.get('image_sha256'),dict)
            or any(not _hex(auth['image_sha256'].get(side)) for side in ('amazon','temu'))):
        raise ValueError('Invalid schema retry binding')
    starts=[r for r in _rows(folder/'request_intents.jsonl') if r.get('status')=='started'
            and r.get('request_id')==item['original_request_id']]
    if len(starts)!=1 or starts[0].get('pairs')!=1 or starts[0].get('vision_revision_keys')!=[item['original_vision_revision_key']]:
        raise ValueError('Retry must bind the complete original single-pair request')
    original=[r for r in _rows(folder/'results.jsonl') if all(r.get(k)==item[k] for k in ('asin','goods_id','pair_revision'))
              and r.get('vision_revision_key')==item['original_vision_revision_key']
              and r.get('attempt')==item['baseline_attempt'] and r.get('api_status')=='failed'
              and r.get('billing_state')=='UNKNOWN' and r.get('failure_status')==400]
    if not original:raise ValueError('Original UNKNOWN result is missing')
    block=_read(folder/'provider_blocked.json')
    if (_hash(folder/'provider_blocked.json')!=auth['block_sha256']
            or _hash(folder/'schema-retry-audit'/auth['authorization_id']/'provider_blocked.json')!=auth['block_sha256']
            or block.get('status')!=400 or block.get('provider')!=auth.get('provider')
            or block.get('key_identity')!=auth['key_identity']
            or (block.get('provider_error') or {}).get('code')!='invalid_request_error'
            or not str((block.get('provider_error') or {}).get('message','')).startswith(ERROR)):
        raise ValueError('Original provider block changed or is not this schema fault')
    diagnostics=[r for r in _rows(folder/'exception_diagnostics.jsonl') if r.get('request_id')==item['original_request_id']
                 and r.get('status_code')==400 and (r.get('provider_error') or {}).get('code')=='invalid_request_error'
                 and str((r.get('provider_error') or {}).get('message','')).startswith(ERROR)]
    if not diagnostics:raise ValueError('Original request schema rejection is not proven')
    claim=folder/'schema-retry-claims'/f"{auth['authorization_id']}.json"
    auth={**auth,'claimed':claim.exists()}
    if claim.exists():
        value=_read(claim)
        if (value.get('authorization_id')!=auth['authorization_id'] or value.get('original_request_id')!=item['original_request_id']
                or not isinstance(value.get('request_id'),str) or not value['request_id']):
            raise ValueError('Invalid durable schema retry claim')
    return auth


def create_authorization(folder,category,candidate_path,expected_request_id=None):
    """Only the explicit web consent handler calls this; never starts analysis."""
    folder=Path(folder)
    with ProcessLock(folder/'schema-retry.lock'):
        if (folder/FILE).exists() or (folder/'billing_retry_authorizations.json').exists():
            raise ValueError('This task already has a retry authorization; no extra budget can be created')
        starts={r.get('request_id'):r for r in _rows(folder/'request_intents.jsonl') if r.get('status')=='started'}
        settled={r.get('request_id') for r in _rows(folder/'request_intents.jsonl') if r.get('status')=='settled'}
        remaining=set(starts)-settled
        if len(remaining)!=1:raise ValueError('Exactly one original unknown request is required')
        rid=next(iter(remaining));start=starts[rid]
        if expected_request_id is not None and rid!=expected_request_id:
            raise ValueError('Original request changed after the cost confirmation was displayed')
        if start.get('pairs')!=1 or not isinstance(start.get('vision_revision_keys'),list) or len(start['vision_revision_keys'])!=1:
            raise ValueError('Only a complete original single-pair request is supported')
        originals=[r for r in _rows(folder/'results.jsonl') if r.get('vision_revision_key')==start['vision_revision_keys'][0]
                   and r.get('api_status')=='failed' and r.get('billing_state')=='UNKNOWN' and r.get('failure_status')==400]
        if len(originals)!=1:raise ValueError('Original schema failure is ambiguous')
        previous=originals[0]
        pairs=[r for r in _rows(candidate_path) if all(r.get(k)==previous.get(k) for k in ('asin','goods_id','pair_revision'))]
        if len(pairs)!=1:raise ValueError('Original candidate is no longer exactly available')
        binding=protocol_binding(category,pairs[0])
        if binding['original_key']!=previous['vision_revision_key']:
            raise ValueError('Images, rules, provider or evidence changed beyond the exact schema repair')
        from .stage3_provider import from_category
        provider={k:from_category(category)[k] for k in ('mode','base_url','model','protocol')}
        block=_read(folder/'provider_blocked.json')
        if block.get('provider')!=provider:raise ValueError('Provider or model changed; this retry cannot release it')
        if (block.get('status')!=400 or not _hex(block.get('key_identity'))
                or (block.get('provider_error') or {}).get('code')!='invalid_request_error'
                or not str((block.get('provider_error') or {}).get('message','')).startswith(ERROR)
                or not any(r.get('request_id')==rid and r.get('status_code')==400
                    and (r.get('provider_error') or {}).get('code')=='invalid_request_error'
                    and str((r.get('provider_error') or {}).get('message','')).startswith(ERROR)
                    for r in _rows(folder/'exception_diagnostics.jsonl'))):
            raise ValueError('Only the proven original empty-object HTTP400 can be rechecked')
        aid=uuid.uuid4().hex
        item={k:previous[k] for k in ('asin','goods_id','pair_revision')}
        item.update(original_request_id=rid,original_vision_revision_key=previous['vision_revision_key'],
                    retry_vision_revision_key=binding['retry_key'],baseline_attempt=previous['attempt'])
        auth={'version':2,'policy':POLICY,'authorization_id':aid,'authorized_at':time.time(),
              'task_key':folder.parent.name,'max_extra_attempts':1,'scope':[item],
              'block_sha256':_hash(folder/'provider_blocked.json'),'key_identity':block.get('key_identity'),
              'provider':provider,**{k:binding[k] for k in ('original_protocol_sha256','retry_protocol_sha256','image_sha256')}}
        audit=folder/'schema-retry-audit'/aid;audit.mkdir(parents=True)
        with (audit/'provider_blocked.json').open('xb') as stream:
            stream.write((folder/'provider_blocked.json').read_bytes());stream.flush()
            import os
            os.fsync(stream.fileno())
        atomic_write(folder/FILE,json.dumps(auth,ensure_ascii=False,indent=2))
        try:return read_authorization(folder)
        except Exception:
            # Preserve a failed authorization and its audit evidence; never reopen budget.
            raise ValueError('Schema retry authorization validation failed; no request was sent') from None


def validate_current(auth,category,pair,key_identity,folder):
    item=auth['scope'][0]
    if auth.get('claimed'):raise ValueError('The one schema retry budget is already consumed')
    if any(pair.get(k)!=item[k] for k in ('asin','goods_id','pair_revision')) or key_identity!=auth['key_identity']:
        raise ValueError('Schema retry identity changed')
    from .stage3_provider import from_category
    if {k:from_category(category)[k] for k in ('mode','base_url','model','protocol')}!=auth['provider']:
        raise ValueError('Schema retry provider changed')
    binding=protocol_binding(category,pair)
    if (binding['original_key']!=item['original_vision_revision_key'] or binding['retry_key']!=item['retry_vision_revision_key']
            or any(binding[k]!=auth[k] for k in ('original_protocol_sha256','retry_protocol_sha256','image_sha256'))):
        raise ValueError('Schema retry protocol or image binding changed')


def claim(folder,authorization_id,request_id,*,category=None,pair=None,key_identity=None):
    """Durable one-shot budget, consumed even if the process then crashes."""
    folder=Path(folder)
    if not isinstance(request_id,str) or not request_id:raise ValueError('Invalid retry request identity')
    with ProcessLock(folder/'schema-retry.lock'):
        auth=read_authorization(folder)
        if not auth or auth['authorization_id']!=authorization_id or auth['claimed']:
            raise ValueError('Schema retry already consumed or changed')
        if category is not None:
            validate_current(auth,category,pair,key_identity,folder)
        directory=folder/'schema-retry-claims';directory.mkdir(exist_ok=True)
        with (directory/f'{authorization_id}.json').open('x',encoding='utf-8') as stream:
            json.dump({'authorization_id':authorization_id,'original_request_id':auth['scope'][0]['original_request_id'],
                       'request_id':request_id,'claimed_at':time.time()},stream)
            stream.flush()
            import os
            os.fsync(stream.fileno())


def billing_view(folder):
    auth=read_authorization(folder)
    if not auth:return None
    item=auth['scope'][0]
    rows=_rows(Path(folder)/'results.jsonl')
    return {'request_ids':{item['original_request_id']},'by_pair':{(item['asin'],item['goods_id']):{
                **item,'vision_revision_key':item['retry_vision_revision_key'],'schema_retry':True,'claimed':auth['claimed']}},
            'success_pairs':{(item['asin'],item['goods_id']) for r in rows if r.get('api_status')=='success'
                and all(r.get(k)==item[k] for k in ('asin','goods_id','pair_revision'))
                and r.get('vision_revision_key')==item['retry_vision_revision_key']},
            'attempts':{(item['asin'],item['goods_id']):max((r.get('attempt',0) for r in rows
                if (r.get('asin'),r.get('goods_id'))==(item['asin'],item['goods_id'])),default=0)},
            'schema_retry':auth}
