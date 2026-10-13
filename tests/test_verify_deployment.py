from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import company_setup
import verify_deployment
from selection_automation import secrets


def test_fresh_checkout_does_not_claim_workflow_ready(tmp_path):
    result = verify_deployment.inspect(tmp_path)
    assert not result["ready"]
    assert any("公司" in item for item in result["missing"])


def test_company_key_is_checked_before_workflow_is_ready(tmp_path, monkeypatch):
    secret = tmp_path / "runtime/secrets/company.dpapi"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"fixture")
    monkeypatch.setattr(verify_deployment.shutil, "which", lambda name: name)
    monkeypatch.setattr(verify_deployment.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(company_setup, "find_adspower_executable", lambda company: "AdsPower.exe")
    checked = []
    monkeypatch.setattr(company_setup, "ensure_adspower_api", lambda company: checked.append(True))
    company = {"adspower_api_key": "REPLACE_WITH_COMPANY_ADSPOWER_API_KEY",
               "proxy": {"host": "proxy.example", "port": 1234, "username": "user_session-one", "password": "secret"}}
    monkeypatch.setattr(secrets, "load", lambda path: company)
    invalid = verify_deployment.inspect(tmp_path)
    assert not invalid["ready"]
    assert not checked
    company["adspower_api_key"] = "fixture-key"
    valid = verify_deployment.inspect(tmp_path)
    assert valid == {"ready": True, "missing": []}
    assert checked == [True]
