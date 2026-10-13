"""Shared TEMU concurrency bounds; old configurations retain four workers."""
DEFAULT_WORKER_COUNT = 4
MAX_WORKER_COUNT = 32


def worker_count(value=DEFAULT_WORKER_COUNT):
    if type(value) is not int or not 1 <= value <= MAX_WORKER_COUNT:
        raise ValueError(f'TEMU 路数必须为 1–{MAX_WORKER_COUNT} 的整数')
    return value


def category_worker_count(category):
    return worker_count(category.get('temu', {}).get('worker_count', DEFAULT_WORKER_COUNT))
