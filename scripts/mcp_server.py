"""Small MCP stdio server. Business execution stays in the local supervisor."""
import json
import os
from pathlib import Path
import subprocess
import sys
from selection import ROOT, save, prepare, doctor


def schema(properties,required=()): return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}
STRING={'type':'string'}
TOOLS=[
 {'name':'selection_category_contract','description':'只读获取任意新品类的配置骨架和字段要求，由当前Agent生成规则；零请求零文件写入','inputSchema':schema({})},
 {'name':'selection_doctor','description':'本地依赖检查，不发起付费请求','inputSchema':schema({})},
 {'name':'selection_prepare','description':'冻结 Agent 已生成并核实榜单来源的品类配置，不调用模型','inputSchema':schema({'category':{'type':'object'}},['category'])},
 {'name':'selection_set_api_key','description':'把用户提供的 DeepSeek key 按本机用户加密保存，不返回密钥','inputSchema':schema({'api_key':STRING},['api_key'])},
 {'name':'selection_start','description':'启动采集和本地前两层；第三层必须用户在网页手动点击，paid 参数不能代替点击授权','inputSchema':schema({'category_key':STRING,'paid':{'type':'boolean'}},['category_key'])},
 {'name':'selection_status','description':'读取任务状态和结果目录','inputSchema':schema({'category_key':STRING},['category_key'])},
 {'name':'selection_stop','description':'停止本任务后台进程，保留数据和浏览器','inputSchema':schema({'category_key':STRING},['category_key'])},
 {'name':'selection_resume_api','description':'用户更新余额或API授权后，解除本任务API阻断并保留成功断点','inputSchema':schema({'category_key':STRING},['category_key'])},
 {'name':'selection_rotate','description':'替换本任务指定 worker 的 AdsPower 环境及住宅代理 session','inputSchema':schema({'category_key':STRING,'workers':{'type':'array','items':{'type':'integer','minimum':1,'maximum':32}}},['category_key','workers'])}
]


def validate_arguments(name, args):
    tool = next((tool for tool in TOOLS if tool['name'] == name), None)
    if tool is None or not isinstance(args, dict):
        raise ValueError('unknown tool or invalid arguments')
    contract = tool['inputSchema']
    properties = contract['properties']
    if set(args) - set(properties) or set(contract['required']) - set(args):
        raise ValueError('unexpected or missing arguments')
    for key, value in args.items():
        expected = properties[key]['type']
        kinds = {'string': str, 'object': dict, 'boolean': bool, 'array': list}
        if type(value) is not kinds[expected]:
            raise ValueError(f'{key} must be {expected}')
    if name == 'selection_rotate':
        workers = args['workers']
        if not workers or any(type(n) is not int or n not in range(1, 33) for n in workers):
            raise ValueError('workers must contain integers 1..32')
        if len(workers) != len(set(workers)):
            raise ValueError('workers must be unique')


def execute(name,args):
    validate_arguments(name, args)
    if name=='selection_category_contract':
        from selection_automation.category_contract import category_contract
        return category_contract()
    if name=='selection_doctor': return doctor()
    if name=='selection_prepare':
        from selection_automation.rules import validate
        cat=args['category']
        if cat.get('execution_mode')=='collection':
            from selection_automation.collection_contract import validate_collection
            validate_collection(cat)
        else:
            if 'identity_policy' in cat.get('stage3', {}):
                from selection_automation.identity_policy import compile_policy
                cat = compile_policy(cat)
            validate(cat)
        path=ROOT/'runtime'/'incoming'/f"{cat['category_key']}.json"; save(path,cat)
        frozen,_,_=prepare(path); return {'配置':str(frozen),'API请求数':0}
    if name=='selection_set_api_key':
        from selection_automation.secrets import store
        if not args['api_key'].strip(): raise ValueError('empty key')
        store(ROOT/'runtime'/'secrets'/'deepseek.dpapi',{'key':args['api_key'].strip()}); return {'状态':'已加密保存'}
    import re
    key=args['category_key']
    if not re.fullmatch('[a-z][a-z0-9_]{0,63}',key): raise ValueError('invalid category key')
    action=name.removeprefix('selection_').replace('_','-')
    if action not in ('start','status','stop','rotate','resume-api'): raise ValueError('unknown tool')
    command=[sys.executable,str(ROOT/'scripts'/'selection.py'),action]
    if action=='start':
        command+=['--category',str(ROOT/'runs'/key/'category.json')]
        if args.get('paid') is True: command+=['--paid']
    else: command+=['--category-key',key]
    if action=='rotate': command+=['--workers',','.join(map(str,args['workers']))]
    result=subprocess.run(command,cwd=ROOT,text=True,capture_output=True,encoding='utf-8',timeout=30,
                          env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'})
    if result.returncode: raise RuntimeError(result.stderr[-1500:])
    return {'结果':result.stdout.strip()}


def handle(message):
    method=message.get('method'); params=message.get('params',{})
    if method=='initialize':
        requested=params.get('protocolVersion')
        version=requested if requested in ('2024-11-05','2025-03-26','2025-06-18') else '2025-06-18'
        return {'protocolVersion':version,'capabilities':{'tools':{}},'serverInfo':{'name':'amazon-temu-selection','version':'1.0.0'}}
    if method=='ping': return {}
    if method=='tools/list': return {'tools':TOOLS}
    if method=='tools/call':
        try:
            result=execute(params['name'],params.get('arguments',{}))
            return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}
        except Exception as exc:
            return {'isError':True,'content':[{'type':'text','text':str(exc)}]}
    raise ValueError('unsupported method')


if __name__=='__main__':
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    for line in sys.stdin:
        try:
            message=json.loads(line)
            if 'id' not in message: continue
            try: result={'jsonrpc':'2.0','id':message['id'],'result':handle(message)}
            except ValueError as exc: result={'jsonrpc':'2.0','id':message['id'],'error':{'code':-32601,'message':str(exc)}}
        except (ValueError,TypeError): result={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Parse error'}}
        print(json.dumps(result,ensure_ascii=False),flush=True)
