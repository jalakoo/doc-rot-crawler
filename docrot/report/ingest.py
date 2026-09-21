"""Read an exported run back in: JSON Lines or SARIF, from this tool or another.

The dashboard renders report.json, so an import has to rebuild one. Everything
needed is in the records - SARIF keeps each full record under
`properties.docrot`, and a SARIF file from elsewhere is read from its own
fields, with whatever is missing left empty rather than guessed at.

Results and a minimal extraction are rebuilt too, so an imported run diffs and
re-exports like a scanned one.
"""
from __future__ import annotations

import json
from typing import Any

from .build import DRIFT_KINDS, _verdict

DAMAGE = {"stale_pin": "-1 RELEASE", "signature": "-1 PARAM", "missing_symbol": "-1 SYMBOL",
          "runtime": "-1 RUN", "blocked": "NOT COUNTED", "clean": "NO DAMAGE", "prose": "NO TARGET",
          "unverified": "NOT COUNTED"}
SPRITE = {"stale_pin": "mimic", "signature": "mimic", "missing_symbol": "wraith",
          "runtime": "slime", "blocked": "wraith"}
CHIP = {"fail": "st-fail", "blocked": "st-block", "pass": "st-pass", "unverifiable": "tier-unv"}


class BadExport(ValueError):
    """The file is not an export this can read. The message says why."""


def read(text: str) -> list[dict]:
    """Records from a JSON Lines or SARIF file. Raises BadExport if neither."""
    stripped = (text or "").strip()
    if not stripped:
        raise BadExport("The file is empty.")
    if stripped[0] == "{" and '"runs"' in stripped[:2000] and "sarif" in stripped[:2000].lower():
        try:
            return from_sarif(json.loads(stripped))
        except json.JSONDecodeError as e:
            raise BadExport(f"Not valid SARIF JSON: {e}") from e
    return from_jsonl(stripped)


def from_jsonl(text: str) -> list[dict]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise BadExport(f"Line {n} is not valid JSON: {e}") from e
        if not isinstance(rec, dict) or rec.get("type") not in ("scan", "finding"):
            raise BadExport(f"Line {n} is not a docrot record: expected a "
                               '"scan" or "finding" object.')
        out.append(rec)
    if not any(r["type"] == "finding" for r in out):
        raise BadExport("No findings in this file.")
    return out


def from_sarif(doc: dict) -> list[dict]:
    runs = doc.get("runs") or []
    if not runs:
        raise BadExport("This SARIF file has no runs.")
    run = runs[0]
    scan = (run.get("properties") or {}).get("docrot")
    records: list[dict] = [scan] if scan else []
    for result in run.get("results") or []:
        rec = (result.get("properties") or {}).get("docrot")
        records.append(rec if rec else _record_from_result(result, run))
    if len(records) <= (1 if scan else 0):
        raise BadExport("This SARIF file has no results.")
    return records


def _record_from_result(result: dict, run: dict) -> dict:
    """A result from another tool: keep what SARIF carries, leave the rest empty."""
    loc = ((result.get("locations") or [{}])[0].get("physicalLocation") or {})
    artifact = loc.get("artifactLocation") or {}
    uri = artifact.get("uri", "")
    rule = str(result.get("ruleId") or "imported")
    kind = rule.split("/")[-1]
    level = result.get("level", "warning")
    tool = ((run.get("tool") or {}).get("driver") or {}).get("name", "an external tool")
    return {
        "type": "finding",
        "id": (result.get("partialFingerprints") or {}).get("docrotFindingId/v1")
              or f"{tool}:{uri}:{rule}",
        "kind": kind if kind in DAMAGE else "runtime",
        "label": rule,
        "status": "fail" if level in ("error", "warning") else "blocked",
        "severity": {"error": "error", "warning": "warning", "note": "info"}.get(level, "error"),
        "title": ((result.get("message") or {}).get("text") or rule).split("\n")[0][:80],
        "summary": (result.get("message") or {}).get("text", ""),
        "doc": {"url": uri, "title": uri.rsplit("/", 1)[-1], "section": f"Imported from {tool}",
                "source": "site" if uri.startswith("http") else "repo",
                "origin": "", "path": uri, "line": (loc.get("region") or {}).get("startLine"),
                "snippet_id": None, "snippet": "", "lang": ""},
        "code": {"package": "", "import_name": "", "repo": "", "commit": "", "url": "",
                 "where": "", "actual": ""},
        "versions": {"tested": [], "failing": [], "passing": [], "blocked": [], "unverified": []},
        "symbols": [], "tier": "", "blocked_by": None, "days_behind": 0,
        "evidence": "", "fix_hint": "",
    }


# --------------------------------------------------------------- rebuild a run

