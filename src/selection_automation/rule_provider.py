"""User-selected Chat Completions provider for rule authoring only."""
from urllib.parse import urlsplit, urlunsplit

DEFAULT_URL = 'https://api.deepseek.com'
DEFAULT_MODEL = 'deepseek-flash'


def settings(base_url='', model=''):
    if not isinstance(base_url, str) or not isinstance(model, str):
        raise ValueError('API 服务地址和模型名必须是文字')
    address = base_url.strip() or DEFAULT_URL
    if len(address) > 2048 or any(c.isspace() or ord(c) < 32 for c in address):
        raise ValueError('API 服务地址无效，请填写完整的 http:// 或 https:// 地址')
    try:
        parsed = urlsplit(address)
        port = parsed.port
    except ValueError:
        raise ValueError('API 服务地址或端口无效') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('API 服务地址须包含协议和主机，不能包含密钥、账号、查询参数或片段')
    path = parsed.path.rstrip('/')
    if path.endswith('/chat/completions'):
        path = path[:-len('/chat/completions')]
    address = urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
    name = model.strip()
    if not name:
        if address != DEFAULT_URL:
            raise ValueError('使用其他 API 服务时，请填写该服务支持的模型名')
        name = DEFAULT_MODEL
    if len(name) > 200 or any(c.isspace() or ord(c) < 32 for c in name):
        raise ValueError('模型名无效，请填写服务商提供的模型 ID')
    return {'base_url': address, 'model': name}


def environment(provider):
    return {'SELECTION_RULES_BASE_URL': provider['base_url'],
            'SELECTION_RULES_MODEL': provider['model']}
