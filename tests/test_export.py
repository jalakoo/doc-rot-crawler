from __future__ import annotations

import json

import pytest

from docrot.models import Extraction, Page, Release, Result, Snippet, Target
from docrot.report import build_report, export
from tests.fakes import make_outcome

DOCS = ["https://docs.a.io", "https://guides.a.io"]
REPOS = ["https://github.com/a/alpha", "https://github.com/a/beta"]


def _save(store, docs=DOCS, repos=REPOS, outcome=None):
    rec = store.create(docs, repos, name="Alpha")
    out = outcome or make_outcome(docs, repos)
    run = store.save_run(rec.id, out.report, [r.model_dump() for r in out.results],
                         out.extraction.model_dump_json())
    return rec, run


def _lines(text):
    return [json.loads(line) for line in text.splitlines()]


def test_saving_a_run_writes_findings_jsonl(store):
    rec, run = _save(store)
    path = store.run_dir(rec.id, run.id) / "findings.jsonl"
    assert path.exists()
    assert path.read_text() == store.export_text(rec.id, run.id)


def test_scan_record_comes_first(store):
    rec, run = _save(store)
    head, *finds = _lines(store.export_text(rec.id))
    assert head["type"] == "scan" and head["schema"] == export.SCHEMA
    assert head["scan"] == {"id": rec.id, "name": "Alpha"} and head["run"] == run.id
    assert [p["package"] for p in head["packages"]] == ["alpha", "beta"]
    assert head["packages"][1]["versions_tested"] == ["1.0.0", "1.1.0", "2.0.0"]
    assert [s["url"] for s in head["sources"]] == DOCS + REPOS
    assert head["stats"]["findings"] == len(finds)
    assert sum(head["stats"]["page_states"].values()) == head["stats"]["pages"]
    assert head["stats"]["results"]["fail"] > 0
    assert all(f["type"] == "finding" for f in finds)


def test_finding_records_are_self_contained(store):
    rec, _ = _save(store)
    finds = _lines(store.export_text(rec.id))[1:]
    gone = [f for f in finds if f["kind"] == "missing_symbol"]
    assert len(gone) == 2
    for f in gone:
        assert f["status"] == "fail" and f["severity"] == "error"
        assert f["title"] == "Symbol no longer exists"
        assert "<" not in f["summary"] and f["summary"]
        assert f["doc"]["source"] == "site" and f["doc"]["snippet_id"]
        assert f["doc"]["snippet"].startswith("import ")
        assert f["versions"] == {"tested": ["1.0.0", "1.1.0", "2.0.0"], "failing": ["1.1.0", "2.0.0"],
                                 "passing": ["1.0.0"], "blocked": [], "unverified": []}
        # the code side is the snippet's own package, not the scan's primary
        assert f["code"]["repo"] == f"https://github.com/a/{f['code']['package']}"
        assert f["symbols"] == [f"{f['code']['import_name']}.run"]
        assert "AttributeError" in f["code"]["actual"]
        assert f["fix_hint"]
    assert {f["code"]["package"] for f in gone} == {"alpha", "beta"}

    [pin] = [f for f in finds if f["kind"] == "stale_pin"]
    assert pin["severity"] == "warning"
    assert pin["fix_hint"].startswith("Update the pinned version 0.9.0 to the current release 2.0.0")


def test_ids_are_stable_across_runs(store):
    rec, _ = _save(store)
    out = make_outcome(DOCS, REPOS)
    store.save_run(rec.id, out.report, [r.model_dump() for r in out.results],
                   out.extraction.model_dump_json())
    first, second = (store.runs(rec.id)[0].id, store.runs(rec.id)[1].id)
    ids = [{f["id"] for f in _lines(store.export_text(rec.id, run))[1:]} for run in (first, second)]
    assert ids[0] == ids[1] and len(ids[0]) == 3


def test_include_clean_adds_passing_and_prose_pages(store):
    rec, _ = _save(store)
    drift = _lines(store.export_text(rec.id))[1:]
    everything = _lines(store.export_text(rec.id, include_clean=True))[1:]
    extra = {(f["kind"], f["status"], f["severity"]) for f in everything} - {
        (f["kind"], f["status"], f["severity"]) for f in drift}
    assert extra == {("clean", "pass", "none"), ("prose", "unverifiable", "none")}
    assert len(everything) > len(drift)


