"""One bounded generation call over the current task's actual Amazon products."""
import argparse
from pathlib import Path
import time
import uuid

import selection
from generate_category import model_json
from selection_automation.match_criteria import attach,seed,merge_proposals,proposal_prompt,digest


def extract(category,snapshot,ask=model_json,receipt=None):
    previous=category.get('match_criteria')
    catalog=seed(category,snapshot,previous)
    if previous and previous.get('source_fingerprint')==catalog['source_fingerprint'] and previous.get('state')=='ready':
        return category
    if receipt and selection.read(receipt,{}).get('state')=='started':
        catalog.update(state='interrupted',message='上次特征生成中断，未自动重发；实际规格和原选择已保留。')
        catalog['revision']=digest({k:v for k,v in catalog.items() if k!='revision'})
        return attach(category,catalog)
    if receipt:selection.save(receipt,{'state':'started','time':time.time(),'request_id':uuid.uuid4().hex,'source_fingerprint':catalog['source_fingerprint']})
    try:
        proposal=ask(proposal_prompt(category,snapshot,catalog),max_tokens=16000,retry_truncated=False,
                     images=[row['url'] for row in catalog['image_sources']])
        catalog=merge_proposals(catalog,snapshot,proposal)
        if receipt:selection.save(receipt,{'state':'complete','time':time.time(),'source_fingerprint':catalog['source_fingerprint']})
    except Exception:
        catalog.update(state='structured',message='图片和语义特征生成未完成；已保留实际规格，未将未核实建议冒充图片证据。')
        catalog['revision']=digest({k:v for k,v in catalog.items() if k!='revision'})
        if receipt:selection.save(receipt,{'state':'failed','time':time.time(),'source_fingerprint':catalog['source_fingerprint']})
    return attach(category,catalog)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--category',type=Path,required=True)
    parser.add_argument('--amazon',type=Path,required=True)
    args=parser.parse_args()
    category=selection.read(args.category,{})
    snapshot=selection.read(args.amazon,{})
    if not snapshot.get('records'):raise SystemExit('Amazon 商品记录为空，尚未生成匹配条件')
    value=extract(category,snapshot,receipt=args.category.with_suffix('.criteria-request.json'))
    selection.save(args.category,value)
    print('已生成实际 Amazon 商品的匹配条件表')
