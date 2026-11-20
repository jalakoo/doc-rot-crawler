from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from docrot.scan import execute_scan
from docrot.server import create_app
from docrot.worker import ScanWorker
from tests.fakes import fake_runner


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    """Cloning never touches the real data/cache."""
    import docrot.acquire.repo as repo
    monkeypatch.setattr(repo, "CACHE", tmp_path / "cache")
    return tmp_path / "cache"


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


def test_package_positions_survive_create(client):
    body = {**BODY, "packages": ["", "pkg-b", "pkg-c", ""]}
    r = client.post("/api/scans", json=body)
    assert r.status_code == 201 and r.json()["packages"] == ["", "pkg-b", "pkg-c"]
    assert client.get(f"/api/scans/{r.json()['id']}").json()["packages"] == ["", "pkg-b", "pkg-c"]
    client.worker.join()


def test_docs_and_a_bare_package_are_accepted(client):
    r = client.post("/api/scans", json={"docs": ["https://docs.a.io"], "packages": ["only-pkg"]})
    assert r.status_code == 201 and r.json()["packages"] == ["only-pkg"]
    client.worker.join()


def test_a_scan_stuck_running_from_a_dead_process_can_be_run_again(client, store):
    scan = client.post("/api/scans", json=BODY).json()
    client.worker.join()
    # a status as an older docrot, or a crashed process, left it
    store._write(store._dir(scan["id"]) / "status.json",
                 {"state": "running", "phase": 2, "log": []})
    assert client.post(f"/api/scans/{scan['id']}/runs").status_code == 202
    client.worker.join()
    assert store.status(scan["id"]).state == "done"


# ------------------------------------------------------------------- detect

def _git(*args, cwd):
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})


@pytest.fixture
def monorepo(tmp_path):
    """A polyglot repo in a real git repository: PyPI and npm packages, and
    Rust and Go manifests docrot cannot check."""
    root = tmp_path / "mono"
    (root / "py" / "monopy").mkdir(parents=True)
    (root / "py" / "pyproject.toml").write_text('[project]\nname = "mono-py"\n')
    (root / "py" / "monopy" / "__init__.py").write_text("def go(): ...\n")
    (root / "js").mkdir()
    (root / "js" / "package.json").write_text('{"name": "@mono/js", "version": "1.0.0"}')
    (root / "Cargo.toml").write_text('[package]\nname = "mono"\n')
    (root / "tools").mkdir()
    (root / "tools" / "go.mod").write_text("module example.com/tools\n")
    (root / "README.md").write_text("# Mono\n")
    _git("init", "-q", "-b", "main", cwd=root)
    _git("add", ".", cwd=root)
    _git("commit", "-qm", "one", cwd=root)
    return root


def _by_name(found):
    return {p["name"]: p for p in found["packages"]}


def test_detect_lists_the_packages_of_a_local_repo(client, monorepo):
    r = client.post("/api/detect", json={"repos": [str(monorepo)]})
    assert r.status_code == 200
    (found,) = r.json()["repos"]
    assert found["repo"] == str(monorepo) and "error" not in found
    pkgs = _by_name(found)
    assert pkgs["mono-py"] == {"name": "mono-py", "import_name": "monopy", "ecosystem": "pypi",
                               "path": "py", "spec": "pypi:mono-py"}
    assert pkgs["@mono/js"]["spec"] == "npm:@mono/js" and pkgs["@mono/js"]["path"] == "js"
    assert found["other"] == ["go", "rust"]
    assert found["commit"] and found["branch"] == "main"


def test_detect_clones_a_repo_url_and_reads_other_ecosystems_from_git(client, monorepo, cache):
    from docrot.acquire.detect import detect
    found = detect(f"file://{monorepo}")           # the clone is sparse: no Cargo.toml on disk
    assert sorted(_by_name(found)) == ["@mono/js", "mono-py"]
    assert found["other"] == ["go", "rust"]
    assert not list(cache.glob("*/Cargo.toml"))
    assert detect(f"file://{monorepo}")["commit"] == found["commit"]      # cached clone, refreshed


def test_detect_reports_a_bad_repo_inline_not_as_a_500(client, tmp_path):
    missing = str(tmp_path / "nowhere")
    r = client.post("/api/detect", json={"repos": [missing, "https://nowhere.invalid/no/repo"]})
    assert r.status_code == 200
    bad = r.json()["repos"]
    assert [b["repo"] for b in bad] == [missing, "https://nowhere.invalid/no/repo"]
    assert all(b["error"] and "packages" not in b for b in bad)


def test_detect_finding_nothing_is_an_empty_list_not_an_error(client, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "README.md").write_text("# nothing to ship\n")
    (found,) = client.post("/api/detect", json={"repos": [str(empty)]}).json()["repos"]
    assert found["packages"] == [] and found["other"] == [] and "error" not in found


def test_detect_validates_repos_like_the_scan_form(client, monorepo):
    assert client.post("/api/detect", json={"repos": []}).status_code == 422
    assert client.post("/api/detect", json={"repos": ["  "]}).status_code == 422
    r = client.post("/api/detect", json={"repos": [str(monorepo), "not a repo"]})
    assert r.status_code == 422
    assert r.json()["detail"] == ["Repository 2 isn't a git URL or a local path (/…, ./…, ~/…)."]


