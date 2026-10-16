import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from selection_automation import rules
from selection_workflow.incremental_stage12 import IncrementalStage12, _evaluate_text_gate_args
from selection_workflow.text_attribute_cache import cached_attribute_values, install_attribute_cache

ROOT = Path(__file__).resolve().parents[1]


def category():
    result = json.loads((ROOT / 'config/categories/power_drill.json').read_text('utf-8'))
    result['stage1']['attributes'] = [{'name': 'material', 'hard_conflict': True,
        'values': {'cotton': ['cotton', '纯棉'], 'polyester': ['polyester', '聚酯']},
        'ambiguous_phrases': []}]
    return result


def test_complete_gate_outputs_match_uncached_fixtures(monkeypatch):
    original = getattr(rules.attribute_values, '__wrapped__', rules.attribute_values)
    wrapper = cached_attribute_values(original)
    bodies = ['cotton', 'polyester', 'not made of cotton; polyester', 'cotton-free',
        'free from cotton', 'non-cotton', '不是纯棉 聚酯', 'cotton-like polyester',
        'polyester with cotton feel', '仿纯棉 聚酯', '纯棉质感 聚酯',
        'cotton / polyester SKU choices', '', 'polyester item; pair it with a cotton towel',
        'double layer with cotton lining', 'cotton robe supplied paired with polyester towels']
    configurations = [category(), json.loads((ROOT / 'config/categories/power_drill.json').read_text('utf-8'))]
    variant = copy.deepcopy(configurations[0])
    variant['stage3']['variant_fields'] = ['material']
    configurations.append(variant)
    for cat in configurations:
        for left in bodies:
            for right in bodies:
                monkeypatch.setattr(rules, 'attribute_values', original)
                expected = rules.text_gate(cat, left, right)
                monkeypatch.setattr(rules, 'attribute_values', wrapper)
                assert rules.text_gate(cat, left, right) == expected


def test_alias_mutation_and_returned_set_isolation_and_bound():
    original = getattr(rules.attribute_values, '__wrapped__', rules.attribute_values)
    wrapper = cached_attribute_values(original, maxsize=4)
    aliases = {'cotton': ['cotton']}
    values, uncertain = wrapper('cotton', aliases)
    values.clear()
    assert wrapper('cotton', aliases) == ({'cotton'}, uncertain)
    assert wrapper.cache_info().hits == 1
    aliases['cotton'][0] = 'linen'
    assert wrapper('cotton', aliases) == original('cotton', aliases)
    for index in range(20):
        assert wrapper(f'cotton {index}', aliases) == original(f'cotton {index}', aliases)
    assert wrapper.cache_info().currsize == 4


def test_install_once_and_worker_contract_unchanged(tmp_path, monkeypatch):
    original = getattr(rules.attribute_values, '__wrapped__', rules.attribute_values)
    monkeypatch.setattr(rules, 'attribute_values', original)
    engine = Path(rules.__file__)
    before_digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    cat = category()
    path = tmp_path / 'category.json'
    path.write_text(json.dumps(cat), encoding='utf-8')
    kwargs = {'vectorizer': SimpleNamespace(revision='CACHE_CONTRACT_FIXTURE'),
              'text_gate_name': 'declarative:' + str(path)}
    run = tmp_path / 'run'
    first = IncrementalStage12(run, **kwargs)
    try:
        first.ensure_processing_contract()
        contract = first.con.execute("SELECT value FROM processing_metadata WHERE key='stage12_contract'").fetchone()[0]
        revision = first._text_gate_revision
        with first.con:
            first.con.execute("""INSERT INTO pairs(path_key,asin,goods_id,amazon_version,
                temu_version,pair_revision,status,updated_at)
                VALUES('fixture','A','T','a','t','r','CANDIDATE','now')""")
    finally:
        first.close()
    expected = rules.text_gate(cat, 'cotton', 'polyester')
    assert _evaluate_text_gate_args(('declarative:fixture', 'cotton', 'polyester', json.dumps(cat))) == expected
    installed = rules.attribute_values
    assert install_attribute_cache() is installed
    assert hasattr(installed, 'cache_info')
    second = IncrementalStage12(run, **kwargs)
    try:
        assert second._text_gate_revision == revision
        assert second.ensure_processing_contract() == {'changed': False, 'rescheduled': 0, 'pair_revisions_preserved': True}
        assert second.con.execute("SELECT value FROM processing_metadata WHERE key='stage12_contract'").fetchone()[0] == contract
        assert second.con.execute('SELECT status FROM pairs').fetchone()[0] == 'CANDIDATE'
    finally:
        second.close()
    assert hashlib.sha256(engine.read_bytes()).hexdigest() == before_digest
