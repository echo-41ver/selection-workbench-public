"""Background discovery must expose early models and remain cancellable.

Every endpoint and metadata reader is fake. Never inspect actual listening
ports, walk real disks, start a model server, or send inference requests.
"""
from concurrent.futures import Future, TimeoutError as FuturesTimeout
from types import SimpleNamespace
import threading
import time

import pytest

from selection_automation import local_model_inventory as inventory
from selection_automation import local_model_service as service


def ready_catalog(*, partial=False):
    model = {"id": "d1-3b", "name": "fixture local D1", "display_name": "D1",
             "vision": True, "source": "fixture native D1", "status": "ready",
             "base_url": "http://127.0.0.1:18181/v1/systemone", "protocol": "decisions"}
    return {"models": [model], "candidates": [], "partial": partial,
            "service_connected": True, "started": False,
            "services": [{"base_url": "http://127.0.0.1:18181", "source": "fixture native D1"}],
            "message": "Fixture metadata only; no inference has been sent."}


def test_native_metadata_is_published_before_slow_listener_and_not_probed_twice(tmp_path, monkeypatch):
    entered, release, visible = threading.Event(), threading.Event(), threading.Event()
    native = 'http://127.0.0.1:25001'; slow = 'http://127.0.0.1:25002'; probed = []; native_calls = []
    from selection_automation.local_d1 import SOURCE
    model={**ready_catalog()['models'][0], 'source':SOURCE, 'base_url':native+'/v1/decide', 'size':0}
    def native_probe(base):
        native_calls.append(base)
        if base==native:return {'base_url':native,'source':SOURCE,'rows':[model]}
        entered.set();assert release.wait(2);return None
    monkeypatch.setattr(service,'_endpoints',lambda:[slow,native])
    monkeypatch.setattr(service,'_probe_native',native_probe)
    monkeypatch.setattr(service,'_probe',lambda base:probed.append(base))
    result=[]
    def progress(data):
        if data['models']:visible.set()
    thread=threading.Thread(target=lambda:result.append(service.catalog(tmp_path,on_progress=progress)))
    thread.start()
    try:
        assert entered.wait(1) and visible.wait(1)
        assert thread.is_alive() and not probed
    finally:release.set();thread.join(2)
    assert not thread.is_alive() and result[0]['models'][0]['base_url']==native+'/v1/decide'
    assert sorted(native_calls)==sorted([native,slow]) and probed==[slow]


