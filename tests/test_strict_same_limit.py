from selection_automation.commercial import settings,match_metric


def test_strict_less_than_keeps_the_n_boundary_out():
    policy=settings({'same_limit':5,'same_operator':'lt'})
    assert match_metric({'confirmed':4},4,policy)['state']=='SAMPLE_PASS'
    assert match_metric({'confirmed':5},5,policy)['state']=='FAIL'
    assert match_metric({'confirmed':3,'pending':1},4,policy)['state']=='UNKNOWN'


def test_legacy_tasks_keep_their_existing_inclusive_limit():
    assert match_metric({'confirmed':5},5,settings({'same_limit':5}))['state']=='SAMPLE_PASS'
