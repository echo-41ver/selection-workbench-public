"""Read-only local model inventory. Never load weights or execute model files."""
import ctypes
from collections import deque
import json
import os
from pathlib import Path
import threading
import time
import uuid

import psutil


_LOCK = threading.RLock()
_SCANS = {}
_LIMIT = 2000
_SKIP = {'$recycle.bin', 'system volume information'}


def vision_capability(info):
    """Use explicit metadata, never infer image support from a model name."""
    if not isinstance(info, dict):
        return None
    for key in ('vision', 'supports_vision', 'has_vision_adapter'):
        if type(info.get(key)) is bool:
            return info[key]
    for key in ('capabilities', 'modalities'):
        value = info.get(key)
        if isinstance(value, dict) and type(value.get('vision')) is bool:
            return value['vision']
        if isinstance(value, list) and any(x in value for x in ('vision', 'image')):
            return True
    inputs = info.get('input_modalities')
    if isinstance(inputs, list):
        return 'image' in inputs or 'vision' in inputs
    return None


def service_endpoints():
    """All locally listening TCP ports, including nonstandard model ports."""
    result = ['http://127.0.0.1:11434', 'http://127.0.0.1:1234']
    try:
        for connection in psutil.net_connections(kind='tcp'):
            if connection.status != psutil.CONN_LISTEN:
                continue
            ip = connection.laddr.ip
            if ip not in {'0.0.0.0', '::', '::1'} and not ip.startswith('127.'):
                continue
            host = '[::1]' if ':' in ip else ip if ip.startswith('127.') else '127.0.0.1'
            base = f'http://{host}:{connection.laddr.port}'
            if base not in result:
                result.append(base)
    except (OSError, psutil.Error):
        pass
    return result


def scan_roots():
    """Prioritize known caches, then walk every local fixed disk."""
    home = Path.home()
    candidates = [home / '.ollama/models', home / '.lmstudio/models',
                  home / '.cache/lm-studio/models', home / '.cache/huggingface/hub']
    for key in ('OLLAMA_MODELS', 'LMSTUDIO_MODELS', 'HF_HUB_CACHE',
                'HUGGINGFACE_HUB_CACHE', 'TRANSFORMERS_CACHE'):
        if os.environ.get(key):
            candidates.insert(0, Path(os.environ[key]))
    if os.environ.get('HF_HOME'):
        candidates.insert(0, Path(os.environ['HF_HOME']) / 'hub')
    # Only inspect known path arguments; do not expose process command lines.
    try:
        for process in psutil.process_iter(['cmdline', 'cwd']):
            args = process.info.get('cmdline') or []
            for index, arg in enumerate(args):
                path = None
                if arg in ('--model', '-m', '--model-path', '--model-dir') and index + 1 < len(args):
                    path = args[index + 1]
                elif arg.startswith(('--model=', '--model-path=', '--model-dir=')):
                    path = arg.split('=', 1)[1]
                if path:
                    candidate = Path(path)
                    if not candidate.is_absolute():
                        candidate = Path(process.info.get('cwd') or '.') / candidate
                    if candidate.exists():
                        candidates.insert(0, candidate if candidate.is_dir() else candidate.parent)
    except (OSError, psutil.Error):
        pass
    for disk in psutil.disk_partitions(all=False):
        if os.name == 'nt':
            if ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(str(disk.mountpoint))) != 3:
                continue
        elif disk.fstype in {'nfs', 'nfs4', 'cifs', 'smbfs'}:
            continue
        candidates.append(Path(disk.mountpoint))
    result = []
    for path in candidates:
        try:
            if os.name == 'nt' and (not path.is_absolute() or
                    ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(path.anchor)) != 3):
                continue
            if path.is_symlink() or os.name == 'nt' and path.stat().st_file_attributes & 0x400:
                continue
            if path.is_dir() and not any(_path_key(path) == _path_key(old) for old in result):
                result.append(path)
        except OSError:
            pass
    return result


def _path_key(path):
    return os.path.normcase(os.path.abspath(path))


def _read_json(path):
    try:
        with Path(path).open('rb') as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
        return json.loads(data) if len(data) <= 2 * 1024 * 1024 else None
    except (OSError, ValueError):
        return None


