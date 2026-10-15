"""Run one explicitly dispatched, owned desktop-agent job."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--agent',choices=['workbuddy','acciowork','doubao'],required=True)
    parser.add_argument('--job',type=Path,required=True);parser.add_argument('--model');args=parser.parse_args()
    from selection_automation.native_agent_transport import accio_run,doubao_run,workbuddy_run
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    prompt=sys.stdin.read();job=args.job.resolve()
    try:
        {'workbuddy':workbuddy_run,'acciowork':accio_run,'doubao':doubao_run}[args.agent](ROOT,job,prompt,model=args.model)
        print('Completed owned desktop-agent job.')
    except Exception as exc:
        from selection_automation.merge_temu_snapshots import atomic_write
        import json
        safe=str(exc)[:240] if type(exc) is RuntimeError and str(exc).startswith(('AccioWork','豆包','原豆包','不能把已有')) else type(exc).__name__
        atomic_write(job/'transport-error.json',json.dumps({'type':type(exc).__name__,'message':safe},ensure_ascii=False))
        # No gateway password, account token, headers or full remote payload.
        print('Desktop Agent job failed: '+type(exc).__name__,file=sys.stderr)
        raise SystemExit(1)
