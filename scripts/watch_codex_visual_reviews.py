"""Persistent independent-review importer and non-paid Excel publication driver."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from selection_automation.category_config import load_category
from selection_automation.codex_visual_review import import_reviews, normalize_review
from selection_automation.jsonl_records import read_jsonl
from selection_automation.process_lock import ProcessLock
from watch_stage3_excel import ensure_publisher, read, save, signature, source_paths


def import_task(category, manifest_path, base):
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    pairs = manifest['pairs']
    bound = {(row['asin'], row['goods_id']):row for row in pairs}
    if len(bound) != len(pairs):
        raise ValueError('duplicate manifest pairs')
    suffix = manifest_path.stem.removeprefix('manifest_')
    task_id = f'codex-{suffix}'
    raw_path = manifest_path.with_name(f'raw_{suffix}.jsonl')
    output = base/'stage123/codex_reviews'/f'{task_id}.jsonl'
    valid = {}
    errors = []
    warnings = []
    if raw_path.exists():
        for number, row in enumerate(read_jsonl(raw_path, allow_incomplete_tail=True, warnings=warnings), 1):
            pair = (row.get('asin'),row.get('goods_id'))
            try:
                if pair not in bound:
                    raise ValueError('pair absent from task manifest')
                normalize_review(category,row,bound[pair],task_id)
                valid[pair] = row
            except (ValueError,KeyError,TypeError) as exc:
                errors.append({'line':number,'error':str(exc)})
        outcome = import_reviews(category,manifest,list(valid.values()),output,task_id)
    else:
        outcome = {'changed':False}
    completed = sum(row.get('vision_revision_key') == bound.get((row.get('asin'),row.get('goods_id')),{}).get('vision_revision_key')
                    for row in read_jsonl(output))
    return {'task_id':task_id,'target':len(pairs),'completed':completed,'raw_exists':raw_path.exists(),
            'changed':outcome['changed'],'complete':bool(pairs) and completed==len(pairs),
            'errors':errors,'unfinished_tail':warnings}


def publication_current(category_path, base):
    # The publisher receives an absolute path; relative CLI paths must hash identically.
    category_path = Path(category_path).absolute()
    base = Path(base).absolute()
    state = read(base/'publish_state.json',{})
    return (state.get('stage3_excel') == signature(source_paths(category_path,base)) and
            bool(state.get('deliveries',{}).get('layer123')) and
            not state.get('errors',{}).get('stage3_excel') and
            not state.get('pending_views',{}).get('layer123'))


def batch_tasks(base, review_batch):
    """Observe atomic agent outputs without importing or overwriting their files."""
    batch = Path(review_batch).resolve()
    batch.relative_to((base/'nightly_review').resolve())
    manifest = json.loads((batch/'review-batch.json').read_text(encoding='utf-8-sig'))
    category_path = base / 'category.json'
    category = json.loads(category_path.read_text(encoding='utf-8-sig')) if category_path.exists() else {}
    from selection_automation.codex_visual_review import usable_review
    tasks = []
    errors = []
    for item in manifest['tasks']:
        try:
            pair_file = Path(item['manifest']).resolve()
            pair_file.relative_to(batch)
            output = Path(item['output']).resolve()
            output.relative_to((base/'stage123/codex_reviews').resolve())
            pairs = list(read_jsonl(pair_file))
            expected = {(row['asin'], row['goods_id'], row['vision_revision_key']) for row in pairs}
            if not expected or len(expected) != len(pairs):
                raise ValueError('empty or duplicate batch manifest')
            received = set()
            if output.exists():
                for row in read_jsonl(output):
                    key = (row.get('asin'), row.get('goods_id'), row.get('vision_revision_key'))
                    if key in expected and row.get('review_status') in {'SAME','DIFFERENT','UNKNOWN'} and usable_review(category, row):
                        received.add(key)
            tasks.append({'task_id':item['task_id'], 'target':len(expected),
                          'completed':len(received), 'complete':received == expected})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({'task_id':item.get('task_id'), 'error':str(exc)})
    return tasks, errors


def all_batch_tasks(base, review_batch):
    """Include new sibling batches without restarting the existing observer."""
    batch = Path(review_batch).resolve()
    folder = (base/'nightly_review').resolve()
    batch.relative_to(folder)
    tasks, errors, seen = [], [], set()
    for manifest in sorted(folder.glob('*/review-batch.json')):
        try:
            data = json.loads(manifest.read_text(encoding='utf-8-sig'))
            if manifest.parent.resolve() != batch and (data.get('schema_version') != 'codex-visual-review-batch-v1' or data.get('category_key') != base.name):
                continue
            pairs = [Path(item['manifest']).resolve() for item in data.get('tasks', [])]
            for pair_file in pairs:
                pair_file.relative_to(manifest.parent.resolve())
            # Preparation can leave an unassigned batch with empty shard files.
            if pairs and all(pair_file.is_file() and pair_file.stat().st_size == 0 for pair_file in pairs):
                continue
            rows, issues = batch_tasks(base, manifest.parent)
            errors.extend(issues)
            for row in rows:
                if row['task_id'] in seen:
                    errors.append({'task_id':row['task_id'], 'error':'duplicate batch task id'})
                    continue
                seen.add(row['task_id'])
                tasks.append(row)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            errors.append({'batch':manifest.parent.name,'error':str(exc)})
    return tasks, errors


def tick(category_path, expected_tasks=8, review_batch=None):
    category_path = Path(category_path).absolute()
    category = load_category(category_path)
    base = ROOT/'runs'/category['category_key']
    if read(base/'stage123/codex_pool_config.json',{}).get('enabled'):
        from run_codex_visual_pool import ensure_pool
        ensure_pool(category_path,root=ROOT)
    folder = base/'stage123/codex_review'
    tasks = []
    errors = []
    if review_batch is not None:
        tasks, errors = all_batch_tasks(base, review_batch)
        expected_tasks = len(tasks)
    else:
        for path in sorted(folder.glob('manifest_*.json')):
            try:
                tasks.append(import_task(category,path,base))
            except (OSError,ValueError,KeyError,TypeError) as exc:
                errors.append({'manifest':path.name,'error':str(exc)})
    all_complete = len(tasks)==expected_tasks and not errors and all(task['complete'] for task in tasks)
    try:
        ensure_publisher(category_path,root=ROOT)
        excel_current = publication_current(category_path,base)
    except (OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
        errors.append({'publisher':str(exc)})
        excel_current = False
    return {'phase':'全部独立复核与Excel发布完成' if all_complete and excel_current else '持续导入并发布独立复核',
            'tasks':tasks,'expected_tasks':expected_tasks,'completed':sum(task['completed'] for task in tasks),
            'target':sum(task['target'] for task in tasks),'all_reviews_complete':all_complete,
            'excel_current':excel_current,'errors':errors,'done':all_complete and excel_current,
            'api_requests_sent':0,'updated_at':time.time()}


def watch(category_path, interval=30, expected_tasks=8, review_batch=None):
    import psutil
    category_path = Path(category_path).absolute()
    category = load_category(category_path)
    folder = ROOT/'runs'/category['category_key']/'stage123/codex_review'
    with ProcessLock(folder/'consumer.lock'):
        process = psutil.Process()
        identity = {'pid':os.getpid(),'created':process.create_time()}
        while True:
            try:
                state = tick(category_path,expected_tasks,review_batch)
            except (OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
                state = {'phase':'导入暂缓，将自动重试','error':str(exc),'done':False,'updated_at':time.time()}
            save(folder/'consumer.json',{**identity,**state})
            if state['done'] and not read(folder.parent/'codex_pool_config.json',{}).get('enabled'):
                return state
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--category',type=Path,required=True)
    parser.add_argument('--interval',type=int,default=30)
    parser.add_argument('--expected-tasks',type=int,default=8)
    parser.add_argument('--review-batch',type=Path,help='观察本轮看图任务的清单及已归一化输出，不改写代理结果')
    args=parser.parse_args()
    try:
        watch(args.category,max(5,args.interval),max(1,args.expected_tasks),args.review_batch)
    except RuntimeError as exc:
        if str(exc) != 'Another supervisor owns this installation':
            raise


if __name__=='__main__':
    main()
