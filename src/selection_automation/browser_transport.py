"""Shared local VPN route for AdsPower and Amazon, without residential credentials."""
import json
import os
from pathlib import Path
from .local_proxy_prepare import verify_saved, refresh_saved_owner
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


def amazon_environment(root, *, mode=None):
    """Configure only the Amazon child browser; shared proxy settings stay intact.

    DIRECT bypasses Chromium's application/system proxy. Operating-system routes
    (including a VPN/TUN, when present) still apply. This does not prove egress.
    """
    configured_mode = mode if mode is not None else os.environ.get('AMAZON_BROWSER_ROUTE_MODE')
    selected = str(configured_mode or 'AUTO').strip().upper()
    if selected not in {'AUTO', 'DIRECT'}:
        raise ValueError('Amazon browser route must be AUTO or DIRECT')
    environment = {'AMAZON_BROWSER_ROUTE_MODE': selected} if configured_mode is not None else {}
    if selected == 'DIRECT':
        # An empty value clears an inherited Amazon proxy in the child only.
        # The collector also passes --no-proxy-server to bypass WinINET.
        return {**environment, 'AMAZON_BROWSER_PROXY': ''}
    path=Path(root)/'runtime/secrets/proxyshare.settings.json'
    if not path.exists():return environment
    settings=json.loads(path.read_text(encoding='utf-8-sig'))
    if not verify_saved(settings):
        settings=refresh_saved_owner(root,settings)
        if not verify_saved(settings):
            raise NetworkNotReady('Amazon 本机网络出口已变化，请重新检测；未回退直连。','AUTO')
    if not settings.get('preproxy_port'):return environment
    scheme='socks5' if settings['preproxy_protocol']=='SOCKS5' else 'http'
    host=settings['preproxy_host']
    if ':' in host:host='['+host+']'
    return {**environment, 'AMAZON_BROWSER_PROXY':f"{scheme}://{host}:{settings['preproxy_port']}"}
