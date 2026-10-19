"""Offline regression cases for the three matching layers; no API or downloads."""
from category_fixtures import category_path
import copy
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from selection_automation.rules import score, text_gate, validate
from selection_automation.vision import decode
from identity_fixtures import identity
from selection_workflow.incremental_stage12 import IncrementalStage12, PathSpec, SourceSpec

ROOT = Path(__file__).resolve().parents[1]


def category():
    return json.loads((ROOT / 'config/categories/power_drill.json').read_text('utf-8'))


def visual_payload(cat):
    return {'candidates': [{'i': 'C00', 'm': {
        str(i): {'r': 'S', 'a': 'Amazon结构可见', 't': 'TEMU结构可见', 'h': 'NONE'}
        for i in range(len(cat['stage3']['modules']))}, 'c': .9, 'v': identity()}]}


def test_missing_and_ambiguous_text_continue_without_claiming_attribute_agreement():
    cat = category()
    for left, right in [('cordless drill', 'drill'), ('corded drill', 'not corded cordless drill')]:
        result = text_gate(cat, left, right)
        assert result['stage1_status'] == 'TEXT_INSUFFICIENT'
        assert not result['excluded']
    assert text_gate(cat, 'cordless drill driver', 'corded drill driver')['excluded']
    assert text_gate(cat, 'cordless drill driver', 'cordless drill driver')['stage1_status'] == 'TEXT_PASS'


@pytest.mark.parametrize('confidence', [True, '0.9', None, float('nan'), float('inf'), -.1, 1.1])
def test_malformed_confidence_cannot_confirm(confidence):
    cat = category()
    payload = visual_payload(cat)
    payload['candidates'][0]['c'] = confidence
    with pytest.raises(ValueError, match='confidence'):
        decode(cat, payload, ['C00'])


@pytest.mark.parametrize('mutation', ['evidence_list', 'relation', 'extra_field', 'missing_module', 'duplicate_id'])
def test_structured_visual_response_is_checked_locally(mutation):
    cat = category()
    payload = visual_payload(cat)
    module = payload['candidates'][0]['m']['0']
    if mutation == 'evidence_list':
        module['a'] = ['not a string']
    elif mutation == 'relation':
        module['r'] = 'INVALID'
    elif mutation == 'extra_field':
        module['score'] = 100
    elif mutation == 'missing_module':
        del payload['candidates'][0]['m']['0']
    else:
        payload['candidates'].append(copy.deepcopy(payload['candidates'][0]))
    with pytest.raises(ValueError):
        decode(cat, payload, ['C00'])


def test_disabled_similar_relation_is_rejected_even_if_provider_ignores_schema():
    cat = category()
    cat['stage3']['allow_similar_relation'] = False
    payload = visual_payload(cat)
    payload['candidates'][0]['m']['0']['r'] = 'L'
    with pytest.raises(ValueError, match='relation'):
        decode(cat, payload, ['C00'])


@pytest.mark.parametrize('filename', ['power_drill.json', 'shower_curtain_local_union_20260922.json'])
def test_same_relation_with_conflict_code_requires_review(filename):
    cat = json.loads(category_path(filename).read_text('utf-8'))
    payload = visual_payload(cat)
    payload['candidates'][0]['m']['0']['h'] = cat['stage3']['identity_hard_conflicts'][0]
    facts = decode(cat, payload, ['C00'])['C00']
    result = score(cat, facts)
    assert result['state'] == '待复核'
    assert result['contradictory_modules'] == [next(iter(cat['stage3']['modules']))]


def test_invalid_minimum_module_cannot_silently_disable_confirmation_gate():
    cat = category()
    cat['stage3']['confirmation_minimums']['typo_module'] = 10
    with pytest.raises(ValueError, match='unknown module'):
        validate(cat)


@pytest.mark.parametrize('threshold', [True, float('nan'), float('inf'), -1, 101])
def test_invalid_candidate_threshold_is_rejected(threshold, tmp_path):
    cat = category()
    cat['stage2']['candidate_threshold'] = threshold
    with pytest.raises(ValueError, match='threshold'):
        validate(cat)
    with pytest.raises(ValueError, match='threshold'):
        IncrementalStage12(tmp_path / 'unused', threshold=threshold)
    assert not (tmp_path / 'unused').exists()


def run_pair(tmp_path, left_vector, right_vector):
    class Vectors:
        revision = 'offline-boundary-vector'

        def encode(self, path):
            with Image.open(path) as image:
                is_amazon = image.getpixel((0, 0))[0] == 255
            return np.asarray(left_vector if is_amazon else right_vector, dtype=np.float32)

    sources = []
    for side, identity, color in [('amazon', 'A1', (255, 0, 0)), ('temu', 'T1', (0, 255, 0))]:
        image = tmp_path / f'{side}.png'
        Image.new('RGB', (64, 64), color).save(image)
        source = tmp_path / f'{side}.jsonl'
        source.write_text(json.dumps({('asin' if side == 'amazon' else 'goods_id'): identity,
                                     'title': 'shower curtain', 'local_image_path': str(image)}) + '\n', encoding='utf-8')
        sources.append(SourceSpec(side, 'AMAZON' if side == 'amazon' else 'TEMU',
                                  'amazon_generic' if side == 'amazon' else 'temu_frontend', source))
    with_pipeline = IncrementalStage12(tmp_path / 'run', vectorizer=Vectors(), threshold=50)
    try:
        with_pipeline.configure(sources, [PathSpec('match', 'amazon', 'temu', '测试')])
        for side in ('amazon', 'temu'):
            with_pipeline.ingest_source(side)
        with_pipeline.process_batch(10)
        row = dict(with_pipeline.con.execute('SELECT * FROM pairs').fetchone())
        row['candidate_count'] = with_pipeline.con.execute('SELECT count(*) FROM candidates').fetchone()[0]
        return row
    finally:
        with_pipeline.close()


def test_rounding_cannot_promote_below_threshold_pair(tmp_path):
    cosine = .4999996
    row = run_pair(tmp_path, [1, 0], [cosine, np.sqrt(1 - cosine ** 2)])
    assert row['vector_score'] == 50.0  # Display rounding is deliberately separate.
    assert row['status'] == 'BELOW_VECTOR_THRESHOLD'
    assert row['candidate_count'] == 0


def test_large_finite_vectors_do_not_overflow_into_false_exclusion(tmp_path):
    row = run_pair(tmp_path, [1e30, 1e30], [1e30, 1e30])
    assert row['status'] == 'CANDIDATE'
    assert row['vector_score'] == 100
    assert row['candidate_count'] == 1


@pytest.mark.parametrize('right', [[0, 0], [1, 0, 0], [float('nan'), 1], [float('inf'), 1]])
def test_invalid_vector_is_failure_without_numeric_decision(tmp_path, right):
    row = run_pair(tmp_path, [1, 0], right)
    assert row['status'] == 'FAILED_RETRYABLE'
    assert 'vector' in row['last_error']
    assert row['vector_score'] is None
    assert row['candidate_count'] == 0
