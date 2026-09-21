"""A real dashboard server on a free port: temp store, fake scans."""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from docrot.config import Config
from docrot.scan import execute_scan
from docrot.server import create_app
from docrot.store import Store
from docrot.worker import ScanWorker
from tests.fakes import fake_runner

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Dashboard:
    def __init__(self, root, delay=0.25):
        self.store = Store(root)
        cfg = Config(neo4j_uri="bolt://127.0.0.1:1")
        self.fail_next = ""

        def execute(store, scan_id):
            fail, self.fail_next = self.fail_next, ""
            execute_scan(store, scan_id, runner=fake_runner(delay=delay, fail=fail), cfg=cfg)

        self.worker = ScanWorker(self.store, execute)
        port = _free_port()
        self.url = f"http://127.0.0.1:{port}"
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.store, self.worker),
                                                    host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("dashboard did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.worker.stop()


@pytest.fixture
def dashboard(tmp_path):
    with Dashboard(tmp_path / "data") as d:
        yield d
