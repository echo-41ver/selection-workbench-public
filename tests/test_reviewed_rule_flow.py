import copy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pipeline
import rule_editor
from selection_automation.rules import text_gate
from selection_automation.vision import protocol


def test_reviewed_rules_reach_text_gate_vector_config_and_visual_prompt(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    category = json.loads((root / 'config/categories/power_drill.json').read_text('utf-8'))
    category['execution_mode']='collection'
    assert text_gate(category, 'corded electric drill', 'cordless drill')['excluded']
    attributes = copy.deepcopy(category['stage1']['attributes'])
    for attribute in attributes:
        attribute['hard_conflict'] = False
    edits = rule_editor.draft_view(category, 'draft')
    edits.update(stage1_text='接电和电池供电的差异不作为第一层硬排除。',
                 stage2_threshold=72, stage3_text='用户确认的完整视觉判断提示词。')
    reviewed = rule_editor.edit_rules(category, edits, compile_model=lambda *args, **kwargs: {'attributes': attributes})
    assert reviewed['execution_mode']=='full'
    assert not text_gate(reviewed, 'corded electric drill', 'cordless drill')['excluded']
    assert reviewed['stage1']['rule_version'] != category['stage1'].get('rule_version')
    monkeypatch.setattr(pipeline, 'ROOT', tmp_path)
    config_path = pipeline.write_stage12_config(reviewed)
    config = json.loads(config_path.read_text('utf-8'))
    assert config['candidate_threshold'] == 72
    assert config['text_gate'].endswith(str(tmp_path / 'runs' / reviewed['category_key'] / 'category.json'))
    assert edits['stage3_text'] in protocol(reviewed, 2)[0]
    from selection_automation.identity import PROTOCOL_VERSION
    assert PROTOCOL_VERSION in protocol(reviewed, 2)[0]