def inspect_directory(path, files, cancel=None):
    """Return metadata evidence; a file discovery is never a callable service."""
    result = []
    names = {name.lower(): name for name in files}
    if 'config.json' in names and any(
            name.endswith('.safetensors') or name in {'pytorch_model.bin', 'model.bin'}
            or name.startswith('pytorch_model-') and name.endswith('.bin') for name in names):
        info = _read_json(path / names['config.json'])
        if isinstance(info, dict) and (info.get('model_type') or info.get('architectures')):
            vision = True if isinstance(info.get('vision_config'), dict) else vision_capability(info)
            result.append({'name': path.name, 'path': str(path), 'source': 'Hugging Face / Transformers',
                           'vision': vision, 'status': 'files_only',
                           'reason': '发现模型配置与权重；尚未验证权重完整性及可调用服务。'})
    parts = list(path.parts)
    manifest_offset = next((i for i, part in enumerate(parts) if part.lower() == 'manifests'), None)
    store = Path(*parts[:manifest_offset]) if manifest_offset is not None else None
    # Windows also contains a very large Manifests directory. Read only stores
    # with Ollama's sibling blobs layout, not unrelated application manifests.
    in_manifests = store is not None and (store / 'blobs').is_dir()
    for lower, name in names.items():
        if cancel is not None and cancel.is_set():
            break
        target = path / name
        if lower.endswith('.gguf') and not lower.startswith(('mmproj', 'projector')):
            try:
                with target.open('rb') as stream:
                    if stream.read(4) != b'GGUF':
                        continue
            except OSError:
                continue
            result.append({'name': name, 'path': str(target), 'source': 'GGUF', 'vision': None,
                           'status': 'files_only', 'reason': '发现 GGUF 权重；看图能力及服务待确认。'})
        elif in_manifests:
            info = _read_json(target)
            if not isinstance(info, dict) or not isinstance(info.get('layers'), list):
                continue
            layers = [row for row in info['layers'] if isinstance(row, dict)]
            if not any(row.get('mediaType') == 'application/vnd.ollama.image.model' for row in layers):
                continue
            target_parts = list(target.parts)
            digests = [row.get('digest', '') for row in layers]
            complete = bool(digests) and all(
                digest.startswith('sha256:') and len(digest) == 71
                and all(c in '0123456789abcdef' for c in digest[7:])
                and (store / 'blobs' / digest.replace(':', '-')).is_file() for digest in digests)
            result.append({'name': '/'.join(target_parts[manifest_offset + 1:]), 'path': str(target), 'source': 'Ollama 文件',
                           'vision': True if any('projector' in str(row.get('mediaType')) for row in layers) else None,
                           'status': 'files_only' if complete else 'incomplete_files',
                           'reason': '发现模型清单与权重文件；需连接模型服务。' if complete else '发现模型清单，部分权重缺失。'})
    return result


def _snapshot(state):
    return {key: value for key, value in state.items() if key != '_cancel'} | {
        'models': [dict(row) for row in state['models']],
        'elapsed_seconds': round(time.time() - state['started_at'], 1) if state['running'] else state.get('elapsed_seconds', 0)}


def scan_status(root):
    with _LOCK:
        state = _SCANS.get(_path_key(root))
        return _snapshot(state) if state else {'running': False, 'status': 'not_started', 'models': []}


def start_scan(root, restart=False):
    key = _path_key(root)
    with _LOCK:
        existing = _SCANS.get(key)
        if existing and (existing['running'] or not restart):
            return _snapshot(existing)
        state = {'scan_id': uuid.uuid4().hex, 'running': True, 'status': 'scanning',
                 'started_at': time.time(), 'directories': 0, 'files': 0, 'skipped': 0,
                 'truncated': False, 'roots': [], 'models': [], 'services': {}, 'current_directory': '',
                 'message': '正在读取本机服务，并扫描本地固定磁盘上的模型文件。', '_cancel': threading.Event()}
        _SCANS[key] = state
        threading.Thread(target=_run_scan, args=(root, state), name='local-model-inventory', daemon=True).start()
        return _snapshot(state)


def cancel_scan(root):
    with _LOCK:
        state = _SCANS.get(_path_key(root))
        if state and state['running']:
            state['_cancel'].set()
            state['message'] = '正在停止扫描，已发现的结果保留。'
        return scan_status(root)


def _run_scan(root, state):
    try:
        from .local_model_service import catalog
        services = catalog(root, full=True, cancel=state['_cancel'])
        roots = scan_roots()
        with _LOCK:
            state['services'] = services
            state['roots'] = [str(path) for path in roots]
        covered, found = set(), set()
        root_keys = {_path_key(path) for path in roots}
        # Breadth-first traversal visits every disk early; a large C: directory
        # tree must not postpone D: model discovery until C: is exhausted.
        pending = deque(roots)
        while pending and not state['_cancel'].is_set():
            path = pending.popleft()
            key = _path_key(path)
            if key in root_keys:
                if key in covered:
                    continue
                covered.add(key)
            with _LOCK:
                state['current_directory'] = str(path)
            try:
                with os.scandir(path) as entries:
                    files, folders = [], []
                    for entry in entries:
                        if state['_cancel'].is_set():
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                # Do not traverse junctions, mounts, or symlink cycles.
                                if entry.name.lower() in _SKIP or entry.is_symlink() or (
                                        os.name == 'nt' and entry.stat(follow_symlinks=False).st_file_attributes & 0x400):
                                    with _LOCK:
                                        state['skipped'] += 1
                                else:
                                    folders.append(Path(entry.path))
                            elif entry.is_file():
                                files.append(entry.name)
                        except OSError:
                            with _LOCK:
                                state['skipped'] += 1
                records = inspect_directory(path, files, state['_cancel'])
                with _LOCK:
                    state['directories'] += 1
                    state['files'] += len(files)
                    for record in records:
                        identity = _path_key(record['path'])
                        if identity not in found:
                            found.add(identity)
                            if len(state['models']) < _LIMIT:
                                state['models'].append(record)
                            else:
                                state['truncated'] = True
                pending.extend(folders)
                if state['directories'] % 100 == 0:
                    state['_cancel'].wait(.01)
            except OSError:
                with _LOCK:
                    state['skipped'] += 1
        with _LOCK:
            state['status'] = 'cancelled' if state['_cancel'].is_set() else 'complete'
            state['message'] = ('扫描已停止，保留已发现结果。' if state['_cancel'].is_set() else
                                '已扫描可访问的本地固定磁盘；权限受限目录、回收站、系统卷目录和链接目录已跳过。')
    except Exception as error:
        with _LOCK:
            state['status'] = 'failed'
            state['message'] = f'扫描未完成（{type(error).__name__}），已发现结果保留。'
    finally:
        with _LOCK:
            state['running'] = False
            state['elapsed_seconds'] = round(time.time() - state['started_at'], 1)
