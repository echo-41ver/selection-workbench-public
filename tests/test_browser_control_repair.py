import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import repair_browser_control
import company_setup
import replace_proxyshare_worker_environment as replacement

def test_local_control_repair_keeps_the_existing_profile_and_targets_only_one_lane(monkeypatch):
    session={'name':'worker-03-new','profile_id':'existing-profile','webdriver_url':'http://127.0.0.1:4453'}
    calls=[]
    monkeypatch.setattr(replacement,'api_base',lambda:'local-api')
    monkeypatch.setattr(company_setup,'rebind_existing_worker',lambda *args:calls.append(args))
    repair_browser_control.repair(session)
    assert len(calls)==1
    assert calls[0][:3]==('local-api',session,4453)
    assert session['profile_id']=='existing-profile'
