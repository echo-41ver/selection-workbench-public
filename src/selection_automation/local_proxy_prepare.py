"""Adopt a discovered proxy only during idle project preparation."""
import json
from pathlib import Path
from .local_proxy import discover,owned
from .network_readiness import select_transport,NetworkNotReady,describe
from .route_guard import RouteLock,collection_active

def verify_saved(settings,check_owner=owned):
    if not settings or 'preproxy_port' not in settings:return False
    if not settings.get('preproxy_port'):return settings.get('preproxy_protocol') in {'DIRECT','SYSTEM_ROUTE'}
    owner=settings.get('local_proxy_owner')
    if not isinstance(owner,dict):return False
    try:return check_owner({**owner,'host':settings['preproxy_host'],'port':settings['preproxy_port']})
    except (KeyError,TypeError,ValueError):return False

def prepare(root,company,read,save,*,observation=discover,business_active=collection_active,check_owner=owned):
    root=Path(root)
    with RouteLock(root/'runtime'):
        active=business_active(root)
        if active:raise NetworkNotReady('任务或人工验证正在使用出口，未重新探测或切换。',company.get('preproxy_mode','AUTO'))
        next_path=root/'runtime/local-proxy-next-choice.json'
        # A future preference is adopted only here, after the idle check. Reuse
        # of an already prepared task deliberately ignores this pending file.
        next_choice=read(next_path,{}) if company.get('proxy_architecture')=='LOCAL_VPN' else {}
        choice=next_choice or read(root/'runtime/local-proxy-choice.json',{})
        if choice:company={**company,'local_proxy_endpoint':choice}
        saved=read(root/'runtime/secrets/proxyshare.settings.json',{})
        if not verify_saved(saved,check_owner) and check_owner is owned:
            saved=refresh_saved_owner(root,saved)
        try:
            transport,status=select_transport(company,local_discovery=observation,saved=saved)
            if business_active(root) or not verify_saved(transport,check_owner):
                raise NetworkNotReady('任务已开始或代理端口归属已变化；未采用新出口。',company.get('preproxy_mode','AUTO'))
        except NetworkNotReady as error:
            save(root/'runtime/network-readiness.json',error.diagnostic)
            raise
        save(root/'runtime/network-readiness.json',status)
        if saved:
            # Preserve gateway configuration and checkpoint; remove stale owner
            # metadata when an explicitly selected route has no local proxy.
            updated={k:v for k,v in saved.items() if k!='local_proxy_owner'}
            updated.update(transport)
            save(root/'runtime/secrets/proxyshare.settings.json',updated)
        if next_choice:
            adopted={key:transport[field] for key,field in
                     [('host','preproxy_host'),('port','preproxy_port'),('protocol','preproxy_protocol')]}
            save(root/'runtime/local-proxy-choice.json',adopted)
            # Clear last so an interrupted commit can retry the same preference.
            save(next_path,{})
        return transport,status


def refresh_saved_owner(root, saved, *, evidence=None, protocol_probe=None, check_owner=owned):
    from .local_proxy import snapshot,probe
    from .merge_temu_snapshots import atomic_write
    if not saved.get('preproxy_port'):return saved
    evidence=snapshot() if evidence is None else evidence
    protocol_probe=probe if protocol_probe is None else protocol_probe
    expected=(str(saved.get('preproxy_host')),int(saved['preproxy_port']))
    candidates=[]
    for process in evidence.get('processes',[]):
        if process.get('name')!=(saved.get('local_proxy_owner') or {}).get('name'):continue
        for listener in process.get('listeners',[]):
            if (listener.get('host'),listener.get('port'))==expected:
                candidates.append({**listener,**{k:process[k] for k in ('pid','created','name')}})
    if len(candidates)!=1 or not check_owner(candidates[0]):return saved
    candidate=candidates[0]
    observed=protocol_probe(candidate).get('protocol')
    if observed not in {saved.get('preproxy_protocol'),'MIXED'} or not check_owner(candidate):return saved
    updated={**saved,'local_proxy_owner':{k:candidate[k] for k in ('pid','created','name')}}
    atomic_write(Path(root)/'runtime/secrets/proxyshare.settings.json',json.dumps(updated,ensure_ascii=False))
    return updated


def reuse_prepared(root,company,read,*,check_owner=owned):
    """Validate an unchanged saved route under the same lock; never discover/adopt."""
    from .network_readiness import validate_saved_route
    root=Path(root)
    with RouteLock(root/'runtime'):
        saved=read(root/'runtime/secrets/proxyshare.settings.json',{})
        if not verify_saved(saved,check_owner) and check_owner is owned:
            saved=refresh_saved_owner(root,saved)
        if not verify_saved(saved,check_owner):
            raise NetworkNotReady('已保存出口的进程或端口归属变化；未复用或切换。',company.get('preproxy_mode','AUTO'))
        proxy=company.get('proxy',{})
        local=company.get('proxy_architecture')=='LOCAL_VPN'
        if local and saved.get('proxy_architecture')!='LOCAL_VPN':
            raise NetworkNotReady('当前任务仍为住宅代理模式，请停止后创建本机 VPN 任务。',company.get('preproxy_mode','AUTO'))
        if not local and (saved.get('proxy_host')!=proxy.get('host') or int(saved.get('proxy_port',0))!=int(proxy.get('port',0))):
            raise NetworkNotReady('代理网关配置与已准备任务不一致；未复用或切换。',company.get('preproxy_mode','AUTO'))
        choice=read(root/'runtime/local-proxy-choice.json',{}) or company.get('local_proxy_endpoint')
        if choice and any(str(choice.get(key,'')).casefold()!=str(saved.get(field,'')).casefold()
                          for key,field in [('host','preproxy_host'),('port','preproxy_port'),('protocol','preproxy_protocol')]):
            raise NetworkNotReady('所选本机出口与已准备任务不一致；未复用或切换。',company.get('preproxy_mode','AUTO'))
        status=validate_saved_route(company,saved)
        status.update(route_reused=True,probe_level='SAVED_ROUTE_OWNER_RECHECKED')
        fields=('preproxy_host','preproxy_port','preproxy_protocol','preproxy_mode','network_probe_level','local_proxy_owner')
        return {k:saved[k] for k in fields if k in saved},status
