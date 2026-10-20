"""DeepSeek's official read-only Search and Fetch APIs (2026-10-17)."""
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .model_transport import open_model_url


class WebLookupError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def supported(provider):
    url = urlsplit(provider['base_url'])
    return url.scheme == 'https' and url.hostname == 'api.deepseek.com' and url.port in (None,443)


def request(tool, payload, key, provider):
    if tool not in ('search','fetch') or not supported(provider):
        raise WebLookupError('当前服务不是 DeepSeek 官方接口，未向其他服务发送密钥')
    if not key:
        raise WebLookupError('缺少 DeepSeek API Key')
    for attempt in range(3):
        req = urllib.request.Request('https://api.deepseek.com/v1/tools/'+tool,
            data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json',
                     'User-Agent':'selection-workbench/1.0'})
        try:
            with open_model_url(req,timeout=60) as response:
                result=json.loads(response.read(8*1024*1024))
            if not isinstance(result,dict):
                raise WebLookupError('DeepSeek 联网接口返回结构无效')
            return result
        except urllib.error.HTTPError as error:
            if error.code in (408,429,500,502,503,504) and attempt<2:
                time.sleep(attempt+1)
                continue
            try:
                detail=json.loads(error.read(4096)).get('error',{}).get('message','')
            except (ValueError,AttributeError):detail=''
            detail=re.sub(r'(?i)Bearer\s+[^\s,;]+|sk[-_][A-Za-z0-9_-]{8,}','[密钥已隐藏]',str(detail).replace(key,'[密钥已隐藏]'))
            raise WebLookupError(f'DeepSeek 联网接口 HTTP {error.code}：{detail[:180]}',status=error.code) from None
        except (urllib.error.URLError,OSError) as error:
            if attempt<2:
                time.sleep(attempt+1)
                continue
            raise WebLookupError('DeepSeek 联网接口连接失败：'+str(error).replace(key,'[密钥已隐藏]')[:180]) from None
        except (ValueError,TypeError):
            raise WebLookupError('DeepSeek 联网接口返回无效 JSON') from None


def search(query, key, provider, *, original=''):
    payload={'query':query[:128],'count':10,'need_content':True}
    if original:payload['ori_question']=original[:128]
    result=request('search',payload,key,provider)
    if not isinstance(result.get('results'),list):
        raise WebLookupError('DeepSeek 搜索结果缺少 results 列表')
    return result['results']


def fetch(url, key, provider):
    result=request('fetch',{'url':url},key,provider)
    if not isinstance(result.get('content'),str):
        raise WebLookupError('DeepSeek 网页抓取缺少 content 字段')
    return result