def test_blocked_snippets_are_exported_as_blocked(store):
    v = [Release(label="1.0", released_at="2026-01-01")]
    t = Target(package="alpha", import_name="alpha", repo_url=REPOS[0], releases=v, versions=v)
    page = Page(url="https://docs.a.io/x/guide", path="/x/guide", title="Guide", origin=DOCS[0])
    snips = [Snippet(id="s1", page=page.url, lang="python", code="import alpha", tier="execute",
                     package="alpha", line=3),
             Snippet(id="s2", page=page.url, lang="python", code="alpha.go()", tier="execute",
                     package="alpha", line=9)]
    results = [Result(id="s1", page=page.url, version="1.0", status="pass", package="alpha"),
               Result(id="s2", page=page.url, version="1.0", status="blocked", blocked_by="install",
                      package="alpha")]
    ex = Extraction(package="alpha", import_name="alpha", packages=[t], pages=[page], snippets=snips)
    report = build_report(ex, results, [t], DOCS[:1], REPOS[:1], "llms.txt", 1.0)
    from docrot.scan import ScanOutcome
    rec, _ = _save(store, DOCS[:1], REPOS[:1], ScanOutcome(report=report, extraction=ex, results=results))

    [blocked] = _lines(store.export_text(rec.id))[1:]
    assert blocked["kind"] == "blocked" and blocked["status"] == "blocked" and blocked["severity"] == "info"
    assert blocked["blocked_by"] == "install" and blocked["versions"]["blocked"] == ["1.0"]
    assert blocked["doc"]["line"] == 9 and blocked["doc"]["snippet_id"] == "s2"
    assert "install" in blocked["fix_hint"]


def test_reports_from_before_multi_source_still_export(store):
    """No `packages`, no `sources`, and findings without `snippet`/`line`."""
    rec, run = _save(store, DOCS[:1], REPOS[:1])
    d = store.run_dir(rec.id, run.id)
    report = json.loads((d / "report.json").read_text())
    for key in ("packages", "sources", "docs_urls", "repo_urls"):
        report.pop(key)
    for sec in report["sections"]:
        for page in sec["pages"]:
            for f in page["findings"]:
                f.pop("snippet"), f.pop("line")
    (d / "report.json").write_text(json.dumps(report))

    head, *finds = _lines(store.export_text(rec.id))
    assert head["packages"][0]["package"] == "alpha"
    assert [s["url"] for s in head["sources"]] == DOCS[:1] + REPOS[:1]
    [gone] = [f for f in finds if f["kind"] == "missing_symbol"]
    assert gone["doc"]["snippet_id"] == "s00" and gone["versions"]["failing"] == ["1.1.0", "2.0.0"]


def test_export_of_a_missing_run(store):
    rec = store.create(DOCS[:1], [])
    assert store.export(rec.id) is None
    assert store.export("missing-000000") is None
    with pytest.raises(KeyError):
        store.export_text(rec.id)


@pytest.mark.parametrize("finding,actual,expected", [
    ({"kind": "signature", "label": "signature drift"}, "def go(x)", "Update the documented signature"),
    ({"kind": "signature", "label": "kwarg renamed"}, "go(x)", "Replace the keyword argument"),
    ({"kind": "signature", "label": "wrong import path"}, "from a.b import go", "Change the import"),
    ({"kind": "missing_symbol", "label": "never shipped"}, "", "No tested release has this symbol"),
    ({"kind": "missing_symbol", "label": "symbol gone"}, "", "The symbol was removed"),
    ({"kind": "runtime", "label": "snippet fails"}, "", "Run the snippet"),
    ({"kind": "clean", "label": "no drift"}, "", ""),
])
def test_fix_hints(finding, actual, expected):
    hint = export.fix_hint(finding, actual)
    assert hint.startswith(expected) if expected else hint == ""


def test_plain_strips_ui_markup():
    assert export.plain("The <b>close_async</b> call &mdash; gone.\n  Really") == "The close_async call — gone. Really"
