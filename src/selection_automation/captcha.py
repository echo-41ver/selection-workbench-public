"""Installation-owned 2Captcha client. No browser, credential or answer logging."""
import base64
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .secrets import load, transform

DEFAULTS = {'enabled': False, 'max_requests': 10, 'budget_usd': 0.10,
            'reserve_usd': 0.01, 'timeout_seconds': 180}
MESSAGES = {
    'NOT_CONFIGURED': '2Captcha 未配置；已保留浏览器、session 和采集断点。',
    'DISABLED': '2Captcha 自动解题已关闭；已保留当前现场。',
    'UNSUPPORTED': '当前验证码题型或页面控件尚未适配；已保留断点，请在原窗口处理。',
    'PENDING': '2Captcha 正在解题；保留当前环境，等待结果。',
    'ANSWER_READY': '已取得解题答案，正在验证网站是否放行。',
    'APPLYING': '上次答案执行尚未确认；保留页面，请检查后继续，不重复提交或付费。',
    'VERIFIED': '网站已解除验证码；采集将沿原断点继续。',
    'REJECTED': '网站未确认放行；停止本题自动付费重试，保留当前现场。',
    'TIMEOUT': '2Captcha 等待超时；保留任务编号和断点，不重复付费提交。',
    'ZERO_BALANCE': '2Captcha 余额不足；已保留现场，等待余额恢复。',
    'INVALID_KEY': '2Captcha Key 无效或未获授权；请由维护者更新内部包配置，当前断点保留。',
    'BUDGET_LIMIT': '2Captcha 已达到次数或预算预留限制；保留当前断点。',
    'SUBMIT_UNKNOWN': '解题提交结果未知；已停止重复提交，保留现场以避免重复扣费。',
    'SERVICE_FAILED': '2Captcha 服务失败；本题自动重试已停止，当前现场保留。',
    'DEPENDENCY_MISSING': '2Captcha SDK 未安装；请运行部署修复，当前断点保留。',
    'STOPPED': '已停止自动解题；服务任务和采集断点已保存。',
}


def normalize(value):
    if not isinstance(value, dict) or set(value) - {*DEFAULTS, 'key'}:
        raise ValueError('2Captcha 配置字段无效')
    result = {**DEFAULTS, **value}
    if type(result['enabled']) is not bool:
        raise ValueError('2Captcha enabled 必须为布尔值')
    for name, low, high in [('max_requests', 1, 100), ('timeout_seconds', 30, 600)]:
        if type(result[name]) is not int or not low <= result[name] <= high:
            raise ValueError('2Captcha ' + name + ' 超出范围')
    for name in ('budget_usd', 'reserve_usd'):
        number = result[name]
        if type(number) not in (int, float) or not math.isfinite(number) or not 0 < number <= 10:
            raise ValueError('2Captcha ' + name + ' 超出范围')
    if result['reserve_usd'] > result['budget_usd']:
        raise ValueError('单题预算预留不能大于任务预算')
    key = result.get('key', '')
    if not isinstance(key, str) or len(key) > 256 or any(ord(c) < 33 for c in key):
        raise ValueError('2Captcha Key 格式无效')
    if key.startswith(('YOUR_', 'REPLACE_')):
        key = ''
    result['key'] = key
    return result


def settings(root):
    path = Path(root) / 'runtime/secrets/twocaptcha.dpapi'
    return normalize(load(path)) if path.is_file() else normalize({})


def reply(status, **extra):
    return {'status': status, 'message': MESSAGES[status], **extra}


def sdk_client(key):
    from twocaptcha import TwoCaptcha
    from twocaptcha.api import ApiClient
    from twocaptcha.exceptions.api import ApiException, NetworkException
    import requests

    class BoundedTransport(ApiClient):
        # The official SDK's default transport has no HTTP deadline. Only image
        # base64 parameters are used here, so no file handles or URL downloads.
        def request(self, method, endpoint, params):
            try:
                response = requests.request(method, 'https://2captcha.com/' + endpoint,
                                            **({'data': params} if method == 'POST' else {'params': params}),
                                            timeout=(5, 15))
            except requests.RequestException:
                raise NetworkException() from None
            if response.status_code != 200:
                raise NetworkException()
            text = response.text
            if 'ERROR' in text:
                # Keep only an allowlisted service error code, never the raw reply.
                code = re.search(r'ERROR_[A-Z_]+', text)
                raise ApiException(code[0] if code else 'ERROR_SERVICE')
            return text

        def in_(self, files=None, **kwargs):
            return self.request('POST', 'in.php', kwargs)

        def res(self, **kwargs):
            return self.request('GET', 'res.php', kwargs)

    client = TwoCaptcha(key, softId=0)
    client.api_client = BoundedTransport()
    return client


