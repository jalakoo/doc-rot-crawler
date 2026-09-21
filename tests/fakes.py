"""Deterministic stand-ins for the network-bound pipeline.

`make_outcome` builds a real report through `build_report` from synthetic
pages, snippets and sandbox results, so tests exercise the same report shape
the dashboard renders without Daytona, OpenRouter or a docs site.
"""
from __future__ import annotations

import json
import time

from docrot.models import Extraction, Page, Release, Result, Snippet, Target
from docrot.report import build_report
from docrot.scan import ScanInputs, ScanOutcome

VERSIONS = [Release(label="1.0.0", released_at="2026-03-01"),
            Release(label="1.1.0", released_at="2026-06-01"),
            Release(label="2.0.0", released_at="2026-09-01")]


def _name(repo: str) -> str:
    return repo.rstrip("/").split("/")[-1].removesuffix(".git")


def make_targets(repos: list[str]) -> list[Target]:
    repos = repos or ["https://github.com/acme/acme"]
    return [Target(package=_name(r), import_name=_name(r).replace("-", "_"),
                   repo_url=r, branch="main", commit=f"abc{i}def0123",
                   releases=list(VERSIONS), versions=list(VERSIONS))
            for i, r in enumerate(repos)]


def make_outcome(docs: list[str], repos: list[str], failing: bool = True) -> ScanOutcome:
    targets = make_targets(repos)
    pages: list[Page] = []
    snippets: list[Snippet] = []
    results: list[Result] = []
    labels = [v.label for v in VERSIONS]

    for d_i, docs_url in enumerate(docs):
        for n, (title, path) in enumerate([("Quickstart", "/docs/guide/quickstart"),
                                           ("Install", "/docs/guide/install"),
                                           ("About", "/docs/about/overview")]):
            url = docs_url.rstrip("/") + path
            pin = ["0.9.0"] if title == "Install" and d_i == 0 else []
            pages.append(Page(url=url, path=path, title=title, source="site",
                              origin=docs_url, pinned_versions=pin))
            if title == "About":
                continue                      # prose only
            t = targets[(d_i + n) % len(targets)]
            sid = f"s{d_i}{n}"
            snippets.append(Snippet(id=sid, page=url, lang="python", tier="structural",
                                    code=f"import {t.import_name}\n{t.import_name}.run()",
                                    symbols=[f"{t.import_name}.run"], package=t.package,
                                    line=10 + n * 7))
            for i, v in enumerate(labels):
                broken = failing and title == "Quickstart" and i > 0
                stderr = json.dumps({"probe": {}, "failed": [
                    f"{t.import_name}.run: AttributeError: module '{t.import_name}' "
                    "has no attribute 'run'"]}) if broken else ""
                results.append(Result(id=sid, page=url, version=v, package=t.package,
                                      status="fail" if broken else "pass", stderr=stderr))

    for t in targets:
        url = f"repo://{t.package}/README.md" if len(targets) > 1 else "repo://README.md"
        pages.append(Page(url=url, path="README.md", title="README.md", source="repo",
                          origin=t.repo_url))

    ex = Extraction(package=targets[0].package, import_name=targets[0].import_name,
                    packages=targets, pages=pages, snippets=snippets)
    report = build_report(ex, results, targets, docs, repos, "llms.txt", 1.5)
    return ScanOutcome(report=report, extraction=ex, results=results)


def fake_runner(delay: float = 0.0, fail: str = "", failing: bool = True):
    """A `scan.Runner`: logs every phase like the real pipeline, optionally
    slowly, then returns `make_outcome` - or raises `fail`."""
    from docrot.scan import ScanError
    from docrot.store import PHASES

    def run(inp: ScanInputs, cfg, log, phase) -> ScanOutcome:
        for i, name in enumerate(PHASES):
            phase(i)
            log(f"  {name} step")
            if delay:
                time.sleep(delay)
            if fail and name == "registry":
                raise ScanError(fail)
        outcome = make_outcome(inp.docs, inp.repos, failing=failing)
        log(f"› scan complete — {outcome.report['stats']['findings']} findings")
        return outcome
    return run
