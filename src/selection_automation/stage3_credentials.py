"""Endpoint-bound third-layer credentials; persisted only with current-user DPAPI."""
from pathlib import Path
import os

D1_URL = 'https://api.liquid.ai/decisions/v1/systemone'


def validate_key(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or any(ord(c) < 32 for c in value)):
        raise ValueError('API key 无效')
    return value.strip()


def saved_key(root, provider):
    from .secrets import load
    endpoint = provider['base_url']
    bound = os.getenv('SELECTION_STAGE3_ENDPOINT', '').rstrip('/')
    if bound == endpoint and os.getenv('SELECTION_STAGE3_API_KEY'):
        return validate_key(os.environ['SELECTION_STAGE3_API_KEY'])
    if provider['mode'] == 'liquid_d1' and endpoint == D1_URL:
        for name in ('LIQUID_API_KEY', 'D1_API_KEY'):
            if os.getenv(name):
                return validate_key(os.environ[name])
        filename = 'liquid_d1.dpapi'
    elif provider['mode'] == 'multimodal_api' and endpoint == 'https://api.deepseek.com':
        if os.getenv('DEEPSEEK_API_KEY'):
            return validate_key(os.environ['DEEPSEEK_API_KEY'])
        filename = 'deepseek.dpapi'
    else:
        filename = 'stage3-api.dpapi'
    try:
        value = load(Path(root) / 'runtime/secrets' / filename)
        if value.get('base_url', '').rstrip('/') == endpoint:
            return validate_key(value.get('key'))
    except (OSError, ValueError, RuntimeError):
        pass
    return ''


def save_key(root, provider, value):
    from .secrets import store
    if provider['mode'] not in {'liquid_d1', 'multimodal_api'}:
        raise ValueError('此方案无需保存远程 API key')
    filename = ('liquid_d1.dpapi' if provider['mode'] == 'liquid_d1' and provider['base_url'] == D1_URL
                else 'deepseek.dpapi' if provider['base_url'] == 'https://api.deepseek.com'
                else 'stage3-api.dpapi')
    store(Path(root) / 'runtime/secrets' / filename,
          {'key': validate_key(value), 'base_url': provider['base_url'], 'model': provider['model']})
