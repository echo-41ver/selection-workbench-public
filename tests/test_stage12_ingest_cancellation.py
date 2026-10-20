"""Normal Stop rolls back only the uncommitted snapshot, preserving prior work."""
from concurrent.futures import CancelledError
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_incremental_stage12 as fixtures
from selection_workflow import incremental_stage12 as engine


@pytest.fixture
def case():
    value = fixtures.IncrementalStage12Tests()
    value.setUp()
    try:
        yield value
    finally:
        value.tearDown()


def write_front(case, identities):
    fixtures.write_jsonl(case.front_path, [case.front(identity, 'red floral shower curtain', case.t1)
                                          for identity in identities])


def table_state(pipeline):
    return {table: [tuple(row) for row in pipeline.con.execute(f'SELECT * FROM {table} ORDER BY rowid')]
            for table in ('products', 'product_versions', 'observations', 'pairs', 'candidates', 'sources', 'counters')}


def test_stop_during_normalization_preserves_prior_source_error_and_snapshot(case, monkeypatch):
    write_front(case, ['old'])
    case.pipeline.ingest_source('front')
    before = table_state(case.pipeline)
    write_front(case, ['old', 'new1', 'new2'])
    stopped = [False]
    original = engine.normalize_record
    calls = []
    def normalize(*args, **kwargs):
        calls.append(args[0]['goods_id'])
        result = original(*args, **kwargs)
        stopped[0] = True
        return result
    monkeypatch.setattr(engine, 'normalize_record', normalize)
    case.pipeline.stop_requested = lambda: stopped[0]
    with pytest.raises(CancelledError):
        case.pipeline.ingest_source('front')
    assert calls == ['old'] and table_state(case.pipeline) == before


def test_stop_during_pair_scheduling_rolls_back_current_snapshot_then_resumes(case, monkeypatch):
    fixtures.write_jsonl(case.amazon_path, [case.amazon('A1', 'red floral shower curtain', case.a1),
                                          case.amazon('A2', 'red floral shower curtain', case.a2)])
    write_front(case, ['old'])
    case.pipeline.ingest_source('amazon')
    case.pipeline.ingest_source('front')
    before = table_state(case.pipeline)
    write_front(case, ['old', 'new1', 'new2'])
    stopped = [False]
    original = case.pipeline._upsert_pair
    calls = []
    def schedule(*args):
        calls.append(args[:3])
        original(*args)
        stopped[0] = True
    case.pipeline.stop_requested = lambda: stopped[0]
    with monkeypatch.context() as context:
        context.setattr(case.pipeline, '_upsert_pair', schedule)
        with pytest.raises(CancelledError):
            case.pipeline.ingest_source('front')
    assert len(calls) == 1 and table_state(case.pipeline) == before
    stopped[0] = False
    result = case.pipeline.ingest_source('front')
    assert result['new'] == 2 and result['received'] == 3
    assert case.pipeline.con.execute('SELECT count(*) FROM pairs').fetchone()[0] == 6
