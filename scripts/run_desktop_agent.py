"""Run one explicitly dispatched, owned desktop-agent job."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--agent',choices=['workbuddy','acciowork','doubao'],required=True)
    parser.add_argument('--job',type=Path,required=True);parser.add_argument('--model')
    parser.add_argument('--observe-only',action='store_true');args=parser.parse_args()
    from selection_automation.native_agent_transport import accio_run,doubao_run,workbuddy_run
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    prompt=sys.stdin.read();job=args.job.resolve()
    try:
        if args.observe_only:
            from selection_automation.native_agent_transport import read
            receipt=read(job/'native-session.json',{})
            if (receipt.get('agent')!=args.agent or receipt.get('model')!=args.model
                    or not (receipt.get('url') and receipt.get('marker') if args.agent=='doubao'
                            else receipt.get('conversation_id') and receipt.get('agent_id') if args.agent=='acciowork' else False)):
                raise RuntimeError('不能把已有观察任务重新发送，原会话凭据不完整')
        {'workbuddy':workbuddy_run,'acciowork':accio_run,'doubao':doubao_run}[args.agent](ROOT,job,prompt,model=args.model)
        print('Completed owned desktop-agent job.')
    except Exception as exc:
        import subprocess
        from selection_automation.agent_blockers import AgentActionRequired
        from selection_automation.native_agent_transport import read
        from selection_automation.merge_temu_snapshots import atomic_write
        import json
        safe=str(exc)[:240] if isinstance(exc,AgentActionRequired) or type(exc) is RuntimeError and str(exc).startswith(('AccioWork','豆包','原豆包','不能把已有')) else type(exc).__name__
        receipt=read(job/'native-session.json',{})
        error={'type':type(exc).__name__,'message':safe,'model_turn_started':True if args.agent=='workbuddy' else bool(receipt.get('sent_at'))}
        if args.agent=='workbuddy' and isinstance(exc,subprocess.TimeoutExpired):error['send_uncertain']=True
        if isinstance(exc,AgentActionRequired):error.update(action_required=True,code=exc.code,agent_id=exc.agent_id)
        atomic_write(job/'transport-error.json',json.dumps(error,ensure_ascii=False))
        # No gateway password, account token, headers or full remote payload.
        print('Desktop Agent job failed: '+type(exc).__name__,file=sys.stderr)
        raise SystemExit(1)
