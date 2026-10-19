"""Transport doubles for offline inference tests, including the connectivity probe."""
from contextlib import contextmanager


class OfflineTransport:
    def __init__(self, **kwargs):
        pass

    @contextmanager
    def stream(self, method, url, **kwargs):
        assert method == 'GET', 'Inference must go through the fake provider'
        yield self

    def close(self):
        self.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def grid(batch):
    return ('data:image/png;base64,fixture', [
        {'id': f'C{i:02}', 'goods_id': row['goods_id']} for i, row in enumerate(batch)
    ])
