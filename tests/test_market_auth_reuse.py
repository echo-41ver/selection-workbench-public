import io
import json
import time

from selection_automation.market_provider import Provider


def test_completed_device_authorization_is_saved_and_reused_by_next_provider(tmp_path, monkeypatch):
    import geekbi_temu_mcp.auth as auth
    path = tmp_path / 'shared-user-auth.json'
    monkeypatch.setattr(auth, 'default_auth_path', lambda: path)
    base = 'https://openapi.geekbi.com'
    monkeypatch.setenv('GEEKBI_BASE_URL', base)
    path.write_text(json.dumps({'version': 1, 'servers': {base: {'pending': {
        'deviceCode': 'fixture-device', 'jumpUrl': base + '/agent/auth/login',
        'expiresAt': int(time.time()) + 600}}}}), encoding='utf8')
    requests = []

    def response(request, timeout):
        requests.append(request)
        if request.full_url.endswith(auth.TOKEN_ENDPOINT):
            data = {'accessToken': 'fixture-token', 'expiresIn': 3600}
        else:
            assert request.get_header('Token') == 'Bearer fixture-token'
            data = {'list': [], 'total': 0}
        return io.BytesIO(json.dumps({'code': 0, 'data': data}).encode())

    monkeypatch.setattr(auth, 'urlopen', response)
    query = {'keyword': '键盘', 'siteId': 48, 'page': 1, 'size': 1}
    assert Provider().categories(query)['list'] == []
    assert Provider().categories(query)['list'] == []
    assert sum(r.full_url.endswith(auth.TOKEN_ENDPOINT) for r in requests) == 1
    saved = json.loads(path.read_text('utf8'))['servers'][base]
    assert saved['accessTokenExpiresAt'] > time.time()
    assert 'pending' not in saved
