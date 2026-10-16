"""Describe transport evidence without claiming target access or changing networks."""
DEFAULT_CANDIDATES = []
MODES = {'AUTO', 'SOCKS5_REQUIRED', 'DIRECT'}

def mode(company):
    value = company.get('preproxy_mode', 'AUTO')
    if not isinstance(value,str) or value not in MODES:
        raise ValueError('Invalid preproxy_mode; use AUTO, SOCKS5_REQUIRED or DIRECT')
    return value

class NetworkNotReady(RuntimeError):
    def __init__(self, reason, requested):
        self.diagnostic = {'requested_mode':requested, 'selected_mode':'NONE',
            'probe_level':'NOT_READY', 'target_access':'UNVERIFIED', 'message':reason}
        super().__init__(reason)

def describe(settings):
    if not settings:
        return {'selected_mode':'UNKNOWN', 'probe_level':'NOT_CONFIGURED',
                'target_access':'UNVERIFIED', 'message':'网络出口尚未配置；目标访问尚未验证。'}
    port=settings.get('preproxy_port',0)
    protocol=settings.get('preproxy_protocol','SOCKS5' if port else 'DIRECT')
    selected=protocol if port else 'SYSTEM_ROUTE' if protocol=='SYSTEM_ROUTE' else 'DIRECT'
    message={'SOCKS5':'已检测并选用本机 SOCKS5 出口','HTTP':'已检测并选用本机 HTTP 代理出口',
             'SYSTEM_ROUTE':'仅检测到 TUN VPN，沿用现有系统路由','DIRECT':'已明确选择直连代理网关'}[selected]
    return {'requested_mode':settings.get('preproxy_mode','AUTO'),'selected_mode':selected,
            'probe_level':settings.get('network_probe_level','SAVED_CONFIGURATION_ONLY'),
            'target_access':'UNVERIFIED','message':message+'；目标访问尚未验证。'}

def select_transport(company, socks_probe=None, tcp_probe=None, *, local_discovery=None, saved=None, active=False):
    requested=mode(company)
    if active:raise NetworkNotReady('当前任务或人工验证仍在运行，不能切换出口；先安全停止再准备。',requested)
    if requested=='DIRECT':
        settings={'preproxy_host':'127.0.0.1','preproxy_port':0,'preproxy_protocol':'DIRECT',
                  'preproxy_mode':requested,'network_probe_level':'EXPLICIT_DIRECT_NOT_PROBED'}
        return settings,describe(settings)
    from . import local_proxy
    observation=(local_discovery or local_proxy.discover)()
    if observation.get('status')=='TUN_ONLY' and requested=='AUTO' and not company.get('local_proxy_endpoint'):
        if saved and saved.get('preproxy_port'):
            raise NetworkNotReady('已保存的本机代理出口不可用，不能自动改成系统/TUN路由；请明确选择。',requested)
        settings={'preproxy_host':'127.0.0.1','preproxy_port':0,'preproxy_protocol':'SYSTEM_ROUTE',
                  'preproxy_mode':requested,'network_probe_level':'EXISTING_SYSTEM_ROUTE_ONLY'}
    else:
        preferred=company.get('local_proxy_endpoint')
        if not preferred and saved and saved.get('preproxy_port'):
            preferred={'host':saved['preproxy_host'],'port':saved['preproxy_port'],'protocol':saved.get('preproxy_protocol','SOCKS5')}
        try:settings=local_proxy.choose(observation,requested,preferred)
        except NetworkNotReady as error:
            error.diagnostic['candidates']=observation.get('candidates',[])
            error.diagnostic['warnings']=observation.get('warnings',[])
            raise
        if saved and saved.get('local_proxy_owner') and not company.get('local_proxy_endpoint'):
            previous=saved['local_proxy_owner'];current=settings['local_proxy_owner']
            if previous.get('pid')!=current['pid'] or abs(previous.get('created',0)-current['created'])>.01:
                raise NetworkNotReady('原出口进程已变化；请明确选择检测到的新出口，未自动替换。',requested)
    result=describe(settings)
    result['candidates']=observation.get('candidates',[])
    result['warnings']=observation.get('warnings',[])
    return settings,result


def validate_saved_route(company, settings):
    requested = mode(company)
    if requested == 'SOCKS5_REQUIRED' and (not settings.get('preproxy_port') or settings.get('preproxy_protocol','SOCKS5')!='SOCKS5'):
        raise NetworkNotReady('配置要求 SOCKS5，但已有断点使用直连；需确认接收端出口后更新网络配置。', requested)
    if requested == 'DIRECT' and settings.get('preproxy_port'):
        raise NetworkNotReady('配置要求直连，但已有断点使用 SOCKS5；未自动切换已有链路。', requested)
    return describe(settings)
