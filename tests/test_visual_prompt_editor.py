import copy
import json
from pathlib import Path

import pytest
from selection_automation.vision import protocol, vision_revision


def test_edited_prompt_preserves_identity_contract_and_invalidates_cache(tmp_path):
    category = json.loads((Path(__file__).resolve().parents[1] / 'config/categories/power_drill.json').read_text('utf-8'))
    image = tmp_path / 'image'
    image.write_bytes(b'image fixture')
    pair = {'asin': 'A', 'goods_id': 'T', 'pair_revision': 'r',
            'amazon_image_path': str(image), 'temu_image_path': str(image)}
    old_revision = vision_revision(category, pair)
    edited = copy.deepcopy(category)
    edited['stage3']['prompt_override'] = '用户编辑：只比较可见结构，配件数量差异允许作为变体。'
    prompt, schema = protocol(edited, 3)
    assert edited['stage3']['prompt_override'] in prompt
    from selection_automation.identity import PROTOCOL_VERSION
    assert PROTOCOL_VERSION in prompt
    assert 'v.matches' in prompt
    assert schema['properties']['candidates']['minItems'] == 3
    assert vision_revision(edited, pair) != old_revision
    edited['stage3']['prompt_override'] = '  '
    with pytest.raises(ValueError):
        protocol(edited, 3)
