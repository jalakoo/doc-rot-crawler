"""SARIF 2.1.0 output, and what CI tools require of it."""
from __future__ import annotations

import json

from tests.fakes import make_outcome
from tests.test_export import DOCS, REPOS, _save


def _doc(store):
    rec, run = _save(store)
    return rec, run, json.loads(store.export_text(rec.id, fmt="sarif"))


def test_document_shape(store):
    rec, run, doc = _doc(store)
    assert doc["version"] == "2.1.0" and doc["$schema"].endswith("sarif-2.1.0.json")
    [srun] = doc["runs"]
    driver = srun["tool"]["driver"]
    assert driver["name"] == "docrot" and driver["version"]
    assert srun["automationDetails"]["id"] == f"docrot/{rec.id}/{run.id}"
    assert srun["invocations"][0]["executionSuccessful"] is True
    assert srun["versionControlProvenance"] == [
        {"repositoryUri": REPOS[0], "revisionId": "abc0def0123"},
        {"repositoryUri": REPOS[1], "revisionId": "abc1def0123"}]
    # the whole scan record rides along, so the file can be imported back
    assert srun["properties"]["docrot"]["type"] == "scan"


def test_rules_are_declared_once_for_every_kind_used(store):
    _, _, doc = _doc(store)
    run = doc["runs"][0]
    declared = {r["id"] for r in run["tool"]["driver"]["rules"]}
    used = {r["ruleId"] for r in run["results"]}
    assert used <= declared and declared == used
    for rule in run["tool"]["driver"]["rules"]:
        assert rule["shortDescription"]["text"] and rule["fullDescription"]["text"]
        assert rule["name"].isalnum()


def test_results_carry_level_location_and_fingerprint(store):
    _, _, doc = _doc(store)
    results = doc["runs"][0]["results"]
    levels = {r["ruleId"]: r["level"] for r in results}
    assert levels["docrot/missing_symbol"] == "error"
    assert levels["docrot/stale_pin"] == "warning"

    gone = next(r for r in results if r["ruleId"] == "docrot/missing_symbol")
    assert "Fix:" in gone["message"]["text"]
    location = gone["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"].startswith("https://docs.a.io/")
    assert location["region"]["startLine"] >= 1
    assert gone["partialFingerprints"]["docrotFindingId/v1"] == gone["properties"]["docrot"]["id"]
    assert gone["relatedLocations"][0]["physicalLocation"]["artifactLocation"]["uri"].startswith(
        "https://github.com/a/")


def test_repo_docs_are_located_in_the_repository(store):
    rec, _ = _save(store)
    store.save_run(rec.id, *_repo_only(rec))
    doc = json.loads(store.export_text(rec.id, fmt="sarif", include_clean=True))
    repo_results = [r for r in doc["runs"][0]["results"]
                    if r["properties"]["docrot"]["doc"]["source"] == "repo"]
    assert repo_results
    for r in repo_results:
        artifact = r["locations"][0]["physicalLocation"]["artifactLocation"]
        assert artifact["uriBaseId"] == "%SRCROOT%" and not artifact["uri"].startswith("/")


def _repo_only(rec):
    out = make_outcome(DOCS, REPOS)
    return out.report, [r.model_dump() for r in out.results], out.extraction.model_dump_json()


def test_unknown_format_is_refused(store):
    rec, _ = _save(store)
    try:
        store.export_text(rec.id, fmt="xml")
    except ValueError as e:
        assert "unknown export format" in str(e)
    else:
        raise AssertionError("expected ValueError")