def test_detect_caps_how_many_repos_one_request_may_clone(client, tmp_path):
    many = [str(tmp_path / f"r{i}") for i in range(11)]
    r = client.post("/api/detect", json={"repos": many})
    assert r.status_code == 422 and "At most 10" in r.json()["detail"][0]
    assert client.post("/api/detect", json={"repos": many[:10]}).status_code == 200


def test_detect_repeats_are_looked_up_once(store, cfg, monorepo):
    seen = []
    worker = ScanWorker(store, lambda s, i: None)
    app = create_app(store, worker, detect=lambda repo: seen.append(repo) or {"repo": repo, "packages": []})
    with TestClient(app, base_url="http://127.0.0.1:8080") as c:
        r = c.post("/api/detect", json={"repos": [str(monorepo), f" {monorepo} "]})
    worker.stop()
    assert seen == [str(monorepo)] and len(r.json()["repos"]) == 1


def test_detect_is_a_write_so_a_foreign_origin_is_refused(client, monorepo):
    r = client.post("/api/detect", json={"repos": [str(monorepo)]}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.post("/api/detect", json={"repos": [str(monorepo)]},
                       headers={"Origin": "http://localhost:8080"}).status_code == 200


def test_two_detects_of_one_new_repo_at_once_both_succeed(monorepo):
    """`clone` builds in `<dest>.partial` and renames it, so unserialised clones
    of the same repo delete each other's work."""
    import threading

    from docrot.acquire.detect import detect
    url = f"file://{monorepo}"
    out: list[dict] = []
    threads = [threading.Thread(target=lambda: out.append(detect(url))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 4 and all("error" not in o and len(o["packages"]) == 2 for o in out)


def test_detect_lists_a_cli_row_for_a_github_repo_whose_releases_carry_its_binary(
        store, cfg, monorepo, monkeypatch):
    import docrot.acquire.detect as det
    from docrot.acquire.binaries import CliLookup, CliRelease
    asked = []

    def lookup(repo, name):
        asked.append((repo, name))
        return CliLookup(name, [CliRelease("1.1.0", "v1.1.0"), CliRelease("1.2.0", "v1.2.0")], installable=True)

    monkeypatch.setattr(det, "clone", lambda repo, log, timeout: (monorepo, "abc123", "main"))
    app = create_app(store, ScanWorker(store, lambda s, i: None),
                     detect=lambda repo: det.detect(repo, cli_lookup=lookup))
    with TestClient(app, base_url="http://127.0.0.1:8080") as c:
        (found,) = c.post("/api/detect", json={"repos": ["https://github.com/o/mono"]}).json()["repos"]
    row = _by_name(found)["mono"]
    assert row == {"name": "mono", "import_name": "mono", "ecosystem": "cli", "path": "",
                   "spec": "cli:mono", "latest": "1.2.0"}
    assert asked == [("https://github.com/o/mono", "mono")]            # one lookup for the repo
    assert sorted(p["spec"] for p in found["packages"][:2]) == ["npm:@mono/js", "pypi:mono-py"]
    assert found["packages"][-1] is row                                 # the CLI row comes last


def test_detect_leaves_the_cli_row_out_with_a_note_when_github_cannot_be_asked(monorepo, monkeypatch):
    import docrot.acquire.detect as det
    from docrot.acquire.binaries import RATE_LIMITED, CliLookup
    monkeypatch.setattr(det, "clone", lambda repo, log, timeout: (monorepo, "abc123", "main"))
    found = det.detect("https://github.com/o/mono",
                       cli_lookup=lambda r, n: CliLookup(n, why=RATE_LIMITED, failed=True))
    assert "error" not in found and not any(p["ecosystem"] == "cli" for p in found["packages"])
    assert "No standalone mono CLI listed" in found["note"] and "GITHUB_TOKEN" in found["note"]


def test_detect_does_not_ask_github_about_a_local_repo_or_when_an_npm_package_owns_the_bin(
        monorepo, monkeypatch):
    import docrot.acquire.detect as det
    from docrot.acquire.binaries import CliLookup
    asked = []

    def lookup(repo, name):
        asked.append(name)
        return CliLookup(name, installable=True)

    assert not any(p["ecosystem"] == "cli" for p in det.detect(str(monorepo), cli_lookup=lookup)["packages"])
    (monorepo / "js" / "package.json").write_text('{"name": "@mono/js", "bin": {"mono": "cli.js"}}')
    monkeypatch.setattr(det, "clone", lambda repo, log, timeout: (monorepo, "abc123", "main"))
    found = det.detect("https://github.com/o/mono", cli_lookup=lookup)
    assert not any(p["ecosystem"] == "cli" for p in found["packages"])
    assert asked == []


def test_detect_survives_a_lookup_that_raises(monorepo, monkeypatch):
    import docrot.acquire.detect as det
    monkeypatch.setattr(det, "clone", lambda repo, log, timeout: (monorepo, "abc123", "main"))

    def boom(repo, name):
        raise RuntimeError("surprise")

    found = det.detect("https://github.com/o/mono", cli_lookup=boom)
    assert sorted(p["ecosystem"] for p in found["packages"]) == ["npm", "pypi"]
    assert "could not look for a standalone mono CLI" in found["note"]
