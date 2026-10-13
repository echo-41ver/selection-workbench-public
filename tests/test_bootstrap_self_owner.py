"""The new supervisor owns startup, but must not block retirement of an ended task."""
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import company_setup
import selection
import retain_proxy_connections
import setup_proxyshare_adspower_workers as setup


def fixture(monkeypatch,tmp_path,owner):
    monkeypatch.setattr(company_setup,'ROOT',tmp_path)
    monkeypatch.setattr(selection,'identity',lambda:'mine')
    monkeypatch.setattr(selection,'read',lambda *args:{'task_key':'old','machine_id':'mine','workers':[]})
    monkeypatch.setattr(selection,'live_owner',lambda:owner)
    monkeypatch.setattr(selection,'save',lambda *args:None)
    monkeypatch.setattr(retain_proxy_connections,'owned_chains',lambda root:{})
    monkeypatch.setattr(retain_proxy_connections,'stop_owned',lambda root:None)
    retired=[]
    monkeypatch.setattr(setup,'retire_for_task',retired.append)
    return retired


def test_new_starting_supervisor_can_retire_ended_previous_task(monkeypatch,tmp_path):
    retired=fixture(monkeypatch,tmp_path,{'category_key':'new'})
    assert company_setup.cleanup_ended_task('old',preparing_task='new') is True
    assert retired==['new']


@pytest.mark.parametrize('owner,preparing',[
    ({'category_key':'old'},'new'),({'category_key':'other'},'new'),
    ({'category_key':'new'},None),({'category_key':'old'},'old'),
])
def test_live_task_remains_protected(monkeypatch,tmp_path,owner,preparing):
    retired=fixture(monkeypatch,tmp_path,owner)
    with pytest.raises(RuntimeError,match='监督器仍在运行'):
        company_setup.cleanup_ended_task('old',preparing_task=preparing)
    assert retired==[]
