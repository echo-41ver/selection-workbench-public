"""Installation-owned authorization; shared clients cannot replace its login state."""
import time
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError, URLError


def use_installation_auth(client, state_path):
    from geekbi_temu_mcp.auth import ActionRequired, AuthManager

    class InstallationAuth(AuthManager):
        def complete_pending_login(self, timeout):
            completed = super().complete_pending_login(timeout)
            if completed:
                from .market_login import reset_company_login
                reset_company_login(self)
            return completed

        def request_json(self, url, timeout, **options):
            current=self._mutate(lambda state:(False,state['servers'].get(self.server_key,{})))
            retry_get=options.get('method','GET')=='GET' and token(current) and not current.get('pending')
            try:
                for attempt in range(2):
                    try:
                        return super().request_json(url, timeout, **options)
                    except URLError as error:
                        if not retry_get or attempt or isinstance(error,HTTPError):
                            raise
                        time.sleep(.3)
            except ActionRequired as error:
                if error.action != 'AUTH_REQUIRED':
                    raise
                from .market_login import complete_company_login
                root = Path(self.state_path).resolve().parents[2]
                if not complete_company_login(self, error, timeout, root):
                    raise
            # Login only authorizes the original request; a second challenge is
            # returned to the caller without submitting another login.
            return super().request_json(url, timeout, **options)

    shared=client.auth
    local=InstallationAuth(client.base_url, state_path=state_path)
    now=int(time.time())
    def token(row):
        return (isinstance(row,dict) and isinstance(row.get('accessToken'),str)
                and bool(row['accessToken']) and type(row.get('accessTokenExpiresAt')) is int
                and row['accessTokenExpiresAt']>int(time.time())+30)
    if local._mutate(lambda state:(False,token(state['servers'].get(local.server_key)))):
        client.auth=local
        return
    authorized=None
    secret=Path(state_path).resolve().parents[2]/'runtime/secrets/company.dpapi'
    if local.server_key=='https://openapi.geekbi.com' and secret.is_file():
        from .secrets import load
        account=load(secret).get('geekbi') or {}
        if account.get('expires_at',0)>now+30:
            authorized={'accessToken':account['access_token'],'accessTokenExpiresAt':account['expires_at']}
    def read_shared(state):
        row=state.get('servers',{}).get(shared.server_key,{})
        return False,deepcopy(row) if token(row) else None
    if authorized is None:authorized=shared._mutate(read_shared)
    def initialize(state):
        current=state['servers'].get(local.server_key,{})
        if token(current) or authorized is None:
            return False,None
        state['servers'][local.server_key]={
            'accessToken':authorized['accessToken'],
            'accessTokenExpiresAt':authorized['accessTokenExpiresAt']}
        return True,None
    local._mutate(initialize)
    client.auth=local
