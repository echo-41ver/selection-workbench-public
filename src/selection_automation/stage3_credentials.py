"""Endpoint-bound third-layer credentials; persisted only with current-user DPAPI."""
from pathlib import Path
import hashlib
import os

D1_URL = 'https://api.liquid.ai/decisions/v1/systemone'


def validate_key(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or any(ord(c) < 32 for c in value)):
        raise ValueError('API key 无效')
    return value.strip()


def credential_file(provider):
    """One encrypted file per endpoint so providers never overwrite each other."""
    endpoint = provider['base_url']
    if provider['mode'] == 'liquid_d1' and endpoint == D1_URL:
        return 'liquid_d1.dpapi'
    if endpoint == 'https://api.deepseek.com':
        return 'deepseek.dpapi'
    return 'stage3-' + hashlib.sha256(endpoint.encode('utf-8')).hexdigest()[:16] + '.dpapi'


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
    elif endpoint == 'https://api.deepseek.com':
        if os.getenv('DEEPSEEK_API_KEY'):
            return validate_key(os.environ['DEEPSEEK_API_KEY'])
    elif 'api.openai.com' in endpoint and os.getenv('OPENAI_API_KEY'):
        return validate_key(os.environ['OPENAI_API_KEY'])
    folder = Path(root) / 'runtime/secrets'
    names = [credential_file(provider)]
    if names[0] not in {'liquid_d1.dpapi', 'deepseek.dpapi'}:
        names.append('stage3-api.dpapi')  # legacy single-slot file
    for name in names:
        try:
            value = load(folder / name)
            if value.get('base_url', '').rstrip('/') == endpoint:
                return validate_key(value.get('key'))
        except (OSError, ValueError, RuntimeError):
            continue
    return ''


def save_key(root, provider, value):
    from .secrets import store
    if provider['mode'] not in {'liquid_d1', 'multimodal_api'}:
        raise ValueError('此方案无需保存远程 API key')
    store(Path(root) / 'runtime/secrets' / credential_file(provider),
          {'key': validate_key(value), 'base_url': provider['base_url'], 'model': provider['model']})
