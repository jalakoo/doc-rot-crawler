"""S2 - Structural probe precision.

Adjudicates every `fail` in the 2026-09-09 run against ground truth, so the
decay score has a measured false-positive rate rather than an assumed one.

Ground truth sources, in order of authority:
  1. the cloned repo at HEAD, walked with `ast` - tells us whether a name
     exists at all, and *what kind* of thing it is (module-level function,
     method, Enum, Pydantic model)
  2. the per-version probe output already captured in results.json - tells us
     whether a name's existence actually differs between releases, which is
     the only evidence that distinguishes real rot from a probe artifact

Adjudication is rule-based, not by hand, so it is reproducible and can be
re-run after the probe is fixed.

Also measures the blocked cascade: `run_chain` blocks the rest of a page after
any fail, including a structural one - but a structural probe mutates no
sandbox state, so those blocks are unjustified.

Usage:  python spikes/s2_probe_precision.py
"""
from __future__ import annotations

import ast
import collections
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "data" / "runs" / "latest"
REPO = ROOT / "data" / "cache" / "repo-perseus-client"
OUT = ROOT / "spikes" / "out" / "s2_probe_precision.json"


# --------------------------------------------------------------------------
# Ground truth: what exists at HEAD, and what kind of thing it is
# --------------------------------------------------------------------------
def head_symbols(pkg_root: Path) -> dict:
    """name -> {kind, module, qualified, bases}. Methods recorded separately
    from module-level functions: conflating the two is one of the bugs under
    test."""
    table: dict[str, dict] = {}
    for py in sorted(pkg_root.rglob("*.py")):
        rel = py.relative_to(pkg_root.parent).with_suffix("")
        module = ".".join(rel.parts)
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                table.setdefault(node.name, {
                    "kind": "function", "module": module,
                    "qualified": f"{module}.{node.name}", "bases": []})
            elif isinstance(node, ast.ClassDef):
                bases = [ast.unparse(b) for b in node.bases]
                table.setdefault(node.name, {
                    "kind": "class", "module": module,
                    "qualified": f"{module}.{node.name}", "bases": bases})
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        key = f"{node.name}.{sub.name}"
                        table.setdefault(key, {
                            "kind": "method", "module": module,
                            "qualified": f"{module}.{key}", "bases": []})
                        # also record the bare method name, which is how the
                        # docs print it and how the probe searched for it
                        table.setdefault(sub.name, {
                            "kind": "method", "module": module,
                            "qualified": f"{module}.{key}", "bases": []})
    return table


def toplevel_exports(init_py: Path) -> set[str]:
    """Names actually importable from the package root."""
    names: set[str] = set()
    tree = ast.parse(init_py.read_text(encoding="utf-8", errors="replace"))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def is_enum(entry: dict | None) -> bool:
    return bool(entry) and any("Enum" in b for b in entry.get("bases", []))


def is_model(entry: dict | None) -> bool:
    return bool(entry) and any("BaseModel" in b for b in entry.get("bases", []))


# --------------------------------------------------------------------------
# Adjudication rules
# --------------------------------------------------------------------------
BAD_PARAM = re.compile(r"docs document parameter\(s\) (.+?) that do not exist")
UNDOC_REQ = re.compile(r"required parameter\(s\) (.+?) are missing from the docs")
NOT_IMPORTABLE = re.compile(r"documented (\w+)\(\) is not importable as shown")
NOT_EXIST = re.compile(r"documented (\w+)\(\) does not exist in this release")
ATTR_ERR = re.compile(r"^([\w.]+): (\w+Error): ")
BAD_KWARG = re.compile(r"(\w+)\(\) has no parameter '(\w+)'")

# A parameter name that is really a fragment of a type annotation. The
# declaration parser split `Optional[Dict[str, Any]]` on the comma inside the
# subscript, so the tail arrived as a parameter called `Any]]`.
ANNOTATION_FRAGMENT = re.compile(r"[\[\]]|^(str|int|bool|float|Any|None)$")


