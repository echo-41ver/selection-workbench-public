import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import attach_local_analysis as bridge


def test_bridge_requires_explicit_current_run_web_authorization(tmp_path):
    base=tmp_path/'remote';(base/'stage123').mkdir(parents=True)
    marker=base/'stage123/paid_enabled.json'
    assert not bridge.paid_requested(base)
    for data in ({'category_key':'other','source':'web-console'},
                 {'category_key':'remote','source':'sample-test'},
                 {'category_key':'remote','source':'web-console'}):
        marker.write_text(json.dumps(data))
        assert bridge.paid_requested(base)==(data['category_key']=='remote' and data['source']=='web-console')


def test_full_supervisor_handoff_waits_for_exact_bridge_process(tmp_path,monkeypatch):
    state=tmp_path/'analysis_bridge.json';state.write_text(json.dumps({'pid':123,'created':10.0}))
    calls=[]
    monkeypatch.setattr(bridge.psutil,'Process',lambda pid:SimpleNamespace(create_time=lambda:10.0,wait=lambda timeout:calls.append(timeout)))
    bridge.handoff(tmp_path)
    assert calls==[15] and (tmp_path/'analysis_bridge.handoff').exists()
    (tmp_path/'analysis_bridge.handoff').unlink()
    monkeypatch.setattr(bridge.psutil,'Process',lambda pid:SimpleNamespace(create_time=lambda:11.0))
    bridge.handoff(tmp_path)
    assert not (tmp_path/'analysis_bridge.handoff').exists()
