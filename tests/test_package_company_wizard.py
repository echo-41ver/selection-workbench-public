"""The approved wizard exports only valid authorization for the unchanged account."""
import copy
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import package_company_wizard as wizard


@pytest.fixture
def run_wizard(tmp_path, monkeypatch):
    def run(server=None, *, configured=None, edit_account=None, selected_company=None):
        config = {'adspower_api_key': 'TEST_ONLY_ADSPOWER',
                  'proxy': {'host': 'proxy.example', 'port': 5959,
                            'username': 'TEST_ONLY_session-fixture', 'password': 'TEST_ONLY_PROXY_PASSWORD'},
                  'deepseek': {'key': 'TEST_ONLY_MODEL_KEY'},
                  'geekbi': {'username': 'fixture-account', 'password': 'TEST_ONLY_ACCOUNT_PASSWORD',
                             **(configured or {})}}
        root = tmp_path / 'install'
        secret = root / 'runtime/secrets/company.dpapi'
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.touch()
        if server is not None:
            auth = root / 'runtime/market-auth/agent-auth.json'
            auth.parent.mkdir(parents=True, exist_ok=True)
            auth.write_text(json.dumps({'servers': {'https://openapi.geekbi.com': server,
                'https://unrelated.example': {'accessToken': 'MUST_NOT_COPY_OTHER_SERVER',
                                            'accessTokenExpiresAt': 9999}}}), encoding='utf-8')
        company_json = None
        if selected_company is not None:
            company_json = tmp_path / 'selected-company.json'
            company_json.write_text(json.dumps({**config, 'geekbi': selected_company}), encoding='utf-8')
        commands, labels, prompts, captured = [], {}, [], []
        current_label = ['']

        class Widget:
            def grid(self, **kwargs): pass

        class Window:
            def withdraw(self): pass
            def deiconify(self): pass
            def title(self, text): pass
            def destroy(self): pass
            def mainloop(self):
                if edit_account is not None:
                    labels['GeekBI 账号（可选）'].value = edit_account
                commands[0]()

        class Entry(Widget):
            def __init__(self, *args, **kwargs): labels[current_label[0]] = self
            def insert(self, index, value): self.value = value
            def get(self): return self.value

        def label(*args, **kwargs):
            current_label[0] = kwargs['text']
            return Widget()

        def button(*args, **kwargs):
            commands.append(kwargs['command'])
            return Widget()

        def confirm(title, message, **kwargs):
            prompts.append(message)
            return True

        fake = ModuleType('tkinter')
        fake.Tk, fake.Label, fake.Entry, fake.Button = Window, label, Entry, button
        fake.messagebox = SimpleNamespace(askyesno=confirm, showinfo=lambda *a, **k: None,
            showerror=lambda *a, **k: pytest.fail('wizard rejected fixture configuration'))
        fake.filedialog = SimpleNamespace()
        monkeypatch.setitem(sys.modules, 'tkinter', fake)
        monkeypatch.setattr('selection_automation.secrets.load', lambda path: copy.deepcopy(config))
        monkeypatch.setattr(wizard.time, 'time', lambda: 1000)
        monkeypatch.setattr(wizard, 'write_bundle',
            lambda base, output, payload: captured.append(copy.deepcopy(payload)) or Path(output))
        wizard.wizard(root, tmp_path / 'base.zip', tmp_path / 'out.zip', company_json)
        assert len(captured) == 1
        return captured[0], prompts
    return run


def test_approved_wizard_carries_only_current_official_token_and_expiry(run_wizard, capsys):
    payload, prompts = run_wizard({'accessToken': 'TEST_ONLY_INSTALLATION_TOKEN', 'accessTokenExpiresAt': 4600,
        'pending': {'deviceCode': 'MUST_NOT_COPY_PENDING'}, 'browserSession': 'MUST_NOT_COPY_SESSION'})
    assert payload['geekbi'] == {'username': 'fixture-account', 'password': 'TEST_ONLY_ACCOUNT_PASSWORD',
                               'access_token': 'TEST_ONLY_INSTALLATION_TOKEN', 'expires_at': 4600}
    assert 'MUST_NOT_COPY' not in json.dumps(payload)
    assert len(prompts) == 2 and all('接口授权凭证及有效期' in prompt for prompt in prompts)
    output = capsys.readouterr()
    assert 'TEST_ONLY_INSTALLATION_TOKEN' not in output.out + output.err + ''.join(prompts)


@pytest.mark.parametrize('server', [None, {}, {'accessToken': 'TEST_ONLY_EXPIRED', 'accessTokenExpiresAt': 999},
    {'accessToken': 'TEST_ONLY_INVALID', 'accessTokenExpiresAt': True},
    {'accessToken': 'TEST_ONLY_INVALID\n', 'accessTokenExpiresAt': 4600}])
def test_absent_expired_or_invalid_installation_token_is_not_exported(run_wizard, server):
    payload, _ = run_wizard(server)
    assert set(payload['geekbi']) == {'username', 'password'}


def test_valid_company_json_authorization_is_preserved_instead_of_installation_token(run_wizard):
    payload, _ = run_wizard({'accessToken': 'TEST_ONLY_OTHER_TOKEN', 'accessTokenExpiresAt': 4600},
        selected_company={'username': 'fixture-account', 'password': 'TEST_ONLY_SELECTED_PASSWORD',
                          'access_token': 'TEST_ONLY_JSON_TOKEN', 'expires_at': 4700})
    assert payload['geekbi']['access_token'] == 'TEST_ONLY_JSON_TOKEN'
    assert payload['geekbi']['expires_at'] == 4700


def test_editing_account_drops_both_preexisting_token_sources(run_wizard):
    payload, _ = run_wizard({'accessToken': 'TEST_ONLY_INSTALLATION_TOKEN', 'accessTokenExpiresAt': 4600},
        configured={'access_token': 'TEST_ONLY_JSON_TOKEN', 'expires_at': 4700}, edit_account='different-account')
    assert payload['geekbi']['username'] == 'different-account'
    assert set(payload['geekbi']) == {'username', 'password'}


def test_selected_company_with_another_account_does_not_inherit_installation_token(run_wizard):
    payload, _ = run_wizard({'accessToken': 'TEST_ONLY_INSTALLATION_TOKEN', 'accessTokenExpiresAt': 4600},
        selected_company={'username': 'another-company-account', 'password': 'TEST_ONLY_SELECTED_PASSWORD'})
    assert set(payload['geekbi']) == {'username', 'password'}
