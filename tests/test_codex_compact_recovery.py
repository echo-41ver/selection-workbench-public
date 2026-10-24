import json
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_codex_visual_pool as pool
from test_compact_rules import candidate
from test_match_criteria import configured, products


def setup_job(tmp_path, monkeypatch, products, *, attempt=1, relation='S'):
    category = configured(products)
    category_path = tmp_path / 'category.json'
    category_path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(pool, 'load_category', lambda _: category)
    monkeypatch.setattr('selection_automation.category_config.load_category', lambda _: category)
    pair = {'asin': 'A1', 'goods_id': 'T1', 'vision_revision_key': 'bound-revision', 'vector_score': 80}
    for side in ('amazon', 'temu'):
        image = tmp_path / (side + '.png')
        if not image.exists():
            Image.new('RGB', (80, 60), 'red' if side == 'amazon' else 'blue').save(image)
        pair[side + '_image_path'] = str(image)
    task = {'task_id': 'original-task', 'rows': [pair],
            'output': tmp_path / 'stage123/codex_reviews/original.jsonl',
            'raw_reviews': tmp_path / 'original-raw.jsonl'}
    digest = pool._group_digest([pair])
    folder = tmp_path / 'stage123/codex_pool/jobs' / f'{digest}-{attempt}'
    folder.mkdir(parents=True)
    pool._write_jsonl(folder / 'manifest.jsonl', [pair])
    pool.bind_original_images(folder, [pair])
    pool.save(folder / 'analysis-contract.json', {'category': str(category_path), 'task_id': task['task_id']})
    pool.save(folder / 'compact-output.json', {'version': 'selected-visual-compact-v1'})
    pool.save(folder / 'compact-response.json', {'candidates': [candidate(relation=relation, condition='U' if relation == 'U' else 'S')]})
    (folder / 'reviews.jsonl').touch()
    row = {'task_id': task['task_id'], 'folder': str(folder), 'pid': -100 - attempt,
           'created': 10 + attempt, 'started_at': 20 + attempt, 'finished_at': 30 + attempt,
           'state': 'retry', 'identities': [list(pool.key(pair))], 'digest': digest,
           'attempt': attempt, 'schema_revision': pool.POOL_SCHEMA_REVISION,
           'agent_id': 'codex', 'model': 'gpt-6.1-sol'}
    pool.save(folder / 'job.json', row)
    return category, category_path, task, folder, row


@pytest.mark.parametrize('blank', ['', ' \n\t\n'])
def test_saved_compact_converts_precreated_blank_reviews(tmp_path, monkeypatch, products, blank):
    cat, _, task, job, _ = setup_job(tmp_path, monkeypatch, products)
    (job / 'reviews.jsonl').write_text(blank, encoding='utf-8')
    compact = (job / 'compact-response.json').read_bytes()
    count, errors = pool.import_job(cat, task, job)
    assert (count, errors) == (1, [])
    assert not pool.pending_rows(cat, task)
    assert (job / 'compact-response.json').read_bytes() == compact
    assert list(pool.read_jsonl(task['output']))[0]['review_status'] == 'SAME'


def test_nonempty_valid_reviews_are_not_replaced_by_compact(tmp_path, monkeypatch, products):
    from selection_automation.native_agent_transport import write_response
    cat, _, task, job, _ = setup_job(tmp_path, monkeypatch, products)
    assert write_response(job, json.dumps({'candidates': [candidate(relation='U', condition='U')]}))
    before = (job / 'reviews.jsonl').read_bytes()
    assert pool.import_job(cat, task, job) == (1, [])
    assert (job / 'reviews.jsonl').read_bytes() == before
    assert list(pool.read_jsonl(task['output']))[0]['review_status'] == 'UNKNOWN'


def recovery_pool(category_path, task, monkeypatch):
    monkeypatch.setattr(pool, 'tasks', lambda _: iter([task]))
    monkeypatch.setattr(pool, 'alive_job', lambda _: False)
    monkeypatch.setattr(pool, 'ensure_publisher', lambda *a, **kw: None)
    monkeypatch.setattr(pool, 'prepare_batch', lambda *a, **kw: {'selected': 0})
    monkeypatch.setattr(pool, 'codex_executable', lambda _: 'TEST_ONLY_CODEX')
    monkeypatch.setattr(pool.subprocess, 'Popen', lambda *a, **kw: pytest.fail('saved response must not launch a model'))
    return pool.Pool(category_path, {'enabled': True, 'workers': 1, 'group_size': 1})