def to_run(records: list[dict], filename: str = "") -> dict:
    """{scan, run, report, results, extraction} rebuilt from the records."""
    scan = next((r for r in records if r.get("type") == "scan"), {})
    findings = [r for r in records if r.get("type") == "finding"]
    packages = scan.get("packages") or [{"package": "", "import_name": "", "versions_tested": []}]

    pages: dict[str, dict] = {}
    results: list[dict] = []
    snippets: list[dict] = []
    impact: dict[str, list[str]] = {}
    # (page url, package) -> which versions failed and which passed
    verdicts: dict[tuple[str, str], dict[str, set]] = {}

    for rec in findings:
        doc = rec.get("doc") or {}
        url = doc.get("url") or doc.get("path") or rec["id"]
        page = pages.setdefault(url, {
            "id": f"p{len(pages) + 1}", "tag": str(len(pages) + 1),
            "name": doc.get("title") or doc.get("path") or url, "path": doc.get("path", ""),
            "url": url, "section": doc.get("section") or "Imported", "st": "pass", "sev": 0,
            "findings": [], "source": doc.get("source", "site"), "origin": doc.get("origin", ""),
        })
        page["findings"].append(_finding(rec, page))
        _tally(page, rec)

        versions = rec.get("versions") or {}
        seen = verdicts.setdefault((url, (rec.get("code") or {}).get("package", "")),
                                   {"failing": set(), "passing": set()})
        seen["failing"].update(versions.get("failing") or [])
        seen["passing"].update(versions.get("passing") or [])

        snippet_id = doc.get("snippet_id")
        if snippet_id:
            snippets.append({
                "id": snippet_id, "page": url, "lang": doc.get("lang", ""),
                "code": doc.get("snippet", ""), "tier": rec.get("tier") or "structural",
                "symbols": rec.get("symbols") or [], "package": (rec.get("code") or {}).get("package", ""),
                "line": doc.get("line") or 0, "requires": [], "kind": "program", "declares": [],
            })
            results.extend(_results(rec, snippet_id, url))
        for symbol in rec.get("symbols") or []:
            for key in {symbol.lower(), symbol.split(".")[-1].lower()}:
                if len(key) >= 4 and page["id"] not in impact.setdefault(key, []):
                    impact[key].append(page["id"])

    report = _report(scan, list(pages.values()), packages, impact, filename, len(findings),
                     verdicts)
    return {
        "scan": {"name": scan.get("scan", {}).get("name") or (filename or "Imported scan"),
                 "docs": [s["url"] for s in scan.get("sources", []) if s.get("kind") == "docs"],
                 "repos": [s["url"] for s in scan.get("sources", []) if s.get("kind") == "repo"],
                 "options": scan.get("options") or {}},
        "run": scan.get("run") or "",
        "report": report,
        "results": results,
        "extraction": {
            "package": packages[0].get("package", ""),
            "import_name": packages[0].get("import_name", ""),
            "packages": [{"package": p.get("package", ""), "import_name": p.get("import_name", ""),
                          "ecosystem": p.get("ecosystem", "pypi")} for p in packages],
            "pages": [{"url": p["url"], "path": p["path"], "title": p["name"],
                       "source": p["source"], "origin": p["origin"], "text": "",
                       "pinned_versions": []} for p in pages.values()],
            "snippets": snippets, "claims": [],
        },
    }


def _finding(rec: dict, page: dict) -> dict:
    doc, code = rec.get("doc") or {}, rec.get("code") or {}
    kind = rec.get("kind", "runtime")
    versions = rec.get("versions") or {}
    failing = versions.get("failing") or []
    return {
        "kind": kind, "label": rec.get("label", kind), "title": (rec.get("title") or "").upper(),
        # kept as plain text: the dashboard escapes it at render (lib.richText),
        # so storing entities here would only double-escape it
        "desc": rec.get("summary", "") or "", "page": page["url"],
        "snippet": doc.get("snippet_id") or "", "line": doc.get("line") or 0,
        "doc_where": " · ".join(x for x in (doc.get("path"), doc.get("snippet_id")) if x),
        "doc_url": doc.get("url", ""), "doc_stamp_label": "docs reflect",
        "doc_stamp": ", ".join(versions.get("passing") or []) or "—",
        "code_where": code.get("where", ""), "code_url": code.get("url", ""),
        "code_stamp_label": "code is at",
        "code_stamp": ", ".join(failing) or ", ".join(versions.get("tested") or []) or "—",
        "doc_parts": [[doc.get("snippet") or "(not in the export)", "same"]],
        "code_parts": [[code.get("actual") or rec.get("evidence") or "(not in the export)", "void"]],
        "damage": DAMAGE.get(kind, "-1 RUN"), "days_behind": rec.get("days_behind", 0),
        "evidence": "\n\n".join(x for x in (rec.get("evidence"), rec.get("fix_hint")) if x),
        "chips": ([["tier-struct", rec["tier"]]] if rec.get("tier") else []) + [
            [CHIP.get(rec.get("status", "fail"), "st-fail"), rec.get("status", "fail")],
            ["", "imported"]],
        "sprite": SPRITE.get(kind, "none"),
    }


