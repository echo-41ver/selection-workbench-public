import json
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from rule_editor import draft_view,edit_rules


def fixture(): return json.loads((ROOT/'config/categories/power_drill.json').read_text('utf-8'))


def test_unchanged_text_no_model_and_edited_prompt_threshold_apply():
    cat=fixture();edit=draft_view(cat,'draft')
    edit['stage2_threshold']=77;edit['stage3_text']='用户完整视觉提示词'
    out=edit_rules(cat,edit,compile_model=lambda *a,**k:pytest.fail('unchanged text must not call model'))
    assert out['stage2']['candidate_threshold']==77
    assert out['stage3']['prompt_override']=='用户完整视觉提示词'
    assert out['stage1']['rule_version'].startswith('human-reviewed-')
    assert cat['stage2']['candidate_threshold']!=77


def test_edited_first_layer_compiled_and_original_saved():
    cat=fixture();edit=draft_view(cat,'draft');edit['stage1_text']='新的人工文字规则'
    attrs=[{'name':'identity','hard_conflict':True,'values':{'A':['alpha'],'B':['beta']}}]
    out=edit_rules(cat,edit,compile_model=lambda *a,**k:{'attributes':attrs})
    assert out['stage1']['attributes']==attrs
    assert out['human_stage1_text']=='新的人工文字规则'


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1,101,True])
def test_bad_threshold_rejected(value):
    cat=fixture();edit=draft_view(cat,'draft');edit['stage2_threshold']=value
    with pytest.raises(ValueError):edit_rules(cat,edit)


def test_module_schema_cannot_drift_from_prompt_mapping():
    cat=fixture();edit=draft_view(cat,'draft');modules=json.loads(edit['stage3_modules_json'])
    modules['module_descriptions'][next(iter(modules['modules']))]='changed description'
    edit['stage3_modules_json']=json.dumps(modules)
    with pytest.raises(ValueError,match='只读'):edit_rules(cat,edit)