def test_restart_recovers_latest_original_retry_without_new_turn(tmp_path, monkeypatch, products):
    cat, path, task, old, first = setup_job(tmp_path, monkeypatch, products)
    _, _, _, latest, second = setup_job(tmp_path, monkeypatch, products, attempt=2, relation='U')
    originals = {p: p.read_bytes() for p in (old / 'compact-response.json', latest / 'compact-response.json')}
    attempts = {first['digest']: {'count': 2, 'schema_revision': pool.POOL_SCHEMA_REVISION}}
    pool.save(tmp_path / 'stage123/codex_pool/attempts.json', attempts)
    worker = recovery_pool(path, task, monkeypatch)
    status = worker.tick()
    assert not worker.jobs and status['running'] == 0
    assert not pool.pending_rows(cat, task)
    assert list(pool.read_jsonl(task['output']))[0]['review_status'] == 'UNKNOWN'
    saved = pool.read(latest / 'job.json')
    assert saved['state'] == 'complete' and saved['imported'] == 1
    assert (saved['pid'], saved['created'], saved['attempt'], saved['finished_at']) == (
        second['pid'], second['created'], 2, second['finished_at'])
    assert pool.read(old / 'job.json')['state'] == 'retry'
    assert worker.attempts == attempts
    assert status['completed_groups'] == 1
    assert all(p.read_bytes() == data for p, data in originals.items())


def test_recovery_preserves_existing_valid_accepted_result(tmp_path, monkeypatch, products):
    from selection_automation.native_agent_transport import write_response
    cat, path, task, job, _ = setup_job(tmp_path, monkeypatch, products)
    assert write_response(job, json.dumps({'candidates': [candidate(relation='U', condition='U')]}))
    assert pool.import_job(cat, task, job) == (1, [])
    accepted = {p: p.read_bytes() for p in (task['output'], task['raw_reviews'])}
    (job / 'reviews.jsonl').write_text('', encoding='utf-8')
    worker = recovery_pool(path, task, monkeypatch)
    worker.tick()
    assert pool.read(job / 'job.json')['state'] == 'complete'
    assert all(p.read_bytes() == data for p, data in accepted.items())
    assert len(list(pool.read_jsonl(task['output']))) == 1


def test_complete_job_does_not_resurrect_older_duplicate_retry(tmp_path, monkeypatch, products):
    cat, path, task, old, _ = setup_job(tmp_path, monkeypatch, products)
    _, _, _, latest, second = setup_job(tmp_path, monkeypatch, products, attempt=2)
    assert pool.import_job(cat, task, latest) == (1, [])
    pool.save(latest / 'job.json', {**second, 'state': 'complete', 'imported': 1})
    original = (old / 'job.json').read_bytes()
    worker = recovery_pool(path, task, monkeypatch)
    status = worker.tick()
    assert not worker.jobs and status['completed_groups'] == 1
    assert (old / 'job.json').read_bytes() == original
    assert len(list(pool.read_jsonl(task['output']))) == 1


@pytest.mark.parametrize('damage', ['invalid-compact', 'changed-image', 'changed-revision', 'changed-contract', 'missing-images'])
def test_invalid_or_stale_saved_reply_is_held_without_new_turn(tmp_path, monkeypatch, products, damage):
    cat, path, task, job, _ = setup_job(tmp_path, monkeypatch, products, relation='U')
    if damage == 'invalid-compact':
        (job / 'compact-response.json').write_text('{"candidates": []}', encoding='utf-8')
    elif damage == 'changed-image':
        Image.new('RGB', (80, 60), 'green').save(task['rows'][0]['temu_image_path'])
    elif damage == 'changed-revision':
        task['rows'][0]['vision_revision_key'] = 'new-revision'
    elif damage == 'changed-contract':
        pool.save(job / 'analysis-contract.json', {'category': str(path), 'task_id': 'another-task'})
    else:
        (job / 'image-inputs.json').unlink()
    worker = recovery_pool(path, task, monkeypatch)
    if damage == 'changed-revision':
        worker.config['workers'] = 0  # A new revision requires its own future dispatch decision.
    status = worker.tick()
    saved = pool.read(job / 'job.json')
    assert saved['state'] == 'retry' and saved['imported'] == 0
    assert saved['hold_reason'] == 'SAVED_RESPONSE_INVALID'
    assert status['held_pending'] >= 1 and saved['errors']
    assert not Path(task['output']).exists()
    assert pool.pending_rows(cat, task)
