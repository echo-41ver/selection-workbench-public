import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'import_codex_visual_reviews.py'
SPEC = importlib.util.spec_from_file_location('import_codex_visual_reviews', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def pair(asin='A'):
    return {'asin': asin, 'goods_id': f'T-{asin}', 'vision_revision_key': 'rev',
            'amazon_image_path': '/a.jpg', 'temu_image_path': '/t.jpg'}


def test_load_manifest_accepts_legacy_json_and_jsonl(tmp_path):
    first, second = pair(), pair('B')
    legacy = tmp_path / 'manifest.json'
    legacy.write_text(json.dumps({'pairs': [first]}), encoding='utf-8')
    assert MODULE.load_manifest(legacy) == {'pairs': [first]}
    jsonl = tmp_path / 'manifest.jsonl'
    jsonl.write_text(json.dumps(first) + '\n\n' + json.dumps(second) + '\n', encoding='utf-8')
    assert MODULE.load_manifest(jsonl) == {'pairs': [first, second]}


@pytest.mark.parametrize('content, message', [
    ('', 'empty'),
    ('{"asin":', 'invalid JSONL manifest line'),
    (json.dumps({'pairs': [{'asin': 'A'}]}), 'invalid manifest pair at row 1'),
    ('{}', 'non-empty pairs list'),
])
def test_load_manifest_rejects_invalid_or_incomplete_rows(tmp_path, content, message):
    path = tmp_path / 'manifest.jsonl'
    path.write_text(content, encoding='utf-8')
    with pytest.raises(ValueError, match=message):
        MODULE.load_manifest(path)
