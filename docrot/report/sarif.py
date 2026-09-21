"""A run as SARIF 2.1.0, the interchange format for static-analysis results.

GitHub code scanning, VS Code and most CI tools render SARIF as annotations on
the file and line. That fits findings in docs committed to a repo; findings on
a docs site are still included, located by URL.

SARIF carries less structure than the JSON Lines export (one message and one
location per result), so each result also keeps its full record under
`properties.docrot` - which is what makes a SARIF file importable again with
nothing lost.
"""
from __future__ import annotations

from collections.abc import Iterable

from .. import __version__

SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
LEVEL = {"error": "error", "warning": "warning", "info": "note", "none": "none"}

RULES = {
    "stale_pin": ("Stale version pin",
                  "A page pins a release the maintainers have moved on from. Anyone "
                  "following it installs old code."),
    "signature": ("Documented signature is wrong",
                  "The documented parameters, keyword arguments or import path do not "
                  "match the installed package."),
    "missing_symbol": ("Documented symbol does not exist",
                       "The page documents a symbol that cannot be resolved in the "
                       "releases tested - removed, renamed, or never shipped."),
    "runtime": ("Snippet fails when run",
                "The snippet exits non-zero in a clean sandbox against the tested "
                "releases."),
    "blocked": ("Snippet never ran",
                "Nothing here is known to be wrong: an earlier step failed, so this "
                "snippet was never executed. Excluded from the drift score."),
    "clean": ("No drift found", "Every snippet on this page agrees with the package."),
    "unverified": ("Not verified", "Nothing on this page was checked - every snippet is "
                   "unverifiable, or the install it needed failed."),
    "prose": ("Nothing to check", "No code fences and no symbol references on this page."),
}


def document(records: Iterable[dict]) -> dict:
    """SARIF for one run. `records` is `export.records(...)`: scan first."""
    scan: dict = {}
    results, kinds = [], []
    for rec in records:
        if rec.get("type") == "scan":
            scan = rec
            continue
        results.append(_result(rec))
        if rec["kind"] not in kinds:
            kinds.append(rec["kind"])

    run: dict = {
        "tool": {"driver": {
            "name": "docrot",
            "version": __version__,
            "informationUri": "https://github.com/your-org/docrot",
            "rules": [_rule(k) for k in kinds],
        }},
        "automationDetails": {"id": f"docrot/{scan.get('scan', {}).get('id', '')}/{scan.get('run', '')}"},
        "invocations": [{
            "executionSuccessful": True,
            "commandLine": "docrot scan",
            "properties": {"elapsed_s": scan.get("elapsed_s", 0)},
        }],
        "results": results,
        "properties": {"docrot": scan},
    }
    invocation: dict = run["invocations"][0]
    if scan.get("generated_at"):
        # SARIF wants UTC with a trailing Z; scans record local time
        invocation["endTimeUtc"] = scan["generated_at"] + "Z"
    provenance = [{"repositoryUri": p["repo"], "revisionId": p.get("commit", "")}
                  for p in scan.get("packages", []) if p.get("repo")]
    if provenance:
        run["versionControlProvenance"] = provenance
    return {"$schema": SCHEMA, "version": "2.1.0", "runs": [run]}


def _rule(kind: str) -> dict:
    name, description = RULES.get(kind, (kind, ""))
    return {
        "id": f"docrot/{kind}",
        "name": "".join(part.title() for part in kind.split("_")),
        "shortDescription": {"text": name},
        "fullDescription": {"text": description},
        "help": {"text": description},
        "properties": {"tags": ["documentation", "docrot"]},
    }


def _result(rec: dict) -> dict:
    doc, code = rec["doc"], rec["code"]
    message = rec["summary"]
    if rec.get("fix_hint"):
        message += f"\n\nFix: {rec['fix_hint']}"
    result = {
        "ruleId": f"docrot/{rec['kind']}",
        "level": LEVEL.get(rec.get("severity", "error"), "error"),
        "message": {"text": message},
        "locations": [_location(doc)],
        # stable across runs, so code scanning tracks one alert rather than
        # closing and reopening it every scan
        "partialFingerprints": {"docrotFindingId/v1": rec["id"]},
        "properties": {"docrot": rec},
    }
    if code.get("url"):
        result["relatedLocations"] = [{
            "id": 1,
            "physicalLocation": {"artifactLocation": {"uri": code["url"]}},
            "message": {"text": code.get("actual") or code.get("where") or "the code"},
        }]
    return result


def _location(doc: dict) -> dict:
    """Repo docs point at the file in the repository; site pages at the URL."""
    if doc.get("source") == "repo":
        artifact = {"uri": (doc.get("path") or "").lstrip("/"), "uriBaseId": "%SRCROOT%"}
    else:
        artifact = {"uri": doc.get("url", "")}
    location: dict = {"physicalLocation": {"artifactLocation": artifact}}
    if doc.get("line"):
        location["physicalLocation"]["region"] = {"startLine": max(1, int(doc["line"]))}
    if doc.get("snippet_id"):
        location["logicalLocations"] = [{"name": doc["snippet_id"], "kind": "snippet"}]
    return location
