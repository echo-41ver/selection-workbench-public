import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from observe_collection_recovery import recovery_evidence


def page(*responses, gate=False, goods=40):
    return {'matching_query':True,'goods':goods,'gate':gate,'responses':list(responses)}


def response(status, at, endpoint='/api/poppy/v1/search'):
    return {'status':status,'at_ms':at,'endpoint':endpoint}


def test_old_cards_and_initial_activation_do_not_clear_failed_pagination():
    current=page(response(200,50),response(200,200,'/api/poppy/v2/search_activation'))
    assert recovery_evidence(current,'/api/poppy/v1/search',100)=='WAITING_SUCCESS_RESPONSE'


def test_latest_real_rejection_overrides_old_success_and_goods():
    assert recovery_evidence(page(response(200,200),response(429,300)))=='WAITING_SITE_ACCEPTANCE'


def test_fresh_matching_response_after_human_verification_confirms_recovery():
    assert recovery_evidence(page(response(200,200)),'/api/poppy/v1/search',100)=='RECOVERY_CONFIRMED'
    assert recovery_evidence(page(response(200,200),gate=True),'/api/poppy/v1/search',100)=='WAITING_PAGE_RECOVERY'


def test_missing_network_proof_or_goods_never_claims_recovery():
    assert recovery_evidence(page())=='WAITING_SUCCESS_RESPONSE'
    assert recovery_evidence(page(response(200,200),goods=0))=='WAITING_CURRENT_QUERY'
