import json
import time
from types import SimpleNamespace

import pytest

from selection_automation import local_agents


@pytest.mark.parametrize('changed', [None, 'model', 'version', 'login', 'failed_probe'])
def test_verified_cli_capability_survives_time_but_not_identity_or_auth_changes(tmp_path, monkeypatch, changed):
    exe = tmp_path / 'codex.exe'; exe.touch()
    monkeypatch.setattr(local_agents, 'find_executable', lambda root, agent: str(exe))
    def run(prefix, args, root):
        return SimpleNamespace(returncode=1 if changed == 'login' and args == ['login', 'status'] else 0,
                               stdout='codex-cli 0.162.0', stderr='')
    monkeypatch.setattr(local_agents, '_run', run)
    receipt = local_agents.connection_receipt(tmp_path, 'codex', 'selected-model')
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({'executable': str(exe), 'version': 'old' if changed == 'version' else '0.162.0',
        'tested_at': time.time() - 86400, 'connected': changed != 'failed_probe', 'probe_revision': 2,
        'selected_model': 'other' if changed == 'model' else 'selected-model'}))
    row = local_agents._probe_cli(tmp_path, 'codex', 'selected-model')
    assert row['ready'] is (changed is None)


@pytest.mark.parametrize('blocked', [False, True])
def test_desktop_capability_still_requires_current_native_transport(tmp_path, monkeypatch, blocked):
    from selection_automation import desktop_agents
    exe = tmp_path / 'app.exe'; exe.touch()
    monkeypatch.setattr(desktop_agents, 'probe', lambda root, agent: {
        'id': agent, 'installed': True, 'executable': str(exe), 'desktop_path': str(exe), 'version': None,
        'transport_available': not blocked, 'authenticated': None, 'requires_enable': blocked,
        'ready': False, 'connected': False, 'status': '待连接验证'})
    receipt = local_agents.connection_receipt(tmp_path, 'doubao', 'selected-model')
    receipt.parent.mkdir(parents=True)
    stamp = exe.stat()
    receipt.write_text(json.dumps({'executable': str(exe), 'version': None, 'tested_at': time.time() - 86400,
        'installation_stamp': f'{stamp.st_mtime_ns}:{stamp.st_size}', 'connected': True,
        'probe_revision': 2, 'selected_model': 'selected-model'}))
    assert local_agents._probe(tmp_path, 'doubao', 'selected-model')['ready'] is (not blocked)
