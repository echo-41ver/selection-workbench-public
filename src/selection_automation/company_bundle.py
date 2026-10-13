"""Explicit, minimal portable configuration and reversible local import."""
from pathlib import Path
import os,uuid,shutil
from urllib.parse import urlsplit

SCHEMA='company-bundle-v2'
def required(value,name):
    if not isinstance(value,str) or not value.strip() or any(ord(c)<32 for c in value):
        raise ValueError('Missing or invalid configuration field: '+name)
    return value.strip()
def normalize(value,require_model=False):
    if not isinstance(value,dict):raise ValueError('Company configuration must be an object')
    version=value.get('schema_version')
    if version not in (None,SCHEMA):raise ValueError('Unsupported company configuration schema')
    proxy=value.get('proxy',{})
    if not isinstance(proxy,dict):raise ValueError('Invalid proxy configuration')
    architecture=value.get('proxy_architecture','LOCAL_CHAIN_PROXYSHARE')
    if architecture not in {'LOCAL_VPN','LOCAL_CHAIN_PROXYSHARE'}:raise ValueError('Invalid proxy_architecture')
    if architecture!='LOCAL_VPN':
        user=required(proxy.get('username'),'proxy.username')
        if '_session-' not in user:raise ValueError('proxy.username requires _session- segment')
        port=proxy.get('port')
        if isinstance(port,bool) or not isinstance(port,int) or not 1<=port<=65535:raise ValueError('Invalid proxy.port')
    api=required(value.get('adspower_api_url','http://127.0.0.1:50325'),'adspower_api_url')
    parsed=urlsplit(api)
    if parsed.scheme not in ('http','https') or parsed.hostname not in ('localhost','127.0.0.1','::1') or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('AdsPower API must be a local address without credentials')
    out={'schema_version':SCHEMA,'adspower_api_key':required(value.get('adspower_api_key'),'adspower_api_key'),'adspower_api_url':api,
         'proxy_architecture':architecture}
    if architecture!='LOCAL_VPN':
        out['proxy']={'host':required(proxy.get('host'),'proxy.host'),'port':port,'username':user,'password':required(proxy.get('password'),'proxy.password')}
    from .network_readiness import mode
    mode(value)
    if 'preproxy_mode' in value:out['preproxy_mode']=value['preproxy_mode']
    candidates=value.get('preproxy_candidates',[])
    if not isinstance(candidates,list):raise ValueError('Invalid preproxy_candidates')
    out['preproxy_candidates']=[]
    for row in candidates:
        if not isinstance(row,dict) or not isinstance(row.get('port'),int) or isinstance(row.get('port'),bool) or not 1<=row['port']<=65535:raise ValueError('Invalid preproxy_candidates port')
        out['preproxy_candidates'].append({'host':required(row.get('host'),'preproxy.host'),'port':row['port']})
    if 'local_proxy_endpoint' in value:
        from .local_proxy import loopback
        endpoint=value['local_proxy_endpoint']
        if not isinstance(endpoint,dict) or set(endpoint)-{'host','port','protocol'}:raise ValueError('Invalid local_proxy_endpoint')
        host=endpoint.get('host');port=endpoint.get('port');protocol=endpoint.get('protocol','AUTO')
        if not isinstance(host,str) or not loopback(host) or isinstance(port,bool) or not isinstance(port,int) or not 1<=port<=65535 or not isinstance(protocol,str) or protocol not in {'AUTO','SOCKS5','HTTP'}:raise ValueError('Invalid local_proxy_endpoint')
        out['local_proxy_endpoint']={'host':host,'port':port,'protocol':protocol}
    model=value.get('deepseek')
    if model is not None:
        if not isinstance(model,dict):raise ValueError('Invalid deepseek configuration')
        endpoint=required(model.get('base_url','https://api.deepseek.com'),'deepseek.base_url').rstrip('/')
        name=required(model.get('model','deepseek-flash'),'deepseek.model')
        if endpoint!='https://api.deepseek.com' or name!='deepseek-flash':raise ValueError('This release requires the existing official DeepSeek endpoint and deepseek-flash model')
        out['deepseek']={'key':required(model.get('key'),'deepseek.key'),'base_url':endpoint,'model':name}
    elif require_model:raise ValueError('Missing deepseek.key; complete the local packaging form')
    return out

def import_bundle(root,value,store):
    """All validation precedes writes; rollback both encrypted files on failure."""
    normalized=normalize(value,require_model=value.get('schema_version')==SCHEMA);folder=Path(root)/'runtime/secrets';folder.mkdir(parents=True,exist_ok=True)
    targets=[('company.dpapi',{k:v for k,v in normalized.items() if k!='deepseek'})]
    if 'deepseek' in normalized:targets.append(('deepseek.dpapi',normalized['deepseek']))
    before={name:(folder/name).read_bytes() if (folder/name).exists() else None for name,_ in targets}
    transaction=folder/('import-backup-'+uuid.uuid4().hex);transaction.mkdir()
    for name,data in before.items():
        if data is not None:(transaction/name).write_bytes(data)
    prepared=[]
    try:
        for name,data in targets:
            temporary=folder/(uuid.uuid4().hex+'.dpapi');prepared.append(temporary);store(temporary,data)
        for (name,_),temporary in zip(targets,prepared):os.replace(temporary,folder/name)
    except Exception:
        for name,data in before.items():
            target=folder/name
            if data is None:target.unlink(missing_ok=True)
            else:
                temporary=folder/(uuid.uuid4().hex+'.rollback');temporary.write_bytes(data);os.replace(temporary,target)
        raise RuntimeError('Company import failed; previous encrypted settings restored') from None
    finally:
        for temporary in prepared:
            temporary.unlink(missing_ok=True)
            temporary.with_suffix('.tmp').unlink(missing_ok=True)
    return normalized

def apply_model_settings(saved,environ):
    # Child-process settings only; no setx, registry or persistent environment changes.
    model=normalize({'adspower_api_key':'placeholder','proxy':{'host':'placeholder','port':1,'username':'placeholder_session-test','password':'placeholder'},'deepseek':saved})['deepseek']
    environ['DEEPSEEK_API_KEY']=model['key'];environ['OPENAI_BASE_URL']=model['base_url'];environ['OPENAI_VISION_MODEL']=model['model']
