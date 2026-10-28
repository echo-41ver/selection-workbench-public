import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from selection_automation import vision

ROOT = Path(__file__).resolve().parents[1]
CAT = json.loads((ROOT / 'config/categories/power_drill.json').read_text(encoding='utf-8'))


def fixture(tmp_path):
    amazon = tmp_path / 'amazon.jpg'
    temu = tmp_path / 'temu.jpg'
    amazon.write_bytes(b'image-one')
    temu.write_bytes(b'image-two')
    return {'asin': 'A', 'goods_id': 'T', 'pair_revision': 'p',
            'amazon_image_path': str(amazon), 'temu_image_path': str(temu)}


def test_gui_local_provider_switch_accepts_legacy_string_checkpoint(tmp_path):
    pair = fixture(tmp_path)
    cat = copy.deepcopy(CAT)
    previous = copy.deepcopy(cat['stage3'])
    previous['provider'] = 'deepseek'
    cat['stage3_checkpoint_sources'] = [previous]
    cat['stage3']['provider'] = {'mode': 'local_model', 'protocol': 'decisions'}
    keys = vision.compatible_vision_keys(cat, pair)
    assert keys[0] == vision.vision_revision(cat, pair)
    assert vision.vision_revision({**cat, 'stage3': previous}, pair) in keys


def test_decision_checkpoint_is_not_reused_by_full_visual_provider(tmp_path):
    pair = fixture(tmp_path)
    cat = copy.deepcopy(CAT)
    previous = copy.deepcopy(cat['stage3'])
    previous['provider'] = {'mode': 'local_model', 'protocol': 'decisions'}
    cat['stage3_checkpoint_sources'] = [previous]
    cat['stage3']['provider'] = {'mode': 'multimodal_api', 'protocol': 'responses'}
    assert vision.vision_revision({**cat, 'stage3': previous}, pair) not in vision.compatible_vision_keys(cat, pair)


@pytest.mark.parametrize('side', ['amazon', 'temu'])
def test_same_length_image_rewrite_with_original_mtime_changes_all_keys(tmp_path, side):
    pair = fixture(tmp_path)
    path = Path(pair[f'{side}_image_path'])
    original = path.stat()
    before = vision.compatible_vision_keys(CAT, pair)
    path.write_bytes(b'image-new')
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_size == original.st_size
    assert path.stat().st_mtime_ns == original.st_mtime_ns
    after = vision.compatible_vision_keys(CAT, pair)
    assert all(old != new for old, new in zip(before, after))
    assert vision.image_digest(path) == hashlib.sha256(b'image-new').hexdigest()


def test_same_tick_rapid_rewrite_needs_no_sleep(tmp_path):
    pair = fixture(tmp_path)
    path = Path(pair['amazon_image_path'])
    observed = set()
    for i in range(20):
        path.write_bytes(f'{i:09}'.encode())
        observed.add(vision.vision_revision(CAT, pair))
    assert len(observed) == 20


def test_new_and_legacy_keys_share_one_read_per_image_per_call(tmp_path, monkeypatch):
    pair = fixture(tmp_path)
    original = vision.image_digest
    reads = []

    def digest(path):
        reads.append(Path(path))
        return original(path)

    monkeypatch.setattr(vision, 'image_digest', digest)
    first = vision.compatible_vision_keys(CAT, pair)
    assert len(reads) == 2
    assert vision.compatible_vision_keys(CAT, pair) == first
    assert len(reads) == 4  # No unsafe cross-call metadata cache.


def test_local_scoring_changes_reuse_visual_facts_but_question_changes_do_not(tmp_path):
    pair = fixture(tmp_path)
    before = vision.vision_revision(CAT, pair)
    changed = copy.deepcopy(CAT)
    changed['stage3']['modules']['body'] = 39
    changed['stage3']['modules']['kit'] = 11
    changed['stage3']['same_threshold'] = 91
    changed['stage3']['different_score_ceiling'] = 69
    changed['stage3']['rule_version'] = 'local-scoring-revision-only'
    pair['pair_revision'] = 'local-text-revision-only'
    assert vision.vision_revision(changed, pair) == before
    changed['stage3']['module_descriptions']['body'] += ' More visible structural facts.'
    assert vision.vision_revision(changed, pair) != before


