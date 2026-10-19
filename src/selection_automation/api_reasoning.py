"""Read explicit model capabilities; never discover them by paid inference."""
import hashlib
from copy import deepcopy
import json
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

_CACHE = {}
_MODEL_CACHE = {}
_LOCK = threading.Lock()


def valid_effort(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', value))


def capabilities(record, provider):
    """Only an exact model's explicit enum is evidence for selectable levels."""
    result = {'model': provider['model'], 'protocol': provider['protocol'],
              'efforts': [], 'default_effort': None, 'status': 'unreported',
              'message': '接口未提供该模型的推理强度档位，仅使用接口默认。'}
    if not isinstance(record, dict) or record.get('id') != provider['model']:
        return result
    scope = record.get('capabilities') or {}
    if not isinstance(scope, dict): scope = {}
    specific = scope.get(provider['protocol'])
    scopes = [specific] if isinstance(specific, dict) else [record, scope]
    for entry in scopes:
        reasoning = entry.get('reasoning') or {}
        effort = entry.get('effort') or {}
        if not isinstance(effort, dict): effort = {}
        parameters = entry.get('parameters') or {}
        if not isinstance(reasoning, dict): reasoning = {}
        if not isinstance(parameters, dict): parameters = {}
        effort_parameter = parameters.get('reasoning_effort') or {}
        nested = parameters.get('reasoning') or {}
        if not isinstance(effort_parameter, dict): effort_parameter = {}
        if not isinstance(nested, dict): nested = {}
        properties = nested.get('properties') or {}
        nested_effort = properties.get('effort') if isinstance(properties, dict) else {}
        if not isinstance(nested_effort, dict): nested_effort = {}
        choices = next((value for value in (
            entry.get('supported_reasoning_efforts'), entry.get('reasoning_efforts'), effort.get('supported_levels'),
            reasoning.get('efforts'), reasoning.get('supported_efforts'),
            effort_parameter.get('enum'), nested_effort.get('enum')) if isinstance(value, list)), None)
        if choices is None: continue
        efforts = list(dict.fromkeys(value for value in choices if valid_effort(value) and value != 'default'))
        default = (entry.get('default_reasoning_effort') or effort.get('default_level') or reasoning.get('default_effort')
                   or effort_parameter.get('default') or nested_effort.get('default'))
        result.update(efforts=efforts, default_effort=default if default in efforts else None,
                      status='ready' if efforts else 'unsupported',
                      message='已读取接口返回的模型推理档位。' if efforts else '接口未开放可选推理强度，使用接口默认。')
        return result
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _read(url, key):
    headers = {'Accept': 'application/json', 'User-Agent': 'SelectionWorkbench/1.0'}
    if key: headers['Authorization'] = 'Bearer ' + key
    with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=6) as response:
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024: raise ValueError('Model metadata exceeds size limit')
        return json.loads(data)


def list_models(provider, key='', *, refresh=False, fetch=None):
    identity=(provider['base_url'],provider['protocol'],hashlib.sha256(key.encode()).hexdigest())
    now=time.monotonic()
    if fetch is None and not refresh:
        with _LOCK:cached=_MODEL_CACHE.get(identity)
        if cached and now-cached[0]<120:return deepcopy(cached[1])
    result={'status':'unavailable','models':[],'message':'暂时无法读取模型列表，请检查服务地址和 API Key；也可保留手动填写。'}
    try:
        value=(fetch or (lambda url:_read(url,key)))(provider['base_url'].rstrip('/')+'/models')
        rows=value.get('data') if isinstance(value,dict) else None
        if isinstance(rows,list):
            models=[];seen=set()
            for row in rows[:10000]:
                if not isinstance(row,dict):continue
                model=row.get('id')
                if not isinstance(model,str) or not model or len(model)>200 or any(c.isspace() or ord(c)<32 for c in model) or model in seen:continue
                seen.add(model)
                name=row.get('name')
                name=name if isinstance(name,str) and 0<len(name)<=200 and all(ord(c)>=32 for c in name) else model
                modalities=row.get('input_modalities')
                models.append({'id':model,'name':name,'vision':('image' in modalities) if isinstance(modalities,list) else None,
                               'reasoning':capabilities(row,{**provider,'model':model})})
            result={'status':'ready','models':models,'message':'已读取接口当前可用的 '+str(len(models))+' 个模型。'}
    except (OSError,ValueError,TypeError):pass
    if fetch is None:
        with _LOCK:
            if len(_MODEL_CACHE)>=128:_MODEL_CACHE.clear()
            _MODEL_CACHE[identity]=(now,deepcopy(result))
    return result


def discover(provider, key='', *, refresh=False, fetch=None):
    identity = (provider['base_url'], provider['model'], provider['protocol'],
                hashlib.sha256(key.encode()).hexdigest())
    now = time.monotonic()
    if fetch is None and not refresh:
        with _LOCK: cached = _CACHE.get(identity)
        if cached and now - cached[0] < 120: return dict(cached[1], efforts=list(cached[1]['efforts']))
    result = capabilities(None, provider)
    if provider['mode'] != 'multimodal_api' or provider['protocol'] not in ('responses', 'chat_completions'):
        return dict(result, status='unsupported', message='该分析方式使用自身默认设置。')
    reader = fetch or (lambda url: _read(url, key))
    base = provider['base_url'].rstrip('/') + '/models'
    received = False
    for url in (base + '/' + quote(provider['model'], safe=''), base):
        try:
            value = reader(url)
            received = True
            rows = value.get('data') if isinstance(value, dict) else None
            record = (next((row for row in rows[:10000] if isinstance(row, dict) and row.get('id') == provider['model']), None)
                      if isinstance(rows, list) else rows if isinstance(rows, dict) else value)
            result = capabilities(record, provider)
            if result['status'] != 'unreported': break
        except HTTPError as error:
            if error.code in (401, 403):
                result.update(status='unavailable', message='模型能力读取被服务拒绝，请核对该服务的 API Key 和权限。')
                break
        except (OSError, ValueError, TypeError):
            continue
    if not received and result['status'] == 'unreported':
        result.update(status='unavailable', message='暂时无法读取模型能力；任务配置保留，可重新读取。')
    if fetch is None:
        with _LOCK:
            if len(_CACHE) >= 128: _CACHE.clear()
            _CACHE[identity] = (now, dict(result, efforts=list(result['efforts'])))
    return result


def validate(provider, key='', *, catalog=None):
    effort = provider.get('reasoning_effort', 'default')
    if effort == 'default': return
    catalog = discover(provider, key) if catalog is None else catalog
    if catalog.get('status') != 'ready' or effort not in catalog.get('efforts', []):
        raise ValueError('该接口尚未确认支持所选推理强度，请重新读取档位或选择默认。')
