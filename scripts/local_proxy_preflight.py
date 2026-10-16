"""Read-only local discovery or saved-owner check; no external target requests."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from selection_automation.local_proxy import discover
from selection_automation.local_proxy_prepare import verify_saved

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--validate-saved',action='store_true');parser.add_argument('--route-change-check',action='store_true')
    args=parser.parse_args()
    if args.route_change_check:
        from selection_automation.route_guard import collection_active
        ready=not collection_active(ROOT)
        print(json.dumps({'ready':ready},ensure_ascii=False))
        return 0 if ready else 1
    if args.validate_saved:
        try:settings=json.loads((ROOT/'runtime/secrets/proxyshare.settings.json').read_text(encoding='utf-8-sig'))
        except (OSError,ValueError):settings={};ready=False
        else:ready=verify_saved(settings)
        print(json.dumps({'ready':ready,'message':'本机出口归属已核对' if ready else '出口端口归属尚未确认；请先在空闲状态准备环境'},ensure_ascii=False))
        return 0 if ready else 1
    print(json.dumps(discover(),ensure_ascii=False));return 0
if __name__=='__main__':raise SystemExit(main())