@pytest.mark.parametrize('effort', ['low', 'medium', 'high'])
def test_higher_effort_does_not_reuse_old_results_actually_run_at_low(tmp_path, effort):
    pair = fixture(tmp_path)
    cat = copy.deepcopy(CAT)
    cat['stage3']['reasoning_effort'] = effort
    stage = cat['stage3']
    previous = vision.revision({'version': 3, 'pair': [pair['asin'], pair['goods_id']],
        'protocol': vision.protocol(cat, 1), 'model': stage.get('model', 'deepseek-flash'),
        'endpoint': stage.get('base_url', 'https://api.deepseek.com').rstrip('/'),
        'effort': effort, 'high_rerun_policy': stage.get('high_rerun_policy'),
        'images': [vision.image_digest(pair[f'{side}_image_path']) for side in ('amazon', 'temu')]})
    keys = vision.compatible_vision_keys(cat, pair)
    assert (previous in keys) == (effort == 'low')
    assert (vision.legacy_vision_revision(cat, pair) in keys) == (effort == 'low')


@pytest.mark.parametrize('effort', ['low', 'medium', 'high'])
def test_report_context_preserves_all_revision_keys_and_reuses_fixed_serialization(tmp_path, monkeypatch, effort):
    pair = fixture(tmp_path)
    cat = copy.deepcopy(CAT)
    cat['stage3']['reasoning_effort'] = effort
    pair['goods_id'] = '中文 "quoted"\\商品'
    expected = vision.compatible_vision_keys(cat, pair)
    context = vision.VisionRevisionContext(cat)
    calls = []
    original = vision.json.dumps
    def dumps(value, *args, **kwargs):
        calls.append(value)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(vision.json, 'dumps', dumps)
    monkeypatch.setattr(vision, 'protocol', lambda *args: pytest.fail('Fixed protocol must not be rebuilt'))
    assert vision.compatible_vision_keys(cat, pair, context=context) == expected
    assert not any(isinstance(value, dict) and value == cat['stage3'] for value in calls)


def test_report_context_invalidates_in_place_rules_and_prompt_changes(tmp_path):
    pair = fixture(tmp_path)
    cat = copy.deepcopy(CAT)
    context = vision.VisionRevisionContext(cat)
    before = vision.compatible_vision_keys(cat, pair, context=context)
    cat['stage3']['module_descriptions']['body'] += ' Changed actual question.'
    after = vision.compatible_vision_keys(cat, pair, context=context)
    assert after == vision.compatible_vision_keys(cat, pair)
    assert all(old != new for old, new in zip(before, after))
    cat['stage3']['same_threshold'] += 1
    changed = vision.compatible_vision_keys(cat, pair, context=context)
    assert changed == vision.compatible_vision_keys(cat, pair)
    assert changed[-1] != after[-1]  # Legacy includes every original scoring rule.


def test_report_context_never_caches_image_bytes_even_with_unchanged_metadata(tmp_path):
    pair = fixture(tmp_path)
    context = vision.VisionRevisionContext(CAT)
    before = vision.compatible_vision_keys(CAT, pair, context=context)
    image = Path(pair['temu_image_path'])
    stat = image.stat()
    image.write_bytes(b'image-new')
    os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = vision.compatible_vision_keys(CAT, pair, context=context)
    assert all(old != new for old, new in zip(before, after))
    assert after == vision.compatible_vision_keys(CAT, pair)


def test_report_context_keeps_per_pair_text_and_reviewed_examples_dynamic(tmp_path, monkeypatch):
    from selection_automation import visual_examples
    pair = fixture(tmp_path)
    cat = copy.deepcopy(CAT)
    cat['stage3']['reference_examples'] = [{'test': 'only selection is mocked'}]
    examples = [{'identity': 'first reviewed example'}]
    monkeypatch.setattr(visual_examples, 'select_examples', lambda *args: examples)
    context = vision.VisionRevisionContext(cat)
    pair['amazon_title'] = 'Actual recorded title'
    before = vision.compatible_vision_keys(cat, pair, context=context)
    assert before == vision.compatible_vision_keys(cat, pair)
    pair['amazon_title'] += ' changed'
    after_text = vision.compatible_vision_keys(cat, pair, context=context)
    assert after_text == vision.compatible_vision_keys(cat, pair)
    assert after_text[0] != before[0]
    examples[:] = [{'identity': 'different reviewed example'}]
    after_examples = vision.compatible_vision_keys(cat, pair, context=context)
    assert after_examples == vision.compatible_vision_keys(cat, pair)
    assert after_examples[0] != after_text[0]
