"""Turn extraction + results into the finding list the UI renders.

A finding is always a two-sided comparison: what the docs say, and what the
code does. That framing is the whole point - a status alone ("this page is
broken") carries one bit; the diff carries the fix.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from urllib.parse import urlparse

from ..models import Extraction, Finding, Page, Result, Target

DRIFT_KINDS = ("stale_pin", "signature", "missing_symbol", "runtime")

PIN_RE = re.compile(r'(.*?)(==\s*[0-9][\w.\-]*)(.*)', re.S)
KWARG_ERR = re.compile(r"has no parameter '(\w+)' -- signature is (\(.*?\))")
DOC_PARAM = re.compile(r"(\w+)\(\): docs document parameter\(s\) ([^-]+) that do not exist -- actual signature is (\(.*?\))")
DOC_REQ   = re.compile(r"(\w+)\(\): required parameter\(s\) ([^-]+) are missing from the docs -- actual signature is (\(.*?\))")
DOC_GONE  = re.compile(r"documented (\w+)\(\) does not exist in this release")
DOC_MOVED = re.compile(r"documented (\w+)\(\) is not importable as shown -- "
                       r"it lives at ([\w.]+)")
ATTR_ERR = re.compile(r"([\w.]+): (AttributeError|ModuleNotFoundError|ImportError): (.*)")


def _days(a: str, b: str) -> int:
    try:
        d1 = dt.date.fromisoformat(a[:10])
        d2 = dt.date.fromisoformat(b[:10])
        return abs((d2 - d1).days)
    except (ValueError, TypeError):
        return 0


def _norm(v: str) -> str:
    return v.replace("-", "").replace("_", "").replace(".", "").lower()


def _code_url(repo_url: str, branch: str, path: str, line: int = 0) -> str:
    if not repo_url:
        return ""
    base = repo_url.rstrip("/").removesuffix(".git")
    anchor = f"#L{line}" if line else ""
    return f"{base}/blob/{branch}/{path}{anchor}"


def _probe_json(stderr: str) -> dict:
    m = re.search(r"\{[\s\S]*\"probe\"[\s\S]*\}", stderr or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def build_report(ex: Extraction, results: list[Result], targets: list[Target],
                 docs_urls: list[str], repo_urls: list[str], strategy: str,
                 elapsed: float) -> dict:
    """The report for one run. `targets[0]` is the primary package; every other
    target is a further repo in the same scan, with its own releases."""
    if not targets:
        raise ValueError("build_report needs at least one target")
    by_pkg = {t.package: t for t in targets}
    by_page: dict[str, list[Finding]] = {}
    home: dict[str, Target] = {}

    res_by_snip: dict[str, dict[str, Result]] = {}
    for r in results:
        res_by_snip.setdefault(r.id, {})[r.version] = r
    snips_by_page: dict[str, list] = {}
    for s in ex.snippets:
        snips_by_page.setdefault(s.page, []).append(s)

    for page in ex.pages:
        found: list[Finding] = []
        page_target = _page_target(page, snips_by_page.get(page.url, []), targets)
        home[page.url] = page_target

        # ---- 1. stale version pin (needs no execution at all) ----
        for pin in page.pinned_versions:
            # the package that actually published this version, if any does
            t = next((t for t in targets
                      if any(_norm(r.label) == _norm(pin) for r in t.releases)),
                     page_target)
            latest, releases, repo_url, branch = t.latest, t.releases, t.repo_url, t.branch
            if _norm(pin) == _norm(latest.label):
                continue
            match = next((r for r in releases if _norm(r.label) == _norm(pin)), None)
            gap = _days(match.released_at, latest.released_at) if match else 0
            src = next((s for s in snips_by_page.get(page.url, [])
                        if pin in s.code), None)
            doc_line = src.line if src else 0
            base = (src.code if src else f"install {t.package}=={pin}").strip()
            m = PIN_RE.match(base)
            head, tail = (m.group(1), m.group(3)) if m else (base, "")
            found.append(Finding(
                kind="stale_pin", label="version pin", line=doc_line,
                snippet=src.id if src else "",
                title="STALE VERSION PIN",
                desc=(f"This page pins <b>{pin}</b>"
                      + (f", released {match.released_at}" if match else "")
                      + f". The current release is <b>{latest.label}</b>"
                      + (f" ({latest.released_at})" if latest.released_at else "")
                      + (f" — <b>{gap} days</b> later. " if gap else ". ")
                      + "Anyone following this page installs a version the "
                        "maintainers have moved on from."),
                page=page.url,
                doc_where=f"{page.path} · line {doc_line}" if doc_line else page.path,
                doc_url=page.url, doc_stamp_label="docs pin",
                doc_stamp=f"{pin}" + (f" · {match.released_at}" if match else ""),
                code_where="pyproject.toml / registry",
                code_url=_code_url(repo_url, branch, "pyproject.toml"),
                code_stamp_label="current release",
                code_stamp=f"{latest.label}" +
                           (f" · {latest.released_at}" if latest.released_at else ""),
                doc_parts=[[head, "same"], [f"=={pin}", "del"], [tail, "same"]],
                code_parts=[[head, "same"], [f"=={latest.label}", "ins"], [tail, "same"]],
                damage="-1 RELEASE", days_behind=gap,
                evidence=_pin_evidence(t.package, releases, pin, latest),
                chips=[["tier-struct", "structural"], ["st-drift", "stale pin"],
                       ["", "no creds needed"]],
                sprite="mimic",
            ))

        # ---- 2/3/4. whatever the sandbox actually found ----
        for s in snips_by_page.get(page.url, []):
            t = by_pkg.get(s.package) or page_target
            labels, latest, repo_url, branch = t.labels, t.latest, t.repo_url, t.branch
            per_version = res_by_snip.get(s.id, {})
            failing = [v for v in labels
                       if per_version.get(v) and per_version[v].status == "fail"]
            blocked = [v for v in labels
                       if per_version.get(v) and per_version[v].status == "blocked"]

            if failing:
                r = per_version[failing[0]]
                finding = _from_failure(s, r, page, failing, labels,
                                        repo_url, branch, latest, t)
                finding.snippet, finding.line = s.id, s.line
                found.append(finding)
            elif blocked and not failing:
                r = per_version[blocked[0]]
                found.append(Finding(
                    kind="blocked", label="blocked upstream", snippet=s.id, line=s.line,
                    title="NEVER RAN — BLOCKED UPSTREAM",
                    desc=("Nothing here has been shown to be wrong. An earlier "
                          f"step in the chain (<b>{r.blocked_by}</b>) failed, so "
                          "this snippet never executed. Blocked is tracked apart "
                          "from failed — one bad line must not condemn a page."),
                    page=page.url, doc_where=f"{page.path} · {s.id}",
                    doc_url=page.url, doc_stamp_label="snippet",
                    doc_stamp="not evaluated",
                    code_where=f"blocked by {r.blocked_by}",
                    code_url=_code_url(repo_url, branch, "") or page.url,
                    code_stamp_label="root cause", code_stamp=str(r.blocked_by),
                    doc_parts=[[s.code[:400], "same"]],
                    code_parts=[["Chain halted upstream.\nThis snippet never ran, "
                                 "so nothing here\nis known to be wrong.", "void"]],
                    damage="NOT COUNTED", days_behind=0,
                    evidence=(r.stderr or "")[:900] or
                             f"blocked by {r.blocked_by}\nexcluded from the drift score",
                    chips=[["tier-exec", s.tier], ["st-block", "blocked"],
                           ["", f"root cause: {r.blocked_by}"]],
                    sprite="wraith",
                ))

        if not found:
            t = page_target
            # "no findings" is only clean if something actually ran. When an
            # install fails, or every snippet is unverifiable, the page has not
            # been checked - saying "no drift" there is the quiet lie this tool
            # exists to avoid.
            checked = any(r.status in ("pass", "fail")
                          for s in snips_by_page.get(page.url, [])
                          for r in res_by_snip.get(s.id, {}).values())
            found.append(_clean(page, t, snips_by_page, t.latest, t.repo_url, t.branch,
                                checked=checked))
        by_page[page.url] = found

    return _assemble(ex, by_page, results, targets, home, docs_urls, repo_urls,
                     strategy, elapsed)


def _page_target(page: Page, snips: list, targets: list[Target]) -> Target:
    """The package a page is about: its repo's package for repo docs, else the
    package most of its snippets exercise, else the primary."""
    if page.source == "repo":
        for t in targets:
            if t.repo_url and t.repo_url == page.origin:
                return t
    by_pkg = {t.package: t for t in targets}
    counts = Counter(s.package for s in snips if s.package in by_pkg)
    if counts:
        return by_pkg[counts.most_common(1)[0][0]]
    return targets[0]


def _pin_evidence(pkg, releases, pin, latest) -> str:
    lines = [f"registry  {pkg}"]
    for r in releases[-8:]:
        tag = ""
        if _norm(r.label) == _norm(pin):
            tag = "   <- docs pin"
        elif r.label == latest.label:
            tag = "   <- current"
        lines.append(f"  {r.label:<12} {r.released_at}{tag}")
    return "\n".join(lines)


def _from_failure(s, r, page, failing, labels, repo_url, branch, latest, ex: Target) -> Finding:
    probe = _probe_json(r.stderr)
    fails = probe.get("failed") or []
    blob = "\n".join(fails) if fails else (r.stderr or "")

    dm = DOC_PARAM.search(blob) or DOC_REQ.search(blob)
    if dm:
        fn, params, sig = dm.group(1), dm.group(2).strip(), dm.group(3)
        surplus = bool(DOC_PARAM.search(blob))
        decl = next((d for s2 in [s] for d in s2.declares if d["name"] == fn), None)
        doc_sig = (f"def {fn}(" + ", ".join(decl["params"]) + ")"
                   + (f" -> {decl['returns']}" if decl and decl["returns"] else "")
                   ) if decl else f"{fn}(...)"
        return Finding(
            kind="signature", label="signature drift",
            title="DOCUMENTED SIGNATURE IS WRONG",
            desc=(f"This page prints the signature of <b>{fn}</b>. The installed "
                  + (f"package has no parameter <b>{params}</b>."
                     if surplus else
                     f"package requires <b>{params}</b>, which the docs omit.")
                  + " Both sides are explicit here, so the difference is exact - "
                    "a reader copying this signature writes a call that cannot work."),
            page=page.url, doc_where=f"{page.path} · {s.id}", doc_url=page.url,
            doc_stamp_label="docs declare", doc_stamp=f"{len(decl['params']) if decl else 0} params",
            code_where=f"introspected · {failing[0]}",
            code_url=_code_url(repo_url, branch, ""),
            code_stamp_label="code is at",
            code_stamp=f"{latest.label} · {latest.released_at}",
            doc_parts=[[doc_sig, "del"]],
            code_parts=[[f"def {fn}{sig}", "ins"]],
            damage="-1 PARAM" if surplus else "+1 REQUIRED", days_behind=0,
            evidence=blob[:900],
            chips=[["tier-struct", "structural"], ["st-fail", "drift"],
                   ["", " + ".join(failing)]],
            sprite="mimic",
        )

    mm = DOC_MOVED.search(blob)
    if mm:
        fn, real = mm.group(1), mm.group(2)
        return Finding(
            kind="signature", label="wrong import path",
            title="DOCUMENTED AT THE WRONG IMPORT PATH",
            desc=(f"<b>{fn}</b> still exists, but not where this page says it "
                  f"does. It now lives at <b>{real}</b>. A reader copying the "
                  "import gets a bare ImportError that names no alternative."),
            page=page.url, doc_where=f"{page.path} · {s.id}", doc_url=page.url,
            doc_stamp_label="docs import from", doc_stamp=ex.import_name,
            code_where=f"actual location · {failing[0]}",
            code_url=_code_url(repo_url, branch, f"{ex.import_name}/__init__.py"),
            code_stamp_label="code is at",
            code_stamp=f"{latest.label} · {latest.released_at}",
            doc_parts=[[f"from {ex.import_name} import ", "same"], [fn, "del"]],
            code_parts=[["from ", "same"], [real.rsplit(".", 1)[0], "ins"],
                        [f" import {fn}", "same"]],
            damage="MOVED", days_behind=0, evidence=blob[:900],
            chips=[["tier-struct", "structural"], ["st-fail", "drift"],
                   ["", "still exists, moved"]],
            sprite="mimic",
        )

    gm = DOC_GONE.search(blob)
    if gm:
        fn = gm.group(1)
        return Finding(
            kind="missing_symbol", label="documented, absent",
            title="DOCUMENTS A FUNCTION THAT DOES NOT EXIST",
            desc=(f"This page documents <b>{fn}()</b> in full - signature, "
                  "parameters, return type - but the installed package has no "
                  f"such name in {', '.join(failing)}."),
            page=page.url, doc_where=f"{page.path} · {s.id}", doc_url=page.url,
            doc_stamp_label="docs declare", doc_stamp=f"{fn}()",
            code_where=f"{ex.import_name} · exports",
            code_url=_code_url(repo_url, branch, f"{ex.import_name}/__init__.py"),
            code_stamp_label="code is at", code_stamp="name absent",
            doc_parts=[[s.code[:300], "del"]],
            code_parts=[[f"AttributeError: no attribute '{fn}'", "void"]],
            damage="-1 SYMBOL", days_behind=0, evidence=blob[:900],
            chips=[["tier-struct", "structural"], ["st-fail", "drift"],
                   ["", f"absent in {len(failing)}/{len(labels)}"]],
            sprite="wraith",
        )

    km = KWARG_ERR.search(blob)
    if km:
        bad, sig = km.group(1), km.group(2)
        fn = next((f.split("()")[0] for f in fails if "has no parameter" in f), "")
        head, tail = _split_on(s.code, bad)
        return Finding(
            kind="signature", label="kwarg renamed",
            title="RENAMED KEYWORD ARGUMENT",
            desc=(f"The symbol still resolves, so the import succeeds and nothing "
                  f"looks wrong. But <b>{bad}</b> is not a parameter of "
                  f"<b>{fn or 'the function'}</b> in {', '.join(failing)}. It fails "
                  "at call time, not import time, which is why it goes unnoticed."),
            page=page.url, doc_where=f"{page.path} · {s.id}", doc_url=page.url,
            doc_stamp_label="docs reflect", doc_stamp="an earlier release",
            code_where=f"introspected signature · {failing[0]}",
            code_url=_code_url(repo_url, branch, ""),
            code_stamp_label="code is at",
            code_stamp=f"{latest.label} · {latest.released_at}",
            doc_parts=[[head, "same"], [bad, "del"], [tail, "same"]],
            code_parts=[[f"{fn}{sig}", "ins"]],
            damage="-1 KWARG", days_behind=0,
            evidence=blob[:900],
            chips=[["tier-struct", s.tier], ["st-fail", "drift"],
                   ["", " + ".join(failing)]],
            sprite="slime",
        )

    am = ATTR_ERR.search(blob)
    if am:
        sym, kind, msg = am.group(1), am.group(2), am.group(3)
        everywhere = len(failing) == len(labels)
        return Finding(
            kind="missing_symbol",
            label="never shipped" if everywhere else "symbol gone",
            title=("DOCUMENTS A SYMBOL THAT NEVER SHIPPED" if everywhere
                   else "SYMBOL NO LONGER EXISTS"),
            desc=(f"<b>{sym}</b> could not be resolved in "
                  + ("<b>any version tested</b>. Documentation that ran ahead of "
                     "the code and was never walked back — there is no release a "
                     "reader could install to make this work."
                     if everywhere else
                     f"{', '.join(failing)}. It resolved in earlier releases, so "
                     "this page documents code that has since been removed.")),
            page=page.url, doc_where=f"{page.path} · {s.id}", doc_url=page.url,
            doc_stamp_label="docs reflect",
            doc_stamp="unreleased work" if everywhere else "an earlier release",
            code_where=f"{ex.import_name} · exports",
            code_url=_code_url(repo_url, branch,
                               f"{ex.import_name}/__init__.py"),
            code_stamp_label="code is at", code_stamp="symbol absent",
            doc_parts=[[s.code[:300], "same"]],
            code_parts=[[f"{kind}: {msg}"[:220], "void"]],
            damage="-1 SYMBOL", days_behind=0,
            evidence=blob[:900],
            chips=[["tier-struct", s.tier], ["st-fail", "drift"],
                   ["", f"absent in {len(failing)}/{len(labels)}"]],
            sprite="wraith",
        )

    return Finding(
        kind="runtime", label="snippet fails",
        title="SNIPPET FAILS WHEN RUN",
        desc=(f"This snippet exits non-zero on {', '.join(failing)}. The captured "
              "output is below — the docs describe behaviour the package no "
              "longer has."),
        page=page.url, doc_where=f"{page.path} · {s.id}", doc_url=page.url,
        doc_stamp_label="docs reflect", doc_stamp="an earlier release",
        code_where=f"executed in sandbox · {failing[0]}",
        code_url=_code_url(repo_url, branch, ""),
        code_stamp_label="code is at",
        code_stamp=f"{latest.label} · {latest.released_at}",
        doc_parts=[[s.code[:400], "same"]],
        code_parts=[[(blob or "non-zero exit, no output captured")[:300], "void"]],
        damage="-1 RUN", days_behind=0,
        evidence=blob[:900] or "exit code != 0",
        chips=[["tier-exec", s.tier], ["st-fail", "drift"],
               ["", " + ".join(failing)]],
        sprite="slime",
    )


def _split_on(code: str, token: str) -> tuple[str, str]:
    i = code.find(token)
    if i < 0:
        return code[:200], ""
    return code[max(0, i - 160):i], code[i + len(token):i + len(token) + 160]


def _clean(page: Page, ex: Target, snips_by_page, latest, repo_url, branch,
           checked: bool = True) -> Finding:
    snips = snips_by_page.get(page.url, [])
    if not snips:
        return Finding(
            kind="prose", label="nothing to check",
            title="PROSE ONLY — NOTHING TO CHECK",
            desc=("No code fences and no symbol references on this page. Recorded "
                  "as <b>unverifiable</b> and left out of the drift score, rather "
                  "than quietly counted as clean."),
            page=page.url, doc_where=page.path, doc_url=page.url,
            doc_stamp_label="page", doc_stamp="no code fences",
            code_where="nothing to compare against",
            code_url=_code_url(repo_url, branch, ""),
            code_stamp_label="code", code_stamp="—",
            doc_parts=[["0 code fences parsed.\n0 claims bound to symbols.", "void"]],
            code_parts=[["No comparison possible.\nRecorded as unverifiable.", "void"]],
            damage="NO TARGET", days_behind=0,
            evidence="0 fences · 0 bound claims\nstatus: unverifiable\n"
                     "excluded from the drift score",
            chips=[["tier-unv", "unverifiable"], ["", "excluded from score"]],
            sprite="none",
        )
    s = snips[0]
    if not checked:
        tiers = sorted({x.tier for x in snips})
        return Finding(
            kind="unverified", label="not verified",
            title="NOT VERIFIED — NOTHING RAN HERE",
            desc=(f"None of the {len(snips)} snippet(s) on this page were checked against "
                  f"<b>{ex.package}</b>: they are <b>{', '.join(tiers)}</b>, or the install they "
                  "needed failed. Recorded as unverified rather than counted as clean."),
            page=page.url, doc_where=f"{page.path} · {len(snips)} snippets",
            doc_url=page.url, doc_stamp_label="page", doc_stamp="not checked",
            code_where=f"{ex.package} · not installed",
            code_url=_code_url(repo_url, branch, ""),
            code_stamp_label="code", code_stamp="—",
            doc_parts=[[s.code[:300], "same"]],
            code_parts=[["Nothing was run against this page.\nNo comparison was made.", "void"]],
            damage="NOT COUNTED", days_behind=0,
            evidence=f"{len(snips)} snippet(s), tiers: {', '.join(tiers)}\n"
                     "no pass or fail result for any of them\nexcluded from the drift score",
            chips=[["tier-unv", "unverified"], ["", "excluded from score"]],
            sprite="none",
        )
    return Finding(
        kind="clean", label="no drift",
        title="NO DRIFT FOUND",
        desc=(f"All {len(snips)} snippet(s) on this page agree with the package on "
              "every version tested. Nothing to fix here."),
        page=page.url, doc_where=f"{page.path} · {len(snips)} snippets",
        doc_url=page.url, doc_stamp_label="docs reflect",
        doc_stamp=f"{latest.label} · current",
        code_where=f"{ex.import_name} · introspected",
        code_url=_code_url(repo_url, branch, f"{ex.import_name}/__init__.py"),
        code_stamp_label="code is at",
        code_stamp=f"{latest.label} · {latest.released_at}",
        doc_parts=[[s.code[:300], "same"]],
        code_parts=[["all referenced symbols resolved\nsignatures accept the "
                     "documented arguments", "same"]],
        damage="NO DAMAGE", days_behind=0,
        evidence="probe: all symbols resolved\ndrift: none",
        chips=[["tier-struct", s.tier], ["st-pass", "no drift"], ["", "clean"]],
        sprite="none",
    )


def _assemble(ex, by_page, results, targets, home, docs_urls, repo_urls,
              strategy, elapsed) -> dict:
    primary = targets[0]
    multi_docs = len({p.origin for p in ex.pages if p.source != "repo"}) > 1
    multi_repos = len({p.origin for p in ex.pages if p.source == "repo"}) > 1

    sections: dict[str, list] = {}
    for i, page in enumerate(ex.pages, 1):
        finds = by_page.get(page.url, [])
        drifted = [f for f in finds if f.kind in DRIFT_KINDS]
        blocked = [f for f in finds if f.kind == "blocked"]
        st = ("drift" if any(f.kind == "stale_pin" for f in drifted)
              else "fail" if drifted
              else "block" if blocked
              else "unlit" if finds and finds[0].kind in ("prose", "unverified")
              else "pass")
        sections.setdefault(_section_of(page, multi_docs, multi_repos), []).append({
            "id": f"p{i}", "tag": str(i), "name": page.title or page.path,
            "path": page.path, "url": page.url, "st": st,
            "sev": min(3, len(drifted)) if drifted else (1 if blocked else 0),
            "findings": [f.model_dump() for f in finds],
        })

    timeline: list[dict] = []
    for n, t in enumerate(targets):
        timeline.extend(dict(row, pkg=n)
                        for row in _timeline(ex, results, t, primary))
    impact = _impact(ex)

    total_findings = sum(
        len([f for f in by_page.get(p.url, []) if f.kind in DRIFT_KINDS])
        for p in ex.pages)
    drifted_pages = sum(
        1 for p in ex.pages
        if any(f.kind in DRIFT_KINDS for f in by_page.get(p.url, [])))
    worst = max((f.days_behind for finds in by_page.values() for f in finds),
                default=0)

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        # single-source fields name the first docs site and the primary package
        "docs_url": docs_urls[0] if docs_urls else "",
        "repo_url": primary.repo_url, "branch": primary.branch,
        "commit": primary.commit, "strategy": strategy,
        "package": primary.package, "import_name": primary.import_name,
        "docs_urls": list(docs_urls), "repo_urls": list(repo_urls),
        "sources": _sources(ex.pages, docs_urls, repo_urls),
        "packages": [{
            "package": t.package, "import_name": t.import_name,
            "ecosystem": t.ecosystem, "repo_url": t.repo_url,
            "branch": t.branch, "commit": t.commit,
            "versions": [v.model_dump() for v in t.versions],
        } for t in targets],
        "elapsed": round(elapsed, 1),
        "stats": {
            "pages": len(ex.pages), "drifted": drifted_pages,
            "findings": total_findings, "worst_gap": worst,
            "versions": sum(len(t.versions) for t in targets),
            "snippets": len(ex.snippets),
        },
        "versions": [v.model_dump() for v in primary.versions],
        "sections": [{"name": k, "pages": v} for k, v in sections.items()],
        "timeline": timeline,
        "impact": impact,
        "log": [],
    }


def _sources(pages: list[Page], docs_urls: list[str], repo_urls: list[str]) -> list[dict]:
    """Pages contributed by each docs site and repo, in the order given."""
    counts: Counter = Counter()
    for p in pages:
        origin = p.origin or ((docs_urls or [""])[0] if p.source != "repo"
                              else (repo_urls or [""])[0])
        counts[(p.source == "repo", origin)] += 1
    return ([{"kind": "docs", "url": u, "pages": counts[(False, u)]} for u in docs_urls]
            + [{"kind": "repo", "url": u, "pages": counts[(True, u)]} for u in repo_urls])


def _short(url: str) -> str:
    p = urlparse(url)
    if not p.netloc:
        return url.rstrip("/").split("/")[-1]
    return (p.netloc + p.path).rstrip("/")


def _section_of(page, multi_docs: bool = False, multi_repos: bool = False) -> str:
    if page.source == "repo":
        name = "Repository docs"
        if multi_repos and page.origin:
            name += " · " + page.origin.rstrip("/").split("/")[-1].removesuffix(".git")
        return name
    parts = [p for p in urlparse(page.url).path.split("/") if p]
    for skip in ("docs", "en", "latest"):
        if parts and parts[0] == skip:
            parts = parts[1:]
    name = parts[0].replace("-", " ").title() if len(parts) > 1 else "Overview"
    if multi_docs and page.origin:
        name += " · " + _short(page.origin)
    return name


def _timeline(ex, results, target: Target, primary: Target) -> list[dict]:
    """Per-page, per-version verdicts for one package's releases."""
    labels = target.labels
    by_page_ver: dict[str, dict[str, list[str]]] = {}
    for r in results:
        if (r.package or primary.package) != target.package:
            continue
        by_page_ver.setdefault(r.page, {}).setdefault(r.version, []).append(r.status)

    rows = []
    for page in ex.pages:
        per = by_page_ver.get(page.url)
        if not per:
            continue
        cells = []
        for v in labels:
            st = per.get(v, [])
            if not st:
                cells.append("na")
            elif "fail" in st:
                cells.append("fail")
            elif all(x == "blocked" for x in st):
                cells.append("na")
            elif "unverified" in st:
                cells.append("na")
            else:
                cells.append("pass")
        if set(cells) == {"na"}:
            continue

        pin = None
        for i, v in enumerate(labels):
            if any(_norm(p) == _norm(v) for p in page.pinned_versions):
                pin = i
        rows.append({
            "n": page.title or page.path, "p": page.path,
            "c": cells, "pin": pin, "v": _verdict(cells, labels),
            "ok": "fail" not in cells, "hold": "na" in cells and "fail" not in cells,
        })
    rows.sort(key=lambda r: (r["ok"], r["n"]))
    return rows[:14]


def _verdict(cells, labels) -> str:
    if "fail" not in cells:
        return "clean on all versions"
    if all(c == "fail" for c in cells):
        return "never true, any version"
    for i in range(1, len(cells)):
        if cells[i] == "fail" and cells[i - 1] == "pass":
            return f"died at {labels[i]}"
    clean = [labels[i] for i, c in enumerate(cells) if c == "pass"]
    return f"true for {', '.join(clean)} only" if clean else "broken"


def _impact(ex) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    page_id = {p.url: f"p{i}" for i, p in enumerate(ex.pages, 1)}
    for s in ex.snippets:
        for sym in s.symbols:
            if sym.startswith(("pip:", "env:")):
                continue
            short = sym.split(".")[-1]
            for key in {sym.lower(), short.lower()}:
                if len(key) < 4:
                    continue
                out.setdefault(key, set()).add(page_id.get(s.page, ""))
    for c in ex.claims:
        for sym in c.symbols:
            out.setdefault(sym.split(".")[-1].lower(), set()).add(page_id.get(c.page, ""))
    return {k: sorted(x for x in v if x) for k, v in out.items() if v}
