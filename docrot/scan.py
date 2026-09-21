"""One scan, end to end: acquire, registry, extract, verify, graph, report.

`run_scan` is the pipeline. `execute_scan` wraps it for the store - it records
status, phase and log as the scan runs and saves the finished run - and is the
one code path behind both `docrot scan` and the dashboard's queue.

A scan may span several docs sites and several repos. Every repo that declares
a package becomes a verification target with its own releases; snippets are
attributed to the package they exercise (see `extract.pipeline.attribute`).
"""
from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import config as cfgmod
from .models import Extraction, Release, Result, Target
from .store import PHASES, ScanOptions, ScanRecord, ScanStatus, Store, now

Log = Callable[[str], None]


class ScanError(Exception):
    """A scan that cannot proceed for a reason the user can fix."""


@dataclass
class ScanInputs:
    docs: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    options: ScanOptions = field(default_factory=ScanOptions)

    @classmethod
    def of(cls, rec: ScanRecord) -> ScanInputs:
        return cls(list(rec.docs), list(rec.repos), list(rec.packages), rec.options)


@dataclass
class ScanOutcome:
    report: dict
    extraction: Extraction
    results: list[Result]


def style(msg: str) -> str:
    """Log line colour class, as the scan log panel renders it."""
    s = msg.strip()
    if s.startswith("!") or "fail" in msg.lower():
        return "t-hit"
    if "clean" in msg or "complete" in msg:
        return "t-ok"
    if s.startswith("★") or "!" in msg:
        return "t-gold"
    return "t-sys"


def _source_spec(repo: str, commit: str, clone: Path, code_root: Path) -> str:
    """A pip spec for the repo itself, for a package no registry carries.

    Only for an http(s) remote: the sandbox cannot see a local checkout. The
    subdirectory is where the package metadata actually lives, which in a
    monorepo is not the repo root.
    """
    if not repo.startswith("http") or not commit or commit in ("unknown", "local"):
        return ""
    spec = f"git+{repo.removesuffix('.git')}@{commit}"
    try:
        inside = code_root.resolve().relative_to(clone.resolve()).as_posix()
    except ValueError:
        return spec
    return spec if inside in ("", ".") else f"{spec}#subdirectory={inside}"


def _short(url: str) -> str:
    p = urlparse(url)
    return (p.netloc + p.path).rstrip("/") if p.netloc else url


