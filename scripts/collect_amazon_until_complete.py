"""Keep the same Amazon checkpoint retrying until complete or explicitly stopped."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from selection_automation.process_lock import ProcessLock
from selection_automation.browser_transport import amazon_environment


def read(path):
    try:return json.loads(path.read_text('utf-8-sig'))
    except (OSError,ValueError):return {}


@contextmanager
def shared_collector_lock(path,output,stop):
    while True:
        lock=ProcessLock(path)
        try:lock.__enter__();break
        except RuntimeError as error:
            if str(error)!='Another supervisor owns this installation':raise
            if stop.exists() or read(output).get('status')=='COMPLETE':
                yield False;return
            time.sleep(1)
    try:yield True
    finally:lock.__exit__(None,None,None)


def collect(category_path):
    category_path=Path(category_path).resolve();category=read(category_path)
    base=ROOT/'runs'/category['category_key']
    if category_path!=base.resolve()/'category.json':raise ValueError('Expected this installation frozen run category')
    output=base/'amazon/amazon_frontend_latest.json';output.parent.mkdir(parents=True,exist_ok=True)
    saved_path=read(ROOT/'runtime/web-console/service-path-8765.json').get('PATH')
    if saved_path:os.environ['PATH']=saved_path
    env={**os.environ,**amazon_environment(ROOT),'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    env.pop('DEEPSEEK_API_KEY',None)
    with shared_collector_lock(output.parent/'continuous-collector.lock',output,base/'STOP') as owns:
        if not owns:return 0
        while not (base/'STOP').exists():
            payload=read(output)
            if (payload.get('status')=='COMPLETE' and payload.get('details_complete') is True
                    and len(payload.get('records',[]))==category['amazon']['target_count']):return 0
            queue=(read(Path(str(output)+'.checkpoint.json')).get('detail_queue') or payload.get('detail_queue') or {})
            if queue.get('manual_action_asins'):
                # Retain a real gate for the user; never claim those details complete.
                time.sleep(1);continue
            print('Amazon 未完成详情恢复持续补采；保留成功商品及累计请求记录。',flush=True)
            child=subprocess.Popen([shutil.which('node') or 'node',str(ROOT/'scripts/collect_amazon_search_frontend.mjs'),
                '--config',str(category_path),'--output',str(output)],cwd=ROOT,env=env,
                stdout=sys.stdout,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                while child.poll() is None:
                    if (base/'STOP').exists():
                        from selection import terminate_owned
                        terminate_owned(child);break
                    time.sleep(1)
                child.wait()
            finally:
                if child.poll() is None:child.terminate();child.wait()
            if read(output).get('status')=='COMPLETE':continue
            # Also recover process/network crashes without a finite retry budget.
            for _ in range(30):
                if (base/'STOP').exists():return 0
                time.sleep(1)
    return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--category',type=Path,required=True)
    args=parser.parse_args()
    log=ROOT/'runs'/read(args.category)['category_key']/'logs/amazon-continuous.log'
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('a',encoding='utf-8',buffering=1) as stream:
        sys.stdout=sys.stderr=stream
        raise SystemExit(collect(args.category))
