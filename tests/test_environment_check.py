import json
from pathlib import Path
from selection_automation.environment_check import compare

BASELINE=json.loads((Path(__file__).resolve().parents[1]/'config/runtime_baseline.json').read_text('utf-8'))


def test_matching_runtime_and_unknown_ads_are_distinct():
    inventory={'platform':'Windows',**BASELINE['observed_reference']}
    assert compare(inventory,BASELINE)['compatible']
    inventory['adspower']='99.0.0'
    result=compare(inventory,BASELINE)
    assert not result['compatible'] and '99.0.0' in result['missing'][0]
    inventory['adspower']=None
    assert not compare(inventory,BASELINE)['compatible']
