"""Native Responses SSE: only completed output may become category rules."""
import time
from .model_stream import ModelStreamError
from .model_parse_diagnostic import parse_json, read_response_json


def _text(response):
    return ''.join(part.get('text','') for item in response.get('output',[])
        if item.get('type')=='message' and item.get('role')=='assistant'
        for part in item.get('content',[]) if part.get('type')=='output_text')


def _result(response):
    if response.get('status')=='incomplete':
        reason=(response.get('incomplete_details') or {}).get('reason')
        if reason=='max_output_tokens':
            return {'choices':[{'finish_reason':'length','message':{'content':_text(response)}}]}
    if response.get('status')!='completed':
        raise ModelStreamError('模型未完成回答；服务可能已计费，请先核对调用记录，不会自动重发','MODEL_STREAM_INTERRUPTED')
    return {'choices':[{'finish_reason':'stop','message':{'content':_text(response)}}]}


def _metadata(response):
    result={};identifier=response.get('id')
    if isinstance(identifier,str) and len(identifier)<=200:result['response_id']=identifier
    usage=response.get('usage') or {}
    if isinstance(usage,dict):
        result['model_usage']={k:v for k,v in usage.items() if k in ('input_tokens','output_tokens','total_tokens') and type(v) is int}
    return result


def read_responses(response,notify=lambda **fields:None,*,started=None,max_seconds=600):
    if 'text/event-stream' not in (getattr(response,'headers',{}) or {}).get('Content-Type','').lower():
        packet=read_response_json(response);notify(**_metadata(packet));return _result(packet)
    started=time.monotonic() if started is None else started
    lines=[];events=0;characters=0;received=0;last_update=0;metadata={}
    while True:
        line=response.readline(1024*1024)
        if time.monotonic()-started>max_seconds:
            raise ModelStreamError('模型读取等待超过10分钟；可能已计费，不会自动重复请求','MODEL_RESPONSE_TIMEOUT')
        if not line:break
        received+=len(line)
        if received>8*1024*1024:raise ModelStreamError('模型响应超过大小上限，未发布规则','MODEL_STREAM_TOO_LARGE')
        value=line.decode('utf-8').strip()
        if value.startswith('data:'):lines.append(value[5:].lstrip())
        elif value:continue
        else:
            if not lines:continue
            data='\n'.join(lines);lines=[]
            if data=='[DONE]':break
            packet=parse_json(data,'stream_packet_json')
            if not isinstance(packet,dict):raise ModelStreamError('模型流式响应结构无效','MODEL_STREAM_INVALID')
            events+=1;kind=packet.get('type','');record=packet.get('response') or {}
            if isinstance(record,dict):metadata.update(_metadata(record))
            if kind=='response.output_text.delta' and isinstance(packet.get('delta'),str):characters+=len(packet['delta'])
            now=time.monotonic()
            if events==1 or now-last_update>=3 or kind in ('response.completed','response.incomplete','response.failed','error'):
                notify(events=events,characters=characters,model_event=kind,**metadata);last_update=now
            if kind in ('response.completed','response.incomplete'):return _result(record)
            if kind in ('error','response.failed'):
                error=packet.get('error') or record.get('error') or packet
                detail=error.get('message','服务返回错误') if isinstance(error,dict) else '服务返回错误'
                raise ModelStreamError(str(detail),'MODEL_STREAM_SERVICE_ERROR')
    notify(events=events,characters=characters,**metadata)
    raise ModelStreamError('模型连接在完整回答前断开；可能已计费，请先核对调用记录，不会自动重发','MODEL_STREAM_INTERRUPTED')
