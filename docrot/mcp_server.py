"""docrot as an MCP server, so other AI agents can run scans and read findings.

    docrot mcp          # stdio; register it with an MCP client, see the README

The tools are the agent-facing twin of the CLI and the dashboard's API, over
the same `Store`: a scan started here shows up in `docrot scans` and the
dashboard, and a finding is the very record `docrot export` writes (see
report/export.py), so an agent never has to learn a second shape.

Traps this module is built around:

* **stdout is the protocol.** On stdio every byte of stdout must be a JSON-RPC
  frame. Nothing reachable from a tool prints (scan logging goes through
  `execute_scan`'s log callback into the store, and `echo` is left unset), and
  the SDK points fd 1 at stderr while serving, so a stray `print` from a
  library lands on stderr instead of corrupting a frame. Diagnostics go to
  stderr. tests/test_mcp.py launches the real command and checks this.
* **One scan at a time, across processes.** Every scan ends by sweeping all
  live sandboxes in the Daytona account (see worker.py), so a scan started
  here while the dashboard - or another agent's MCP server - is mid-scan would
  delete that scan's sandboxes. `ScanWorker` serialises scans inside one
  process; `_refuse_if_busy` extends the rule to the store, which is shared.
* **Agents pay per token.** Tools return small structured results and page
  through the big ones; the text block a client reads is compact JSON, not the
  SDK's indented default, because it duplicates the structured content.
* **Errors are for the model.** Anything an agent can fix (unknown id, bad
  URL) is raised as `ToolError`, whose text the SDK passes through verbatim.
  Anything else is a crash the SDK reports without detail.

Nothing here accepts or returns credentials or reads files the scan itself
would not: a "repo" may be a local path, exactly as on the CLI.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import threading
from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from . import config as cfgmod
from .acquire.detect import detect as detect_repo
from .report import export
from .report.diff import diff, exit_code, load_results
from .store import (
    PHASES,
    ScanOptions,
    ScanRecord,
    Store,
    align_packages,
    clean_sources,
    is_repo,
    now,
)
from .worker import AlreadyActive, ScanWorker

LOG_TAIL = 20           # log lines get_scan returns: enough to see what a scan is doing
MAX_DETECT = 10         # repos per detect_packages call: each may be a clone
MAX_LIMIT = 200         # findings per page, however much the caller asks for
KNOWN_IDS_SHOWN = 15    # ids listed in an "unknown scan" error
FINDING_KINDS = (*export.DRIFT_KINDS, "blocked")
CLEAN_KINDS = ("clean", "prose")        # only present when asked for by name

BlastRadius = Callable[[str], list[dict]]


def say(msg: str) -> None:
    """A diagnostic. Always stderr: stdout belongs to the protocol."""
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------- results

class ScanSummary(BaseModel):
    id: str
    name: str
    docs: list[str]
    repos: list[str]
    packages: list[str]
    state: str = Field(description="idle, queued, running, done or failed")
    runs: int
    last_run: str | None = Field(None, description="newest run id, or null if never finished")
    last_run_at: str | None = None
    pages: int | None = None
    drifted_pages: int | None = None
    findings: int | None = None


class ScanList(BaseModel):
    scans: list[ScanSummary]


class RunBrief(BaseModel):
    id: str
    at: str
    elapsed_s: float
    pages: int | None = None
    drifted_pages: int | None = None
    findings: int | None = None


class ScanStatusView(BaseModel):
    id: str
    name: str
    state: str = Field(description="idle, queued, running, done or failed")
    phase: str = Field(description="while running: acquiring, registry, extracting, verifying, graph or report")
    error: str = ""
    queued_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    log_tail: list[str] = Field(description=f"the last {LOG_TAIL} log lines")
    log_lines: int = Field(description="total log lines so far")
    runs: list[RunBrief] = Field(description="finished runs, oldest first")


class Coverage(BaseModel):
    tiers: dict[str, int] = Field(description="snippets by how far they were checked: execute, structural, unverifiable")
    unverifiable: dict[str, int] = Field(description="why snippets were not checked: reason code -> count, largest first")
    no_checker: dict[str, int] = Field(description="fence languages docrot has no checker for -> count")


class FindingsPage(BaseModel):
    scan_id: str
    run_id: str
    coverage: Coverage | None = Field(None, description=(
        "what the run did NOT check, and why. Zero findings with a large `unverifiable` count is not "
        "'clean'. null for runs from before this was recorded"))
    total: int = Field(description="findings matching the filters, across all pages")
    offset: int
    returned: int
    next_offset: int | None = Field(None, description="pass as offset for the next page; null at the end")
    by_kind: dict[str, int] = Field(description="matching findings per kind")
    findings: list[dict[str, Any]] = Field(description="records in the docrot/1 JSON Lines shape")


class DiffEntry(BaseModel):
    page: str
    id: str = Field(description="snippet id")
    version: str
    was: str | None
    now: str | None
    detail: str = ""


class ScanDiff(BaseModel):
    scan_id: str
    compared: bool = Field(description="false when there is nothing to compare yet; see note")
    note: str = ""
    from_run: str | None = None
    to_run: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    worse: bool = Field(False, description="true when `docrot diff` would exit 1: regressions or new failures")
    regressions: list[DiffEntry] = Field(default_factory=list, description="passed before, fails now")
    appeared: list[DiffEntry] = Field(default_factory=list, description="new snippets that fail")
    fixes: list[DiffEntry] = Field(default_factory=list)
    disappeared: list[DiffEntry] = Field(default_factory=list, description="failing snippets the docs dropped")
    truncated: bool = False


class ImpactPage(BaseModel):
    name: str
    url: str = ""
    touchpoints: int | None = None


class RenameImpact(BaseModel):
    scan_id: str
    symbol: str
    source: Literal["report", "graph", "none"] = Field(
        description="report: the scan's own symbol index (no Neo4j needed). graph: Neo4j, which holds only "
                    "the most recently scanned project. none: nothing references the symbol")
    pages: list[ImpactPage]
    note: str = ""


class DetectedPackage(BaseModel):
    name: str
    ecosystem: str = Field(description="pypi, npm, nuget, or cli (a standalone command-line tool whose binaries the repo releases on GitHub)")
    import_name: str
    path: str = Field(description="folder inside the repo; empty for the root")
    spec: str = Field(description="<ecosystem>:<name>: pass this in start_scan's packages to verify exactly it")


class DetectedRepo(BaseModel):
    repo: str
    commit: str = ""
    branch: str = ""
    packages: list[DetectedPackage] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list, description=(
        "ecosystems the repo also holds that docrot has no checker for (rust, go, java, ...)"))
    error: str = ""
    note: str = ""


class Detected(BaseModel):
    repos: list[DetectedRepo]


class Queued(BaseModel):
    scan_id: str
    name: str
    state: str
    created: bool = Field(description="true if this call recorded a new scan; false if it reused a matching one")
    queued_behind: list[str] = Field(description="scan ids of this process's own queue that run first")
    next: str


def reply(model: BaseModel) -> CallToolResult:
    """Compact JSON for the model to read, the same data as structured content.

    The SDK's own text rendering is indented, which on a page of findings costs
    a third more tokens for nothing."""
    data = model.model_dump(mode="json")
    return CallToolResult(content=[TextContent(text=json.dumps(data, separators=(",", ":"),
                                                               ensure_ascii=False))],
                          structured_content=data)


# ----------------------------------------------------------------------- tools

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                       open_world_hint=False)
# Starting a scan is additive (new records, a new run) and reaches the world:
# remote sandboxes, a model API, git remotes and the docs sites.
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                      open_world_hint=True)

ScanId = Annotated[str, Field(description="a scan id from list_scans (or start_scan)")]
RunId = Annotated[str | None, Field(description="a run id from get_scan; default: the newest finished run")]

INSTRUCTIONS = """\
docrot checks documentation against the code it documents, across releases, and reports where they disagree.
Read first: list_scans, then get_findings / scan_diff / rename_impact on a scan id.
Before start_scan on a repo you do not know, call detect_packages: a monorepo holds several packages \
and a scan verifies only the ones named, or else the ones the docs mention.
start_scan and rerun_scan spend sandbox time and model credit and run for minutes; only one scan runs at a \
time, and you follow it with get_scan."""


def build_server(store: Store, worker: ScanWorker, blast: BlastRadius | None = None,
                 detect: Callable[[str], dict] = detect_repo) -> MCPServer:
    """The server, over a store and a worker handed in - tests pass a temp store
    and a fake runner, as tests/test_api.py does for the HTTP app. `blast` is
    the Neo4j lookup behind rename_impact's fallback; `detect` the clone-and-
    look behind detect_packages."""
    server = MCPServer("docrot", instructions=INSTRUCTIONS, log_level="WARNING")
    lookup = blast or graph_blast_radius
    # check-then-queue must be atomic within this process; across processes it
    # cannot be, and the window is the few milliseconds between two calls.
    queue_lock = threading.Lock()

    def scan_of(scan_id: str) -> ScanRecord:
        rec = store.get(scan_id)
        if rec is None:
            raise ToolError(unknown_scan(store, scan_id))
        return rec

    def newest(scan_id: str, run_id: str | None):
        """(summary, report, extraction, results) of a finished run, or a ToolError saying what to do."""
        runs = store.runs(scan_id)
        if not runs:
            raise ToolError(f"Scan {scan_id} has no finished run yet. Check get_scan for its state, "
                            "or queue one with rerun_scan.")
        if run_id and run_id not in {r.id for r in runs}:
            raise ToolError(f"Scan {scan_id} has no run {run_id!r}. Runs: "
                            f"{', '.join(r.id for r in runs[-10:])}.")
        loaded = store.load_run(scan_id, run_id)
        assert loaded is not None
        return loaded

    @server.tool(annotations=READ, title="List scans")
    def list_scans() -> Annotated[CallToolResult, ScanList]:
        """List every recorded scan with its state and headline numbers.

        Start here: it gives the scan ids every other tool takes. Free and
        instant; reads only the local store.
        """
        out = []
        for rec in store.all():
            runs, st = store.runs(rec.id), store.status(rec.id)
            last = runs[-1] if runs else None
            stats = last.stats if last else {}
            out.append(ScanSummary(
                id=rec.id, name=rec.name, docs=rec.docs, repos=rec.repos, packages=rec.packages,
                state=st.state, runs=len(runs), last_run=last.id if last else None,
                last_run_at=last.at if last else None, pages=stats.get("pages"),
                drifted_pages=stats.get("drifted"), findings=stats.get("findings")))
        return reply(ScanList(scans=out))

    @server.tool(annotations=READ, title="Get scan status")
    def get_scan(scan_id: ScanId) -> Annotated[CallToolResult, ScanStatusView]:
        """A scan's state, current phase, error, recent log lines and run history.

        Poll this after start_scan or rerun_scan until state is `done` or
        `failed` (a scan takes minutes; poll every 30 seconds or so, not in a
        tight loop). Returns only the last 20 log lines; findings come from
        get_findings. Free and instant.
        """
        rec = scan_of(scan_id)
        st = store.status(rec.id)
        phase = PHASES[min(st.phase, len(PHASES) - 1)] if st.state == "running" else ""
        return reply(ScanStatusView(
            id=rec.id, name=rec.name, state=st.state, phase=phase, error=st.error,
            queued_at=st.queued_at, started_at=st.started_at, finished_at=st.finished_at,
            log_tail=[line[1] for line in st.log[-LOG_TAIL:]], log_lines=len(st.log),
            runs=[run_brief(r) for r in store.runs(rec.id)]))

    @server.tool(annotations=READ, title="Get findings")
    def get_findings(
        scan_id: ScanId,
        run_id: RunId = None,
        kinds: Annotated[list[str] | None, Field(description=(
            "only these kinds: stale_pin, signature, missing_symbol, runtime, blocked. "
            "Add clean or prose to also get pages with no drift. Default: all drift and blocked findings"
        ))] = None,
        page_contains: Annotated[str | None, Field(description=(
            "only findings whose page url, path or title contains this text, case-insensitive"
        ))] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT, description="findings per call")] = 50,
        offset: Annotated[int, Field(ge=0, description="findings to skip, for paging")] = 0,
    ) -> Annotated[CallToolResult, FindingsPage]:
        """The documentation/code disagreements of a finished run.

        Each finding is the record `docrot export` writes (docrot/1 JSON Lines):
        `status` fail means the docs are wrong, `blocked` means the snippet
        never ran. It carries the documented snippet, what the code actually
        does, where to edit (`doc.path`, `doc.line`) and a `fix_hint`. Records
        are large, so page with limit/offset and narrow with kinds or
        page_contains. Free and instant; reads a saved run.
        """
        rec = scan_of(scan_id)
        wanted = list(dict.fromkeys(kinds or []))
        unknown = [k for k in wanted if k not in (*FINDING_KINDS, *CLEAN_KINDS)]
        if unknown:
            raise ToolError(f"Unknown kind {', '.join(map(repr, unknown))}. "
                            f"Valid: {', '.join((*FINDING_KINDS, *CLEAN_KINDS))}.")
        run, *_ = newest(rec.id, run_id)
        cov = (store.report(rec.id, run.id) or {}).get("coverage")
        records = store.export(rec.id, run.id, include_clean=any(k in CLEAN_KINDS for k in wanted))
        assert records is not None
        next(records)                                   # the scan record, not a finding
        needle = (page_contains or "").lower()
        hits = [f for f in records
                if (not wanted or f["kind"] in wanted) and (not needle or needle in _page_text(f))]
        by_kind: dict[str, int] = {}
        for f in hits:
            by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        window = hits[offset:offset + limit]
        more = offset + len(window) < len(hits)
        return reply(FindingsPage(
            scan_id=rec.id, run_id=run.id, coverage=Coverage(**{k: cov.get(k) or {} for k in
                                                                ("tiers", "unverifiable", "no_checker")})
            if cov else None, total=len(hits), offset=offset, returned=len(window),
            next_offset=offset + len(window) if more else None, by_kind=by_kind, findings=window))

    @server.tool(annotations=READ, title="What changed since the previous run")
    def scan_diff(
        scan_id: ScanId,
        since_run: Annotated[str | None, Field(description=(
            "run id to compare against; default: the run before the newest"))] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT, description="entries per list")] = 25,
    ) -> Annotated[CallToolResult, ScanDiff]:
        """What got better or worse between a scan's newest run and the one before it.

        The same comparison as `docrot diff`: `regressions` (passed before,
        fails now) and `appeared` (new failing snippets) are the news; flaky
        and unverified results are left out. Needs at least two runs, otherwise
        `compared` is false. Free and instant.
        """
        rec = scan_of(scan_id)
        runs = store.runs(rec.id)
        if len(runs) < 2:
            return reply(ScanDiff(scan_id=rec.id, compared=False,
                                  note="Need two runs to compare; this scan has "
                                       f"{len(runs)}. Queue another with rerun_scan."))
        newer = runs[-1]
        older = runs[-2] if not since_run else next((r for r in runs if r.id == since_run), None)
        if older is None or older.id == newer.id:
            raise ToolError(f"No earlier run {since_run!r} to compare against. Runs: "
                            f"{', '.join(r.id for r in runs[-10:])}.")
        try:
            d = diff(load_results(store.run_dir(rec.id, older.id) / "results.json"),
                     load_results(store.run_dir(rec.id, newer.id) / "results.json"))
        except (OSError, ValueError) as e:
            raise ToolError(f"Could not read the results of {older.id} or {newer.id}: {e}") from e
        lists = {k: [DiffEntry(**row) for row in d[k]] for k in
                 ("regressions", "appeared", "fixes", "disappeared")}
        return reply(ScanDiff(
            scan_id=rec.id, compared=True, from_run=older.id, to_run=newer.id, counts=d["counts"],
            worse=bool(exit_code(d)), truncated=any(len(v) > limit for v in lists.values()),
            **{k: v[:limit] for k, v in lists.items()}))

    @server.tool(annotations=READ, title="Rename impact")
    def rename_impact(
        scan_id: ScanId,
        symbol: Annotated[str, Field(description=(
            "a function, class or method name, e.g. close_async or perseus_client.close_async"))],
    ) -> Annotated[CallToolResult, RenameImpact]:
        """Which documentation pages would break if a symbol were renamed or removed.

        Answered from the scan's own symbol index, which needs no database;
        only if that finds nothing is Neo4j asked, and `source` says which
        answered. Free and instant.
        """
        rec = scan_of(scan_id)
        raw = symbol.strip()
        if not raw:
            raise ToolError("symbol is empty; pass a name such as close_async.")
        pages = report_impact(store.report(rec.id), raw)
        if pages:
            return reply(RenameImpact(scan_id=rec.id, symbol=raw, source="report", pages=pages))
        rows = lookup(raw.rstrip("()").split(".")[-1])
        if rows:
            return reply(RenameImpact(
                scan_id=rec.id, symbol=raw, source="graph",
                pages=[ImpactPage(name=r["url"], url=r["url"], touchpoints=r.get("touchpoints"))
                       for r in rows],
                note="From the Neo4j graph, which holds only the most recently scanned project; "
                     "it may not be this scan."))
        note = "" if store.runs(rec.id) else (
            "This scan has no finished run, so there is no symbol index to search.")
        return reply(RenameImpact(scan_id=rec.id, symbol=raw, source="none", pages=[],
                                  note=note or "No page references it; safe to rename."))

    def refuse_if_busy() -> None:
        """Refuse to queue while any scan not in our own queue is queued or running.

        The store is shared: a dashboard in another process, or another agent's
        MCP server, may be mid-scan, and our scan's closing sandbox sweep would
        delete its sandboxes. A scan in *our* queue is fine: the worker runs it
        first and ours waits, which is the serial behaviour it was built for.

        Scans whose owning process has died are failed first (`Store.recover`),
        so a crash does not block every later start. A live owner still does;
        so does one on another host, which cannot be checked."""
        store.recover()
        for rec in store.all():
            if store.status(rec.id).state in ("queued", "running") and not worker.owns(rec.id):
                raise ToolError(
                    f"Scan {rec.id} ({rec.name}) is {store.status(rec.id).state} in another docrot "
                    "process (the dashboard, `docrot scan`, or another MCP server). Two scans at once "
                    "delete each other's sandboxes, so nothing was started. Wait for it with get_scan, "
                    "then try again. If you know that process is gone, stop it or its host's docrot "
                    "and retry: a scan whose process has exited on this machine is cleared "
                    "automatically.")

    def queue(scan_id: str, created: bool) -> CallToolResult:
        ahead = [r.id for r in store.all() if r.id != scan_id and worker.owns(r.id)]
        try:
            st = worker.enqueue(scan_id)
        except AlreadyActive as e:
            raise ToolError(f"Scan {scan_id} is already queued or running; follow it with get_scan.") from e
        rec = scan_of(scan_id)
        return reply(Queued(scan_id=scan_id, name=rec.name, state=st.state, created=created,
                            queued_behind=ahead,
                            next=f"Scans take minutes. Poll get_scan({scan_id!r}), then call get_findings."))

    @server.tool(annotations=READ, title="Detect packages in repos")
    def detect_packages(
        repos: Annotated[list[str], Field(description=(
            f"up to {MAX_DETECT} git URLs or local paths (/..., ./..., ~/...) to look in"))],
    ) -> Annotated[CallToolResult, Detected]:
        """Which publishable packages a repo holds, found by reading its manifests.

        Call this BEFORE start_scan on a repo you do not know: monorepos ship
        several packages (e.g. PyPI + npm + NuGet, or a standalone CLI installed
        from GitHub release binaries - `cli:name`) and a scan can only verify
        what it is pointed at. Pass the `spec` of the ones you want as
        start_scan's `packages` (positionally: packages[i] for repos[i]); name
        none and the scan picks the found packages the docs mention (at most 6 per repo; if the docs
        mention none, the single best one). `other`
        lists ecosystems docrot cannot check yet. Clones the repo (cached, so
        later calls take a second; the first can take up to a minute) but runs
        no sandbox and spends no model credit. An unreachable repo comes back
        as an entry with `error`, not a failure.
        """
        wanted = list(dict.fromkeys(r.strip() for r in repos if r and r.strip()))
        if not wanted:
            raise ToolError("repos is empty; pass at least one git URL or local path.")
        if len(wanted) > MAX_DETECT:
            raise ToolError(f"At most {MAX_DETECT} repos per call; got {len(wanted)}.")
        bad = [f"Repository {i} ({r!r}) isn't a git URL or a local path (/..., ./..., ~/...)."
               for i, r in enumerate(wanted, 1) if not is_repo(r)]
        if bad:
            raise ToolError(" ".join(bad))
        out = []
        for found in map(detect, wanted):
            entry = DetectedRepo(**found)
            if not entry.error and not entry.packages:
                entry.note = ("No package found. If the repo has one, name it in start_scan's packages "
                              "(with the ecosystem set, or as pypi:/npm:/nuget:name; cli:name for a standalone tool).")
            out.append(entry)
        return reply(Detected(repos=out))

    @server.tool(annotations=ACT, title="Start a scan")
    def start_scan(
        docs: Annotated[list[str], Field(description=(
            "documentation site root URLs (http or https) to crawl; may be empty if repos hold the docs"))],
        repos: Annotated[list[str], Field(description=(
            "git URLs or local paths (/..., ./..., ~/...) of the code the docs describe; may be empty "
            "if packages names what to check"))],
        packages: Annotated[list[str] | None, Field(description=(
            "package names, matched to repos by position: packages[0] belongs to repos[0]. A name may carry "
            "an ecosystem prefix - pypi:relate-db, npm:@relate/relate, nuget:Relate.Net, or cli:relate for a "
            "standalone command-line tool installed from the repo's GitHub release binaries, which needs a "
            "GitHub repo in repos (see detect_packages, whose `spec` is exactly this). Naming restricts: if any entry names a package "
            "found in a repo, that repo verifies exactly the named ones (extra entries past the last repo "
            "may also name a package of an earlier repo, to verify several from one); if none is named for "
            "a repo, docrot picks the packages found there that the docs mention (install commands, imports, "
            "using lines, distinctive names), most mentioned first, at most 6 per repo, plus a standalone CLI named like the "
            "repo when the docs show at least 3 of its command lines and the releases carry a Linux binary; named "
            "packages are never capped. Use \"\" to "
            "leave a repo's slot unnamed. An entry naming no package of any repo is fetched from the "
            "registry; one name on two registries is verified once, on the first. Omit to infer everything"))] = None,
        name: Annotated[str | None, Field(description="display name; default: derived from the first source")] = None,
        ecosystem: Annotated[Literal["auto", "pypi", "npm", "nuget"], Field(description=(
            "auto (default): the repo's manifests decide. It only matters for a package named without a "
            "repo and without a prefix, which has no manifest: then set the registry to fetch it from"))] = "auto",
        versions: Annotated[int, Field(ge=1, le=10, description="releases to test each package against")] = 3,
        max_pages: Annotated[int | None, Field(ge=1, le=2000, description="crawl cap per docs site")] = None,
        assume_credentialed: Annotated[bool, Field(description=(
            "treat snippets that touch the package as runnable; set it for targets whose examples need no "
            "key"))] = False,
    ) -> Annotated[CallToolResult, Queued]:
        """Queue a scan of docs against code, and return at once with its id.

        COSTLY: a scan runs for several minutes in remote Daytona sandboxes,
        spends sandbox time and model credit, and clones repos. It returns
        immediately; follow it with get_scan until `done`, then read
        get_findings. If the same docs and repos were scanned before, that
        scan is reused and gains a run. Only one scan runs at a time across
        every docrot process, so this is refused while another is queued or
        running elsewhere.
        """
        try:
            # Validate and de-duplicate to look the scan up, but keep the raw
            # lists for `create`: packages[i] belongs to the repos[i] the
            # caller sent, and align_packages needs that to re-sort them.
            sources = clean_sources(docs, repos)
            opts = ScanOptions(ecosystem=ecosystem, versions=versions, max_pages=max_pages,
                               assume_credentialed=assume_credentialed)
        except ValueError as e:         # clean_sources raises ValueError(list); pydantic's is a ValueError too
            raise ToolError(problems(e)) from e
        with queue_lock:
            refuse_if_busy()
            rec = store.find_by_sources(*sources)
            created = rec is None
            try:
                if rec is None:
                    rec = store.create(docs, repos, name=name or "", packages=packages or [], options=opts)
                else:       # as `docrot scan` does: the new settings replace the old
                    changes: dict[str, Any] = {"options": opts}
                    if packages:
                        changes["packages"] = align_packages(repos, packages) or rec.packages
                    if name:
                        changes["name"] = name
                    rec = store.update(rec.id, **changes)
            except ValueError as e:
                raise ToolError(problems(e)) from e
            return queue(rec.id, created=created)

    @server.tool(annotations=ACT, title="Run a scan again")
    def rerun_scan(scan_id: ScanId) -> Annotated[CallToolResult, Queued]:
        """Queue another run of an existing scan, and return at once.

        COSTLY, like start_scan: minutes of remote sandbox time and model
        credit. Use it after docs or code changed, to see what scan_diff
        reports. Follow it with get_scan. Refused while another scan is queued
        or running in a different docrot process.
        """
        rec = scan_of(scan_id)
        with queue_lock:
            refuse_if_busy()
            return queue(rec.id, created=False)

    return server


# --------------------------------------------------------------------- helpers

def unknown_scan(store: Store, scan_id: str) -> str:
    ids = [r.id for r in store.all()]
    if not ids:
        return f"No scan {scan_id!r}: none are recorded yet. Create one with start_scan."
    shown = ", ".join(ids[:KNOWN_IDS_SHOWN]) + (f" and {len(ids) - KNOWN_IDS_SHOWN} more"
                                                 if len(ids) > KNOWN_IDS_SHOWN else "")
    return f"No scan {scan_id!r}. Known ids: {shown}."


def problems(e: ValueError) -> str:
    """The validation messages the dashboard shows beside its fields, as one string."""
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
    first = e.args[0] if e.args else str(e)
    return " ".join(first) if isinstance(first, list) else str(first)


def run_brief(run) -> RunBrief:
    s = run.stats
    return RunBrief(id=run.id, at=run.at, elapsed_s=run.elapsed, pages=s.get("pages"),
                    drifted_pages=s.get("drifted"), findings=s.get("findings"))


def _page_text(finding: dict) -> str:
    doc = finding.get("doc") or {}
    return " ".join(str(doc.get(k) or "") for k in ("url", "path", "title")).lower()


def report_impact(report: dict | None, symbol: str) -> list[ImpactPage]:
    """Pages the report's symbol index ties to `symbol`, by the dashboard's lookup rules.

    The index (`report["impact"]`) is keyed by lowercased symbol, both dotted
    and bare, and holds page ids; the sections carry the ids' names."""
    if not report:
        return []
    impact = report.get("impact") or {}
    bare = symbol.rstrip("()").split(".")[-1].lower()
    ids = impact.get(symbol.lower().rstrip("()")) or impact.get(bare) or []
    pages = {p["id"]: p for s in report.get("sections", []) for p in s.get("pages", [])}
    return [ImpactPage(name=pages[i]["name"], url=pages[i].get("url", ""))
            for i in ids if i in pages]