def test_native_compatibility_and_capability_phases_share_total_deadline(tmp_path, monkeypatch):
    clock=[0.0]; budgets=[]
    monkeypatch.setattr(service.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(service,'_endpoints',lambda:['http://127.0.0.1:25001','http://127.0.0.1:25002'])
    monkeypatch.setattr(service,'_probe_native',lambda base:None)
    def probe(base):
        return {'base_url':base,'source':'Ollama' if base.endswith('25001') else 'API','rows':[{'name':'fixture'}]}
    monkeypatch.setattr(service,'_probe',probe)
    monkeypatch.setattr(service,'_describe',lambda *args:None)
    monkeypatch.setattr(service,'_api_models',lambda *args:([],[]))
    def complete(pending,seconds,cancel=None):
        budgets.append(seconds);clock[0]+=seconds
        yield from pending
    monkeypatch.setattr(service,'_completed',complete)
    result=service.catalog(tmp_path,full=True)
    assert len(budgets)==4 and sum(budgets)==24 and all(value>=0 for value in budgets)
    assert result['models']==[]


def test_cancel_after_native_result_does_not_start_compatibility_requests(tmp_path, monkeypatch):
    cancel=threading.Event()
    from selection_automation.local_d1 import SOURCE
    model={**ready_catalog()['models'][0],'source':SOURCE,'size':0}
    monkeypatch.setattr(service,'_endpoints',lambda:['http://127.0.0.1:25001'])
    monkeypatch.setattr(service,'_probe_native',lambda base:{'base_url':base,'source':SOURCE,'rows':[model]})
    monkeypatch.setattr(service,'_probe',lambda base:pytest.fail('No compatibility request after cancel'))
    def progress(data):
        if data['models']:cancel.set()
    result=service.catalog(tmp_path,cancel=cancel,on_progress=progress)
    assert result['partial'] and result['models'][0]['id']=='d1-3b'


def wait_for_status(root, predicate, *, seconds=2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = inventory.scan_status(root)
        if predicate(state):
            return state
        time.sleep(0.005)
    raise AssertionError("Background scan did not reach the expected local test state")


@pytest.fixture(autouse=True)
def prevent_machine_discovery(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("This test must not discover real endpoints or start/query a model")
    monkeypatch.setattr(service, "_endpoints", forbidden)
    monkeypatch.setattr(service, "_json", forbidden)
    monkeypatch.setattr(service, "_start", forbidden)
    monkeypatch.setattr(inventory, "scan_roots", lambda: [])


def test_service_progress_is_visible_while_catalog_is_still_waiting(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    metadata = ready_catalog()
    def slow_catalog(root, **kwargs):
        kwargs["on_progress"](metadata)
        entered.set()
        assert release.wait(2), "Test must release the fake metadata reader"
        return metadata
    monkeypatch.setattr(service, "catalog", slow_catalog)
    started = inventory.start_scan(tmp_path)
    try:
        assert entered.wait(1)
        state = inventory.scan_status(tmp_path)
        assert state["scan_id"] == started["scan_id"] and state["running"] is True
        assert state["phase"] == "services"
        assert state["services"]["models"][0]["id"] == "d1-3b"
        assert state["services"]["models"][0]["vision"] is True
    finally:
        release.set()
        terminal = wait_for_status(tmp_path, lambda value: value.get("running") is False)
    assert terminal["status"] == "complete" and terminal["phase"] == "done"
    assert terminal["services"]["models"][0]["id"] == "d1-3b"


def test_cancel_during_service_wait_retains_incrementally_discovered_model(tmp_path, monkeypatch):
    entered, released = threading.Event(), threading.Event()
    metadata = ready_catalog()
    def cancellable_catalog(root, **kwargs):
        kwargs["on_progress"](metadata)
        entered.set()
        deadline = time.monotonic() + 2
        while not kwargs["cancel"].is_set() and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        assert kwargs["cancel"].is_set(), "Cancellation must reach the service metadata wait"
        released.set()
        return {**metadata, "partial": True}
    monkeypatch.setattr(service, "catalog", cancellable_catalog)
    started = inventory.start_scan(tmp_path)
    try:
        assert entered.wait(1)
        inventory.cancel_scan(tmp_path)
        assert released.wait(0.5)
        terminal = wait_for_status(tmp_path, lambda value: value.get("running") is False)
        assert terminal["status"] == "cancelled" and terminal["phase"] == "done"
        assert terminal["scan_id"] == started["scan_id"]
        assert terminal["services"]["models"][0]["id"] == "d1-3b"
    finally:
        inventory.cancel_scan(tmp_path)
        released.wait(0.5)
        wait_for_status(tmp_path, lambda value: value.get("running") is False)


def test_whole_scan_budget_preserves_early_model_and_reports_partial(tmp_path, monkeypatch):
    metadata = ready_catalog()
    monkeypatch.setattr(inventory, "_SCAN_SECONDS", 0.02)
    def slow_catalog(root, **kwargs):
        kwargs["on_progress"](metadata)
        # Simulate one in-flight metadata read returning after the total budget.
        # The inventory must not reset its budget before the filesystem phase.
        threading.Event().wait(0.05)
        return metadata
    monkeypatch.setattr(service, "catalog", slow_catalog)
    inventory.start_scan(tmp_path)
    terminal = wait_for_status(tmp_path, lambda value: value.get("running") is False)
    assert terminal["status"] == "partial" and terminal["partial"] is True
    assert terminal["phase"] == "done"
    assert terminal["services"]["models"][0]["id"] == "d1-3b"


def test_files_phase_budget_retains_service_and_file_discoveries(tmp_path, monkeypatch):
    metadata = ready_catalog()
    (tmp_path / "pending-child").mkdir()
    clock = [0.0]
    monkeypatch.setattr(inventory, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(inventory, "_SCAN_SECONDS", 0.03)
    monkeypatch.setattr(inventory, "scan_roots", lambda: [tmp_path])
    def catalog(root, **kwargs):
        kwargs["on_progress"](metadata)
        return metadata
    monkeypatch.setattr(service, "catalog", catalog)
    visited = []
    def inspect(path, files, cancel=None):
        visited.append(path)
        assert inventory.scan_status(tmp_path)["phase"] == "files"
        clock[0] = 0.06  # Expire the same 30ms budget only after this read starts.
        return [{"name": "fixture.gguf", "path": str(tmp_path / "fixture.gguf"),
                 "source": "GGUF", "vision": None, "status": "files_only"}]
    monkeypatch.setattr(inventory, "inspect_directory", inspect)
    inventory.start_scan(tmp_path)
    terminal = wait_for_status(tmp_path, lambda value: value.get("running") is False)
    assert terminal["status"] == "partial" and terminal["partial"] is True
    assert terminal["phase"] == "done" and visited == [tmp_path]
    assert terminal["services"]["models"][0]["id"] == "d1-3b"
    assert terminal["models"][0]["status"] == "files_only"


def test_completed_observes_cancel_without_any_completed_future():
    cancel, entered, exited = threading.Event(), threading.Event(), threading.Event()
    pending = [Future(), Future()]
    outcome = []
    def wait_for_fake_work():
        entered.set()
        try:
            list(service._completed(pending, seconds=10, cancel=cancel))
            outcome.append("unexpected-success")
        except FuturesTimeout:
            outcome.append("cancelled")
        finally:
            exited.set()
    worker = threading.Thread(target=wait_for_fake_work, name="fixture-local-catalog-wait")
    worker.start()
    try:
        assert entered.wait(0.5)
        cancel.set()
        assert exited.wait(0.5), "Cancellation must not wait for an unrelated future to complete"
        assert outcome == ["cancelled"]
        assert all(not future.done() for future in pending)
    finally:
        cancel.set()
        worker.join(1)
    assert not worker.is_alive()


def test_completed_total_budget_is_bounded_without_completed_futures():
    pending = [Future(), Future()]
    started = time.monotonic()
    with pytest.raises(FuturesTimeout):
        list(service._completed(pending, seconds=0.02, cancel=threading.Event()))
    assert time.monotonic() - started < 0.5
    assert all(not future.done() for future in pending)


def test_completed_yields_existing_results_before_later_cancellation():
    ready, pending = Future(), Future()
    ready.set_result("fixture metadata")
    cancel = threading.Event()
    results = service._completed([ready, pending], seconds=1, cancel=cancel)
    try:
        assert next(results) is ready
        cancel.set()
        with pytest.raises(FuturesTimeout):
            next(results)
        assert not pending.done()
    finally:
        results.close()
