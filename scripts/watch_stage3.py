"""Independent, authorized incremental third-layer consumer; no browser ownership."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'scripts'))
from selection_automation.process_lock import ProcessLock
from selection_automation.merge_temu_snapshots import atomic_write
from selection_automation.stage3_progress import candidate_path, snapshot
from watch_stage3_excel import ensure_publisher


def failure_state(folder, launched_at, provider):
    result={'phase':'分析暂停，请查看错误记录','error':True}
    if provider.get('mode')!='local_model':return result
    try:
        row=json.loads((folder/'local_model_error.json').read_text('utf-8'))
        from selection_automation.local_visual_wire import ERROR_CODE, ERROR_MESSAGE
        if (row.get('code')==ERROR_CODE and row.get('model')==provider.get('model')
                and type(row.get('time')) in (int,float) and row['time']>=launched_at):
            return {'phase':ERROR_MESSAGE,'error':True,'error_code':ERROR_CODE}
        from selection_automation.local_d1 import native_endpoint, IMAGE_BASE64_LIMIT_CODE, IMAGE_BASE64_LIMIT_MESSAGE
        if (native_endpoint(provider) and row.get('code')==IMAGE_BASE64_LIMIT_CODE
                and row.get('model')==provider.get('model')
                and type(row.get('time')) in (int,float) and row['time']>=launched_at):
            return {'phase':IMAGE_BASE64_LIMIT_MESSAGE,'error':True,'error_code':IMAGE_BASE64_LIMIT_CODE}
    except (OSError,ValueError,AttributeError):pass
    return result


def watch(category_path):
    import psutil
    category=json.loads(category_path.read_text(encoding='utf-8'))
    base=ROOT/'runs'/category['category_key']; folder=base/'stage123'
    folder.mkdir(parents=True,exist_ok=True)
    def state(phase, **extra):
        atomic_write(folder/'controller.json',json.dumps({'pid':os.getpid(),
            'created':psutil.Process().create_time(),'phase':phase,'updated_at':time.time(),**extra},ensure_ascii=False))
    with ProcessLock(folder/'controller.lock'):
        state('等待新增候选')
        def maintain_publisher():
            try:
                ensure_publisher(category_path, ROOT)
            except OSError as exc:
                print(f'Excel后台发布启动暂缓：{exc}', flush=True)
        maintain_publisher()
        try:
            while (folder/'paid_enabled.json').exists() and not (folder/'STOP').exists():
                from selection_automation.schema_billing_retry import read_authorization
                repair=read_authorization(folder)
                if repair and repair['claimed']:
                    state('本次单对复验预算已使用；原费用仍待核对，未派发其他候选');return
                from selection_automation.stage3_runtime import dispatch_allowed
                if not dispatch_allowed(folder):
                    state('已暂停派发，结果和断点已保存');time.sleep(2);continue
                category=json.loads(category_path.read_text(encoding='utf-8'))
                from selection_automation.stage3_provider import from_category
                provider=from_category(category)
                if provider['mode']=='local_agent':
                    state('已切换至本机 Agent，断点保留');return
                progress=snapshot(base,category,history=False)
                if progress.get('billing_hold_fail_closed'):
                    state('分析暂停：请求费用记录无法对应，请核对用量',error=True); return
                if not progress.get('runnable_pending', progress['pending']) and progress.get('billing_unknown_pairs'):
                    state(f"可运行候选已处理；{progress['billing_unknown_pairs']} 对费用待核对，未自动重发"); return
                if not progress['pending']:
                    state('当前候选已处理，等待新增候选' if progress['total'] else '等待前两层产生候选')
                    time.sleep(2); continue
                state('分析中')
                launched_at=time.time()
                child=subprocess.Popen([sys.executable,str(ROOT/'scripts/run_category_layer3.py'),
                    '--category',str(category_path),'--candidates',str(candidate_path(base)),
                    '--results',str(folder/'results.jsonl'),'--independent'],cwd=ROOT)
                while True:
                    try:
                        returncode=child.wait(timeout=30)
                        break
                    except subprocess.TimeoutExpired:
                        maintain_publisher()
                if returncode:
                    failure=failure_state(folder,launched_at,provider)
                    state(failure.pop('phase'),**failure); return
                maintain_publisher()
                if repair:
                    state('本次单对复验已结束；原费用仍待核对，未派发其他候选');return
                time.sleep(1)
            state('已停止，结果和断点已保留')
        except Exception:
            state('第三层运行失败，请查看日志',error=True); raise
        finally:
            # A terminal controller state lets the publisher flush even on a billing hold.
            maintain_publisher()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--category',type=Path,required=True)
    watch(p.parse_args().category)
