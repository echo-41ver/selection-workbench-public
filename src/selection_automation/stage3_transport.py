"""Repair transport selection before inference; never retry an inference POST."""
from contextlib import contextmanager
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
def create_transport(provider, *, timeout, limits):
    from openai import DefaultHttpxClient
    from httpx2 import HTTPError, Timeout

    options = transport_options(provider)
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
            reachable(client)
        except (HTTPError, OSError):
            client.close()
            if 'proxy' not in options:
                raise ConnectionError('API 服务连接失败；尚未发送分析，请检查服务地址和网络。') from None
            client = DefaultHttpxClient(trust_env=False, timeout=timeout, limits=limits)
            try:
                reachable(client)
            except (HTTPError, OSError):
                raise ConnectionError('系统代理和直连均无法连接 API 服务；尚未发送分析，请检查代理或网络。') from None
        yield client
    finally:
        client.close()