def step(root, request, *, config=None, client=None, now=None):
    """One durable submit/poll/ack operation, shared by ordinary background collectors."""
    root = Path(root)
    config = settings(root) if config is None else normalize(config)
    if not config['key']:
        return reply('NOT_CONFIGURED')
    if not config['enabled']:
        return reply('DISABLED')
    scope, fingerprint = request.get('scope'), request.get('fingerprint')
    if not isinstance(scope, str) or not re.fullmatch(r'[a-zA-Z0-9_/-]{1,160}', scope):
        raise ValueError('Invalid CAPTCHA scope')
    if not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint):
        raise ValueError('Invalid CAPTCHA fingerprint')
    action = request.get('action', 'solve')
    if action not in ('solve', 'claim', 'verify'):
        raise ValueError('Invalid CAPTCHA action')
    kind = request.get('kind')
    if action == 'solve' and kind not in ('normal', 'temu'):
        return reply('UNSUPPORTED')
    params = {}
    if action == 'solve':
        images = request.get('images')
        if not isinstance(images, list) or len(images) != (4 if kind == 'temu' else 1):
            return reply('UNSUPPORTED')
        for value in images:
            if not isinstance(value, str) or len(value) > 800000:
                raise ValueError('Invalid CAPTCHA image')
            raw = base64.b64decode(value, validate=True)
            if not 100 <= len(raw) <= 600000 or not raw.startswith((b'\x89PNG', b'\xff\xd8', b'GIF8')):
                raise ValueError('Invalid CAPTCHA image')
        params = {'method': 'base64', 'body': images[0]} if kind == 'normal' else {
            'method': 'temuimage', 'body': images[0], **{f'part{i}': images[i] for i in range(1, 4)}}
    directory = root / 'runtime/captcha'
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.time() if now is None else now
    with sqlite3.connect(directory / 'tasks.sqlite3', timeout=25) as db:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS jobs (scope TEXT, fingerprint TEXT, state TEXT, task_id TEXT, '
                   'created REAL, next_poll REAL, errors INTEGER DEFAULT 0, answer BLOB, reserved REAL, '
                   'initial_balance REAL, PRIMARY KEY(scope,fingerprint))')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM jobs WHERE scope=? AND fingerprint=?', (scope, fingerprint)).fetchone()
        if action != 'solve':
            if row is None:
                return reply('SERVICE_FAILED')
            if action == 'claim':
                if row['state'] != 'ANSWER_READY':
                    return reply(row['state'])
                state = 'APPLYING'
            else:
                if row['state'] != 'APPLYING':
                    return reply(row['state'])
                state = 'VERIFIED' if request.get('passed') is True else 'REJECTED'
            db.execute('UPDATE jobs SET state=?,answer=NULL WHERE scope=? AND fingerprint=?', (state, scope, fingerprint))
            return reply(state)
        if row and row['state'] not in ('PENDING', 'ANSWER_READY', 'ZERO_BALANCE', 'INVALID_KEY'):
            return reply(row['state'])
        if row and row['state'] == 'ANSWER_READY':
            return reply('ANSWER_READY', answer=json.loads(transform(row['answer'], False)))
        if row and row['state'] == 'PENDING' and stamp < row['next_poll']:
            return reply('PENDING', retry_after=10)
        try:
            client = client or sdk_client(config['key'])
        except ImportError:
            return reply('DEPENDENCY_MISSING')
        from twocaptcha.exceptions.api import ApiException, NetworkException
        from twocaptcha.exceptions.solver import ValidationException, NetworkException as SolverNetworkException, ApiException as SolverApiException
        try:
            if row is None or row['state'] in ('ZERO_BALANCE', 'INVALID_KEY'):
                used = db.execute('SELECT count(*),coalesce(sum(reserved),0),max(initial_balance) FROM jobs WHERE scope=? AND reserved>0', (scope,)).fetchone()
                if used[0] >= config['max_requests'] or used[1] + config['reserve_usd'] > config['budget_usd'] + 1e-9:
                    return reply('BUDGET_LIMIT')
                balance = float(client.balance())
                if not math.isfinite(balance) or balance < config['reserve_usd']:
                    return reply('ZERO_BALANCE')
                # Balance deltas also stop new submissions after the real account
                # spend reaches the threshold. Reservations are estimates, not invoices.
                initial = used[2] if used[2] is not None else balance
                if initial - balance + config['reserve_usd'] > config['budget_usd'] + 1e-9:
                    return reply('BUDGET_LIMIT')
                db.execute('INSERT OR REPLACE INTO jobs(scope,fingerprint,state,created,next_poll,reserved,initial_balance) '
                           'VALUES(?,?,?,?,?,?,?)', (scope, fingerprint, 'SUBMIT_UNKNOWN', stamp, stamp + 10, config['reserve_usd'], initial))
                db.commit()  # An interrupted/ambiguous POST must never be auto-repeated.
                task_id = str(client.send(**params))
                if not re.fullmatch(r'\d{1,32}', task_id):
                    return reply('SUBMIT_UNKNOWN')
                db.execute('UPDATE jobs SET state=?,task_id=? WHERE scope=? AND fingerprint=?', ('PENDING', task_id, scope, fingerprint))
                return reply('PENDING', retry_after=10)
            if stamp - row['created'] >= config['timeout_seconds']:
                db.execute('UPDATE jobs SET state=? WHERE scope=? AND fingerprint=?', ('TIMEOUT', scope, fingerprint))
                return reply('TIMEOUT')
            answer = client.get_result(row['task_id'])
            # Service result is untrusted; never accept empty or oversized answers.
            if not isinstance(answer, str) or not 1 <= len(answer) <= 4096:
                raise ValidationException()
            if kind == 'normal':
                if not re.fullmatch(r'.{1,64}', answer):
                    raise ValidationException()
            else:
                try:
                    points = json.loads(answer)
                    if isinstance(points, dict): points = points.get('coordinates')
                except ValueError:
                    points = [{'x': int(x), 'y': int(y)} for x, y in re.findall(r'x=(\d+),y=(\d+)', answer)]
                if not isinstance(points, list) or len(points) != 3 or any(not isinstance(p, dict) or any(
                    type(p.get(k)) not in (int, float) or not math.isfinite(p[k]) or not 0 <= p[k] <= 600 for k in ('x', 'y')) for p in points):
                    raise ValidationException()
                answer = json.dumps(points)
            encrypted = transform(json.dumps(answer).encode())
            db.execute('UPDATE jobs SET state=?,answer=? WHERE scope=? AND fingerprint=?', ('ANSWER_READY', encrypted, scope, fingerprint))
            return reply('ANSWER_READY', answer=answer)
        except (NetworkException, SolverNetworkException):
            if row is None or row['state'] in ('ZERO_BALANCE', 'INVALID_KEY'):
                return reply('SUBMIT_UNKNOWN')
            # SDK uses NetworkException for CAPCHA_NOT_READY too. Both have the
            # same bounded polling deadline and never create another paid task.
            db.execute('UPDATE jobs SET next_poll=? WHERE scope=? AND fingerprint=?', (stamp + 10, scope, fingerprint))
            return reply('PENDING', retry_after=10)
        except (ApiException, SolverApiException, ValidationException) as error:
            code = str(error)
            state = 'ZERO_BALANCE' if 'ERROR_ZERO_BALANCE' in code else 'INVALID_KEY' if any(
                s in code for s in ('ERROR_WRONG_USER_KEY', 'ERROR_KEY_DOES_NOT_EXIST', 'ERROR_IP_NOT_ALLOWED')) else 'SERVICE_FAILED'
            db.execute('UPDATE jobs SET state=?,reserved=CASE WHEN task_id IS NULL AND ? IN (\'ZERO_BALANCE\',\'INVALID_KEY\') THEN 0 ELSE reserved END '
                       'WHERE scope=? AND fingerprint=?', (state, state, scope, fingerprint))
            return reply(state)
