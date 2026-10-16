"""Discover and recover installed local vision services without running inference."""
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import psutil
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, Request

from .process_lock import ProcessLock


def _json(base, path, payload=None):
    request = Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
    with build_opener(ProxyHandler({})).open(request, timeout=1) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError('Local model metadata is too large')
    return json.loads(data)


def _endpoints():
    result = ['http://127.0.0.1:11434']
    try:
        owners = {p.pid for p in psutil.process_iter(['name'])
                  if (p.info['name'] or '').lower() in {'ollama', 'ollama.exe'}}
        for connection in psutil.net_connections(kind='tcp'):
            if connection.pid in owners and connection.status == psutil.CONN_LISTEN:
                host = '[::1]' if connection.laddr.ip in {'::1', '::'} else '127.0.0.1'
                address = f'http://{host}:{connection.laddr.port}'
                if address not in result:
                    result.append(address)
    except (OSError, psutil.Error):
        pass
    return result


def _installed():
    found = shutil.which('ollama')
    if found:
        return found
    if not os.environ.get('LOCALAPPDATA'):
        return None
    candidate = Path(os.environ['LOCALAPPDATA']) / 'Programs/Ollama/ollama.exe'
    return str(candidate) if candidate.is_file() else None


def _start(root):
    executable = _installed()
    if not executable:
        return False
    folder = Path(root) / 'runtime/local-models'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with ProcessLock(folder / 'recovery.lock'):
            marker = folder / 'start.json'
            if marker.exists() and time.time() - marker.stat().st_mtime < 60:
                return False
            env = {**os.environ, 'OLLAMA_HOST': '127.0.0.1:11434'}
            with (folder / 'ollama.log').open('ab') as log:
                child = subprocess.Popen([executable, 'serve'], env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            marker.write_text(json.dumps({'pid': child.pid, 'started_at': time.time()}), encoding='utf-8')
            return True
    except (OSError, RuntimeError):
        return False


def _tags(base):
    try:
        data = _json(base, '/api/tags')
        return data.get('models') if isinstance(data, dict) and isinstance(data.get('models'), list) else None
    except (OSError, ValueError):
        return None


def _describe(base, row):
    name = row.get('name') or row.get('model')
    if not isinstance(name, str) or not name or len(name) > 200:
        return None
    try:
        info = _json(base, '/api/show', {'model': name})
        capabilities = info.get('capabilities')
        if not isinstance(capabilities, list) or 'vision' not in capabilities or any(
                data.get(key) for data in (row, info) for key in ('remote_host', 'remote_model')):
            return None
        return {'id': name, 'name': name, 'vision': True, 'source': 'Ollama',
                'base_url': base + '/v1', 'protocol': 'chat_completions', 'size': row.get('size', 0)}
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def catalog(root, start=False):
    endpoints = _endpoints()
    services = [(base, rows) for base in endpoints if (rows := _tags(base)) is not None]
    started = False
    if not services and start:
        listening = False
        for base in endpoints:
            address = urlsplit(base)
            try:
                with socket.create_connection((address.hostname, address.port), timeout=.2):
                    listening = True
            except OSError:
                pass
        if not listening:
            started = _start(root)
            deadline = time.monotonic() + (5 if _installed() else 0)
            while time.monotonic() < deadline:
                rows = _tags(endpoints[0])
                if rows is not None:
                    services = [(endpoints[0], rows)]
                    break
                time.sleep(.2)
    candidates = {}
    for base, rows in services:
        for row in rows:
            name = (row.get('name') or row.get('model')) if isinstance(row, dict) else None
            if isinstance(name, str):
                candidates.setdefault((urlsplit(base).port, name), (base, row))
    models = []
    partial = False
    if candidates:
        pool = ThreadPoolExecutor(max_workers=4)
        pending = [pool.submit(_describe, base, row) for base, row in candidates.values()]
        try:
            for future in as_completed(pending, timeout=6):
                if model := future.result():
                    models.append(model)
        except FuturesTimeout:
            partial = True
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    models.sort(key=lambda m: (m['id'].startswith('llamacpp:'), m['size'] if isinstance(m['size'], int) else 0, m['id']))
    message = ('已读取本机实际安装的看图模型；检测不会发送分析请求。' if models else
               '本机模型服务已连接，但没有检测到已安装的看图模型。' if services else
               '本机服务暂未就绪，请检查服务启动日志；其他分析方案不受影响。' if _installed() else
               '未安装可自动启动的 Ollama 服务；可填写自定义本地服务，或选择 API / Agent 分析。')
    return {'models': models, 'service_connected': bool(services), 'started': started,
            'partial': partial, 'message': message}


def repair_provider(root, provider, allow_default=False):
    address = urlsplit(provider['base_url'])
    legacy_default = (address.port == 18181 and address.path.rstrip('/') == '/v1/systemone'
                      and provider['model'] == 'd1-3b' and provider['protocol'] == 'decisions')
    eligible = legacy_default or address.port == 11434 or allow_default
    data = catalog(root, start=eligible)
    matching = [model for model in data['models'] if model['id'] == provider['model']]
    selected = (matching[0] if matching else data['models'][0] if data['models'] and (legacy_default or allow_default) else None)
    data['provider'] = None
    if eligible and selected:
        data['provider'] = {**provider, 'model': selected['id'],
                            **{key: selected[key] for key in ('base_url', 'protocol')}}
        data['message'] = f"已自动连接 Ollama，看图模型：{selected['id']}。点击开始分析后使用；检测未发送模型消息。"
    elif data['models']:
        data['message'] = '已发现其他可用的本地看图模型；保留你指定的服务和模型，可在下拉框中选择。'
    return data
