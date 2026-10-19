"""Bounded TLS connection failover, before any model request can be sent."""
import http.client
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def open_model_url(request, timeout=600, notify=None):
    if urlsplit(request.full_url).scheme!='https':
        return urllib.request.urlopen(request,timeout=timeout)
    host=urlsplit(request.full_url).hostname or ''
    proxies=urllib.request.getproxies()
    proxy=proxies.get('https') or proxies.get('all')
    proxy=proxy if proxy and not urllib.request.proxy_bypass(host) else None
    routes=[('system_proxy',{'https':proxy}),('direct',{})] if proxy else [('direct',{}),('direct',{})]
    url=request.full_url;headers=dict(request.header_items());data=request.data;method=request.get_method()
    def report(**fields):
        if notify:notify(**fields)
    for index,(route,configuration) in enumerate(routes,1):
        state={'connect_failed':False,'connected':False}
        class Connection(http.client.HTTPSConnection):
            def connect(self):
                original=self.timeout
                self.timeout=min(12,original)
                try:
                    super().connect()
                    state['connected']=True
                    self.sock.settimeout(original)
                except OSError:
                    state['connect_failed']=not state['connected']
                    self.close()
                    raise
                finally:self.timeout=original
        class Handler(urllib.request.HTTPSHandler):
            def https_open(self,req):
                return self.do_open(Connection,req,context=self._context)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler(configuration),Handler())
        report(model_network={'route':route,'attempt':index,'phase':'connecting'})
        try:
            # ProxyHandler mutates Request.host and its tunnel fields. Each
            # route must get a fresh request or direct still targets the proxy.
            attempt=urllib.request.Request(url,data=data,headers=headers,method=method)
            response=opener.open(attempt,timeout=timeout)
            report(model_network={'route':route,'attempt':index,'phase':'response_received'})
            return response
        except (urllib.error.URLError,OSError):
            # A failed connect() precedes HTTP headers and body in http.client.
            # Any error after TLS succeeds can be a billed request: never replay.
            if not state['connect_failed'] or state['connected']:raise
            if index==len(routes):raise
            report(model_network={'route':route,'attempt':index,'phase':'connect_failed','request_sent':False},
                   message='模型连接尚未建立，正在自动切换网络连接；本次请求尚未发送。')
