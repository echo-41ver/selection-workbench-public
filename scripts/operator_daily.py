"""Local daily operator recommendations. No external messaging or paid API."""
import argparse,json,sys,time,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from selection_automation.operations import daily_tick
from selection_automation.daily_collection import collection_tick
from selection_automation.process_lock import ProcessLock
from selection_automation.merge_temu_snapshots import atomic_write


def main():
    p=argparse.ArgumentParser();p.add_argument('--watch',action='store_true');args=p.parse_args()
    folder=ROOT/'runtime/operations';folder.mkdir(parents=True,exist_ok=True)
    with ProcessLock(folder/'service.lock'):
        while True:
            try:
                collection=collection_tick(ROOT)
                result=daily_tick(ROOT)
                state={'status':'RUNNING' if args.watch else 'COMPLETE','updated_at':time.time(),'pid':os.getpid(),
                       'deliveries_checked':len(result),'collection_changes':len(collection),'error':None}
            except Exception as error:
                state={'status':'ERROR','updated_at':time.time(),'pid':os.getpid(),'error':str(error)[:500]}
            atomic_write(folder/'service.json',json.dumps(state,ensure_ascii=False))
            if not args.watch:print(json.dumps(state,ensure_ascii=False));return 0 if state['error'] is None else 1
            time.sleep(30)


if __name__=='__main__':raise SystemExit(main())
