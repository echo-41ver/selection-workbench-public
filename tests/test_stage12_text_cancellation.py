"""Stop revokes undispatched text work without returning a partial success batch."""
from concurrent.futures import CancelledError, Future
from types import SimpleNamespace

import pytest

from selection_workflow import incremental_stage12 as engine


class Pool:
    def __init__(self, max_workers=2):
        self.max_workers = max_workers
        self.calls = []
        self.shutdowns = []

    def submit(self, function, values):
        future = Future()
        self.calls.append((future, function, values))
        return future

    def shutdown(self, *, wait, cancel_futures):
        self.shutdowns.append((wait, cancel_futures))


def pipeline(workers=1, stop=None):
    value = engine.IncrementalStage12.__new__(engine.IncrementalStage12)
    value.text_gate_workers = workers
    value._text_gate_pool = None
    value.stop_requested = stop or (lambda: False)
    return value


def inputs(size=300):
    return [("fixture", "amazon", str(index)) for index in range(size)]


def test_preexisting_stop_never_runs_a_gate_or_constructs_pool(monkeypatch):
    value = pipeline(2, lambda: True)
    monkeypatch.setattr(engine, "ProcessPoolExecutor", lambda **kwargs: pytest.fail("No pool after Stop"))
    monkeypatch.setattr(engine, "_evaluate_text_gate_args", lambda values: pytest.fail("No text work after Stop"))
    with pytest.raises(CancelledError):
        value._evaluate_text_gates(inputs())


@pytest.mark.parametrize("workers", [1, 2])
def test_serial_path_observes_stop_between_items_and_never_returns_partial_batch(monkeypatch, workers):
    stopped = [False]
    value = pipeline(workers, lambda: stopped[0])
    calls = []
    def evaluate(values):
        calls.append(values)
        if len(calls) == 3:
            stopped[0] = True
        return {"index": int(values[-1])}
    monkeypatch.setattr(engine, "_evaluate_text_gate_args", evaluate)
    with pytest.raises(CancelledError):
        value._evaluate_text_gates(inputs(10))
    assert len(calls) == 3 and value._text_gate_pool is None


def test_serial_success_preserves_original_order(monkeypatch):
    value = pipeline()
    monkeypatch.setattr(engine, "_evaluate_text_gate_args", lambda values: {"index": int(values[-1])})
    assert value._evaluate_text_gates(inputs(5)) == [{"index": index} for index in range(5)]


def test_last_serial_item_stop_is_not_reported_as_a_completed_batch(monkeypatch):
    stopped = [False]
    value = pipeline(stop=lambda: stopped[0])
    def evaluate(values):
        stopped[0] = True
        return {"index": 0}
    monkeypatch.setattr(engine, "_evaluate_text_gate_args", evaluate)
    with pytest.raises(CancelledError):
        value._evaluate_text_gates(inputs(1))


def test_parallel_stop_cancels_pending_work_and_does_not_queue_the_rest(monkeypatch):
    import concurrent.futures
    stopped = [False]
    value = pipeline(2, lambda: stopped[0])
    pool = Pool()
    monkeypatch.setattr(engine, "ProcessPoolExecutor", lambda **kwargs: pool)
    def wait(futures, *, timeout, return_when):
        assert timeout <= 0.05
        assert return_when == concurrent.futures.FIRST_COMPLETED
        stopped[0] = True
        return set(), set(futures)
    monkeypatch.setattr(concurrent.futures, "wait", wait)
    with pytest.raises(CancelledError):
        value._evaluate_text_gates(inputs())
    assert len(pool.calls) == 2
    assert all(future.cancelled() for future, _, _ in pool.calls)
    assert pool.shutdowns == [(False, True)]
    assert value._text_gate_pool is None


def test_stop_does_not_wait_for_or_claim_success_of_an_already_running_item(monkeypatch):
    import concurrent.futures
    stopped = [False]
    value = pipeline(2, lambda: stopped[0])
    pool = Pool()
    monkeypatch.setattr(engine, "ProcessPoolExecutor", lambda **kwargs: pool)
    def wait(futures, **kwargs):
        assert futures[0].set_running_or_notify_cancel()
        stopped[0] = True
        return set(), set(futures)
    monkeypatch.setattr(concurrent.futures, "wait", wait)
    with pytest.raises(CancelledError):
        value._evaluate_text_gates(inputs())
    assert pool.calls[0][0].running() and not pool.calls[0][0].done()
    assert pool.calls[1][0].cancelled()
    assert pool.shutdowns == [(False, True)] and len(pool.calls) == 2
    # Finishing the one in-flight call does not requeue anything or publish a
    # partial batch result. No private worker process access is necessary.
    pool.calls[0][0].set_result({"index": 0})
    assert len(pool.calls) == 2


def test_parallel_out_of_order_success_is_bounded_and_returns_input_order(monkeypatch):
    import concurrent.futures
    value = pipeline(2)
    pool = Pool()
    monkeypatch.setattr(engine, "ProcessPoolExecutor", lambda **kwargs: pool)
    maximum_pending = [0]
    def wait(futures, **kwargs):
        maximum_pending[0] = max(maximum_pending[0], len(futures))
        future = futures[-1]
        values = next(values for candidate, _, values in pool.calls if candidate is future)
        future.set_result({"index": int(values[-1])})
        return {future}, set(futures) - {future}
    monkeypatch.setattr(concurrent.futures, "wait", wait)
    assert value._evaluate_text_gates(inputs()) == [{"index": index} for index in range(300)]
    assert maximum_pending[0] == 2 and len(pool.calls) == 300
    assert value._text_gate_pool is pool and pool.shutdowns == []
    value._cancel_text_gate_work()


def test_parallel_worker_exception_is_preserved_and_other_pending_work_cancelled(monkeypatch):
    import concurrent.futures
    value = pipeline(2)
    pool = Pool()
    monkeypatch.setattr(engine, "ProcessPoolExecutor", lambda **kwargs: pool)
    def wait(futures, **kwargs):
        futures[0].set_exception(ValueError("actual text failure"))
        return {futures[0]}, set(futures[1:])
    monkeypatch.setattr(concurrent.futures, "wait", wait)
    with pytest.raises(ValueError, match="actual text failure"):
        value._evaluate_text_gates(inputs())
    assert len(pool.calls) == 2
    assert pool.calls[1][0].cancelled()
    assert pool.shutdowns == [(False, True)]


@pytest.mark.parametrize("stopped, expected_wait", [(True, False), (False, True)])
def test_close_waits_only_for_normal_completion(stopped, expected_wait):
    value = pipeline(stop=lambda: stopped)
    pool = Pool()
    value._text_gate_pool = pool
    calls = []
    value._image_transport = SimpleNamespace(close=lambda: calls.append("transport"))
    value.con = SimpleNamespace(close=lambda: calls.append("database"))
    value.close()
    assert pool.shutdowns == [(expected_wait, True)]
    assert value._text_gate_pool is None and calls == ["transport", "database"]


def test_cancel_is_idempotent_and_does_not_close_the_database():
    value = pipeline()
    pool = Pool()
    value._text_gate_pool = pool
    pending = Future()
    value._cancel_text_gate_work([pending])
    value._cancel_text_gate_work([pending])
    assert pending.cancelled() and pool.shutdowns == [(False, True)]
