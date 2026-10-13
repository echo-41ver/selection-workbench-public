import importlib.util,pathlib
s=importlib.util.spec_from_file_location("diagnostics",pathlib.Path(__file__).resolve().parents[1]/"src/selection_automation/adspower_diagnostics.py");m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
def test_vendor_missing_kernel_keeps_secrets_out():
 error=m.provider_error("/api/v1/browser/start?token=secret",-1,"FlowerBrowser 144 is not ready,please to download! token=secret proxy=user:pass")
 assert error.diagnostic["category"]=="KERNEL_MISSING"
 assert not error.diagnostic["retryable"]
 assert "secret" not in str(error) and "user:pass" not in str(error)
