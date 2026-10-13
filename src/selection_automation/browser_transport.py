"""Shared local VPN route for AdsPower and Amazon, without residential credentials."""
import json
from pathlib import Path
from .local_proxy_prepare import verify_saved
from .network_readiness import NetworkNotReady


def local_vpn(value):
    return value.get('proxy_architecture')=='LOCAL_VPN'


def adspower_proxy(settings):
    if not verify_saved(settings):
        raise NetworkNotReady('本机 VPN 出口归属已变化，请重新准备；未回退住宅代理。','AUTO')
    if not settings.get('preproxy_port'):
        if settings.get('preproxy_protocol')!='SYSTEM_ROUTE':
            raise ValueError('本机 VPN 模式需要已确认的代理出口或 TUN 路由')
        return {'proxy_soft':'no_proxy'}
    protocol=settings['preproxy_protocol']
    return {'proxy_soft':'other','proxy_type':'socks5' if protocol=='SOCKS5' else 'http',
            'proxy_host':settings['preproxy_host'],'proxy_port':str(settings['preproxy_port']),
            'proxy_user':'','proxy_password':''}


def amazon_environment(root):
    path=Path(root)/'runtime/secrets/proxyshare.settings.json'
    if not path.exists():return {}
    settings=json.loads(path.read_text(encoding='utf-8-sig'))
    if not verify_saved(settings):
        raise NetworkNotReady('Amazon 本机网络出口已变化，请重新检测；未回退直连。','AUTO')
    if not settings.get('preproxy_port'):return {}
    scheme='socks5' if settings['preproxy_protocol']=='SOCKS5' else 'http'
    host=settings['preproxy_host']
    if ':' in host:host='['+host+']'
    return {'AMAZON_BROWSER_PROXY':f"{scheme}://{host}:{settings['preproxy_port']}"}
