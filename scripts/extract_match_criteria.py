"""Review every collected product main image in resumable, bounded batches."""
import argparse
from pathlib import Path
import time
import uuid

import selection
from generate_category import model_json
from copy import deepcopy
from selection_automation.match_criteria import attach,seed,digest,preserve,merge_proposals,FIELD_MERGE_VERSION
from selection_automation.visual_criteria import VERSION,BATCH_SIZE,start,merge,prompt


def repair_cached_text_fields(category,snapshot,ledger):
    """Recompile saved text proposals without resending any model request."""
    previous=category.get('match_criteria') or {}
    catalog=seed(category,snapshot)
    if (ledger.get('source_fingerprint')!=catalog['source_fingerprint'] or
            previous.get('source_fingerprint')!=catalog['source_fingerprint']):
        raise ValueError('保存的参数提取记录与当前 Amazon 商品不匹配')
    proposals=[entry['proposal'] for entry in ledger.get('batches',{}).values()
               if entry.get('state') in {'complete','answered'} and isinstance(entry.get('proposal'),dict)]
    if not proposals:raise ValueError('没有可重用的参数提取结果')
    for proposal in proposals:
        text={**proposal,'items':[row for row in proposal.get('items',[])
                                 if isinstance(row,dict) and row.get('kind')=='text']}
        catalog=merge_proposals(catalog,snapshot,text)
    # Keep every already-reviewed visual fact and the user's exact selection.
    catalog['items']=[row for row in catalog['items'] if row['kind']=='text']+[
        deepcopy(row) for row in previous.get('items',[]) if row.get('kind')=='visual' or row.get('origin')=='用户补充文字要求']
    choices={row['id'] for row in previous.get('items',[]) if row.get('selected')}
    unsupported={row['id'] for row in previous.get('items',[]) if row.get('kind')=='text' and row.get('origin')=='规则建议' and not row.get('observations')}
    if choices-{row['id'] for row in catalog['items']}-unsupported:
        raise ValueError('无法还原已勾选条件，保留原任务')
    for row in catalog['items']:row['selected']=row['id'] in choices
    for name in ('visual_version','visual_mode','image_review','automatic_focus'):
        if name in previous:catalog[name]=deepcopy(previous[name])
    catalog.update(state=previous.get('state','ready'),field_merge_version=FIELD_MERGE_VERSION)
    return attach(deepcopy(category),preserve(catalog))


