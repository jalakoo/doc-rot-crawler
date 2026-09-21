from __future__ import annotations

import json

import pytest

from docrot.store import (
    KEEP_RUNS,
    ScanOptions,
    ScanStatus,
    Store,
    clean_sources,
    default_name,
    is_docs_url,
    is_repo,
)
from tests.fakes import make_outcome


@pytest.mark.parametrize("value,ok", [
    ("https://docs.example.com", True),
    ("http://docs.example.com/latest/", True),
    ("ftp://docs.example.com", False),
    ("https://localhost", False),
    ("not a url", False),
])
def test_docs_url_rules(value, ok):
    assert is_docs_url(value) is ok


@pytest.mark.parametrize("value,ok", [
    ("https://github.com/org/repo", True),
    ("git@github.com:org/repo.git", True),
    ("ssh://git@github.com/org/repo", True),
    ("/abs/path", True), ("./rel", True), ("~/code/repo", True),
    ("pydantic-core", False), ("github.com/org/repo", False),
])
def test_repo_rules(value, ok):
    assert is_repo(value) is ok


def test_clean_sources_trims_dedupes_and_reports_every_problem():
    docs, repos = clean_sources([" https://a.io ", "https://a.io", ""], ["./r"])
    assert docs == ["https://a.io"] and repos == ["./r"]
    with pytest.raises(ValueError) as e:
        clean_sources(["https://a.io", "nope"], ["bad"])
    assert e.value.args[0] == [
        "Docs URL 2 isn't an http(s) address.",
        "Repository 1 isn't a git URL or a local path (/…, ./…, ~/…).",
    ]
    with pytest.raises(ValueError, match="at least one"):
        clean_sources(["  "], [])


def test_default_name():
    assert default_name(["https://docs.a.io/latest/"], []) == "docs.a.io/latest"
    assert default_name([], ["git@github.com:org/thing.git"]) == "thing"


def test_create_persists_and_reloads(store, tmp_path):
    rec = store.create(["https://docs.a.io"], ["https://github.com/a/b", "https://github.com/a/c"],
                       name="  Alpha   docs ", options=ScanOptions(versions=2))
    assert rec.name == "Alpha docs" and rec.id.startswith("alpha-docs-")
    again = Store(tmp_path / "data").get(rec.id)
    assert again == rec
    assert again.options.versions == 2


def test_rename_validates(store):
    rec = store.create(["https://docs.a.io"], [])
    assert store.update(rec.id, name="New name").name == "New name"
    with pytest.raises(ValueError):
        store.update(rec.id, name="   ")
    with pytest.raises(ValueError):
        store.update(rec.id, name="x" * 61)
    with pytest.raises(KeyError):
        store.update("missing-000000", name="x")


def test_ids_cannot_escape_the_store(store):
    assert store.get("../etc") is None
    with pytest.raises(KeyError):
        store.status("../../x")


def test_find_by_sources_ignores_order(store):
    rec = store.create(["https://b.io", "https://a.io"], ["./r"])
    assert store.find_by_sources(["https://a.io", "https://b.io"], ["./r"]).id == rec.id
    assert store.find_by_sources(["https://a.io"], ["./r"]) is None


def test_save_run_and_report(store):
    rec = store.create(["https://docs.a.io"], ["https://github.com/a/alpha"])
    out = make_outcome(rec.docs, rec.repos)
    run = store.save_run(rec.id, out.report, [r.model_dump() for r in out.results],
                         out.extraction.model_dump_json())
    assert run.stats["findings"] == out.report["stats"]["findings"]
    assert sum(run.page_states.values()) == out.report["stats"]["pages"]
    assert store.report(rec.id)["stats"] == out.report["stats"]
    assert store.report(rec.id, run.id) is not None
    with pytest.raises(KeyError):
        store.report(rec.id, "../../secrets")


def test_run_ids_are_unique_and_history_is_capped(store):
    rec = store.create(["https://docs.a.io"], [])
    report = {"generated_at": "2026-09-15T10:00:00", "stats": {}, "sections": []}
    for _ in range(KEEP_RUNS + 2):
        store.save_run(rec.id, report, [], "{}")
    runs = store.runs(rec.id)
    assert len(runs) == KEEP_RUNS
    assert len({r.id for r in runs}) == KEEP_RUNS


def test_recover_marks_orphaned_scans_failed(store):
    a = store.create(["https://a.io"], [])
    b = store.create(["https://b.io"], [])
    store.set_status(a.id, ScanStatus(state="running"))
    store.set_status(b.id, ScanStatus(state="done"))
    assert store.recover() == [a.id]
    assert store.status(a.id).state == "failed"
    assert "Interrupted" in store.status(a.id).error
    assert store.status(b.id).state == "done"


def _legacy_run(d, generated_at, findings):
    d.mkdir(parents=True)
    (d / "results.json").write_text("[]")
    (d / "extraction.json").write_text("{}")
    (d / "report.json").write_text(json.dumps({
        "generated_at": generated_at, "docs_url": "https://docs.a.io",
        "repo_url": "https://github.com/a/alpha", "package": "alpha",
        "stats": {"findings": findings}, "sections": []}))


def test_import_legacy_pairs_each_run_with_its_own_report(store, tmp_path):
    runs = tmp_path / "runs"
    # the old archiver copied the previous run's report into each archive
    _legacy_run(runs / "20260912T100500", "2026-09-12T10:00:00", 1)
    _legacy_run(runs / "20260912T101000", "2026-09-12T10:05:00", 2)
    _legacy_run(runs / "latest", "2026-09-12T10:10:00", 3)
    rec = store.import_legacy(runs)
    assert rec.docs == ["https://docs.a.io"] and rec.name == "alpha"
    got = [(r.id, r.stats["findings"]) for r in store.runs(rec.id)]
    assert got == [("20260912T100500", 2), ("20260912T101000", 3)]
    assert store.import_legacy(runs) is None           # only once
