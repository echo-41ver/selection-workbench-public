import json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from replace_proxyshare_worker_environment import timed_phase

def test_phase_failure_is_recorded_and_preserves_original_exception(tmp_path):
    p=tmp_path/'events.jsonl'
    assert timed_phase(p,2,'start_new_browser',lambda: {'ready':True})=={'ready':True}
    error=TimeoutError('sensitive-url-and-password')
    def failure():raise error
    with pytest.raises(TimeoutError) as caught:
        timed_phase(p,2,'bind_browser_attempt_1',failure)
    assert caught.value is error
    events=[json.loads(x) for x in p.read_text().splitlines()]
    assert len(events)==2 and events[1]['error_type']=='TimeoutError'
    assert all(e['elapsed_seconds']>=0 for e in events)
    assert 'sensitive' not in p.read_text()