def extract(category,snapshot,ask=model_json,receipt=None,progress=None):
    previous=category.get('match_criteria') or {}
    catalog=start(seed(category,snapshot,previous))
    if (previous.get('source_fingerprint')==catalog['source_fingerprint'] and
            previous.get('visual_version')==VERSION and previous.get('state')=='ready'):
        if previous.get('field_merge_version')==FIELD_MERGE_VERSION:return category
        ledger=selection.read(receipt,{}) if receipt else {}
        return repair_cached_text_fields(category,snapshot,ledger)
    ledger=selection.read(receipt,{}) if receipt else {}
    if ledger.get('state')=='started' and not ledger.get('version'):
        catalog.update(state='interrupted',message='上次图片请求结果不明，已保留规格，未自动重复计费请求。')
        return attach(category,preserve(catalog))
    if ledger.get('source_fingerprint')!=catalog['source_fingerprint'] or ledger.get('version')!=VERSION:
        ledger={'version':VERSION,'source_fingerprint':catalog['source_fingerprint'],'batches':{}}
    images=catalog['image_sources'];completed=[];errors=[]
    total_asins=set(catalog['source_asins']);available={row['asin'] for row in images}
    def report(state):
        reviewed={row['asin'] for row in completed}
        catalog['image_review']={'version':VERSION,'state':state,'total_images':len(images),
            'completed_images':len(completed),'total_products':len(total_asins),
            'reviewed_products':len(reviewed),'missing_asins':sorted(total_asins-available),
            'failed_batches':len(errors)}
        catalog['state']=state
        catalog['message']=(f'第三层默认自动判断；已核对 {len(completed)}/{len(images)} 张商品主图，'
                            f'覆盖 {len(reviewed)}/{len(total_asins)} 件商品。')
        if state!='ready':catalog['message']+=' 图片提取尚未全部完成，已完成批次保留。'
        attach(category,preserve(catalog,previous))
        category['plain_stage3']='第三层自动判断同款、类似款或不同款，并给出一句关键依据。默认无需勾选；特殊的强制一致要求可在高级设置中补充。'
        if category.get('rule_summary_source')=='generated_conditions_local_summary_v1':
            category['plain_rules_summary']=category.get('plain_stage1','')+'\n'+category['plain_stage3']
        ledger.update(state=state,time=time.time())
        if receipt:selection.save(receipt,ledger)
        if progress:progress(category)
    report('processing')
    pending=[(offset,images[offset:offset+BATCH_SIZE]) for offset in range(0,len(images),BATCH_SIZE)]
    while pending:
        offset,batch=pending.pop(0);question=prompt(category,snapshot,catalog,batch)
        key=digest({'version':VERSION,'images':batch,'category':category.get('category_key'),
                    'provider':category.get('rule_generation_provider'),
                    'source':catalog['source_fingerprint'],'offset':offset})
        entry=ledger['batches'].get(key,{})
        # A provider's explicit HTTP rejection is not an unknown sent request.
        # Adapt to image-count/payload limits without replaying the rejected batch.
        if entry.get('error')=='MODEL_HTTP_400' and len(batch)>1:
            middle=len(batch)//2
            pending[:0]=[(offset,batch[:middle]),(offset+middle,batch[middle:])]
            continue
        if entry.get('state') in {'started','uncertain'}:
            errors.append('上次请求结果不明');report('partial');continue
        for attempt in range(2):
            try:
                if entry.get('state') in {'complete','answered'}:proposal=entry['proposal']
                else:
                    entry={'state':'started','request_id':uuid.uuid4().hex,'attempt':attempt+1}
                    ledger['batches'][key]=entry
                    if receipt:selection.save(receipt,ledger)
                    proposal=ask(question,max_tokens=6000,retry_truncated=False,images=[row['url'] for row in batch])
                    entry.update(state='answered',proposal=proposal)
                    if receipt:selection.save(receipt,ledger)
                catalog=merge(catalog,snapshot,batch,proposal)
                entry.update(state='complete',proposal=proposal)
                completed.extend(batch);break
            except Exception as error:
                code=getattr(error,'code','')
                if code=='MODEL_HTTP_400' and len(batch)>1:
                    entry.update(state='rejected',error=code,message=str(error)[:600])
                    middle=len(batch)//2
                    pending[:0]=[(offset,batch[:middle]),(offset+middle,batch[middle:])]
                    break
                retryable=(entry.get('state')=='answered' or code in {'MODEL_INVALID_JSON','MODEL_OUTPUT_TRUNCATED','MODEL_HTTP_429'})
                entry.update(state='failed' if retryable else 'uncertain',error=code or type(error).__name__,message=str(error)[:600])
                if attempt==0 and retryable:continue
                errors.append(entry['error']);break
        report('processing')
    report('ready' if not errors and available==total_asins and images else 'partial' if completed else 'structured')
    return category


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--category',type=Path,required=True)
    parser.add_argument('--amazon',type=Path,required=True)
    args=parser.parse_args()
    category=selection.read(args.category,{})
    snapshot=selection.read(args.amazon,{})
    if not snapshot.get('records'):raise SystemExit('Amazon 商品记录为空，尚未生成匹配条件')
    value=extract(category,snapshot,receipt=args.category.with_suffix('.criteria-request.json'),
                  progress=lambda current:selection.save(args.category,current))
    selection.save(args.category,value)
    print(value['match_criteria']['message'])
