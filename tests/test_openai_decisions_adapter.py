"""OpenAI's decisions endpoint keeps D1 semantics with a different wire shape."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from selection_automation.stage3_provider import (openai_decisions, openai_decisions_request,
                                                  openai_decisions_response)

PROVIDER = {'protocol': 'decisions', 'base_url': 'https://api.openai.com/v1/decisions', 'model': 'gpt-6-luna'}
D1_PAYLOAD = {
    'model': 'd1',
    'state': {'image_1': 'Amazon 基准商品', 'image_2': 'TEMU 候选商品', 'records': ['记录一']},
    'images': ['data:image/jpeg;base64,AAA', 'data:image/jpeg;base64,BBB'],
    'questions': {'same_product': {'type': 'choice', 'instructions': '比较两张图',
                                   'criteria': {'same': '一致', 'different': '不同', 'similar': '相近'}}},
}


def test_only_openai_decisions_endpoint_is_translated():
    assert openai_decisions(PROVIDER) is True
    assert openai_decisions({**PROVIDER, 'base_url': 'https://api.liquid.ai/decisions/v1/systemone'}) is False
    assert openai_decisions({**PROVIDER, 'protocol': 'chat_completions'}) is False


def test_request_becomes_choice_questions_with_two_images():
    payload = json.loads(openai_decisions_request(json.dumps(D1_PAYLOAD, ensure_ascii=False)))
    assert payload['model'] == 'd1'
    assert len(payload['questions']) == 1
    question = payload['questions'][0]
    assert question['type'] == 'choice' and question['name'] == 'same_product'
    assert [choice['value'] for choice in question['choices']] == ['same', 'different', 'similar']
    parts = payload['input'][0]['content']
    assert [part['type'] for part in parts] == ['input_text', 'input_image', 'input_image']
    assert '记录一' in parts[0]['text']


def test_answers_become_a_name_keyed_mapping_with_dict_probabilities():
    raw = {'answers': [{'type': 'choice', 'name': 'same_product', 'choice': 'same',
                        'probabilities': [{'value': 'same', 'probability': .9},
                                          {'value': 'different', 'probability': .06},
                                          {'value': 'similar', 'probability': .04}],
                        'confidence': .9}]}
    converted = openai_decisions_response(raw)['answers']
    assert list(converted) == ['same_product']
    assert converted['same_product']['choice'] == 'same'
    assert converted['same_product']['probabilities'] == {'same': .9, 'different': .06, 'similar': .04}
    assert converted['same_product']['confidence'] == .9


def test_unknown_shape_is_left_untouched():
    raw = {'answers': {'same_product': {'choice': 'same', 'probabilities': {'same': 1.0}}}}
    assert openai_decisions_response(raw) is raw
