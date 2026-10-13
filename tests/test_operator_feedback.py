import pytest
from selection_automation.operator_feedback import read_feedback, save_feedback, excluded_from_new_recommendations


def test_feedback_is_separate_durable_and_idempotent(tmp_path):
    body = {'asin': 'B01MOUSE', 'state': 'HOLD', 'reason': '包装成本过高', 'source_run_key': 'mouse'}
    first = save_feedback(tmp_path, '数码运营', body)
    assert save_feedback(tmp_path, '数码运营', body) == first
    row = read_feedback(tmp_path, '数码运营')['B01MOUSE']
    assert len(row['history']) == 1 and excluded_from_new_recommendations(row)
    assert read_feedback(tmp_path, '家居运营') == {}
    assert not (tmp_path / 'runs').exists()


def test_returning_to_new_retains_history_and_allows_future_recommendation(tmp_path):
    body = {'asin': 'B01MOUSE', 'state': 'REJECT', 'reason': '价格不合适', 'source_run_key': 'mouse'}
    save_feedback(tmp_path, '运营', body)
    row = save_feedback(tmp_path, '运营', {**body, 'state': 'NEW', 'reason': '价格更新，重新评估'})
    assert len(row['history']) == 2 and not excluded_from_new_recommendations(row)


@pytest.mark.parametrize('patch', [{'asin': '../escape'}, {'source_run_key': '../escape'}, {'state': 'SEND'}, {'state': 'HOLD', 'reason': ''}, {'reason': 10}])
def test_feedback_validates_local_state(tmp_path, patch):
    with pytest.raises(ValueError):
        save_feedback(tmp_path, '运营', {'asin': 'B01MOUSE', 'source_run_key': 'mouse', 'state': 'NEW', **patch})
