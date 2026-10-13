import json
from pathlib import Path
import sqlite3
import sys

import pytest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from prepare_codex_visual_batch import prepare_batch
from selection_automation.vision import VisionRevisionContext, compatible_vision_keys
from test_set_inventory_identity import configured, facts


def setup(tmp_path, monkeypatch, rows):
    cat = configured()
    cat['category_key'] = 'pump_fixture'
    cat['stage2']['candidate_threshold'] = 60
    cat['stage3']['candidate_admission_threshold'] = 70
    cat['stage3']['reasoning_effort'] = 'medium'
    base = tmp_path / 'runs' / cat['category_key']
    base.mkdir(parents=True)
    category_path = base / 'category.json'
    category_path.write_text(json.dumps(cat), encoding='utf-8')
    stage12_dir = base / 'stage12'
    stage12_dir.mkdir()
    (base / 'stage12_config.json').write_text(json.dumps({'output_dir': str(stage12_dir)}), encoding='utf-8')
    image_paths = []
    payloads = []
    for item in rows:
        index = item['index']
        paths = []
        for side, color in (('amazon', (220, 30, 30)), ('temu', (30, 30, 220))):
            path = tmp_path / f'{index}-{side}.png'
            if not item.get('missing') or item['missing'] != side:
                Image.new('RGB', (80, 60), color).save(path)
            paths.append(str(path))
        image_paths.extend(paths)
        payloads.append({
            'asin': item['asin'], 'goods_id': f'G{index}', 'vector_score': item.get('score', 90),
            'pair_revision': f'pair-{index}', 'amazon_image_path': paths[0], 'temu_image_path': paths[1],
            'amazon_image_url': f'https://example.test/a{index}.jpg', 'temu_image_url': f'https://example.test/t{index}.jpg',
            'facts': {'seed': index}, 'amazon_title': f'Pump {item["asin"]}', 'temu_title': f'Pump {index}',
        })
    database = stage12_dir / 'stage12.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE candidates (path_key TEXT, asin TEXT, goods_id TEXT, pair_revision TEXT, payload_json TEXT, queued_at TEXT)')
        for payload in payloads:
            connection.execute('INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?)',
                               (cat['category_key'], payload['asin'], payload['goods_id'], payload['pair_revision'],
                                json.dumps(payload), 'now'))
    return cat, base, category_path, payloads


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def test_prepares_revision_bound_shards_with_contacts_and_batch_watcher_shape(tmp_path, monkeypatch):
    rows = [{'index': i, 'asin': f'A{i % 3}', 'score': 90 - i} for i in range(1, 7)]
    cat, base, category_path, payloads = setup(tmp_path, monkeypatch, rows)
    before = (base / 'stage12' / 'stage12.sqlite3').read_bytes()
    output = base / 'nightly_review' / 'first'
    summary = prepare_batch(category_path, shards=3, per_shard=2, output_dir=output)
    assert summary['selected'] == 6 and summary['shard_targets'] == [2, 2, 2]
    all_rows = []
    for number in range(1, 4):
        shard = output / f'shard-{number}'
        manifest = read_jsonl(shard / 'manifest.jsonl')
        all_rows.extend(manifest)
        assert (shard / 'raw_reviews.jsonl').read_text(encoding='utf-8') == ''
        guide = json.loads((shard / 'visual-facts-guide.json').read_text(encoding='utf-8'))
        assert set(guide['facts_output_template']['modules']) == set(cat['stage3']['modules'])
        assert guide['facts_output_template']['identity']['design_comparison']['relation'] == 'UNKNOWN'
        contacts = sorted((shard / 'contacts').glob('*.jpg'))
        assert len(contacts) == 1 and Image.open(contacts[0]).size == (640, 388)
    assert len({(row['asin'], row['goods_id']) for row in all_rows}) == 6
    assert {row['asin'] for row in all_rows} == {'A0', 'A1', 'A2'}
    context = VisionRevisionContext(cat)
    for row in all_rows:
        payload = next(p for p in payloads if p['asin'] == row['asin'] and p['goods_id'] == row['goods_id'])
        assert row['vision_revision_key'] == compatible_vision_keys(cat, payload, context=context)[0]
        assert row['vector_score'] == payload['vector_score'] and row['facts'] == payload['facts']
    batch = json.loads((output / 'review-batch.json').read_text(encoding='utf-8'))
    assert len(batch['tasks']) == 3
    for task in batch['tasks']:
        assert Path(task['manifest']).is_file() and Path(task['raw_reviews']).is_file()
        assert str(base / 'stage123' / 'codex_reviews') in task['output']
    from watch_codex_visual_reviews import batch_tasks
    tasks, errors = batch_tasks(base, output)
    assert not errors and [task['target'] for task in tasks] == [2, 2, 2]
    assert (base / 'stage12' / 'stage12.sqlite3').read_bytes() == before


