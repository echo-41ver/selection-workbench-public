import json
from pathlib import Path
import pytest

from selection_automation.stage3_admission import admitted, threshold, concurrency
from selection_automation.rules import validate
from selection_automation.stage3_progress import snapshot


def category():
    cat = json.loads((Path(__file__).resolve().parents[1]/'config/categories/power_drill.json').read_text('utf8'))
    cat['stage2']['candidate_threshold'] = 60
    cat['stage3']['candidate_admission_threshold'] = 75
    return cat


@pytest.mark.parametrize('score,expected', [(60,False),(70,False),(74.9999,False),(75,True),(95.6665,True),
                                          (None,False),(True,False),(float('nan'),False)])
def test_paid_admission_without_replacing_local_recall(score, expected):
    cat = category()
    assert admitted(cat, {'vector_score':score}) is expected
    assert cat['stage2']['candidate_threshold'] == 60


@pytest.mark.parametrize('value', [59,101,True,'75',float('nan')])
def test_invalid_admission_configuration(value):
    cat = category(); cat['stage3']['candidate_admission_threshold'] = value
    with pytest.raises(ValueError): validate(cat)


def test_unconfigured_categories_are_compatible():
    cat = category(); del cat['stage3']['candidate_admission_threshold']
    assert threshold(cat) is None and admitted(cat,{})


@pytest.mark.parametrize('value',[0,257,True,'50',4.5])
def test_parallelism_is_validated_instead_of_silently_clamped(value):
    cat=category();cat['stage3']['max_concurrency']=value
    with pytest.raises(ValueError):validate(cat)


@pytest.mark.parametrize('value',[50,64,256])
def test_configured_parallel_groups_are_supported(value):
    cat=category();cat['stage3']['max_concurrency']=value
    validate(cat)
    assert concurrency(cat['stage3'])==value


def test_queue_counts_retained_low_scores_without_deleting_file(tmp_path):
    base = tmp_path/'task'; (base/'stage12').mkdir(parents=True)
    path = base/'stage12/layer3_candidates_task.jsonl'
    rows = [{'asin':'a','goods_id':str(i),'vector_score':score} for i,score in enumerate([60,74,75,95.6665])]
    content = ''.join(json.dumps(r)+'\n' for r in rows); path.write_text(content,encoding='utf8')
    result = snapshot(base, category(), history=False)
    assert result['total'] == result['pending'] == 2
    assert result['source_candidates'] == 4 and result['retained_for_review'] == 2
    assert path.read_text(encoding='utf8') == content
