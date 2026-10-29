"""Repair transport selection before inference; never retry an inference POST."""
from contextlib import contextmanager
import json
from urllib.request import getproxies, proxy_bypass
from urllib.parse import urlsplit


def transport_options(provider):
    host = urlsplit(provider['base_url']).hostname or ''
    if provider['mode'] == 'local_model' or host.lower() in {'localhost', '127.0.0.1', '::1'}:
        return {'trust_env': False}
    proxies = getproxies()
    proxy = proxies.get(urlsplit(provider['base_url']).scheme) or proxies.get('all')
    if proxy and not proxy_bypass(host):
        return {'trust_env': False, 'proxy': proxy}
    return {'trust_env': False}


@contextmanager
def create_transport(provider, *, timeout, limits, route_file=None, route_scope=None):
    from openai import DefaultHttpxClient
    from httpx2 import HTTPError, Limits, Timeout

    options = transport_options(provider)
    identity = {'scope': route_scope, 'mode': provider['mode'], 'base_url': provider['base_url']}
    saved = json.loads(route_file.read_text('utf-8')) if route_file is not None and route_file.exists() else None
    verified = route_scope is not None and saved is not None and saved['identity'] == identity
    if verified and saved['route'] == 'direct':
        options = {'trust_env': False}
    selected_route = 'system' if 'proxy' in options else 'direct'
    if provider['mode'] == 'local_model':
        from .local_d1 import native_endpoint
        if native_endpoint(provider):
            # The Windows D1 server can reset a previously used HTTP connection.
            limits = Limits(max_connections=limits.max_connections, max_keepalive_connections=0)
    client = DefaultHttpxClient(**options, timeout=timeout, limits=limits)
    if provider['mode'] == 'local_model':
        with client:
            yield client
        return
    # Read headers only, with no API key, body, redirects or model invocation.
    # Any HTTP status proves connectivity; 403/429 must not trigger route changes.
    def reachable(candidate):
        with candidate.stream('GET', provider['base_url'], timeout=Timeout(4, connect=2),
                              follow_redirects=False):
            return True
    try:
        try:
            if not verified:
                reachable(client)
        except (HTTPError, OSError):
            client.close()
            if 'proxy' not in options:
                raise ConnectionError('API 服务连接失败；尚未发送分析，请检查服务地址和网络。') from None
            client = DefaultHttpxClient(trust_env=False, timeout=timeout, limits=limits)
            selected_route = 'direct'
            try:
                reachable(client)
            except (HTTPError, OSError):
                raise ConnectionError('系统代理和直连均无法连接 API 服务；尚未发送分析，请检查代理或网络。') from None
        if route_file is not None and route_scope is not None and not verified:
            route_file.write_text(json.dumps({'identity': identity, 'route': selected_route}), encoding='utf-8')
        yield client
    finally:
        client.close()
