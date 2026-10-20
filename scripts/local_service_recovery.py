"""Recover only local union/stage-1/2 watchers; never send model requests."""
from pathlib import Path
import inspect
import json
import re
import sqlite3
import time


def stage12_contract_unchanged(root, base):
    """Read the complete saved contract before permitting a watcher reopen."""
    from selection_automation import rules
    from selection_workflow.incremental_stage12 import declarative_text_revision,VECTOR_REVISION
    config_path=base/'stage12_config.json'
    config=json.loads(config_path.read_text('utf-8'))
    output=Path(config['output_dir'])
    if not output.is_absolute():output=root/output
    if not output.resolve().is_relative_to(base.resolve()):
        return False,'第一二层输出目录不属于当前任务'
    gate=config.get('text_gate','')
    if not gate.startswith('declarative:') or Path(gate.split(':',1)[1]).resolve()!=(base/'category.json').resolve():
        return False,'无法确认当前任务的第一二层规则来源'
    category=rules.read_frozen(base/'category.json')
    revision=declarative_text_revision(category)
    expected={'schema':'stage12-processing-contract-v1','text_gate':gate,'text_gate_revision':revision,
        'rule_version':config.get('stage12_rule_version') or config.get('rule_version'),
        'vector_revision':VECTOR_REVISION,'candidate_threshold':float(config['candidate_threshold']),
        'cosine_policy':'float64-normalized-unrounded-threshold-v2'}
    con=sqlite3.connect((output/'stage12.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)
    try:row=con.execute("SELECT value FROM processing_metadata WHERE key='stage12_contract'").fetchone()
    finally:con.close()
    return (True,'第一二层规则、向量版本和阈值未变，保留全部已有结果') if row and json.loads(row[0])==expected else (
        False,'第一二层规则或阈值已变更，需确认后再重算；现有候选和结果保留')


def service_alive(root, base, name):
    import psutil
    from restart_web import native_process_active
    from selection import read
    record=read(base/('temu/union/watch_state.json' if name=='temu_union' else 'stage12/stage12_watch_owner.json'),{})
    pid=record.get('owner_pid') if name=='temu_union' else record.get('pid')
    if type(pid) is not int:return False
    try:
        process=psutil.Process(pid)
        if not native_process_active(pid):return False
        created=record.get('owner_created') if name=='temu_union' else record.get('created')
        if created is not None and abs(process.create_time()-created)>=.01:return False
        args=process.cmdline()
        script=root/'scripts'/('watch_temu_union.py' if name=='temu_union' else 'run_incremental_stage12.py')
        flag='--category' if name=='temu_union' else '--config'
        target=base/('category.json' if name=='temu_union' else 'stage12_config.json')
        return (any(arg.endswith(script.name) and (root/arg).resolve()==script.resolve() for arg in args)
                and args.count(flag)==1 and (root/args[args.index(flag)+1]).resolve()==target.resolve())
    except psutil.AccessDenied:return None  # Unknown ownership is never a restart permission.
    except (psutil.NoSuchProcess,IndexError):return False


def child_alive(child):
    from restart_web import native_process_active
    import psutil
    if child.poll() is not None:return False
    try:return native_process_active(child.pid)
    except psutil.NoSuchProcess:return False


def apply_local_recovery(root, base, details):
    """Prefer verified recovery over the old supervisor's retained failure."""
    from selection import read
    state=read(base/'local_service_recovery.json',{})
    if not 0<=time.time()-state.get('updated_at',0)<40:return
    for name,recovery in state.get('services',{}).items():
        if name not in ('temu_union','stage12'):continue
        row=details.setdefault('阶段',{}).setdefault(name,{})
        if recovery.get('status')=='RUNNING':
            if not service_alive(root,base,name):continue
            if name=='temu_union':
                from watch_temu_union import watch_health
                health=watch_health(base)
                if health.get('stale') or not health:continue
                row.update(health)
            else:row.update(状态='本机后台程序已自动恢复',阻断=False)
            for field in ('recovery_exhausted','需人工介入','human_action_required','退出码'):
                row.pop(field,None)
            for field in ('阻断阶段','人工介入阶段'):
                if isinstance(details.get(field),list):details[field]=[x for x in details[field] if x!=name]
        elif recovery.get('status') in ('STARTING','RETRYING'):
            row.update(状态=recovery['message'],automatic_recovery=True,需人工介入=False,
                       human_action_required=False,建议动作='本机程序自动接续；已有数据、候选和完成结果保留。')
        elif recovery.get('human_action_required'):
            row.update(状态=recovery['message'],需人工介入=True,automatic_recovery=False)


class LocalServiceRecovery:
    def __init__(self, root, owner, *, launch=None, alive=service_alive, contract=stage12_contract_unchanged, child_running=child_alive):
        self.root=Path(root);self.owner=owner;self.alive=alive;self.contract=contract
        from selection import spawn
        self.launch=launch or spawn;self.children={};self.retry={}
        self.child_running=child_running

    def step(self):
        from selection import read,save_progress
        owner=self.owner() or {};key=owner.get('category_key','')
        if not re.fullmatch('[a-z][a-z0-9_]{0,63}',key):return
        base=self.root/'runs'/key
        if not (base/'category.json').is_file() or (base/'STOP').exists():return
        category=read(base/'category.json',{})
        services={}
        for name in ('temu_union','stage12'):
            if name=='stage12' and (category.get('execution_mode')=='collection' or not (base/'stage12/stage12.sqlite3').is_file()):continue
            if name=='stage12' and (base/'stage12/STOP').exists():continue
            identity=(key,name);now=time.time()
            live=self.alive(self.root,base,name)
            if live is None:
                services[name]={'status':'RETRYING','message':'正在确认本机进程身份，不启动重复进程','automatic_recovery':True}
                continue
            if live:
                services[name]={'status':'RUNNING','message':'本机后台程序正在运行','automatic_recovery':True}
                self.retry.pop(identity,None);continue
            child=self.children.get(identity)
            if child is not None and self.child_running(child):
                services[name]={'status':'STARTING','message':'正在自动恢复，等待后台进程就绪','automatic_recovery':True}
                continue
            retry=self.retry.get(identity,{});attempts=retry.get('attempts',0)
            if now<retry.get('next_at',0):
                services[name]={'status':'RETRYING','message':'本机程序正在自动恢复，稍后重试','automatic_recovery':True,
                                'attempts':attempts,'next_at':retry['next_at']}
                continue
            try:
                if name=='stage12':
                    allowed,reason=self.contract(self.root,base)
                    if not allowed:
                        services[name]={'status':'ACTION_REQUIRED','message':reason,'human_action_required':True}
                        continue
                # Watchers acquire OS locks too, so the original supervisor and
                # this repair thread cannot create duplicate active writers.
                script='watch_temu_union.py' if name=='temu_union' else 'run_incremental_stage12.py'
                args=['--category',str(base/'category.json')] if name=='temu_union' else ['watch','--config',str(base/'stage12_config.json')]
                cmd=[str(self.root/'.venv/Scripts/python.exe'),str(self.root/'scripts'/script),*args]
                self.children[identity]=self.launch(cmd,base/'logs'/f'{name}_local_recovery.log')
                attempts+=1
                self.retry[identity]={'attempts':attempts,'next_at':now+min(300,15*2**min(attempts-1,5))}
                services[name]={'status':'STARTING','message':'正在由本机程序自动恢复，已有数据和断点保留',
                                'automatic_recovery':True,'attempts':attempts}
            except (OSError,ValueError,KeyError,sqlite3.Error) as error:
                attempts+=1
                self.retry[identity]={'attempts':attempts,'next_at':now+min(300,15*2**min(attempts-1,5))}
                services[name]={'status':'RETRYING','message':'本机程序正在重试恢复','automatic_recovery':True,
                                'attempts':attempts,'error':str(error)[:500]}
        save_progress(base/'local_service_recovery.json',{'services':services,'updated_at':time.time(),
            'executor':'LOCAL_PROGRAM','model_requests':0})

    def run(self, stopping):
        while not stopping.is_set():
            try:self.step()
            except Exception as error:print('Local watcher recovery deferred: '+type(error).__name__,flush=True)
            stopping.wait(10)
