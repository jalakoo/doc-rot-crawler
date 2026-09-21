from __future__ import annotations

import threading

import pytest

from docrot.scan import execute_scan
from docrot.worker import AlreadyActive, ScanWorker
from tests.fakes import fake_runner


def test_worker_runs_scans_one_at_a_time(store, cfg):
    active, peak, lock = [0], [0], threading.Lock()

    def execute(s, scan_id):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            execute_scan(s, scan_id, runner=fake_runner(delay=0.01), cfg=cfg)
        finally:
            with lock:
                active[0] -= 1

    worker = ScanWorker(store, execute)
    ids = [store.create([f"https://docs{i}.a.io"], []).id for i in range(3)]
    for i in ids:
        worker.enqueue(i)
    worker.join()
    worker.stop()
    assert peak[0] == 1
    assert [store.status(i).state for i in ids] == ["done"] * 3


def test_enqueue_refuses_an_active_scan(store, cfg):
    gate = threading.Event()
    worker = ScanWorker(store, lambda s, i: gate.wait(5))
    rec = store.create(["https://docs.a.io"], [])
    worker.enqueue(rec.id)
    with pytest.raises(AlreadyActive):
        worker.enqueue(rec.id)
    gate.set()
    worker.join()
    worker.stop()


def test_worker_survives_an_executor_exception(store):
    def explode(s, scan_id):
        raise RuntimeError("boom")

    worker = ScanWorker(store, explode)
    rec = store.create(["https://docs.a.io"], [])
    worker.enqueue(rec.id)
    worker.join()
    assert store.status(rec.id).state == "failed"
    rec2 = store.create(["https://docs2.a.io"], [])
    worker.enqueue(rec2.id)                    # still alive
    worker.join()
    worker.stop()
