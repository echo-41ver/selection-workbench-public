import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import collect_market_evidence as market


@pytest.mark.parametrize('force', [False, True])
def test_only_explicit_market_retry_resets_company_login_block(tmp_path, monkeypatch, force):
    import watch_temu_union

    base = 'https://openapi.geekbi.com'
    server = {'companyLoginBlocked': 'CAPTCHA_REQUIRED',
              'pending': {'companyLogin': {'status': 'CAPTCHA_REQUIRED'}}}
    state = {'servers': {base: server}}
    auth = SimpleNamespace(server_key=base, state_path=tmp_path / 'auth.json')
    auth._mutate = lambda mutation: mutation(state)[1]
    provider = SimpleNamespace(client=SimpleNamespace(auth=auth))
    monkeypatch.setattr(market, 'ROOT', tmp_path)
    monkeypatch.setattr(market, 'Provider', lambda **kwargs: provider)
    monkeypatch.setattr(watch_temu_union, 'publish_once', lambda *args: None)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    observed = []

    def collect(category, current, progress, **kwargs):
        assert current is provider
        observed.append('companyLoginBlocked' in server)
        assert ('companyLogin' in server['pending']) is (not force)
        return {'status': 'COMPLETE', 'records': [], 'pages': []}

    monkeypatch.setattr(market, 'collect', collect)
    category = tmp_path / 'category.json'
    category.write_text(json.dumps({'category_key': 'fixture'}), encoding='utf8')
    assert market.run(category, force=force)['status'] == 'COMPLETE'
    assert observed == [not force]
