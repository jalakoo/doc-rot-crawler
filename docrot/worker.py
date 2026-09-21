"""The dashboard's scan queue: one worker thread, one scan at a time.

Serial on purpose. The sandbox account has a hard CPU ceiling that a single
scan already runs close to, and every scan ends by sweeping *all* live
sandboxes - two scans at once would exceed the quota and delete each other's
sandboxes mid-run.
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from .store import ScanStatus, Store, now


class AlreadyActive(Exception):
    """The scan is already queued or running."""


class ScanWorker:
    def __init__(self, store: Store, execute: Callable[[Store, str], object]):
        self.store = store
        self.execute = execute
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="docrot-scans",
                                        daemon=True)
        self._thread.start()

    def enqueue(self, scan_id: str) -> ScanStatus:
        with self._lock:
            st = self.store.status(scan_id)
            if st.state in ("queued", "running"):
                raise AlreadyActive(scan_id)
            queued = ScanStatus(state="queued", queued_at=now(),
                                log=[["t-sys", "  queued - waiting for the scan ahead to finish"]])
            self.store.set_status(scan_id, queued)
            self._queue.put(scan_id)
            return queued

    def _loop(self) -> None:
        while True:
            scan_id = self._queue.get()
            if scan_id is None:
                return
            try:
                self.execute(self.store, scan_id)
            except Exception as e:           # keep the worker alive
                st = self.store.status(scan_id)
                st.state, st.finished_at = "failed", now()
                st.error = f"{type(e).__name__}: {e}"[:500]
                self.store.set_status(scan_id, st)
            finally:
                self._queue.task_done()

    def join(self) -> None:
        """Wait for everything queued so far (tests)."""
        self._queue.join()

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)