def graph_blast_radius(symbol: str) -> list[dict]:
    """The Neo4j traversal `docrot blast` runs. Empty when the graph is not up
    (GraphStore degrades quietly); its messages go to stderr."""
    from .graph import GraphStore

    graph = GraphStore(cfgmod.load(), say)
    try:
        return graph.blast_radius(symbol)
    finally:
        graph.close()


# ------------------------------------------------------------------------ main

def mark_interrupted(store: Store, scan_ids: list[str]) -> None:
    """Fail the scans *this process* abandoned at shutdown.

    Only ids the worker owned: our own scans are not "gone" to `Store.recover()`
    while this process is alive, so they need marking explicitly. Left alone
    they would read as running until someone recovers them after we exit."""
    for scan_id in scan_ids:
        st = store.status(scan_id)
        st.state, st.finished_at = "failed", now()
        st.error = "Interrupted - the docrot MCP server stopped before this scan finished."
        st.log.append(["t-hit", "! interrupted: the MCP server stopped"])
        store.set_status(scan_id, st)


def serve_stdio(store: Store) -> None:
    """Run the server on stdin/stdout until the client goes away."""
    from .scan import execute_scan

    # Safe now that statuses name their owner: only scans whose process is gone
    # are failed, never one the dashboard or a CLI scan is running. Before the
    # SDK guards fd 1, so anything it prints is sent to stderr by hand.
    with contextlib.redirect_stdout(sys.stderr):
        recovered = store.recover()
    for scan_id in recovered:
        say(f"docrot mcp: {scan_id} was left running by a process that is gone - marked failed")
    worker = ScanWorker(store, lambda s, scan_id: execute_scan(s, scan_id))
    server = build_server(store, worker)
    # While serving, the SDK points fd 1 at stderr, so a stray print misses the
    # wire - but only if it reaches fd 1 then. A piped sys.stdout is block
    # buffered, and its leftovers would be flushed at exit, after the SDK has
    # put the wire back on fd 1: a corrupt frame, late. Flush at every newline.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    # A client that closes the pipe gets a clean exit from EOF; one that sends
    # SIGTERM would otherwise skip the `finally` below and leave scans marked
    # running. Raising lets the cleanup run; the SDK's stdin reader thread
    # would then hold the exit until the pipe closes, so leave at once after.
    terminated = False

    def on_sigterm(*_) -> None:
        nonlocal terminated
        terminated = True
        sys.exit(143)

    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        server.run("stdio")
    finally:
        left = worker.stop()
        mark_interrupted(store, left)
        for scan_id in left:
            say(f"docrot mcp: {scan_id} was interrupted by shutdown - marked failed")
        if terminated:
            os._exit(143)
