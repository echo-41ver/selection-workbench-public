"""Replay saved visual facts without requesting a model or changing checkpoints."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from selection_automation.rules import score


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replay(path):
    row=json.loads(path.read_text(encoding='utf-8'))
    result={'source':str(path.resolve()),'source_sha256':digest(path),'sample':row.get('sample'),
            'api_requests':0,'production_cache_promoted':False}
    try:
        # Use the frozen category stored beside the response, never today's
        # generated rules or an unrelated category selected by its display name.
        category_path=path.parent/Path(str(row['category_path']).replace('\\','/')).name
        expected=row.get('category_sha256')
        if not expected or digest(category_path)!=expected:
            raise ValueError('Frozen category checksum missing or mismatched')
        category=json.loads(category_path.read_text(encoding='utf-8'))
        decision=score(category,copy.deepcopy(row['facts']))
        state=decision['state']
        actual=('SAME' if state in {'确认同款','同款（存在规格变体）'} else
                'DIFFERENT' if state=='不同款' else 'UNRESOLVED')
        result.update(status='REPLAYED',category=category.get('category_name_zh',category.get('category_key')),
                      state=state,actual=actual,reason=decision['reason'],
                      saved_actual=row.get('actual'),saved_state=row.get('state'),
                      decision_changed=actual!=row.get('actual') or state!=row.get('state'))
        # Reference labels are read only after scoring; they cannot affect it.
        reference=row.get('reference')
        if reference in {'SAME','DIFFERENT'}:
            result.update(reference=reference,reference_basis=row.get('reference_basis'),
                          reference_result='UNRESOLVED' if actual=='UNRESOLVED' else
                          'MATCH' if actual==reference else 'MISMATCH')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result.update(status='REJECTED',error=str(exc))
    return result


def audit(paths):
    rows=[replay(path) for path in paths]
    return {'api_requests':0,'production_cache_promoted':False,
            'scope':'Saved facts replay; model observation accuracy is not re-tested',
            'input_count':len(rows),'empty_input':not rows,
            'counts':{key:sum(r.get('reference_result')==key for r in rows)
                      for key in ('MATCH','MISMATCH','UNRESOLVED')},
            'rejected':sum(r['status']=='REJECTED' for r in rows),'results':rows}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    report=audit(sorted(args.directory.glob('result_*.json')))
    text=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:args.output.write_text(text,encoding='utf-8')
    print(text)
    if report['rejected'] or report['empty_input']:raise SystemExit(1)
