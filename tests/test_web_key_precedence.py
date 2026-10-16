import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import selection
from selection_automation import secrets


def test_run_key_survives_old_saved_key_but_accepts_later_update(tmp_path, monkeypatch):
    monkeypatch.setattr(selection, 'ROOT', tmp_path)
    path = tmp_path / 'runtime/secrets/deepseek.dpapi'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'old-encrypted-placeholder')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'web-form-key')
    loads = []
    def load(p):
        loads.append(p)
        return {'key': 'newly-saved-key'}
    monkeypatch.setattr(secrets, 'load', load)
    stamp = selection.saved_api_key_stamp()
    assert selection.refresh_saved_api_key(stamp) == stamp
    assert os.environ['DEEPSEEK_API_KEY'] == 'web-form-key'
    assert loads == []
    path.write_bytes(b'new-encrypted-placeholder-longer')
    assert selection.refresh_saved_api_key(stamp) != stamp
    assert os.environ['DEEPSEEK_API_KEY'] == 'newly-saved-key'
    assert loads == [path]
