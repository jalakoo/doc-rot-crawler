"""The MCP server, driven the way an agent drives it: through a client session.

Most tests run the SDK's in-memory client against `build_server` with a temp
store and `fake_runner()`, so no sandbox, model or network is involved. One test
launches the real `docrot mcp` over stdio, because the thing that can go wrong
there - a stray byte on stdout - is invisible in memory.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from mcp.client import Client

from docrot import settings
from docrot.mcp_server import build_server, mark_interrupted
from docrot.scan import execute_scan
from docrot.store import ScanStatus
from docrot.worker import ScanWorker
from tests.fakes import fake_runner, run_async

DOCS, REPO = "https://docs.a.io", "https://github.com/a/alpha"
READ_ONLY = {"list_scans", "get_scan", "get_findings", "scan_diff", "rename_impact"}
ACTIONS = {"start_scan", "rerun_scan"}


class Agent:
    """A client session per call, which is how little state an agent can rely on."""

    def __init__(self, store, cfg):
        self.store, self.mode = store, {"failing": True, "gate": None}
        self.graph_rows: list[dict] = []
        self.worker = ScanWorker(store, self._execute)
        self.server = build_server(store, self.worker, blast=lambda symbol: self.graph_rows)
        self.cfg = cfg

    def _execute(self, store, scan_id):
        inner = fake_runner(failing=self.mode["failing"])

        def run(inp, cfg, log, phase):
            if self.mode["gate"]:
                phase(2)
                log("  held at extraction")
                self.mode["gate"].wait(10)
            return inner(inp, cfg, log, phase)
        execute_scan(store, scan_id, runner=run, cfg=self.cfg)

    def call(self, tool, **args):
        """(is_error, structured content or error text)"""
        async def go():
            async with Client(self.server) as c:
                r = await c.call_tool(tool, args)
                text = r.content[0].text if r.content else ""
                return r.is_error, (r.structured_content if not r.is_error else text), text
        err, data, text = run_async(go())
        self.last_text = text
        return err, data

    def ok(self, tool, **args):
        err, data = self.call(tool, **args)
        assert not err, data
        return data

    def fails(self, tool, **args) -> str:
        err, data = self.call(tool, **args)
        assert err, data
        return data

    def tools(self):
        async def go():
            async with Client(self.server) as c:
                return (await c.list_tools()).tools
        return {t.name: t for t in run_async(go())}

    def scan(self, **extra):
        data = self.ok("start_scan", docs=[DOCS], repos=[REPO], **extra)
        self.worker.join()
        return data["scan_id"]


@pytest.fixture
def agent(store, cfg):
    a = Agent(store, cfg)
    yield a
    if a.mode["gate"]:
        a.mode["gate"].set()
    a.worker.stop()


# ------------------------------------------------------------------- the menu

def test_tools_are_listed_with_hints_and_described_arguments(agent):
    tools = agent.tools()
    assert set(tools) == READ_ONLY | ACTIONS
    for name in READ_ONLY:
        assert tools[name].annotations.read_only_hint is True
    for name in ACTIONS:
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is False
    for name, tool in tools.items():
        assert tool.description and tool.output_schema, name
        for arg, spec in tool.input_schema.get("properties", {}).items():
            assert spec.get("description"), f"{name}.{arg} has no description"
    start = tools["start_scan"].description
    assert "minutes" in start and "credit" in start and "get_scan" in start
    assert set(tools["start_scan"].input_schema["required"]) == {"docs", "repos"}
    assert tools["start_scan"].input_schema["properties"]["ecosystem"]["enum"] == ["pypi", "npm", "nuget"]


def test_results_are_compact_json_not_indented(agent):
    agent.ok("list_scans")
    assert "\n" not in agent.last_text and json.loads(agent.last_text) == {"scans": []}


# ----------------------------------------------------------------- the round trip

def test_start_poll_and_read_findings(agent, store):
    started = agent.ok("start_scan", docs=[DOCS], repos=[REPO], name="Alpha", versions=2)
    assert started["state"] == "queued" and started["created"] is True and started["queued_behind"] == []
    scan_id = started["scan_id"]
    assert scan_id in started["next"] and "get_scan" in started["next"]
    agent.worker.join()

    status = agent.ok("get_scan", scan_id=scan_id)
    assert status["state"] == "done" and status["error"] == "" and status["phase"] == ""
    assert len(status["log_tail"]) <= 20 and status["log_lines"] >= len(status["log_tail"])
    assert "scan complete" in status["log_tail"][-1]
    [run] = status["runs"]
    assert run["findings"] == 2 and run["pages"] == 4

    [summary] = agent.ok("list_scans")["scans"]
    assert summary["id"] == scan_id and summary["name"] == "Alpha" and summary["state"] == "done"
    assert (summary["runs"], summary["last_run"], summary["findings"]) == (1, run["id"], 2)
    assert summary["docs"] == [DOCS] and summary["repos"] == [REPO]

    page = agent.ok("get_findings", scan_id=scan_id)
    assert page["run_id"] == run["id"] and page["total"] == 2 and page["next_offset"] is None
    assert page["by_kind"] == {"missing_symbol": 1, "stale_pin": 1}
    # the very records `docrot export` writes, not a second shape
    exported = [json.loads(line) for line in store.export_text(scan_id).splitlines()][1:]
    assert page["findings"] == exported
    assert all(f["type"] == "finding" for f in page["findings"])


def test_a_running_scan_reports_its_phase(agent):
    gate = agent.mode["gate"] = threading.Event()
    scan_id = agent.ok("start_scan", docs=[DOCS], repos=[REPO])["scan_id"]
    for _ in range(200):
        status = agent.ok("get_scan", scan_id=scan_id)
        if status["state"] == "running" and any("held at" in line for line in status["log_tail"]):
            break
        threading.Event().wait(0.02)
    assert status["state"] == "running" and status["phase"] == "extracting" and status["started_at"]
    gate.set()
    agent.worker.join()
    assert agent.ok("get_scan", scan_id=scan_id)["state"] == "done"


def test_a_failed_scan_says_why(agent, store):
    agent.worker.execute = lambda s, i: execute_scan(s, i, runner=fake_runner(fail="no registry"),
                                                     cfg=agent.cfg)
    scan_id = agent.ok("start_scan", docs=[DOCS], repos=[REPO])["scan_id"]
    agent.worker.join()
    status = agent.ok("get_scan", scan_id=scan_id)
    assert status["state"] == "failed" and status["error"] == "no registry" and status["runs"] == []
    assert "no finished run" in agent.fails("get_findings", scan_id=scan_id)


# ----------------------------------------------------------------------- findings

def test_findings_page_filter_and_include_clean(agent):
    scan_id = agent.scan()
    first = agent.ok("get_findings", scan_id=scan_id, limit=1)
    assert (first["total"], first["returned"], first["next_offset"]) == (2, 1, 1)
    second = agent.ok("get_findings", scan_id=scan_id, limit=1, offset=1)
    assert second["next_offset"] is None and second["findings"][0]["id"] != first["findings"][0]["id"]
    assert agent.ok("get_findings", scan_id=scan_id, offset=5)["returned"] == 0

    pins = agent.ok("get_findings", scan_id=scan_id, kinds=["stale_pin"])
    assert pins["total"] == 1 and pins["findings"][0]["kind"] == "stale_pin"
    quick = agent.ok("get_findings", scan_id=scan_id, page_contains="QUICKSTART")
    assert [f["kind"] for f in quick["findings"]] == ["missing_symbol"]
    assert agent.ok("get_findings", scan_id=scan_id, page_contains="nowhere")["total"] == 0

    everything = agent.ok("get_findings", scan_id=scan_id, kinds=["clean", "prose"])
    assert everything["total"] > 0 and set(everything["by_kind"]) <= {"clean", "prose"}


def test_findings_errors_say_what_is_valid(agent):
    scan_id = agent.scan()
    assert "Valid: stale_pin" in agent.fails("get_findings", scan_id=scan_id, kinds=["typo"])
    assert "no run '2000'" in agent.fails("get_findings", scan_id=scan_id, run_id="2000")
    assert agent.fails("get_findings", scan_id=scan_id, limit=0)
    assert agent.fails("get_findings", scan_id=scan_id, limit=10_000)
    run = agent.ok("get_scan", scan_id=scan_id)["runs"][0]["id"]
    assert agent.ok("get_findings", scan_id=scan_id, run_id=run)["run_id"] == run


# --------------------------------------------------------------------------- diff

def test_diff_needs_two_runs_then_reports_regressions(agent):
    agent.mode["failing"] = False
    scan_id = agent.scan()
    single = agent.ok("scan_diff", scan_id=scan_id)
    assert single["compared"] is False and "two runs" in single["note"]

    agent.mode["failing"] = True
    agent.ok("rerun_scan", scan_id=scan_id)
    agent.worker.join()
    d = agent.ok("scan_diff", scan_id=scan_id)
    assert d["compared"] and d["worse"] is True and d["from_run"] < d["to_run"]
    assert d["counts"]["regressions"] == 2
    row = d["regressions"][0]
    assert (row["was"], row["now"]) == ("pass", "fail") and row["id"] == "s00"
    assert "AttributeError" in row["detail"]

    capped = agent.ok("scan_diff", scan_id=scan_id, limit=1)
    assert len(capped["regressions"]) == 1 and capped["truncated"] is True

    agent.ok("rerun_scan", scan_id=scan_id)
    agent.worker.join()
    same = agent.ok("scan_diff", scan_id=scan_id)
    assert same["worse"] is False and set(same["counts"].values()) == {0}
    assert "No earlier run" in agent.fails("scan_diff", scan_id=scan_id, since_run="2000")


# ------------------------------------------------------------------------- impact

def test_rename_impact_answers_from_the_report_without_neo4j(agent):
    scan_id = agent.scan()
    hit = agent.ok("rename_impact", scan_id=scan_id, symbol="alpha.run")
    assert hit["source"] == "report"
    assert {p["name"] for p in hit["pages"]} == {"Quickstart", "Install"}
    assert all(p["url"].startswith(DOCS) for p in hit["pages"])
    assert agent.ok("rename_impact", scan_id=scan_id, symbol="Alpha.Run()")["source"] == "report"

    miss = agent.ok("rename_impact", scan_id=scan_id, symbol="never_documented")
    assert (miss["source"], miss["pages"]) == ("none", []) and "safe to rename" in miss["note"]


def test_rename_impact_falls_back_to_the_graph(agent):
    scan_id = agent.scan()
    agent.graph_rows = [{"url": "https://docs.a.io/x", "touchpoints": 3}]
    got = agent.ok("rename_impact", scan_id=scan_id, symbol="close_async")
    assert got["source"] == "graph" and "most recently scanned" in got["note"]
    assert got["pages"] == [{"name": "https://docs.a.io/x", "url": "https://docs.a.io/x", "touchpoints": 3}]
    assert "empty" in agent.fails("rename_impact", scan_id=scan_id, symbol="  ")


def test_rename_impact_on_a_scan_with_no_run(agent, store):
    rec = store.create([DOCS], [])
    got = agent.ok("rename_impact", scan_id=rec.id, symbol="anything")
    assert got["source"] == "none" and "no finished run" in got["note"]


# ----------------------------------------------------------------------- unknown

@pytest.mark.parametrize("tool,args", [
    ("get_scan", {}), ("get_findings", {}), ("scan_diff", {}), ("rerun_scan", {}),
    ("rename_impact", {"symbol": "x"})])
def test_unknown_scan_ids_list_the_valid_ones(agent, store, tool, args):
    assert "none are recorded" in agent.fails(tool, scan_id="nope", **args)
    rec = store.create([DOCS], [])
    msg = agent.fails(tool, scan_id="nope", **args)
    assert "No scan 'nope'" in msg and rec.id in msg


def test_many_known_ids_are_abbreviated(agent, store):
    for i in range(20):
        store.create([f"https://docs{i}.a.io"], [])
    assert "and 5 more" in agent.fails("get_scan", scan_id="nope")


# -------------------------------------------------------------------- validation

def test_start_scan_passes_validation_messages_through(agent, store):
    assert "Docs URL 1 isn't an http(s) address." in agent.fails("start_scan", docs=["nope"], repos=[])
    assert "Repository 1 isn't a git URL" in agent.fails("start_scan", docs=[], repos=["github.com/x"])
    assert "Add at least one docs URL" in agent.fails("start_scan", docs=[], repos=[])
    assert agent.fails("start_scan", docs=[DOCS], repos=[], versions=0)
    assert agent.fails("start_scan", docs=[DOCS], repos=[], max_pages=99_999)
    assert agent.fails("start_scan", docs=[DOCS], repos=[], ecosystem="cargo")
    assert "60 characters" in agent.fails("start_scan", docs=[DOCS], repos=[], name="x" * 61)
    assert store.all() == []                      # a refused call records nothing


def test_start_scan_takes_local_repo_paths_like_the_cli(agent, store):
    started = agent.ok("start_scan", docs=[], repos=["/srv/code/alpha"], name="Local")
    assert store.get(started["scan_id"]).repos == ["/srv/code/alpha"]
    agent.worker.join()


# ---------------------------------------------------------------------- packages

def test_packages_are_positional_with_repos_and_extras_are_repo_less(agent, store):
    repos = ["https://github.com/a/alpha", "https://github.com/a/beta"]
    started = agent.ok("start_scan", docs=[DOCS], repos=repos, packages=["", "pkg-b", "extra"],
                       ecosystem="npm", versions=2, max_pages=40, assume_credentialed=True)
    agent.worker.join()
    rec = store.get(started["scan_id"])
    assert rec.packages == ["", "pkg-b", "extra"]
    assert (rec.options.ecosystem, rec.options.versions, rec.options.max_pages,
            rec.options.assume_credentialed) == ("npm", 2, 40, True)


def test_a_duplicate_repo_does_not_shift_later_packages(agent, store):
    repos = ["https://github.com/a/alpha", "https://github.com/a/alpha", "https://github.com/a/beta"]
    started = agent.ok("start_scan", docs=[], repos=repos, packages=["pkg-a", "", "pkg-b"])
    agent.worker.join()
    assert store.get(started["scan_id"]).packages == ["pkg-a", "pkg-b"]


def test_starting_the_same_sources_reuses_the_scan_and_applies_new_settings(agent, store):
    first = agent.ok("start_scan", docs=[DOCS], repos=[REPO], name="One", versions=3)
    agent.worker.join()
    again = agent.ok("start_scan", docs=[DOCS], repos=[REPO], name="Two", versions=5, packages=["pkg"])
    agent.worker.join()
    assert again["scan_id"] == first["scan_id"] and again["created"] is False
    rec = store.get(first["scan_id"])
    assert (rec.name, rec.options.versions, rec.packages) == ("Two", 5, ["pkg"])
    assert len(store.runs(rec.id)) == 2 and len(store.all()) == 1


# --------------------------------------------------------------------- one at a time

def test_a_scan_running_in_another_process_blocks_start_and_rerun(agent, store):
    other = store.create(["https://docs.other.io"], [], name="Dashboard scan")
    mine = store.create([DOCS], [REPO])
    store.set_status(other.id, ScanStatus(state="running"))

    msg = agent.fails("start_scan", docs=["https://docs.new.io"], repos=[])
    assert other.id in msg and "Dashboard scan" in msg and "running" in msg
    # changed: the refusal no longer sends the user to `docrot serve` / ./stop.sh,
    # since a scan whose process died is cleared automatically
    assert "stop.sh" not in msg and "docrot serve" not in msg
    assert len(store.all()) == 2                 # nothing was recorded for the refused call
    assert other.id in agent.fails("rerun_scan", scan_id=mine.id)
    assert store.status(mine.id).state == "idle"

    store.set_status(other.id, ScanStatus(state="queued"))
    assert "is queued" in agent.fails("start_scan", docs=["https://docs.new.io"], repos=[])

    store.set_status(other.id, ScanStatus(state="done"))
    assert agent.ok("rerun_scan", scan_id=mine.id)["state"] == "queued"
    agent.worker.join()


def test_a_scan_held_by_a_live_foreign_process_blocks_start_until_that_process_dies(agent, store):
    import socket
    other = store.create(["https://docs.other.io"], [], name="Agent two")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.set_status(other.id, ScanStatus(state="running", pid=proc.pid, host=socket.gethostname()))
        assert other.id in agent.fails("start_scan", docs=["https://docs.new.io"], repos=[])
        assert store.status(other.id).state == "running"
        proc.kill()
        proc.wait()
        assert agent.ok("start_scan", docs=["https://docs.new.io"], repos=[])["state"] == "queued"
        agent.worker.join()
        assert store.status(other.id).state == "failed"      # cleaned up by the same rule
    finally:
        proc.kill()


def test_a_scan_with_no_recorded_owner_does_not_block_start(agent, store):
    old = store.create(["https://docs.old.io"], [])
    store._write(store._dir(old.id) / "status.json", {"state": "running"})   # pre-owner status
    assert agent.ok("start_scan", docs=["https://docs.new.io"], repos=[])["state"] == "queued"
    agent.worker.join()
    assert store.status(old.id).state == "failed"


def test_scans_in_our_own_queue_wait_their_turn_instead_of_being_refused(agent, store):
    gate = agent.mode["gate"] = threading.Event()
    first = agent.ok("start_scan", docs=[DOCS], repos=[])
    second = agent.ok("start_scan", docs=["https://docs.b.io"], repos=[])
    assert second["queued_behind"] == [first["scan_id"]]
    assert "already queued" in agent.fails("rerun_scan", scan_id=first["scan_id"])
    gate.set()
    agent.worker.join()
    assert [store.status(s["scan_id"]).state for s in (first, second)] == ["done", "done"]


# ----------------------------------------------------------------------- shutdown

def test_stopping_the_worker_drops_queued_scans_and_returns_what_it_owned(store):
    started, gate, ran = threading.Event(), threading.Event(), []

    def execute(s, scan_id):
        ran.append(scan_id)
        started.set()
        gate.wait(10)

    worker = ScanWorker(store, execute)
    a, b = (store.create([f"https://docs{i}.a.io"], []).id for i in range(2))
    worker.enqueue(a)
    worker.enqueue(b)
    assert started.wait(5) and worker.owns(a) and worker.owns(b) and not worker.owns("other")
    threading.Timer(0.2, gate.set).start()       # the running scan finishes while stop() waits
    assert worker.stop() == [b]                  # b was dropped before it began
    assert ran == [a] and not worker.owns(a)
    assert store.status(b).state == "queued"     # the record is the caller's to fail


def test_interrupted_scans_are_failed_but_only_ours(store):
    ours = store.create(["https://docs.a.io"], []).id
    theirs = store.create(["https://docs.b.io"], []).id
    store.set_status(ours, ScanStatus(state="running"))
    store.set_status(theirs, ScanStatus(state="running"))
    mark_interrupted(store, [ours])
    st = store.status(ours)
    assert st.state == "failed" and "MCP server stopped" in st.error and st.finished_at
    assert store.status(theirs).state == "running"          # a live dashboard's scan is not ours to fail


# ---------------------------------------------------------------------- credentials

def test_no_tool_returns_a_credential(agent, monkeypatch):
    secret = "sk-very-secret-0123456789"
    monkeypatch.setenv("DAYTONA_API_KEY", secret)
    monkeypatch.setenv("SANDBOX_ENV_TOKEN", secret)
    settings.save({"EXTRACTION_API_KEY": secret})
    scan_id = agent.scan()
    outputs = []
    for name, args in [("list_scans", {}), ("get_scan", {"scan_id": scan_id}),
                       ("get_findings", {"scan_id": scan_id, "kinds": ["clean", "prose", "stale_pin"]}),
                       ("scan_diff", {"scan_id": scan_id}),
                       ("rename_impact", {"scan_id": scan_id, "symbol": "alpha.run"}),
                       ("rerun_scan", {"scan_id": scan_id})]:
        agent.call(name, **args)
        outputs.append(agent.last_text)
    agent.worker.join()
    assert secret not in "".join(outputs)
    assert not [t for t in agent.tools() if "setting" in t or "env" in t or "key" in t]


# ------------------------------------------------------------------ real stdio

CHILD = textwrap.dedent("""
    import sys
    from pathlib import Path
    import docrot.config as cfg
    import docrot.store as store
    root = Path(sys.argv[1])
    cfg.DATA, cfg.RUNS = root, root / "runs"
    # a library that prints while a tool runs: it must not reach the wire
    real_all = store.Store.all
    def noisy(self):
        print("STRAY PRINT FROM A LIBRARY")
        return real_all(self)
    store.Store.all = noisy
    from docrot import cli
    sys.exit(cli.main(["mcp"]))