def adjudicate(msg: str, snippet: dict, per_version: dict,
               head: dict, exports: set[str]) -> tuple[str, str, str]:
    """-> (verdict, bucket, reasoning). verdict in TP / FP / AMBIGUOUS."""

    # --- probe artifact: annotation fragment parsed as a parameter name -----
    m = BAD_PARAM.search(msg)
    if m:
        params = [p.strip() for p in m.group(1).split(",")]
        if any(ANNOTATION_FRAGMENT.search(p) for p in params):
            return ("FP", "annotation-fragment-as-param",
                    f"{params!r} is a fragment of a type annotation the "
                    f"declaration parser split on an inner comma")
        return ("AMBIGUOUS", "documented-param-absent",
                "documented parameter genuinely absent from signature")

    # --- probe artifact: vacuous comparison against an empty documented list -
    m = UNDOC_REQ.search(msg)
    if m:
        declared = (snippet.get("declares") or [{}])[0]
        name = declared.get("name", "")
        entry = head.get(name)
        if is_enum(entry):
            return ("FP", "enum-signature",
                    f"{name} is an Enum at HEAD; inspect.signature returns "
                    f"(*values) for every Enum class, so the comparison is "
                    f"meaningless")
        if not declared.get("params"):
            kind = ("Pydantic model" if is_model(entry) else "class")
            return ("FP", "vacuous-empty-documented",
                    f"docs printed no parameter list for this {kind}; the "
                    f"probe compared the real signature against [] and "
                    f"reported every field as undocumented")
        return ("AMBIGUOUS", "undocumented-required-param",
                "docs printed a signature that omits a required parameter")

    # --- name exists at HEAD but not where the probe looked ----------------
    m = NOT_EXIST.search(msg)
    if m:
        name = m.group(1)
        entry = head.get(name)
        # A name that resolves in some tested releases and not others is
        # version drift - the tool's core output - regardless of whether it
        # exists at HEAD. This check must come first: before the probe could
        # find methods at all (F5), drift and scope errors were
        # indistinguishable, and this rule mislabelled the former as the latter.
        statuses = set(per_version.values())
        if "pass" in statuses and "fail" in statuses:
            return ("TP", "absent-in-this-release",
                    f"{name} resolves in another tested release but not this "
                    f"one - documented API that this version does not have")
        if entry and entry["kind"] == "method":
            return ("FP", "method-resolved-as-module-level",
                    f"{name} exists at HEAD as a method ({entry['qualified']}); "
                    f"the probe searched module namespaces only")
        if entry:
            return ("AMBIGUOUS", "exists-at-head-not-in-release",
                    f"{name} exists at HEAD as {entry['kind']} but not in this "
                    f"release - this is drift, not a snippet failure")
        return ("TP", "symbol-absent",
                f"{name} exists nowhere at HEAD and not in this release")

    # --- "not importable as shown" against a bare declaration --------------
    m = NOT_IMPORTABLE.search(msg)
    if m:
        name = m.group(1)
        entry = head.get(name)
        code = snippet.get("code", "")
        claims_import = bool(re.search(r"^\s*(from|import)\s", code, re.M))
        if not claims_import:
            return ("FP", "declaration-never-claimed-import-path",
                    f"the snippet is a class/def declaration, not an import; "
                    f"it never claimed {name} was importable from the package "
                    f"root. Canonical location at HEAD: "
                    f"{entry['qualified'] if entry else 'unknown'}")
        if name in exports:
            return ("FP", "reexport-missed",
                    f"{name} is re-exported from the package root at HEAD")
        return ("TP", "import-path-wrong",
                f"docs import {name} from a path it does not live at")

    # --- resolution failure, version-differentiated ------------------------
    m = ATTR_ERR.search(msg)
    if m:
        dotted, err = m.group(1), m.group(2)
        name = dotted.split(".")[-1]
        statuses = set(per_version.values())
        differs = "pass" in statuses and "fail" in statuses
        entry = head.get(name)
        if differs and not entry:
            return ("TP", "removed-between-releases",
                    f"{dotted} resolves in an earlier release and not in a "
                    f"later one, and is absent at HEAD - a real removal the "
                    f"docs never followed")
        if not entry:
            return ("TP", "symbol-absent",
                    f"{dotted} exists in no tested release and not at HEAD")
        if entry["kind"] == "method":
            return ("FP", "method-resolved-as-module-level",
                    f"{name} is a method at HEAD ({entry['qualified']}), not a "
                    f"module attribute")
        return ("AMBIGUOUS", "resolution-failure",
                f"{dotted} failed to resolve; {name} exists at HEAD as "
                f"{entry['kind']}")

    m = BAD_KWARG.search(msg)
    if m:
        return ("TP", "bad-kwarg",
                f"{m.group(1)}() is called with '{m.group(2)}', which the "
                f"signature does not accept")

    return ("AMBIGUOUS", "unclassified", msg[:120])


