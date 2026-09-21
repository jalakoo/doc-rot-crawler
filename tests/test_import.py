"""Reading an exported run back in - from this tool, or another."""
from __future__ import annotations

import json

import pytest

from docrot.report import ingest
from docrot.store import AlreadyImported, Store
from tests.test_export import DOCS, REPOS, _lines, _save


@pytest.fixture
def exported(store):
    """(jsonl text, sarif text) for a real two-package run."""
    rec, _ = _save(store)
    return store.export_text(rec.id), store.export_text(rec.id, fmt="sarif")


@pytest.mark.parametrize("fmt", [0, 1])
def test_round_trip_keeps_the_run(exported, tmp_path, fmt):
    fresh = Store(tmp_path / "fresh")
    rec, _run = fresh.import_run(exported[fmt], filename=f"alpha.{'jsonl' if not fmt else 'sarif'}")

    assert rec.docs == DOCS and rec.repos == REPOS
    assert rec.name == "Alpha" and rec.imported_from.startswith("alpha.")
    assert fresh.status(rec.id).state == "done"

    report = fresh.report(rec.id)
    assert report["stats"]["findings"] == 3 and report["stats"]["pages"] == 3
    assert [p["package"] for p in report["packages"]] == ["alpha", "beta"]
    assert [v["label"] for v in report["versions"]] == ["1.0.0", "1.1.0", "2.0.0"]
    assert report["imported"]["file"].startswith("alpha.")
    assert any("imported from" in line[1] for line in report["log"])

    rows = {row["n"]: row["c"] for row in report["timeline"]}
    assert rows["Quickstart"] == ["pass", "fail", "fail"]          # verdicts survive
    assert {row["v"] for row in report["timeline"]} == {"died at 1.1.0"}

    # and it exports again to the same findings (ids are scoped to the scan,
    # and an import that starts a new scan gets a new one)
    def suffixes(lines):
        return {f["id"].split(":", 1)[1] for f in lines}

    assert suffixes(_lines(fresh.export_text(rec.id))[1:]) == suffixes(_lines(exported[0])[1:])


def _older(text, when="2025-09-01T09:00:00", run_id="20250901T090000"):
    """The same export, as if it were an earlier run of the same scan."""
    out = []
    for line in text.splitlines():
        rec = json.loads(line)
        if rec.get("type") == "scan":
            rec["generated_at"], rec["run"] = when, run_id
        out.append(json.dumps(rec))
    return "\n".join(out) + "\n"


def test_import_joins_an_existing_scan_with_the_same_sources(store, exported):
    [existing] = store.all()
    before = len(store.runs(existing.id))
    rec, run = store.import_run(_older(exported[0]), filename="colleague.jsonl")
    assert rec.id == existing.id and len(store.runs(existing.id)) == before + 1
    assert run.id == "20250901T090000"            # keeps the original run's identity


def test_importing_the_same_run_twice_is_refused(store, exported, tmp_path):
    fresh = Store(tmp_path / "fresh")
    fresh.import_run(exported[0], filename="alpha.jsonl")
    with pytest.raises(AlreadyImported, match="already has this run"):
        fresh.import_run(exported[1], filename="alpha.sarif")


def test_findings_render_like_scanned_ones(exported, tmp_path):
    fresh = Store(tmp_path / "fresh")
    rec, _ = fresh.import_run(exported[0], filename="alpha.jsonl")
    pages = [p for s in fresh.report(rec.id)["sections"] for p in s["pages"]]
    gone = next(f for p in pages for f in p["findings"] if f["kind"] == "missing_symbol")
    assert gone["title"].isupper() and gone["desc"]
    assert gone["doc_parts"][0][0].startswith("import ")
    assert "AttributeError" in gone["code_parts"][0][0]
    assert ["", "imported"] in gone["chips"] and gone["sprite"] == "wraith"
    assert {p["st"] for p in pages} == {"fail", "drift"}


def test_sarif_from_another_tool_is_readable():
    doc = {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
           "runs": [{"tool": {"driver": {"name": "some-linter"}}, "results": [
               {"ruleId": "docs/broken-link", "level": "warning",
                "message": {"text": "Link target missing"},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": "docs/guide.md"}, "region": {"startLine": 12}}}]}]}]}
    [record] = ingest.read(json.dumps(doc))
    assert record["type"] == "finding" and record["status"] == "fail"
    assert record["severity"] == "warning" and record["doc"]["line"] == 12
    assert record["doc"]["source"] == "repo" and record["doc"]["path"] == "docs/guide.md"
    assert record["id"] == "some-linter:docs/guide.md:docs/broken-link"
    run = ingest.to_run([record], "linter.sarif")
    assert run["report"]["stats"]["findings"] == 1
    assert run["report"]["sections"][0]["name"] == "Imported from some-linter"


@pytest.mark.parametrize("text,message", [
    ("", "empty"),
    ("not json at all", "not valid JSON"),
    ('{"type": "other"}', "not a docrot record"),
    ('{"type": "scan", "scan": {"id": "x"}}', "No findings"),
    ('{"version": "2.1.0", "$schema": "sarif", "runs": []}', "no runs"),
    ('{"version": "2.1.0", "$schema": "sarif", "runs": [{"results": []}]}', "no results"),
])
def test_unreadable_files_say_why(text, message):
    with pytest.raises(ingest.BadExport, match=message):
        ingest.read(text)
