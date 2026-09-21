from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from docrot.scan import execute_scan
from docrot.server import create_app
from docrot.worker import ScanWorker
from tests.fakes import fake_runner


@pytest.fixture
def client(store, cfg):
    worker = ScanWorker(store, lambda s, i: execute_scan(s, i, runner=fake_runner(), cfg=cfg))
    # a real local origin: the server refuses anything else (see server.guard)
    with TestClient(create_app(store, worker), base_url="http://127.0.0.1:8080") as c:
        c.worker = worker
        yield c
    worker.stop()


BODY = {"name": "Alpha", "docs": ["https://docs.a.io", "https://guides.a.io"],
        "repos": ["https://github.com/a/alpha", "https://github.com/a/beta"],
        "options": {"versions": 2, "assume_credentialed": True}}


def test_index_and_assets_are_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "DOC ROT CRAWLER" in r.text
    assert r.headers["cache-control"] == "no-store"
    # every local asset is referenced by a content-versioned URL
    refs = re.findall(r'(?:src|href)="((?:styles\.css|lib\.js|app\.js)[^"]*)"', r.text)
    assert len(refs) == 3 and all(re.search(r"\?v=[0-9a-f]{10}$", u) for u in refs)
    assert client.get(refs[-1]).status_code == 200
    js = client.get("/app.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    assert client.get("/lib.js").status_code == 200


def test_create_queues_a_run_that_finishes(client):
    r = client.post("/api/scans", json=BODY)
    assert r.status_code == 201
    scan = r.json()
    assert scan["name"] == "Alpha" and scan["status"]["state"] == "queued"
    assert scan["options"]["versions"] == 2
    client.worker.join()

    got = client.get(f"/api/scans/{scan['id']}").json()
    assert got["status"]["state"] == "done" and len(got["runs"]) == 1
    assert got["runs"][0]["page_states"]
    report = client.get(f"/api/scans/{scan['id']}/report").json()
    assert [p["package"] for p in report["packages"]] == ["alpha", "beta"]
    assert len(report["sources"]) == 4
    assert [s["id"] for s in client.get("/api/scans").json()] == [scan["id"]]


def test_create_validation_messages(client):
    r = client.post("/api/scans", json={"docs": ["nope"], "repos": []})
    assert r.status_code == 422
    assert r.json()["detail"] == ["Docs URL 1 isn't an http(s) address."]
    assert client.post("/api/scans", json={"docs": [], "repos": []}).status_code == 422
    bad_opts = client.post("/api/scans", json={"docs": ["https://a.io"], "options": {"versions": 0}})
    assert bad_opts.status_code == 422


def test_rename_persists(client, store):
    scan = client.post("/api/scans", json=BODY).json()
    r = client.patch(f"/api/scans/{scan['id']}", json={"name": "  Renamed  "})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    assert store.get(scan["id"]).name == "Renamed"
    assert client.patch(f"/api/scans/{scan['id']}", json={"name": ""}).status_code == 422
    assert client.patch("/api/scans/nope-000000", json={"name": "x"}).status_code == 404


def test_rerun_and_conflict(client):
    scan = client.post("/api/scans", json=BODY).json()
    assert client.post(f"/api/scans/{scan['id']}/runs").status_code in (202, 409)
    client.worker.join()
    assert client.post(f"/api/scans/{scan['id']}/runs").status_code == 202
    client.worker.join()
    assert len(client.get(f"/api/scans/{scan['id']}").json()["runs"]) >= 2


def test_report_404s(client, store):
    rec = store.create(["https://docs.a.io"], [])
    assert client.get(f"/api/scans/{rec.id}/report").status_code == 404
    assert client.get("/api/scans/missing-000000/report").status_code == 404
    assert client.get(f"/api/scans/{rec.id}/report?run=../../x").status_code in (404, 422)


def test_stale_single_report_page_is_told_to_clear_its_cache(client):
    for path in ("/report.json", "/data/report.json"):
        r = client.get(path)
        assert r.status_code == 410
        assert r.headers["clear-site-data"] == '"cache"'


def test_dashboard_url_is_cache_busting():
    from docrot.server import dashboard_url
    assert re.match(r"^http://127\.0\.0\.1:8080/\?t=\d+#/$", dashboard_url(8080))
    assert dashboard_url(8080, "scan/abc").endswith("#/scan/abc")


def test_findings_jsonl_endpoint(client, store):
    import json
    scan = client.post("/api/scans", json=BODY).json()
    assert client.get(f"/api/scans/{scan['id']}/findings.jsonl").status_code in (404, 200)
    client.worker.join()
    run = client.get(f"/api/scans/{scan['id']}").json()["runs"][-1]["id"]

    r = client.get(f"/api/scans/{scan['id']}/findings.jsonl")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    assert r.headers["content-disposition"] == f'attachment; filename="{scan["id"]}-{run}.jsonl"'
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert lines[0]["type"] == "scan" and lines[0]["run"] == run

    everything = client.get(f"/api/scans/{scan['id']}/findings.jsonl?include_clean=true&run={run}")
    assert len(everything.text.splitlines()) > len(lines)
    assert client.get(f"/api/scans/{scan['id']}/findings.jsonl?run=20000101T000000").status_code == 404
    assert client.get("/api/scans/missing-000000/findings.jsonl").status_code == 404
    fresh = store.create(["https://fresh.a.io"], [])
    assert client.get(f"/api/scans/{fresh.id}/findings.jsonl").status_code == 404


def test_import_endpoint(client, store):
    scan = client.post("/api/scans", json=BODY).json()
    client.worker.join()
    exported = client.get(f"/api/scans/{scan['id']}/findings.jsonl").text
    sarif = client.get(f"/api/scans/{scan['id']}/findings.sarif").text

    # same sources as the existing scan: the run joins it
    from tests.test_import import _older
    r = client.post("/api/imports?filename=alpha.jsonl", content=_older(exported))
    assert r.status_code == 201 and r.json()["id"] == scan["id"]
    assert len(r.json()["runs"]) == 2

    assert client.post("/api/imports?filename=alpha.sarif", content=sarif).status_code == 409
    bad = client.post("/api/imports?filename=x.jsonl", content="nonsense")
    assert bad.status_code == 422 and "not valid JSON" in bad.json()["detail"][0]

    store.update(scan["id"], docs=["https://elsewhere.io"])       # no longer matches
    fresh = client.post("/api/imports?filename=alpha.sarif&name=From+a+colleague", content=sarif)
    assert fresh.status_code == 201
    assert fresh.json()["name"] == "From a colleague" and fresh.json()["imported_from"] == "alpha.sarif"
    assert fresh.json()["status"]["state"] == "done" and len(fresh.json()["runs"]) == 1


def test_sarif_endpoint(client):
    import json as jsonlib
    scan = client.post("/api/scans", json=BODY).json()
    client.worker.join()
    r = client.get(f"/api/scans/{scan['id']}/findings.sarif")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/sarif+json")
    assert r.headers["content-disposition"].endswith('.sarif"')
    assert jsonlib.loads(r.text)["runs"][0]["tool"]["driver"]["name"] == "docrot"
    assert client.get("/api/scans/missing-000000/findings.sarif").status_code == 404


def test_only_this_machine_can_reach_it(client):
    """A page on another site must not be able to start scans here, and a
    hostname pointed at 127.0.0.1 must not reach it either."""
    ok = client.post("/api/scans", json=BODY, headers={"Origin": "http://localhost:8080"})
    assert ok.status_code == 201                       # our own dashboard

    foreign = client.post("/api/scans", json=BODY, headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403 and "Refused a write" in foreign.json()["detail"][0]

    # the import endpoint takes text/plain, which needs no preflight at all
    imported = client.post("/api/imports?filename=x.jsonl", content="{}",
                           headers={"Origin": "https://evil.example"})
    assert imported.status_code == 403

    rebind = client.get("/api/scans", headers={"Host": "docrot.evil.example"})
    assert rebind.status_code == 403

    # reading from the CLI or curl, which send no Origin, still works
    assert client.get("/api/scans").status_code == 200


def test_security_headers(client):
    r = client.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
