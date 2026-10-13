"""Reusable verified image connections; no automatic or hidden retries."""
from threading import Lock

MAX_IMAGE_CONNECTIONS = 16


class ImageTransport:
    def __init__(self, timeout=30):
        self.timeout=timeout
        self._clients={}
        self._lock=Lock()

    def _client(self, proxy):
        with self._lock:
            if proxy not in self._clients:
                try:
                    import httpx2 as httpx
                except ImportError:
                    import httpx
                self._clients[proxy]=httpx.Client(
                    verify=True,trust_env=False,proxy=proxy,timeout=self.timeout,
                    follow_redirects=True,headers={'User-Agent':'Mozilla/5.0'},
                    limits=httpx.Limits(max_connections=MAX_IMAGE_CONNECTIONS,
                        max_keepalive_connections=MAX_IMAGE_CONNECTIONS,keepalive_expiry=30))
            return self._clients[proxy]

    def read(self,url,proxy=None):
        response=self._client(proxy).get(url)
        response.raise_for_status()
        return response.content

    def close(self):
        with self._lock:
            for client in self._clients.values():client.close()
            self._clients.clear()
