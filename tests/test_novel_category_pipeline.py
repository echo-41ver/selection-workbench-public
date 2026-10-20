"""Offline, newly declared category; fixture URLs are never fetched."""
import json
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import selection
import pipeline
import export_category_workbooks as exporter
from selection_automation.rules import score
from selection_automation.vision import protocol, decode, vision_revision
from identity_fixtures import identity
from selection_workflow.text_gate_registry import evaluate


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows), encoding='utf-8')


def novel_category():
    # Written from scratch: deliberately no existing category/template import.
    return {
        'category_key': 'fixture_mechanical_keyboard', 'category_name_zh': '测试机械键盘',
        'amazon': {'bestsellers_url': 'https://www.amazon.com/zgbs/pc/900000000002', 'target_count': 1},
        'temu': {'merge_key': 'goods_id', 'max_products_per_keyword': 3},
        'stage1': {'text_gate': 'declarative', 'rule_version': 'keyboard-text-fixture-1', 'attributes': [
            {'name': '按键布局协议', 'hard_conflict': True,
             'values': {'ANSI': ['ANSI layout'], 'ISO': ['ISO layout']}, 'ambiguous_phrases': ['ANSI or ISO']}]},
        'stage2': {'candidate_threshold': 50, 'threshold_status': 'PROVISIONAL_UNCALIBRATED',
                   'below_threshold_action': 'NOT_SELECTED_NOT_MANUAL_REVIEW'},
        'stage3': {'rule_version': 'keyboard-vision-fixture-1', 'same_threshold': 88, 'different_score_ceiling': 69,
                   'modules': {'key_matrix_geometry': 60, 'rotary_encoder_position': 40},
                   'module_labels': {'key_matrix_geometry': '按键矩阵结构', 'rotary_encoder_position': '旋钮位置'},
                   'module_descriptions': {'key_matrix_geometry': '比较回车键轮廓及按键矩阵', 'rotary_encoder_position': '比较旋钮相对按键矩阵位置'},
                   'confirmation_minimums': {'key_matrix_geometry': 50, 'rotary_encoder_position': 30},
                   'identity_hard_conflicts': ['KEY_MATRIX_INCOMPATIBLE'], 'variant_fields': ['键帽颜色']}}


def test_new_category_frozen_rules_visual_protocol_and_excel(tmp_path, monkeypatch):
    for module in (selection, pipeline, exporter):
        monkeypatch.setattr(module, 'ROOT', tmp_path)
    source=tmp_path/'incoming.json'; write_json(source, novel_category())
    frozen, cat, base=selection.prepare(source)
    assert selection.prepare(source)[0] == frozen
    stage12=json.loads((base/'stage12_config.json').read_text(encoding='utf-8'))
    gate=stage12['text_gate']
    assert evaluate(gate, 'ANSI layout mechanical keyboard', 'ISO layout mechanical keyboard')['excluded']
    assert not evaluate(gate, 'ANSI layout mechanical keyboard', 'mechanical keyboard')['excluded']
    assert not evaluate(gate, 'ANSI layout mechanical keyboard', 'ANSI or ISO layout')['excluded']
    changed=novel_category(); changed['stage3']['modules']={'key_matrix_geometry': 70, 'rotary_encoder_position': 30}
    write_json(source, changed)
    with pytest.raises(ValueError, match='冻结'):
        selection.prepare(source)

    prompt, schema=protocol(cat, 1)
    assert '比较回车键轮廓及按键矩阵' in prompt
    assert '比较旋钮相对按键矩阵位置' in prompt
    assert 'KEY_MATRIX_INCOMPATIBLE' in json.dumps(schema)
    facts=decode(cat, {'candidates':[{'i':'T1','m':{
        '0':{'r':'S','a':'矩形回车键','t':'矩形回车键','h':'NONE'},
        '1':{'r':'S','a':'右上角独立旋钮','t':'右上角独立旋钮','h':'NONE'}},'v':identity(),'c':0.95}]}, ['T1'])['T1']
    assert score(cat, facts)['state']=='确认同款'
    assert score(cat, facts)['total_score']==100
    missing=json.loads(json.dumps(facts)); missing['modules']['rotary_encoder_position']['temu_evidence']=''
    assert score(cat, missing)['state']=='待补充证据'
    conflict=json.loads(json.dumps(facts)); conflict['modules']['key_matrix_geometry'].update(relation='DIFFERENT', hard_conflict_code='KEY_MATRIX_INCOMPATIBLE')
    assert score(cat, conflict)['state']=='不同款'

    # Local synthetic image bytes are identity fixtures, never downloaded or sent.
    a=tmp_path/'a.bin'; t=tmp_path/'t.bin'; a.write_bytes(b'amazon-fixture'); t.write_bytes(b'temu-fixture')
    pair={'asin':'A1','goods_id':'T1','pair_revision':'fixture-1','amazon_image_path':str(a),'temu_image_path':str(t)}
    frozen_export=base/'stage123'/'export_frozen'; key=cat['category_key']
    write_json(frozen_export/f'layer12_summary_{key}.json', {'amazon_products':1,'temu_products':1,'actual_pairs':1})
    write_rows(frozen_export/f'layer3_candidates_{key}.jsonl', [pair])
    write_rows(frozen_export/f'layer12_rows_{key}.jsonl', [{**pair,'pair_status':'CANDIDATE','amazon_title':'ANSI fixture keyboard', 'temu_title':'ANSI fixture keyboard', 'stage1_text_fact':'测试属性无冲突','vector_score':95}])
    write_rows(base/'stage123'/'results.jsonl', [{**pair,'vision_revision_key':vision_revision(cat,pair),'api_status':'success','visual_facts':facts,'model':'OFFLINE_FIXTURE','usage':{}}])
    exporter.export_snapshot(frozen, 'layer123')
    book=load_workbook(base/'excel'/'测试机械键盘_第一二三层.xlsx',read_only=True,data_only=True)
    try:
        rows=list(book['全量综合结果'].values); result=dict(zip(rows[3],rows[4]))
        assert result['按键矩阵结构/60']==60
        assert result['旋钮位置/40']==40
        assert result['最终判断']=='确认同款'
        assert '右上角独立旋钮' in result['视觉证据']
        assert len(rows)==5
    finally:
        book.close()
