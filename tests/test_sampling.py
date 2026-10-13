import json
from selection_automation.sampling import prepare


def test_core_category_and_only_source_coverage_are_protected(tmp_path):
    for i,item in enumerate([{'purpose':'CATEGORY_SAMPLE'}, {'source_asins':['a']},
            {'source_asins':['a'],'source_evidence':[{'features':[{'kind':'structure'}]}]}]):
        output=tmp_path/str(i)
        row={'query':'q','max_products':500,**item}
        policy=json.loads(prepare(tmp_path,output,row,[row],'sales').read_text())
        assert policy['protected'] and not policy['enabled']


