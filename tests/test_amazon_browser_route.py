import json
import os

import pytest

from selection_automation import browser_transport as transport
from selection_automation import stage3_transport


def saved_settings(tmp_path, **values):
    path = tmp_path / 'runtime/secrets/proxyshare.settings.json'
    path.parent.mkdir(parents=True)
    settings = {'proxy_architecture': 'LOCAL_CHAIN_PROXYSHARE',
                'preproxy_protocol': 'HTTP', 'preproxy_port': 7890,
                'preproxy_host': '127.0.0.1', **values}
    path.write_text(json.dumps(settings), encoding='utf-8')
    return path, settings


def test_direct_clears_only_child_amazon_proxy_without_reading_shared_settings(tmp_path, monkeypatch):
    path, _ = saved_settings(tmp_path)
    before = path.read_bytes()
    monkeypatch.setenv('AMAZON_BROWSER_PROXY', 'http://127.0.0.1:7890')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7890')
    monkeypatch.setenv('MODEL_API_KEY', 'fixture-key')
    environment_before = dict(os.environ)
    monkeypatch.setattr(transport, 'verify_saved', lambda _: pytest.fail('DIRECT must not adopt or validate the shared route'))
    assert transport.amazon_environment(tmp_path, mode='DIRECT') == {
        'AMAZON_BROWSER_ROUTE_MODE': 'DIRECT', 'AMAZON_BROWSER_PROXY': ''}
    assert path.read_bytes() == before
    assert dict(os.environ) == environment_before


def test_auto_still_requires_valid_saved_route(tmp_path, monkeypatch):
    saved_settings(tmp_path)
    monkeypatch.setattr(transport, 'verify_saved', lambda _: False)
    refreshes = []
    monkeypatch.setattr(transport, 'refresh_saved_owner',
                        lambda root, settings: refreshes.append(root) or settings)
    with pytest.raises(RuntimeError, match='Amazon'):
        transport.amazon_environment(tmp_path, mode='AUTO')
    assert refreshes == [tmp_path]


def test_auto_refreshes_restarted_proxy_owner_without_changing_route(tmp_path, monkeypatch):
    from functools import partial
    from selection_automation.local_proxy_prepare import refresh_saved_owner

    path, settings = saved_settings(tmp_path, local_proxy_owner={
        'pid': 1, 'created': 1, 'name': 'clash-meta.exe'})
    evidence = {'processes': [{'pid': 2, 'created': 2, 'name': 'clash-meta.exe',
                              'listeners': [{'host': '127.0.0.1', 'port': 7890}]}]}
    monkeypatch.setattr(transport, 'verify_saved',
                        lambda value: value['local_proxy_owner']['pid'] == 2)
    monkeypatch.setattr(transport, 'refresh_saved_owner', partial(
        refresh_saved_owner, evidence=evidence,
        protocol_probe=lambda _: {'protocol': 'HTTP'}, check_owner=lambda _: True))

    assert transport.amazon_environment(tmp_path, mode='AUTO') == {
        'AMAZON_BROWSER_ROUTE_MODE': 'AUTO', 'AMAZON_BROWSER_PROXY': 'http://127.0.0.1:7890'}
    updated = json.loads(path.read_text(encoding='utf-8'))
    assert updated == {**settings, 'local_proxy_owner': {
        'pid': 2, 'created': 2, 'name': 'clash-meta.exe'}}


def test_explicit_auto_overrides_inherited_direct_and_keeps_local_proxy(tmp_path, monkeypatch):
    saved_settings(tmp_path)
    monkeypatch.setenv('AMAZON_BROWSER_ROUTE_MODE', 'DIRECT')
    monkeypatch.setattr(transport, 'verify_saved', lambda _: True)
    assert transport.amazon_environment(tmp_path, mode='AUTO') == {
        'AMAZON_BROWSER_ROUTE_MODE': 'AUTO', 'AMAZON_BROWSER_PROXY': 'http://127.0.0.1:7890'}
    assert transport.amazon_environment(tmp_path)['AMAZON_BROWSER_PROXY'] == ''


def test_amazon_direct_preserves_temu_and_model_api_routes(tmp_path, monkeypatch):
    _, settings = saved_settings(tmp_path, proxy_architecture='LOCAL_VPN')
    monkeypatch.setattr(transport, 'verify_saved', lambda _: True)
    monkeypatch.setattr(stage3_transport, 'getproxies', lambda: {'https': 'http://127.0.0.1:7890'})
    monkeypatch.setattr(stage3_transport, 'proxy_bypass', lambda _: False)
    provider = {'mode': 'multimodal_api', 'base_url': 'https://model.invalid/v1'}
    ads_before = transport.adspower_proxy(settings)
    api_before = stage3_transport.transport_options(provider)
    transport.amazon_environment(tmp_path, mode='DIRECT')
    assert transport.adspower_proxy(settings) == ads_before
    assert stage3_transport.transport_options(provider) == api_before
    assert api_before == {'trust_env': False, 'proxy': 'http://127.0.0.1:7890'}


@pytest.mark.parametrize('mode', ['PROXY', 'direct-and-auto', 'http://private.invalid'])
def test_invalid_route_never_silently_falls_back(tmp_path, mode):
    with pytest.raises(ValueError, match='AUTO or DIRECT'):
        transport.amazon_environment(tmp_path, mode=mode)