async def run_scan(inp: ScanInputs, cfg: cfgmod.Config, log: Log = print,
                   phase: Callable[[int], None] = lambda i: None) -> ScanOutcome:
    from .acquire import acquire_repo, acquire_site, pick_versions, releases
    from .acquire.http import Fetcher
    from .acquire.repo import CloneError, clone, find_package, repo_name, symbols_at_head
    from .extract import extract
    from .graph import GraphStore
    from .report import build_report
    from .verify import run_matrix

    opts = inp.options
    if opts.extraction_models:
        # a per-scan choice wins over EXTRACTION_MODELS
        cfg.extraction_models = [m.strip() for m in opts.extraction_models if m.strip()]
    started = time.time()
    if cfg.can_llm:
        # EXTRACTION_MODELS drops empty entries, so `,vendor/model` silently
        # discards whatever preceded the comma. Say out loud what resolved.
        log(f"  extraction models: {', '.join(cfg.extraction_models)}")

    # ------------------------------------------------------------ acquire
    phase(0)
    fetcher = Fetcher(cfg.user_agent, use_cache=not opts.no_cache)
    pages, strategies = [], []
    try:
        for url in inp.docs:
            site_pages, how = await acquire_site(url, fetcher, opts.max_pages or cfg.max_pages, log)
            where = f" · {_short(url)}" if len(inp.docs) > 1 else ""
            log(f"  {len(site_pages)} pages via {how}{where}")
            pages.extend(site_pages)
            strategies.append(how)
    finally:
        await fetcher.aclose()

    targets: list[Target] = []
    head: dict[str, set[str]] = {}
    multi = len(inp.repos) > 1
    for i, repo in enumerate(inp.repos):
        try:
            path, commit, branch = await asyncio.to_thread(clone, repo, log, cfg.clone_timeout)
        except CloneError as e:
            raise ScanError(f"couldn't fetch {repo}: {e}") from e
        dist, imp, eco, code_root = find_package(path, hint=repo_name(repo))
        dist = (inp.packages[i] if i < len(inp.packages) else "") or dist
        pages.extend(acquire_repo(path, log, namespace=repo_name(repo) if multi else "",
                                  origin=repo))
        where = f" · {repo_name(repo)}" if multi else ""
        if not dist:
            log(f"  ! no package metadata in {repo_name(repo)} - its docs are read, "
                "nothing is installed from it")
            continue
        imp = imp or dist.replace("-", "_")
        syms = symbols_at_head(code_root, imp)
        log(f"  {len(syms)} public symbols at HEAD ({commit}){where}")
        known = next((t for t in targets if t.package == dist), None)
        if known:
            head[dist] |= syms
            continue
        targets.append(Target(package=dist, import_name=imp, ecosystem=eco,
                              repo_url=repo, branch=branch, commit=commit,
                              install_spec=_source_spec(repo, commit, path, code_root)))
        head[dist] = syms
    for name in inp.packages[len(inp.repos):]:
        if name and not any(t.package == name for t in targets):
            targets.append(Target(package=name, import_name=name.replace("-", "_"),
                                  ecosystem=opts.ecosystem))

    if not targets:
        raise ScanError("no package name - pass --package or a repo with package metadata")
    if not pages:
        raise ScanError("no pages acquired")

    # ----------------------------------------------------------- registry
    phase(1)
    mentioned = sorted({p for pg in pages for p in pg.pinned_versions})
    for t in targets:
        t.releases = await asyncio.to_thread(releases, t.package, t.ecosystem)
        if not t.releases:
            if t.install_spec:
                log(f"  ! {t.package} has no releases on {t.ecosystem} - installing the repo "
                    f"itself at {t.commit}")
            else:
                log(f"  ! {t.package} has no releases on {t.ecosystem}, and no repo to install "
                    "from - nothing can be verified")
        t.versions = pick_versions(t.releases, opts.versions, mentioned) or [Release(label="HEAD")]
        who = f" · {t.package}" if len(targets) > 1 else ""
        log(f"  {len(t.releases)} releases · testing {', '.join(t.labels)}{who}")
    if mentioned:
        log(f"  ! docs mention versions: {', '.join(mentioned[:6])}")

    # --------------------------------------------------------- extraction
    phase(2)
    all_head = set().union(*head.values()) if head else set()
    ex = await asyncio.to_thread(
        extract, pages, targets, cfg, log,
        assume_credentialed=opts.assume_credentialed, head_symbols=all_head)

    # ------------------------------------------------------- verification
    phase(3)
    results: list[Result] = []
    for t in targets:
        snips = [s for s in ex.snippets if s.package == t.package]
        if not snips:
            continue
        if t.ecosystem != "pypi":
            log(f"  ! {t.package}: {t.ecosystem} packages are not installable in the "
                "sandbox yet - marked unverified")
            got = [Result(id=s.id, page=s.page, version=v, status="unverified",
                          tier=s.tier) for s in snips for v in t.labels]
        else:
            if len(targets) > 1:
                log(f"  {t.package}: {len(snips)} snippets")
            got = await run_matrix(snips, t.package, t.import_name, t.labels, cfg, log,
                                   install_spec=t.install_spec)
        for r in got:
            r.package = t.package
        results.extend(got)
    fails = sum(1 for r in results if r.status == "fail")
    log(f"  {len(results)} results · {fails} failing")

    # -------------------------------------------------------------- graph
    phase(4)
    store = GraphStore(cfg, log)
    try:
        await asyncio.to_thread(store.load, ex, results, targets, head)
    finally:
        store.close()

    # ------------------------------------------------------------- report
    phase(5)
    strategy = " + ".join(dict.fromkeys(s for s in strategies if s)) or "none"
    report = build_report(ex, results, targets, inp.docs, inp.repos, strategy,
                          time.time() - started)
    log(f"› scan complete — {report['stats']['findings']} findings on "
        f"{report['stats']['drifted']} pages, {report['elapsed']}s")
    return ScanOutcome(report=report, extraction=ex, results=results)


Runner = Callable[[ScanInputs, cfgmod.Config, Log, Callable[[int], None]], ScanOutcome]


def _default_runner(inp, cfg, log, phase) -> ScanOutcome:
    return asyncio.run(run_scan(inp, cfg, log, phase))


def execute_scan(store: Store, scan_id: str, echo: Log | None = None,
                 runner: Runner = _default_runner,
                 cfg: cfgmod.Config | None = None) -> ScanStatus:
    """Run one scan synchronously, recording its progress in the store.

    Blocking - the CLI calls it directly, the dashboard from its worker thread.
    Never raises for a failed scan; the returned status says what happened.
    """
    rec = store.get(scan_id)
    if rec is None:
        raise KeyError(scan_id)
    prev = store.status(scan_id)
    st = ScanStatus(state="running", phase=0, queued_at=prev.queued_at, started_at=now())
    store.set_status(scan_id, st)

    def log(msg: str = "") -> None:
        st.log.append([style(msg), msg])
        if echo:
            echo(msg)
        store.set_status(scan_id, st)

    def phase(i: int) -> None:
        st.phase = max(0, min(i, len(PHASES) - 1))
        if echo:
            echo(f"\n› {PHASES[st.phase]}")
        store.set_status(scan_id, st)

    try:
        outcome = runner(ScanInputs.of(rec), cfg or cfgmod.load(), log, phase)
    except ScanError as e:
        st.error = str(e)
        log(f"! {e}")
    except Exception as e:           # a crash must not leave the scan `running`
        st.error = f"{type(e).__name__}: {e}"[:500]
        log(f"! scan crashed - {st.error}")
        for line in traceback.format_exc().strip().splitlines()[-4:]:
            st.log.append(["t-sys", "  " + line])
    else:
        outcome.report["log"] = st.log
        store.save_run(scan_id, outcome.report,
                       [r.model_dump() for r in outcome.results],
                       outcome.extraction.model_dump_json(indent=1))
        st.state, st.finished_at = "done", now()
        store.set_status(scan_id, st)
        return st

    st.state, st.finished_at = "failed", now()
    store.set_status(scan_id, st)
    return st