def test_excludes_current_success_and_unfinished_assignments_and_reports_missing_images(tmp_path, monkeypatch):
    rows = [
        {'index': 1, 'asin': 'A1'}, {'index': 2, 'asin': 'A2'},
        {'index': 3, 'asin': 'A3'}, {'index': 4, 'asin': 'A4'},
        {'index': 5, 'asin': 'A5', 'missing': 'temu'}, {'index': 6, 'asin': 'A6', 'score': 69},
    ]
    cat, base, category_path, payloads = setup(tmp_path, monkeypatch, rows)
    context = VisionRevisionContext(cat)
    current_key = compatible_vision_keys(cat, payloads[0], context=context)[0]
    results = base / 'stage123' / 'results.jsonl'
    results.parent.mkdir(parents=True)
    results.write_text(json.dumps({'asin': 'A1', 'goods_id': 'G1', 'api_status': 'success',
                                   'vision_revision_key': current_key, 'visual_facts': facts(cat)}) + '\n', encoding='utf-8')
    old_batch = base / 'nightly_review' / 'previous'
    old_batch.mkdir(parents=True)
    reserved = {**payloads[1], 'vision_revision_key': compatible_vision_keys(cat, payloads[1], context=context)[0]}
    (old_batch / 'manifest.jsonl').write_text(json.dumps(reserved) + '\n', encoding='utf-8')
    (old_batch / 'review-batch.json').write_text(json.dumps({'tasks': [{
        'task_id': 'previous-shard', 'manifest': str(old_batch / 'manifest.jsonl'),
        'output': str(base / 'stage123' / 'codex_reviews' / 'previous.jsonl')}]}), encoding='utf-8')
    output = base / 'nightly_review' / 'next'
    summary = prepare_batch(category_path, shards=1, per_shard=2, output_dir=output)
    selected = read_jsonl(output / 'shard-1' / 'manifest.jsonl')
    selected_ids = {(row['asin'], row['goods_id']) for row in selected}
    assert ('A1', 'G1') not in selected_ids and ('A2', 'G2') not in selected_ids
    assert all(row['asin'] != 'A5' and row['asin'] != 'A6' for row in selected)
    missing = read_jsonl(output / 'missing_images.jsonl')
    assert any(row['asin'] == 'A5' and row['side'] == 'temu' and row['image_url'].endswith('t5.jpg') for row in missing)
    assert summary['missing_image_sides'] == 1


@pytest.mark.parametrize('source', ['api', 'codex'])
def test_compatible_revision_is_complete_but_changed_image_requires_review(tmp_path, monkeypatch, source):
    from selection_automation.codex_visual_review import import_reviews
    from test_codex_visual_review import fixture

    cat, base, category_path, payloads = setup(tmp_path, monkeypatch, [{'index': 1, 'asin': 'A1'}])
    cat['stage3']['reasoning_effort'] = 'low'
    category_path.write_text(json.dumps(cat), encoding='utf-8')
    payload = payloads[0]
    keys = compatible_vision_keys(cat, payload, context=VisionRevisionContext(cat))
    assert keys[-1] != keys[0]
    revision = keys[-1]
    if source == 'api':
        path = base / 'stage123/results.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            'asin': payload['asin'], 'goods_id': payload['goods_id'], 'api_status': 'success',
            'vision_revision_key': revision, 'visual_facts': facts(cat),
        }) + '\n', encoding='utf-8')
    else:
        _, row = fixture('UNKNOWN')
        pair = {**payload, 'vision_revision_key': revision}
        row.update(asin=pair['asin'], goods_id=pair['goods_id'], vision_revision_key=revision,
                   visual_facts=facts(cat))
        import_reviews(cat, {'pairs': [pair]}, [row], base / 'stage123/codex_reviews/old.jsonl', 'old')

    assert prepare_batch(category_path, skip_empty=True)['selected'] == 0
    Image.new('RGB', (80, 60), (10, 200, 20)).save(payload['temu_image_path'])
    assert prepare_batch(category_path, skip_empty=True)['selected'] == 1
