import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from plain_rules import apply_plain_rules

def test_visual_plain_edit_cannot_modify_text_or_threshold():
    cat=json.loads((Path(__file__).resolve().parents[1]/'config/categories/power_drill.json').read_text('utf-8'))
    reply={'supported':True,'stage1_attributes':cat['stage1']['attributes'],
           'stage2_threshold':cat['stage2']['candidate_threshold'],'stage3_prompt':'仅比较机身结构，背景差异允许。',
           'plain_summary':'图片中的机身结构必须一样，背景可以不同。','changes':['允许背景不同。']}
    result=apply_plain_rules(cat,'允许背景不同',scope='stage3',model=lambda *a,**k:reply)
    assert result['stage1']==cat['stage1'] and result['stage2']==cat['stage2']
    assert result['stage3']['prompt_override']==reply['stage3_prompt']
    assert result['plain_stage3']==reply['plain_summary']
    reply['stage2_threshold']=17
    with pytest.raises(ValueError,match='越过'):
        apply_plain_rules(cat,'允许背景不同',scope='stage3',model=lambda *a,**k:reply)