def _tally(page: dict, rec: dict) -> None:
    kind = rec.get("kind")
    if kind in DRIFT_KINDS:
        page["sev"] = min(3, page["sev"] + 1)
        page["st"] = "drift" if kind == "stale_pin" and page["st"] != "fail" else "fail"
    elif kind == "blocked" and page["st"] == "pass":
        page["st"], page["sev"] = "block", 1
    elif kind in ("prose", "unverified") and page["st"] == "pass":
        page["st"] = "unlit"


def _results(rec: dict, snippet_id: str, url: str) -> list[dict]:
    versions, out = rec.get("versions") or {}, []
    package = (rec.get("code") or {}).get("package", "")
    for status in ("fail", "pass", "blocked", "unverified"):
        for version in versions.get("failing" if status == "fail" else
                                    "passing" if status == "pass" else status) or []:
            out.append({"id": snippet_id, "page": url, "version": version, "status": status,
                        "stderr": rec.get("evidence", "") if status == "fail" else "",
                        "blocked_by": rec.get("blocked_by"), "tier": rec.get("tier") or "structural",
                        "package": package})
    return out


def _report(scan: dict, pages: list[dict], packages: list[dict], impact: dict,
            filename: str, n_findings: int, verdicts: dict) -> dict:
    sections: dict[str, list] = {}
    for page in pages:
        sections.setdefault(page.pop("section"), []).append(page)
    drifted = sum(1 for p in pages if any(f["kind"] in DRIFT_KINDS for f in p["findings"]))
    findings = sum(len([f for f in p["findings"] if f["kind"] in DRIFT_KINDS]) for p in pages)
    labels = [list(p.get("versions_tested") or []) for p in packages]
    original = scan.get("stats") or {}

    timeline = []
    for i, package_labels in enumerate(labels):
        package = packages[i].get("package", "")
        for page in pages:
            seen = verdicts.get((page["url"], package))
            if not seen or not package_labels:
                continue
            cells = ["fail" if v in seen["failing"] else "pass" if v in seen["passing"] else "na"
                     for v in package_labels]
            if set(cells) == {"na"}:
                continue
            timeline.append({"n": page["name"], "p": page["path"], "c": cells, "pin": None,
                             "v": _verdict(cells, package_labels), "ok": "fail" not in cells,
                             "hold": "na" in cells and "fail" not in cells, "pkg": i})

    return {
        "generated_at": scan.get("generated_at", ""),
        "docs_url": next((s["url"] for s in scan.get("sources", []) if s["kind"] == "docs"), ""),
        "repo_url": next((s["url"] for s in scan.get("sources", []) if s["kind"] == "repo"), ""),
        "docs_urls": [s["url"] for s in scan.get("sources", []) if s["kind"] == "docs"],
        "repo_urls": [s["url"] for s in scan.get("sources", []) if s["kind"] == "repo"],
        "sources": [{"kind": s["kind"], "url": s["url"],
                     "pages": sum(1 for p in pages if p["origin"] == s["url"]) or None}
                    for s in scan.get("sources", [])],
        "branch": packages[0].get("branch", ""), "commit": packages[0].get("commit", ""),
        "strategy": f"imported from {filename}" if filename else "imported",
        "package": packages[0].get("package", ""), "import_name": packages[0].get("import_name", ""),
        "packages": [{"package": p.get("package", ""), "import_name": p.get("import_name", ""),
                      "ecosystem": p.get("ecosystem", "pypi"), "repo_url": p.get("repo", ""),
                      "branch": p.get("branch", ""), "commit": p.get("commit", ""),
                      "versions": [{"label": v, "released_at": ""} for v in p.get("versions_tested") or []]}
                     for p in packages],
        "elapsed": scan.get("elapsed_s", 0),
        "stats": {"pages": len(pages), "drifted": drifted, "findings": findings,
                  "worst_gap": max((f["days_behind"] for p in pages for f in p["findings"]), default=0),
                  "versions": sum(len(x) for x in labels),
                  "snippets": sum(len(p["findings"]) for p in pages)},
        "versions": [{"label": v, "released_at": ""} for v in (labels[0] if labels else [])],
        "sections": [{"name": k, "pages": v} for k, v in sections.items()],
        "timeline": timeline[:14], "impact": impact,
        "imported": {"file": filename, "scan": scan.get("scan", {}).get("id", ""),
                     "run": scan.get("run", ""), "records": n_findings, "original_stats": original},
        "log": _log(scan, filename, n_findings, len(pages), original),
    }


def _log(scan: dict, filename: str, n_records: int, n_pages: int,
         original: dict[str, Any]) -> list[list[str]]:
    lines = [["t-sys", f"  imported from {filename or 'an uploaded file'}"]]
    if scan:
        lines.append(["t-sys", f"  original scan {scan.get('scan', {}).get('id', '?')} "
                               f"run {scan.get('run', '?')} · {scan.get('generated_at', '?')}"])
    lines.append(["t-sys", f"  {n_records} record(s) on {n_pages} page(s)"])
    if original.get("pages") and original["pages"] > n_pages:
        lines.append(["t-sys", f"  the original scan covered {original['pages']} pages; an export "
                               "carries only pages with findings unless --include-clean"])
    lines.append(["t-ok", "› imported — nothing was re-verified"])
    return lines
