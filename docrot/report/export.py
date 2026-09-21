"""Scan results as JSON Lines, for AI agents and scripts.

The dashboard's report.json is shaped for rendering - HTML in descriptions,
diff fragments, sprite names, a timeline cut to 14 rows. This is the same run
as self-contained plain-text records, one per line:

    line 1     {"type": "scan", ...}      what was scanned, how, and the totals
    line 2..n  {"type": "finding", ...}   one per docs/code disagreement

Every line stands alone, so a consumer can stream it, grep it, or read only
what fits its context. README.md documents every field; `SCHEMA` is bumped on
any change that is not purely additive.
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Iterator

SCHEMA = "docrot/1"
DRIFT_KINDS = ("stale_pin", "signature", "missing_symbol", "runtime")
SEVERITY = {"signature": "error", "missing_symbol": "error", "runtime": "error",
            "stale_pin": "warning", "blocked": "info", "clean": "none", "prose": "none",
            "unverified": "info"}
STATUS = {"blocked": "blocked", "clean": "pass", "prose": "unverifiable",
          "unverified": "unverifiable"}


def records(scan: dict, run: dict, report: dict, extraction: dict, results: list[dict],
            include_clean: bool = False) -> Iterator[dict]:
    """The scan record, then one record per finding, in site-map order.

    `scan` and `run` are the store's scan.json and run.json; the rest are the
    run's report.json, extraction.json and results.json, as loaded JSON.
    """
    snippets = {s["id"]: s for s in extraction.get("snippets", [])}
    pages = {p["url"]: p for p in extraction.get("pages", [])}
    packages = _packages(report)
    by_snippet: dict[str, dict[str, dict]] = {}
    for r in results:
        by_snippet.setdefault(r["id"], {})[r["version"]] = r

    yield _scan_record(scan, run, report, results, packages)

    for section in report.get("sections", []):
        for page in section.get("pages", []):
            for f in page.get("findings", []):
                if f["kind"] in DRIFT_KINDS or f["kind"] == "blocked" or include_clean:
                    yield _finding(f, section["name"], page, scan, run, pages,
                                   snippets, by_snippet, packages)


def write(lines: Iterable[dict], stream) -> int:
    n = 0
    for rec in lines:
        stream.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n += 1
    return n


# ---------------------------------------------------------------- the records

def _scan_record(scan, run, report, results, packages) -> dict:
    statuses: dict[str, int] = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    return {
        "type": "scan",
        "schema": SCHEMA,
        "scan": {"id": scan["id"], "name": scan["name"]},
        "run": run["id"],
        "generated_at": report.get("generated_at", run.get("at", "")),
        "elapsed_s": report.get("elapsed", run.get("elapsed", 0)),
        "sources": report.get("sources") or (
            [{"kind": "docs", "url": u, "pages": None} for u in scan.get("docs", [])]
            + [{"kind": "repo", "url": u, "pages": None} for u in scan.get("repos", [])]),
        "packages": [{
            "package": p["package"], "import_name": p.get("import_name", ""),
            "ecosystem": p.get("ecosystem", "pypi"), "repo": p.get("repo_url", ""),
            "branch": p.get("branch", ""), "commit": p.get("commit", ""),
            "versions_tested": [v["label"] for v in p.get("versions", [])],
        } for p in packages],
        "options": scan.get("options", {}),
        "stats": {**report.get("stats", {}), "page_states": run.get("page_states", {}),
                  "results": statuses},
    }


def _finding(f, section, page, scan, run, pages, snippets, by_snippet, packages) -> dict:
    snippet_id = f.get("snippet") or _snippet_from_where(f.get("doc_where", ""), snippets)
    snip = snippets.get(snippet_id, {})
    src = pages.get(page["url"], {})
    pkg = _package_for(snip.get("package", ""), packages)
    tested = [v["label"] for v in pkg.get("versions", [])]
    per_version = by_snippet.get(snippet_id, {})

    def versions_with(status):
        return [v for v in tested if per_version.get(v, {}).get("status") == status]

    kind = f["kind"]
    status = STATUS.get(kind, "fail")
    doc_text = "".join(part[0] for part in f.get("doc_parts", []))
    actual = "".join(part[0] for part in f.get("code_parts", []))
    blocked_by = next((r.get("blocked_by") for r in per_version.values() if r.get("blocked_by")), None)

    return {
        "type": "finding",
        "id": ":".join([scan["id"], snippet_id or f"page:{page['path']}", kind]),
        "scan": scan["id"],
        "run": run["id"],
        "kind": kind,
        "label": f.get("label", ""),
        "status": status,
        "severity": SEVERITY.get(kind, "error"),
        "title": f["title"].capitalize(),
        "summary": plain(f.get("desc", "")),
        "doc": {
            "url": page["url"],
            "title": page.get("name", ""),
            "section": section,
            "source": src.get("source", "repo" if page["url"].startswith("repo://") else "site"),
            "origin": src.get("origin", ""),
            "path": page.get("path", ""),
            "line": f.get("line") or snip.get("line") or None,
            "snippet_id": snippet_id or None,
            "snippet": snip.get("code") or doc_text,
            "lang": snip.get("lang", ""),
        },
        "code": {
            "package": pkg.get("package", ""),
            "import_name": pkg.get("import_name", ""),
            "repo": pkg.get("repo_url", ""),
            "commit": pkg.get("commit", ""),
            "url": f.get("code_url", ""),
            "where": f.get("code_where", ""),
            "actual": actual,
        },
        "versions": {
            "tested": tested,
            "failing": versions_with("fail"),
            "passing": versions_with("pass"),
            "blocked": versions_with("blocked"),
            "unverified": versions_with("unverified"),
        },
        "symbols": [s for s in snip.get("symbols", []) if not s.startswith(("pip:", "env:"))],
        "tier": snip.get("tier", ""),
        "blocked_by": blocked_by,
        "days_behind": f.get("days_behind", 0),
        "evidence": f.get("evidence", ""),
        "fix_hint": fix_hint(f, actual, blocked_by),
    }


def fix_hint(f: dict, actual: str, blocked_by: str | None = None) -> str:
    """What to change, from the finding type alone - deterministic, no model."""
    kind, label = f["kind"], f.get("label", "")
    if kind == "stale_pin":
        pin = f.get("doc_stamp", "").split(" · ")[0]
        latest = f.get("code_stamp", "").split(" · ")[0]
        return f"Update the pinned version {pin} to the current release {latest}, or say why the older pin is required."
    if kind == "signature" and label == "wrong import path":
        return f"Change the import to the symbol's current location: {actual.strip()}."
    if kind == "signature" and label == "kwarg renamed":
        return f"Replace the keyword argument that no longer exists. The current signature is {actual.strip()}."
    if kind == "signature":
        return f"Update the documented signature to match the code: {actual.strip()}."
    if kind == "missing_symbol" and label == "never shipped":
        return "No tested release has this symbol. Remove it from the docs, or mark it as unreleased."
    if kind == "missing_symbol":
        return "The symbol was removed. Document its replacement, or remove the example."
    if kind == "runtime":
        return "Run the snippet against the current release and update it to match the error in `evidence`."
    if kind == "blocked":
        return (f"Not known to be wrong: an earlier step ({blocked_by or 'upstream'}) failed, so this never ran. "
                "Fix that step first, then re-scan.")
    if kind == "unverified":
        return ("Nothing ran here. Check that the package installs from the registry "
                "(a package that is not published cannot be verified), then re-scan.")
    return ""


# ------------------------------------------------------------------- helpers

TAG = re.compile(r"<[^>]+>")


def plain(text: str) -> str:
    """Report descriptions carry <b> markup for the UI."""
    return " ".join(html.unescape(TAG.sub("", text or "")).split())


def _packages(report: dict) -> list[dict]:
    """Per-package details; a report from before multi-source had one."""
    if report.get("packages"):
        return report["packages"]
    return [{"package": report.get("package", ""), "import_name": report.get("import_name", ""),
             "repo_url": report.get("repo_url", ""), "branch": report.get("branch", ""),
             "commit": report.get("commit", ""), "versions": report.get("versions", [])}]


def _package_for(name: str, packages: list[dict]) -> dict:
    return next((p for p in packages if p["package"] == name), packages[0] if packages else {})


def _snippet_from_where(doc_where: str, snippets: dict) -> str:
    """Reports written before findings carried `snippet` put the id after ` · `."""
    tail = doc_where.rsplit(" · ", 1)[-1] if " · " in doc_where else ""
    return tail if tail in snippets else ""