""")


def _frame(**msg) -> str:
    return json.dumps({"jsonrpc": "2.0", **msg}) + "\n"


def test_docrot_mcp_over_real_stdio_writes_only_protocol_frames(tmp_path):
    # a pre-dashboard archive makes open_store print its one-time adoption notice
    old = tmp_path / "data" / "runs"
    (old / "latest").mkdir(parents=True)
    (old / "20260101T000000").mkdir()
    (old / "latest" / "report.json").write_text('{"docs_url": "https://docs.a.io", "generated_at": "2026-01-01T00:00:00"}')
    (old / "20260101T000000" / "results.json").write_text("[]")

    child = Path(tmp_path / "child.py")
    child.write_text(CHILD)
    proc = subprocess.Popen([sys.executable, str(child), str(tmp_path / "data")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    requests = [
        _frame(id=1, method="initialize", params={
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"}}),
        _frame(method="notifications/initialized"),
        _frame(id=2, method="tools/list"),
        _frame(id=3, method="tools/call", params={"name": "list_scans", "arguments": {}}),
    ]
    assert proc.stdin is not None and proc.stdout is not None
    answers: dict[int, dict] = {}
    raw: list[str] = []
    for req in requests:
        proc.stdin.write(req)
        proc.stdin.flush()
        if '"id"' in req:
            line = proc.stdout.readline()
            raw.append(line)
            answers[json.loads(line)["id"]] = json.loads(line)
    rest, err = proc.communicate(timeout=30)     # closes stdin: a client going away is a clean shutdown
    raw.extend(rest.splitlines(keepends=True))

    assert proc.returncode == 0, err
    assert set(answers) == {1, 2, 3}
    assert answers[1]["result"]["serverInfo"]["name"] == "docrot"
    assert {t["name"] for t in answers[2]["result"]["tools"]} == READ_ONLY | ACTIONS
    scans = answers[3]["result"]["structuredContent"]["scans"]
    assert [s["docs"] for s in scans] == [["https://docs.a.io"]]      # the adopted legacy scan
    for line in raw:                             # every byte of stdout is a JSON-RPC frame
        assert json.loads(line)["jsonrpc"] == "2.0"
    assert "STRAY PRINT" not in "".join(raw) and "adopted" not in "".join(raw)
    assert "STRAY PRINT" in err and "adopted data/runs/" in err


def test_sigterm_stops_the_server_even_while_the_client_holds_the_pipe_open(tmp_path):
    import signal
    child = tmp_path / "child.py"
    child.write_text(CHILD)
    proc = subprocess.Popen([sys.executable, str(child), str(tmp_path / "data")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(_frame(id=1, method="initialize", params={
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}}))
    proc.stdin.flush()
    proc.stdout.readline()                       # serving
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == 143
    proc.stdout.close()
    proc.stderr.close()      # type: ignore[union-attr]


def test_the_default_graph_lookup_degrades_to_nothing_and_speaks_on_stderr(monkeypatch, capsys, cfg):
    from docrot import config as cfgmod
    from docrot.mcp_server import graph_blast_radius
    monkeypatch.setattr(cfgmod, "load", lambda: cfg)       # a Neo4j that is not there
    assert graph_blast_radius("close_async") == []
    out = capsys.readouterr()
    assert out.out == "" and "graph unavailable" in out.err


def test_a_run_whose_results_file_is_gone_is_a_tool_error_not_a_crash(agent, store):
    scan_id = agent.scan()
    agent.ok("rerun_scan", scan_id=scan_id)
    agent.worker.join()
    older = store.runs(scan_id)[0]
    (store.run_dir(scan_id, older.id) / "results.json").unlink()
    assert "Could not read the results" in agent.fails("scan_diff", scan_id=scan_id)


def test_validation_problems_read_as_one_sentence_whichever_library_raised_them():
    from docrot.mcp_server import problems
    from docrot.store import ScanOptions
    try:
        ScanOptions(versions=0)
    except ValueError as e:
        assert problems(e).startswith("versions:")
    assert problems(ValueError(["a.", "b."])) == "a. b."
    assert problems(ValueError("plain")) == "plain"
