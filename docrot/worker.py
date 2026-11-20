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
        # scans this worker queued and has not finished: lets a caller tell its
        # own queue from a scan some other process left running in the store
        self._owned: set[str] = set()
        self._thread = threading.Thread(target=self._loop, name="docrot-scans",
                                        daemon=True)
        self._thread.start()

    def enqueue(self, scan_id: str) -> ScanStatus:
        with self._lock:
            # a scan stuck "running" from a dead process is not active: re-running
            # it is how the user gets out of that state
            if self.store.is_active(scan_id):
                raise AlreadyActive(scan_id)
            queued = ScanStatus(state="queued", queued_at=now(),
                                log=[["t-sys", "  queued - waiting for the scan ahead to finish"]])
            self.store.set_status(scan_id, queued)
            self._owned.add(scan_id)
            self._queue.put(scan_id)
            return queued

    def owns(self, scan_id: str) -> bool:
        """Is this scan queued or running in *this* worker (not merely in the store)?"""
        with self._lock:
            return scan_id in self._owned

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
                with self._lock:
                    self._owned.discard(scan_id)
                self._queue.task_done()

    def join(self) -> None:
        """Wait for everything queued so far (tests)."""
        self._queue.join()

    def stop(self) -> list[str]:
        """Stop taking work and return the scans this worker still owned.

        Scans waiting in the queue are dropped rather than started, so a
        shutdown does not begin minutes of sandbox work. The scan already
        running cannot be interrupted from here; it gets a few seconds to
        finish, and if it does not it is returned so the caller can record it
        as interrupted (the thread is a daemon and dies with the process)."""
        try:
            while True:
                if self._queue.get_nowait() is not None:
                    self._queue.task_done()
        except queue.Empty:
            pass
        self._queue.put(None)
        self._thread.join(timeout=5)
        with self._lock:
            return sorted(self._owned)
