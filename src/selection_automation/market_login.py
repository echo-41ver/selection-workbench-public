"""Normal company-account web login for an installation-owned SDK challenge."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from .runtime_environment import activate
from .secrets import load
from .merge_temu_snapshots import atomic_write


LOGIN_MESSAGES = {
    'STARTED': '极鲸云公司账号已尝试登录，等待网页授权；请处理当前授权页面。',
    'CAPTCHA_REQUIRED': '极鲸云登录遇到验证码或安全验证，请在授权页面人工处理后继续。',
    'LOGIN_REJECTED': '极鲸云拒绝公司账号登录，请检查账号状态或密码后处理当前授权页面。',
    'FORM_UNAVAILABLE': '极鲸云登录页面未识别到明确的账号密码表单，请处理当前授权页面。',
    'BROWSER_UNAVAILABLE': '极鲸云自动登录浏览器未就绪，请运行部署修复或处理当前授权页面。',
    'LOGIN_FAILED': '极鲸云网页登录未完成，请处理当前授权页面。',
    'LOGIN_HTTP_403': '极鲸云官方登录页面返回 HTTP 403，访问被拒绝，未提交公司账号；请通过正常授权页面处理后重试。',
    'LOGIN_HTTP_429': '极鲸云官方登录页面返回 HTTP 429，请求受限，未提交公司账号；请稍后显式重试。',
    'LOGIN_NETWORK_FAILED': '极鲸云官方登录页面连接失败，未提交公司账号；请检查网络后显式重试。',
    'AUTHORIZATION_PENDING': '极鲸云授权仍在等待官方回调；请处理当前授权页面。',
}


def reset_company_login(auth):
    """Explicit continuation, or a confirmed SDK token, permits a later login."""
    def reset(state):
        server = state['servers'].get(auth.server_key, {})
        changed = server.pop('companyLoginBlocked', None) is not None
        pending = server.get('pending', {})
        changed = pending.pop('companyLogin', None) is not None or changed
        return changed, None
    auth._mutate(reset)


def complete_company_login(auth, error, timeout, root):
    """At most one browser attempt per SDK device code; only the SDK confirms success."""
    from geekbi_temu_mcp.auth import ActionRequired

    root = Path(root)
    secret_path = root / 'runtime/secrets/company.dpapi'
    if not secret_path.is_file():
        return False
    try:
        company = load(secret_path)
    except (OSError, ValueError, RuntimeError):
        raise ActionRequired('无法读取本机加密公司账号，请重新导入公司配置或处理当前授权页面。',
                             error.jump_url, action='AUTH_REQUIRED',
                             expires_in=error.expires_in, pending=True) from None
    account = company.get('geekbi') if isinstance(company, dict) else None
    if not isinstance(account, dict) or not all(isinstance(account.get(key), str) and account[key]
                                               for key in ('username', 'password')):
        return False

    def claim(state):
        server = state['servers'].get(auth.server_key, {})
        blocked = server.get('companyLoginBlocked')
        if isinstance(blocked, str):
            return False, (None, blocked)
        pending = server.get('pending')
        if not isinstance(pending, dict) or pending.get('expiresAt', 0) <= time.time():
            return False, (None, '')
        previous = pending.get('companyLogin')
        if isinstance(previous, dict):
            return False, (None, previous.get('status', 'STARTED'))
        pending['companyLogin'] = {'attemptedAt': int(time.time()), 'status': 'STARTED'}
        return True, (dict(pending), '')

    pending, previous = auth._mutate(claim)
    if pending is None:
        if not previous:
            return False
        raise ActionRequired(LOGIN_MESSAGES.get(previous, LOGIN_MESSAGES['STARTED']),
                             error.jump_url, action='AUTH_REQUIRED',
                             expires_in=error.expires_in, pending=True) from None

    diagnostics = {}

    def save_status(status):
        def save(state):
            server = state['servers'].get(auth.server_key, {})
            current = server.get('pending')
            if not isinstance(current, dict) or current.get('deviceCode') != pending['deviceCode']:
                return False, None
            current['companyLogin']['status'] = status
            if status in {'CAPTCHA_REQUIRED', 'LOGIN_REJECTED', 'LOGIN_HTTP_403', 'LOGIN_HTTP_429', 'LOGIN_NETWORK_FAILED'}:
                server['companyLoginBlocked'] = status
            return True, None
        auth._mutate(save)
        atomic_write(root / 'runtime/market-auth/company-login-status.json', json.dumps({
            'status': status, 'updated_at': int(time.time()), 'diagnostics': diagnostics,
        }, ensure_ascii=False))

    env = activate(root, dict(os.environ))
    node = shutil.which('node', path=env.get('PATH'))
    status = 'BROWSER_UNAVAILABLE'
    if node:
        remaining = max(1, min(60, pending['expiresAt'] - time.time()))
        request = {'jump_url': pending['jumpUrl'], 'username': account['username'],
                   'password': account['password'],
                   'profile': str(root / 'runtime/market-auth/browser-profile'),
                   'timeout_ms': int(remaining * 1000)}
        try:
            result = subprocess.run([node, str(root / 'scripts/geekbi_company_login.mjs')],
                                    cwd=root, env=env, input=json.dumps(request),
                                    capture_output=True, text=True, encoding='utf-8',
                                    timeout=remaining + 5)
            reply = json.loads(result.stdout) if result.returncode == 0 else {}
            status = reply.get('status', 'LOGIN_FAILED') if isinstance(reply, dict) else 'LOGIN_FAILED'
            if status not in {*LOGIN_MESSAGES, 'AUTHORIZATION_SENT'}:
                status = 'LOGIN_FAILED'
            observed = reply.get('diagnostics') if isinstance(reply, dict) else None
            if isinstance(observed, dict):
                diagnostics = {key: observed[key] for key in ('password_count', 'account_count', 'submit_count')
                               if type(observed.get(key)) is int and observed[key] >= 0}
                diagnostics.update({key: observed[key] for key in ('submitted', 'callback')
                                    if type(observed.get(key)) is bool})
            if status == 'AUTHORIZATION_SENT' and not (diagnostics.get('submitted') or diagnostics.get('callback')):
                status = 'FORM_UNAVAILABLE'
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # Browser output may contain page data. Never include it in the
            # exception, saved status or any user-facing diagnostics.
            status = 'LOGIN_FAILED'
        finally:
            request.clear()
    account = None
    company = None
    if status == 'AUTHORIZATION_SENT':
        deadline = time.monotonic() + max(0, min(60, pending['expiresAt'] - time.time()))
        while time.monotonic() < deadline:
            try:
                if auth.complete_pending_login(min(timeout, 10, deadline - time.monotonic())):
                    save_status('AUTHORIZED')
                    return True
                break
            except ActionRequired as waiting:
                if waiting.action != 'AUTH_REQUIRED' or not waiting.pending:
                    raise
                time.sleep(min(2, max(0, deadline - time.monotonic())))
        status = 'AUTHORIZATION_PENDING'
    save_status(status)
    raise ActionRequired(LOGIN_MESSAGES[status], error.jump_url, action='AUTH_REQUIRED',
                         expires_in=max(0, int(pending['expiresAt'] - time.time())),
                         pending=True) from None
