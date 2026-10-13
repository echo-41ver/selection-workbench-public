"""A provider preamble may be stripped; malformed or ambiguous facts may not."""
import json
import pytest
from selection_automation.vision import parse_visual_response


def test_single_intact_object_after_plain_preamble_is_preserved_exactly():
    raw = {'candidates':[{'i':'C00','observation':'颜色红/蓝，具体原始事实'}]}
    text = 'I compared the front-face structure.\n' + json.dumps(raw,ensure_ascii=False)
    assert parse_visual_response(text,allow_preamble=True) == raw
    with pytest.raises(ValueError): parse_visual_response(text)


@pytest.mark.parametrize('text', [
    'Analysis {"candidates": []} {"candidates": []}',
    'Analysis {"wrapper":{"candidates":[]}}',
    'Analysis [{"candidates":[]}]',
    'Analysis {"candidates":[{"i":"C00"}]',
    'Analysis {"candidates":[] extra}',
    'Analysis {"candidates":[]} trailing claims',
])
def test_ambiguity_missing_delimiters_and_wrappers_are_never_repaired(text):
    with pytest.raises(ValueError): parse_visual_response(text,allow_preamble=True)
