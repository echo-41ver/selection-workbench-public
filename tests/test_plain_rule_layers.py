import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from plain_rules import apply_plain_rules

def test_visual_plain_edit_cannot_modify_text_or_threshold():
    cat=json.loads((Path(__file__).resolve().parents[1]/'config/categories/power_drill.json').read_text('utf-8'))
    before=json.loads(json.dumps(cat))
    instruction='仅比较机身结构，背景差异允许。'
    def forbidden(*args,**kwargs):pytest.fail('Visual prose must save without a model request')
    result=apply_plain_rules(cat,instruction,scope='stage3',model=forbidden)
    assert result['stage1']==cat['stage1'] and result['stage2']==cat['stage2']
    assert result['stage3']['prompt_override']==instruction
    assert result['plain_stage3']==instruction
    assert cat==before
    assert {k:v for k,v in result['stage3'].items() if k not in ('prompt_override','prompt_version')}=={k:v for k,v in cat['stage3'].items() if k not in ('prompt_override','prompt_version')}
