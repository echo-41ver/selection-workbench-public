import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from watch_codex_visual_reviews import all_batch_tasks


def test_new_sibling_batch_is_counted_without_observer_restart(tmp_path):
    base=tmp_path/'pump_task'
    outputs=base/'stage123/codex_reviews'
    outputs.mkdir(parents=True)
    for index,completed in [(1,True),(2,False)]:
        batch=base/'nightly_review'/f'batch-{index}'
        batch.mkdir(parents=True)
        pair={'asin':f'A{index}','goods_id':f'G{index}','vision_revision_key':f'V{index}'}
        pairs=batch/'manifest.jsonl'
        pairs.write_text(json.dumps(pair)+'\n')
        output=outputs/f'{index}.jsonl'
        if completed:
            output.write_text(json.dumps({**pair,'review_status':'SAME'})+'\n')
        (batch/'review-batch.json').write_text(json.dumps({
            'schema_version':'codex-visual-review-batch-v1','category_key':base.name,
            'tasks':[{'task_id':f'task-{index}','manifest':str(pairs),'output':str(output)}]}))
    tasks,errors=all_batch_tasks(base,base/'nightly_review/batch-1')
    assert not errors
    assert len(tasks)==2
    assert sum(t['completed'] for t in tasks)==1
    assert sum(t['target'] for t in tasks)==2
    output.write_text(json.dumps({**pair,'review_status':'UNKNOWN'})+'\n')
    tasks,errors=all_batch_tasks(base,base/'nightly_review/batch-1')
    assert sum(t['completed'] for t in tasks)==2
    assert all(t['complete'] for t in tasks)


def test_unassigned_empty_batch_does_not_block_completed_work(tmp_path):
    base=tmp_path/'pump_task'
    outputs=base/'stage123/codex_reviews'
    outputs.mkdir(parents=True)
    for name, empty in [('unused', True), ('active', False)]:
        batch=base/'nightly_review'/name
        batch.mkdir(parents=True)
        pair={'asin':'A1','goods_id':'G1','vision_revision_key':'V1'}
        pairs=batch/'manifest.jsonl'
        pairs.write_text('' if empty else json.dumps(pair)+'\n')
        output=outputs/f'{name}.jsonl'
        if not empty:
            output.write_text(json.dumps({**pair,'review_status':'SAME'})+'\n')
        (batch/'review-batch.json').write_text(json.dumps({
            'schema_version':'codex-visual-review-batch-v1','category_key':base.name,
            'tasks':[{'task_id':name,'manifest':str(pairs),'output':str(output)}]}))
    tasks,errors=all_batch_tasks(base,base/'nightly_review/active')
    assert not errors
    assert len(tasks)==1
    assert tasks[0]['complete']
    # A partly prepared or corrupt active batch must still report an error.
    (base/'nightly_review/unused/manifest.jsonl').write_text('invalid json\n')
    _,errors=all_batch_tasks(base,base/'nightly_review/active')
    assert errors
