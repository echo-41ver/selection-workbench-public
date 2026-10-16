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


def test_console_market_request_uses_installed_project_runtime(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import web_console as web
    console=web.Console(tmp_path)
    python=tmp_path/'.venv/Scripts/python.exe';python.parent.mkdir(parents=True);python.touch()
    commands=[]
    def spawn(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(poll=lambda:0,returncode=0)
    monkeypatch.setattr(web.subprocess,'Popen',spawn)
    console.invoke([str(tmp_path/'scripts/collect_market_evidence.py')])
    assert commands[0][0]==str(python)
    console.invoke([str(tmp_path/'scripts/publish_all.py')])
    assert commands[1][0]==sys.executable


def test_standalone_market_collection_keeps_shared_authorization_network_route(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    import urllib.request
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import collect_market_evidence as market
    import watch_temu_union
    import geekbi_temu_mcp.auth as auth
    monkeypatch.setattr(market, 'ROOT', tmp_path)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setattr(watch_temu_union, 'publish_once', lambda *args: None)
    path=tmp_path/'shared-auth.json'
    monkeypatch.setattr(auth, 'default_auth_path', lambda: path)
    base='https://openapi.geekbi.com';monkeypatch.setenv('GEEKBI_BASE_URL', base)
    path.write_text(json.dumps({'version':1,'servers':{base:{'accessToken':'fixture-token',
        'accessTokenExpiresAt':int(time.time())+3600}}}),encoding='utf8')
    calls=[]
    def approved_route(request, timeout):
        assert request.get_header('Token')=='Bearer fixture-token'
        calls.append(request.full_url)
        data={'list':[{'catId':1,'catName':'fixture'}],'total':1} if '/category/' in request.full_url else {'list':[],'total':0}
        return io.BytesIO(json.dumps({'code':0,'data':data}).encode())
    monkeypatch.setattr(auth, 'urlopen', approved_route)
    def unexpected_route(*args):
        raise AssertionError('saved authorization must retain its existing network route')
    monkeypatch.setattr(urllib.request, 'build_opener', unexpected_route)
    category=tmp_path/'category.json'
    category.write_text(json.dumps({'category_key':'fixture','category_name_zh':'fixture'}),encoding='utf8')
    result=market.run(category, force=True)
    assert result['status']=='COMPLETE'
    assert len(calls)==2


def test_workbench_authorization_survives_another_client_pending_login(tmp_path, monkeypatch):
    import geekbi_temu_mcp.auth as auth
    from selection_automation.market_auth import use_installation_auth
    shared=tmp_path/'shared.json';local=tmp_path/'install-private.json'
    monkeypatch.setattr(auth,'default_auth_path',lambda:shared)
    base='https://openapi.geekbi.com';monkeypatch.setenv('GEEKBI_BASE_URL',base)
    expiry=int(time.time())+3600
    shared.write_text(json.dumps({'version':1,'servers':{base:{'accessToken':'fixture-token','accessTokenExpiresAt':expiry}}}),encoding='utf8')
    first=Provider(auth_state_path=local)
    assert first.client.auth.state_path==local
    shared.write_text(json.dumps({'version':1,'servers':{base:{'pending':{'deviceCode':'other-client','expiresAt':expiry}}}}),encoding='utf8')
    second=Provider(auth_state_path=local)
    assert second.client.auth._authorization_header()=='Bearer fixture-token'
    saved=json.loads(local.read_text('utf8'))['servers'][base]
    assert saved['accessTokenExpiresAt']==expiry
    assert 'pending' not in saved
