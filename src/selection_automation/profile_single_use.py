"""A profile may be opened once; reconnecting an actually running process is safe."""
import json
import re
import time
from pathlib import Path

import psutil
from .adspower_diagnostics import AdsPowerAPIError


def running_profile(profile_id):
    for process in psutil.process_iter(['name', 'cmdline']):
        try:
            if 'flowerbrowser' not in (process.info['name'] or '').lower():
                continue
            args = process.info['cmdline'] or []
            for index, arg in enumerate(args):
                value = (arg.split('=', 1)[1] if arg.startswith('--user-data-dir=') else
                         args[index + 1] if arg == '--user-data-dir' and index + 1 < len(args) else None)
                if value:
                    folder = value.strip('"').replace('\\', '/').rsplit('/', 1)[-1]
                    if folder == profile_id or folder.startswith(profile_id + '_'):
                        return True
        except (psutil.Error, OSError):
            continue
    return False


def reserve_start(root, profile_id, active, process_running=running_profile):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', profile_id):
        raise RuntimeError('Invalid browser profile identifier')
    directory = Path(root) / 'runtime/profile-single-use'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{profile_id}.json'
    try:
        with path.open('x', encoding='utf-8') as output:
            json.dump({'profile_id': profile_id, 'start_reserved_at': time.time()}, output)
        return
    except FileExistsError:
        pass
    if process_running(profile_id):
        if active().get('data', {}).get('status') == 'Active':
            return
        raise AdsPowerAPIError('/api/v1/browser/start','STARTUP_PENDING',
            'browser process is running but startup is not ready; retain the existing profile',retryable=True)
    raise RuntimeError('环境已使用；关闭后禁止再次打开，必须创建新环境和新session')


def closed_used_profile(root, profile_id, process_running=running_profile):
    return bool(profile_id and
        (Path(root) / 'runtime/profile-single-use' / f'{profile_id}.json').exists()
        and not process_running(profile_id))