# --------------------------------------------------------------------------
def main() -> None:
    results = json.loads((RUN / "results.json").read_text())
    extraction = json.loads((RUN / "extraction.json").read_text())
    snippets = {s["id"]: s for s in extraction["snippets"]}

    head = head_symbols(REPO / "perseus_client")
    exports = toplevel_exports(REPO / "perseus_client" / "__init__.py")

    per_version: dict[str, dict] = collections.defaultdict(dict)
    for r in results:
        per_version[r["id"]][r["version"]] = r["status"]

    rows, buckets = [], collections.Counter()
    verdicts = collections.Counter()
    snippet_verdict: dict[str, str] = {}

    for r in results:
        if r["status"] != "fail":
            continue
        try:
            payload = json.loads(r["stderr"])
        except Exception:
            payload = {"failed": ["<unparseable probe output>"]}

        snip = snippets.get(r["id"], {})
        msgs = payload.get("failed") or ["<no message>"]
        # a result is a TP if ANY of its messages is a real finding
        best = "FP"
        for msg in msgs:
            v, b, why = adjudicate(msg, snip, per_version[r["id"]], head, exports)
            rows.append({"id": r["id"], "version": r["version"], "verdict": v,
                         "bucket": b, "message": msg[:160], "why": why})
            buckets[(v, b)] += 1
            if v == "TP" or (v == "AMBIGUOUS" and best == "FP"):
                best = v
        verdicts[best] += 1
        prev = snippet_verdict.get(r["id"])
        if prev is None or best == "TP" or (best == "AMBIGUOUS" and prev == "FP"):
            snippet_verdict[r["id"]] = best

    # ---- blocked cascade -------------------------------------------------
    blocked = [r for r in results if r["status"] == "blocked"]
    by_cause = collections.Counter(r["blocked_by"] for r in blocked)
    structural_causes = {
        cause for cause in by_cause
        if cause in snippets and snippets[cause].get("tier") == "structural"
    }
    structural_cascade = sum(by_cause[c] for c in structural_causes)
    infra_cascade = sum(v for k, v in by_cause.items() if k in ("sandbox", "gather", "install"))

    # ---- decay recomputed with FPs removed -------------------------------
    def decay(exclude_fp: bool, unblock_structural: bool) -> dict:
        per_page = collections.defaultdict(lambda: {"total": 0, "failed": 0})
        for r in results:
            if r["version"] != "1.0.0rc19":
                continue
            if r["status"] == "blocked" and unblock_structural and \
                    r["blocked_by"] in structural_causes:
                continue          # would have been evaluated on its own merits
            per_page[r["page"]]["total"] += 1
            if r["status"] == "fail":
                if exclude_fp and snippet_verdict.get(r["id"]) == "FP":
                    continue
                per_page[r["page"]]["failed"] += 1
        pages_with_fail = sum(1 for v in per_page.values() if v["failed"])
        tot = sum(v["total"] for v in per_page.values())
        fail = sum(v["failed"] for v in per_page.values())
        return {"pages_flagged": pages_with_fail, "pages": len(per_page),
                "snippets": tot, "failing": fail,
                "mean_decay": round(fail / tot, 3) if tot else 0}

    n = sum(verdicts.values())
    report = {
        "fail_results": n,
        "distinct_failing_snippets": len(snippet_verdict),
        "verdicts_by_result": dict(verdicts),
        "verdicts_by_snippet": dict(collections.Counter(snippet_verdict.values())),
        "fp_rate_by_result": round(verdicts["FP"] / n, 3) if n else 0,
        "fp_rate_by_snippet": round(
            sum(1 for v in snippet_verdict.values() if v == "FP")
            / max(len(snippet_verdict), 1), 3),
        "buckets": {f"{v}:{b}": c for (v, b), c in buckets.most_common()},
        "blocked": {
            "total": len(blocked),
            "infra_cascade": infra_cascade,
            "structural_cascade": structural_cascade,
            "structural_cascade_pct": round(structural_cascade / max(len(blocked), 1), 3),
            "causes": dict(by_cause),
        },
        "decay_as_reported": decay(False, False),
        "decay_fp_removed": decay(True, False),
        "decay_fp_removed_and_unblocked": decay(True, True),
        "snippet_verdicts": snippet_verdict,
        "rows": rows,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))

    print(f"fail results            : {n}")
    print(f"distinct failing snippets: {len(snippet_verdict)}")
    print(f"verdicts (by result)    : {dict(verdicts)}")
    print(f"verdicts (by snippet)   : {report['verdicts_by_snippet']}")
    print(f"FP rate (by result)     : {report['fp_rate_by_result']:.1%}")
    print(f"FP rate (by snippet)    : {report['fp_rate_by_snippet']:.1%}")
    print("\nbuckets:")
    for k, v in report["buckets"].items():
        print(f"  {v:>3}  {k}")
    print("\nblocked:", json.dumps(report["blocked"], indent=2))
    print("\ndecay as reported          :", report["decay_as_reported"])
    print("decay, FPs removed         :", report["decay_fp_removed"])
    print("decay, FPs + cascade fixed :", report["decay_fp_removed_and_unblocked"])
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
