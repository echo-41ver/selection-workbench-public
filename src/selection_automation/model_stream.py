"""Read standard Chat Completions SSE without exposing reasoning or partial rules."""
import json
import time


class ModelStreamError(RuntimeError):
    def __init__(self,message,code):
        super().__init__(message)
        self.code=code


def read_chat_completion(response,notify=lambda **fields:None,*,started=None,max_seconds=600):
    if 'text/event-stream' not in (getattr(response,'headers',{}) or {}).get('Content-Type','').lower():
        return json.load(response)
    started=time.monotonic() if started is None else started
    parts=[];finish=None;done=False;lines=[];received=0;events=0;characters=0;last_update=0
    while True:
        line=response.readline(1024*1024)
        if time.monotonic()-started>max_seconds:
            raise ModelStreamError('模型生成等待超过10分钟，请手动重试，不会自动重复请求','MODEL_RESPONSE_TIMEOUT')
        if not line:break
        received+=len(line)
        if received>8*1024*1024:
            raise ModelStreamError('模型响应超过大小上限，未发布不完整规则','MODEL_STREAM_TOO_LARGE')
        text=line.decode('utf-8').strip()
        if text.startswith('data:'):lines.append(text[5:].lstrip())
        elif text:continue
        else:
            if not lines:continue
            data='\n'.join(lines);lines=[]
            if data=='[DONE]':done=True;break
            packet=json.loads(data)
            if not isinstance(packet,dict):
                raise ModelStreamError('模型流式响应结构无效，未发布规则','MODEL_STREAM_INVALID')
            if packet.get('error'):
                error=packet['error']
                detail='服务返回流式错误'
                if isinstance(error,dict):
                    detail='：'.join(str(value) for value in [error.get('code'),error.get('message')]
                        if isinstance(value,(str,int))) or detail
                raise ModelStreamError(str(detail),'MODEL_STREAM_SERVICE_ERROR')
            events+=1
            for choice in packet.get('choices',[]):
                if choice.get('index',0)!=0:continue
                content=(choice.get('delta') or {}).get('content')
                if isinstance(content,str):parts.append(content);characters+=len(content)
                if choice.get('finish_reason'):finish=choice['finish_reason']
            now=time.monotonic()
            if events==1 or now-last_update>=3:
                notify(events=events,characters=characters);last_update=now
    if not (done or finish):
        notify(events=events,characters=characters)
        raise ModelStreamError('模型回答中途断开，规则尚未完成；服务可能已计费，请先核对调用记录，不会自动重发','MODEL_STREAM_INTERRUPTED')
    notify(events=events,characters=characters)
    return {'choices':[{'finish_reason':finish,'message':{'content':''.join(parts)}}]}
