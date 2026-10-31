import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import local_proxy_preflight as command


@pytest.mark.parametrize('new_pid,expected',[(2,0),(1,1)])
def test_saved_validation_reuses_existing_same_endpoint_owner_refresh(tmp_path,monkeypatch,new_pid,expected):
    path=tmp_path/'runtime/secrets/proxyshare.settings.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'local_proxy_owner':{'pid':1}}))
    monkeypatch.setattr(command,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['local_proxy_preflight.py','--validate-saved'])
    monkeypatch.setattr(command,'verify_saved',lambda settings:settings['local_proxy_owner']['pid']==2)
    observed=[]
    def refresh(root,settings):
        observed.append((root,settings['local_proxy_owner']['pid']))
        return {'local_proxy_owner':{'pid':new_pid}}
    monkeypatch.setattr(command,'refresh_saved_owner',refresh)
    assert command.main()==expected
    assert observed==[(tmp_path,1)]
