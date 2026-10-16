"""The public CLI ingest-only flag must suppress model and analysis side effects."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'src')]
import run_incremental_stage12 as entry
from selection_workflow.incremental_stage12 import IncrementalStage12, VECTOR_REVISION


def config_file(tmp_path):
    amazon = tmp_path/'amazon.jsonl'; temu = tmp_path/'temu.jsonl'
    amazon.write_text(json.dumps({'asin':'A1','title':'Offline original','main_image_url':'https://example.invalid/a.jpg'})+'\n')
    temu.write_text(json.dumps({'goods_id':'T1','title':'Offline sample','price_usd':2.5,'sales_text':None,
                               'source':'VISIBLE_TEMU_FRONTEND','main_image_url':'https://example.invalid/t.jpg'})+'\n')
    config = {'output_dir':str(tmp_path/'stage12'),'batch_size':7,'poll_seconds':0,
              'sources':[{'key':'amazon','platform':'AMAZON','adapter':'amazon_generic','path':str(amazon),'snapshot_mode':'replace'},
                         {'key':'temu','platform':'TEMU','adapter':'temu_frontend','path':str(temu),'snapshot_mode':'replace'}],
              'paths':[{'key':'match','amazon_source':'amazon','temu_source':'temu','label':'Offline'}],
              'candidate_materialize_interval_seconds':0,'candidate_materialize_background':True,
              'excel':{'auto_update':True,'background':True,'interval_seconds':0,'min_processed_delta':0}}
    path = tmp_path/'config.json'; path.write_text(json.dumps(config))
    return path


@pytest.mark.parametrize('command', ['once', 'watch'])
def test_cli_ingest_only_has_no_model_processing_or_exports(tmp_path, command):
    config = config_file(tmp_path)
    argv = ['stage12',command,'--config',str(config),'--no-process']
    if command=='watch': argv += ['--max-cycles','2']
    forbidden = AssertionError('ingest-only entered analysis or export')
    with patch.object(sys,'argv',argv), \
         patch('selection_workflow.incremental_stage12.TorchResNet50Vectorizer',side_effect=forbidden), \
         patch.object(IncrementalStage12,'process_batch',side_effect=forbidden), \
         patch.object(IncrementalStage12,'materialize_candidates',side_effect=forbidden), \
         patch.object(entry,'export_excel',side_effect=forbidden), \
         patch.object(entry.subprocess,'Popen',side_effect=forbidden), \
         contextlib.redirect_stdout(io.StringIO()):
        entry.main()
    con = sqlite3.connect(tmp_path/'stage12/stage12.sqlite3')
    try:
        assert dict(con.execute('SELECT source_key,count(*) FROM products GROUP BY source_key')) == {'amazon':1,'temu':1}
        assert con.execute('SELECT count(*) FROM pairs').fetchone()[0]==1
        assert con.execute('SELECT count(*) FROM images').fetchone()[0]==0
        assert con.execute('SELECT count(*) FROM vectors').fetchone()[0]==0
        product = json.loads(con.execute("SELECT record_json FROM products WHERE source_key='temu'").fetchone()[0])
        assert product['raw_record']['price_usd']==2.5 and product['raw_record']['sales_text'] is None
    finally:
        con.close()


@pytest.mark.parametrize('command', ['once', 'watch'])
def test_cli_without_flag_still_constructs_model_and_processes(tmp_path, command):
    config = config_file(tmp_path)
    value = json.loads(config.read_text())
    value['candidate_materialize_background'] = False
    value['excel']['auto_update'] = False
    config.write_text(json.dumps(value))
    argv = ['stage12', command, '--config', str(config)]
    if command == 'watch': argv += ['--max-cycles', '1']
    class LocalModel:
        revision = VECTOR_REVISION
    with patch.object(sys,'argv',argv), \
         patch('selection_workflow.incremental_stage12.TorchResNet50Vectorizer',return_value=LocalModel()) as model, \
         patch.object(IncrementalStage12,'process_batch',return_value={'selected':1}) as process, \
         patch.object(IncrementalStage12,'materialize_candidates',return_value={'count':0}) as candidates, \
         contextlib.redirect_stdout(io.StringIO()):
        entry.main()
    model.assert_called_once_with()
    process.assert_called_once_with(7)
    candidates.assert_called_once_with()


@pytest.mark.parametrize('name', ['light_bulb', 'mirror', 'pet_feeding_bowl'])
@pytest.mark.parametrize('mutation', ['missing_gate', 'unknown_gate', 'missing_revision', 'missing_threshold', 'boolean_threshold'])
def test_new_category_handoff_cannot_inherit_shower_defaults(tmp_path, name, mutation):
    config = {'schema_version':'incremental-stage12-category-v1', 'output_dir':str(tmp_path/name),
              'text_gate':'shower_curtain_v2', 'rule_version':'explicit-fixture',
              'candidate_threshold':60, 'sources':[], 'paths':[]}
    if mutation == 'missing_gate': config.pop('text_gate')
    elif mutation == 'unknown_gate': config['text_gate'] = 'unknown-category-gate'
    elif mutation == 'missing_revision': config.pop('rule_version')
    elif mutation == 'missing_threshold': config.pop('candidate_threshold')
    elif mutation == 'boolean_threshold': config['candidate_threshold'] = True
    with patch('selection_workflow.incremental_stage12.TorchResNet50Vectorizer',
               side_effect=AssertionError('Invalid category must fail before model construction')):
        with pytest.raises(ValueError): entry.open_pipeline(config)
    assert not (tmp_path/name).exists()


@pytest.mark.parametrize('name', ['light_bulb', 'mirror', 'pet_feeding_bowl'])
def test_incomplete_category_configuration_is_explained_and_can_be_corrected(tmp_path, name):
    path=config_file(tmp_path)
    config=json.loads(path.read_text())
    config.update(schema_version='incremental-stage12-category-v1')
    path.write_text(json.dumps(config))
    with patch.object(sys,'argv',['stage12','init','--config',str(path)]):
        with pytest.raises(SystemExit,match='重新导出 stage12_config.json'):
            entry.main()
    assert not (tmp_path/'stage12').exists()
    from selection_automation.identity_policy import compile_policy
    # The existing compiler owns category semantics; this correction fixture
    # uses a valid authored contract, not an API-generated quality claim.
    from test_identity_policy import category
    rule=tmp_path/(name+'.json'); rule.write_text(json.dumps(compile_policy(category(name))),encoding='utf-8')
    config.update(text_gate='declarative:'+str(rule),stage12_rule_version='corrected-'+name,
                  candidate_threshold=60)
    path.write_text(json.dumps(config))
    with patch.object(sys,'argv',['stage12','init','--config',str(path)]), contextlib.redirect_stdout(io.StringIO()):
        entry.main()
    assert (tmp_path/'stage12'/'stage12.sqlite3').exists()
