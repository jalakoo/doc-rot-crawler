"""docrot command line.

    docrot scan --docs-url URL [URL ...] --repo URL [URL ...] [--name NAME]
    docrot packages REPO [REPO ...] # which packages a repo holds, before scanning it
    docrot scans
    docrot serve [--port 8080]
    docrot blast SYMBOL [--scan ID]
    docrot diff [--scan ID] [--since RUN]
    docrot export [--scan ID] [--run RUN] [--format jsonl|sarif] [-o FILE]
    docrot mcp                      # MCP server on stdio, for other AI agents

Every scan is recorded in data/scans/, the same store the dashboard reads, so a
scan started here shows up there and vice versa.
"""
from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.table import Table

from . import config as cfgmod
from .scan import MAX_AUTO_PACKAGES
from .store import ScanOptions, ScanRecord, Store, align_packages

console = Console()


def log(msg: str = ""):
    console.print(msg, highlight=False, markup=False)


def open_store(say=log) -> Store:
    store = Store(cfgmod.DATA)
    adopted = store.import_legacy(cfgmod.RUNS)
    if adopted:
        say(f"  adopted data/runs/ as scan {adopted.id} ({adopted.name})")
    return store


def _latest_scan(store: Store, scan_id: str | None) -> ScanRecord | None:
    if scan_id:
        return store.get(scan_id)
    ran = [(store.runs(r.id)[-1].at, r) for r in store.all() if store.runs(r.id)]
    return max(ran, key=lambda x: x[0])[1] if ran else None


def scan(args) -> int:
    from .scan import execute_scan

    store = open_store()
    options = ScanOptions(
        ecosystem=args.ecosystem, versions=args.versions, max_pages=args.max_pages,
        assume_credentialed=args.assume_credentialed, no_cache=args.no_cache,
        extraction_models=[m.strip() for m in ",".join(args.extraction_models or []).split(",")
                           if m.strip()])
    docs, repos = args.docs_url or [], args.repo or []
    try:
        rec = store.find_by_sources(docs, repos)
        if rec is None:
            rec = store.create(docs, repos, name=args.name or "",
                               packages=args.package or [], options=options)
        else:
            changes: dict = {"options": options, "packages": align_packages(repos, args.package) or rec.packages}
            if args.name:
                changes["name"] = args.name
            rec = store.update(rec.id, **changes)
    except ValueError as e:
        for problem in e.args[0]:
            console.print(f"[red]{problem}[/red]")
        return 2

    store.recover()        # a crashed scan's leftover status is not "also running"
    busy = [r.name for r in store.all()
            if r.id != rec.id and store.status(r.id).state == "running"]
    if busy:
        console.print(f"[yellow]! {', '.join(busy)} is also running - two scans at "
                      "once can exceed the sandbox quota[/yellow]")

    log(f"› scan {rec.id} ({rec.name})")
    status = execute_scan(store, rec.id, echo=log)
    if status.state == "failed":
        console.print(f"[red]{status.error}[/red]")
        return 3

    report = store.report(rec.id) or {}
    _print_summary(report)
    run = store.runs(rec.id)[-1]
    log(f"\n  artifacts: {store.run_dir(rec.id, run.id)}")
    log("  view:      docrot serve")
    if args.serve:
        from .server import serve
        serve(store, args.port, path=f"scan/{rec.id}")
    return 1 if report.get("stats", {}).get("findings") else 0


def packages_cmd(args) -> int:
    """What a scan would find in each repo: every PyPI, npm and NuGet package,
    and a standalone CLI when the repo's GitHub releases carry its binary,
    with the `eco:name` to pass to `--package`. The terminal's "detect" step.
    Exits 1 when a repo holds none, 3 when one cannot be fetched."""
    from .acquire import binaries
    from .acquire.detect import cli_row
    from .acquire.repo import CloneError, clone, find_packages, repo_name
    from .scan import nothing_found

    cfg = cfgmod.load()
    binaries.use_token(cfg.github_token)
    code = 0
    for repo in args.repo:
        try:
            path, commit, _ = clone(repo, log, cfg.clone_timeout)
        except CloneError as e:
            console.print(f"[red]couldn't fetch {repo}: {e}[/red]", highlight=False)
            code = 3
            continue
        found = find_packages(path, repo_name(repo))
        standalone, why = cli_row(repo, found)
        if why:
            console.print(f"[yellow]{why}[/yellow]", highlight=False)
        if not found and not standalone:
            console.print(f"[yellow]{nothing_found(path, repo_name(repo))}[/yellow]", highlight=False)
            code = code or 1
            continue
        t = Table(title=f"\n{repo_name(repo)} ({commit})", title_justify="left",
                  header_style="bold")
        for col in ("package", "import", "path"):
            t.add_column(col, overflow="fold")
        for p in found:
            try:
                where = p.root.resolve().relative_to(path.resolve()).as_posix()
            except ValueError:
                where = str(p.root)
            t.add_row(f"{p.ecosystem}:{p.name}", p.import_name,
                      "." if where in ("", ".") else where)
        if standalone:
            t.add_row(standalone["spec"], standalone["import_name"],
                      f"GitHub release binaries (latest {standalone['latest']})")
        console.print(t)
        if len(found) + bool(standalone) > 1:
            log("  a scan verifies the ones its docs mention (up to "
                f"{MAX_AUTO_PACKAGES}); name packages with --package eco:name to choose")
    return code


def _print_summary(report: dict):
    title = " · ".join(report.get("docs_urls") or report.get("repo_urls") or [])
    t = Table(title=f"\n{title}", title_justify="left", header_style="bold")
    t.add_column("page")
    t.add_column("status")
    t.add_column("findings", justify="right")
    for sec in report.get("sections", []):
        for p in sec["pages"]:
            n = len([f for f in p["findings"] if f["kind"] in
                     ("stale_pin", "signature", "missing_symbol", "runtime")])
            if not n and p["st"] != "block":
                continue
            colour = {"drift": "magenta", "fail": "red", "block": "yellow"}.get(p["st"], "green")
            t.add_row(p["name"], f"[{colour}]{p['st']}[/{colour}]", str(n))
    console.print(t)


def list_scans(args) -> int:
    store = open_store()
    t = Table(header_style="bold")
    for col in ("id", "name", "state", "runs", "last run", "findings"):
        t.add_column(col, justify="right" if col in ("runs", "findings") else "left")
    for rec in store.all():
        runs, st = store.runs(rec.id), store.status(rec.id)
        last = runs[-1] if runs else None
        t.add_row(rec.id, rec.name, st.state, str(len(runs)),
                  last.at.replace("T", " ") if last else "—",
                  str(last.stats.get("findings", "—")) if last else "—")
    console.print(t)
    return 0


def serve_cmd(args) -> int:
    from .server import serve
    serve(open_store(), args.port, open_browser=not args.no_open)
    return 0


def mcp_cmd(args) -> int:
    """The MCP server on stdio. stdout is the protocol channel, so even the
    one-time legacy-import notice open_store prints goes to stderr here."""
    import contextlib

    from .mcp_server import say, serve_stdio

    # Before the server starts nothing guards fd 1 yet (the SDK diverts it to
    # stderr only while serving), so setup output is redirected by hand.
    with contextlib.redirect_stdout(sys.stderr):
        store = open_store(say)
    serve_stdio(store)
    return 0


def blast(args) -> int:
    from .graph import GraphStore

    store = open_store()
    graph = GraphStore(cfgmod.load(), log)
    rows = graph.blast_radius(args.symbol)
    graph.close()
    if not rows:
        rec = _latest_scan(store, args.scan)
        rep = store.report(rec.id) if rec else None
        if rep:
            ids = rep.get("impact", {}).get(args.symbol.lower(), [])
            names = {p["id"]: p["name"] for s in rep["sections"] for p in s["pages"]}
            rows = [{"url": names.get(i, i), "touchpoints": 1} for i in ids]
    if not rows:
        console.print(f"nothing references [cyan]{args.symbol}[/cyan] — safe to rename")
        return 0
    console.print(f"renaming [cyan]{args.symbol}[/cyan] breaks [bold]{len(rows)}[/bold] page(s):")
    for r in rows:
        log(f"  {r['url']}")
    return 0


def run_diff(args) -> int:
    """Compare a scan's last two runs and report only what got worse."""
    from .report.diff import diff, exit_code, load_results

    store = open_store()
    rec = _latest_scan(store, args.scan)
    if rec is None:
        console.print("[yellow]no scans with runs yet - run `docrot scan` first[/yellow]")
        return 0
    runs = store.runs(rec.id)
    if len(runs) < 2:
        console.print(f"[yellow]{rec.name}: need two runs to diff — "
                      "scan it at least twice[/yellow]")
        return 0

    newest = runs[-1]
    prev = runs[-2] if not args.since else next((r for r in runs if r.id == args.since), None)
    if prev is None:
        console.print(f"[red]no run {args.since} for {rec.name}[/red]")
        return 3

    d = diff(load_results(store.run_dir(rec.id, prev.id) / "results.json"),
             load_results(store.run_dir(rec.id, newest.id) / "results.json"))
    c = d["counts"]
    console.print(f"[bold]{rec.name}: {prev.id} → {newest.id}[/bold]")
    console.print(f"  regressions {c['regressions']} · fixes {c['fixes']} · "
                  f"new failures {c['appeared']} · removed {c['disappeared']}")

    for row in d["regressions"] + d["appeared"]:
        console.print(f"  [red]{row['was'] or '—'} → {row['now']}[/red]  "
                      f"{row['id']} @ {row['version']}", markup=True, highlight=False)
        if row["detail"]:
            log(f"      {row['detail'][:140]}")
    for row in d["fixes"]:
        console.print(f"  [green]{row['was']} → {row['now']}[/green]  "
                      f"{row['id']} @ {row['version']}", highlight=False)
    if not any(c.values()):
        console.print("  [green]no change[/green]")

    (store.run_dir(rec.id, newest.id) / "diff.json").write_text(json.dumps(d, indent=1))
    return exit_code(d)


def export_cmd(args) -> int:
    """A run as JSON Lines (a scan record, then one per finding) or SARIF."""
    store = open_store()
    rec = _latest_scan(store, args.scan)
    if rec is None:
        console.print("[red]no such scan, or no scans with runs yet[/red]", highlight=False)
        return 3
    try:
        text = store.export_text(rec.id, args.run, include_clean=args.include_clean,
                                 fmt=args.format)
    except KeyError:
        console.print(f"[red]{rec.name}: no run {args.run or 'yet'}[/red]", highlight=False)
        return 3
    if args.output in (None, "-"):
        sys.stdout.write(text)
        return 0
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(text)
    findings = text.count('"type": "finding"')
    console.print(f"wrote {findings} finding(s) for {rec.name} to {args.output}", highlight=False)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("docrot", description="documentation rot detector")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="crawl docs, run them against the package")
    s.add_argument("--docs-url", action="extend", nargs="+", metavar="URL",
                   help="documentation site root; repeat or list several")
    s.add_argument("--repo", action="extend", nargs="+", metavar="REPO",
                   help="git URL or local path; repeat or list several. Every "
                        "docs site is checked against every repo's package")
    s.add_argument("--package", action="extend", nargs="+", metavar="[ECO:]NAME",
                   help="package to verify, optionally with its registry: pypi:relate-db, "
                        "npm:@relate/relate, nuget:Relate.Net - or cli:relate for a standalone "
                        "command-line tool installed from the repo's GitHub release binaries "
                        "(needs a GitHub --repo). Name one or more of a repo's "
                        "packages and only those are verified from it (the first name goes "
                        "with the first --repo, and so on; any name is matched against every "
                        "repo). Name none and docrot verifies every package in the repo that "
                        f"the docs mention, at most {MAX_AUTO_PACKAGES} per repo, plus a standalone CLI "
                        "named like the repo when the docs show its command lines and the releases "
                        "carry its binary - see "
                        "`docrot packages REPO` for what a repo holds. A name no repo has is "
                        "verified from its registry")
    s.add_argument("--name", help="dashboard name for this scan")
    s.add_argument("--ecosystem", default="auto", choices=["auto", "pypi", "npm", "nuget"],
                   help="registry for a --package that no repo describes and that has no "
                        "eco: prefix; a repo's own package metadata always says for itself. "
                        "auto (the default) asks PyPI, npm and NuGet and uses the one that has "
                        "the name, and stops if several do")
    s.add_argument("--versions", type=int, default=3)
    s.add_argument("--max-pages", type=int, help="per docs site")
    s.add_argument("--no-cache", action="store_true")
    s.add_argument(
        "--assume-credentialed", action="store_true",
        help="treat snippets that touch the target package as runnable. Set it "
             "for targets whose examples need no key (Streamlit, Pydantic); "
             "without it they stay `structural`, which on a credential-free "
             "target discards about a third of the available evidence.")
    s.add_argument(
        "--extraction-models", nargs="+", metavar="VENDOR/MODEL",
        help="OpenRouter model ids in preference order; the first is the "
             "primary and the rest are fallbacks. Overrides EXTRACTION_MODELS. "
             "e.g. --extraction-models anthropic/claude-opus-5 "
             "openai/gpt-sol-latest")
    s.add_argument("--serve", action="store_true", help="open the dashboard when done")
    s.add_argument("--port", type=int, default=8080)

    pk = sub.add_parser(
        "packages", help="the packages a repo holds, before you scan it",
        description="List the PyPI, npm and NuGet packages a repo publishes, and its standalone "
                    "CLI when GitHub releases carry the binary, each with the eco:name "
                    "to give `docrot scan --package`. Exits 1 when a repo holds none, 3 when it "
                    "cannot be fetched.")
    pk.add_argument("repo", nargs="+", metavar="REPO", help="git URL or local path")

    sub.add_parser("scans", help="list recorded scans")

    v = sub.add_parser("serve", help="run the dashboard")
    v.add_argument("--port", type=int, default=8080)
    v.add_argument("--no-open", action="store_true")

    b = sub.add_parser("blast", help="which pages break if this symbol is renamed")
    b.add_argument("symbol")
    b.add_argument("--scan", help="scan id (default: the most recently run)")

    df = sub.add_parser("diff", help="what changed since the previous run (for cron)")
    df.add_argument("--scan", help="scan id (default: the most recently run)")
    df.add_argument("--since", help="run id to compare against, e.g. 20260912T101500")

    sub.add_parser("mcp", help="MCP server on stdio, so other AI agents can run scans and read findings")

    ex = sub.add_parser("export", help="a run as JSON Lines, for agents and scripts")
    ex.add_argument("--scan", help="scan id (default: the most recently run)")
    ex.add_argument("--run", help="run id (default: the newest)")
    ex.add_argument("--format", default="jsonl", choices=["jsonl", "sarif"],
                    help="jsonl for agents and scripts, sarif for CI and code scanning")
    ex.add_argument("-o", "--output", help="file to write (default: stdout)")
    ex.add_argument("--include-clean", action="store_true",
                    help="also emit pages with no drift and prose-only pages")
    return p


def main(argv=None) -> int:
    p = parser()
    args = p.parse_args(argv)
    if args.cmd == "scan":
        if not args.docs_url and not args.repo:
            p.error("pass --docs-url, --repo, or both")
        return scan(args)
    if args.cmd == "packages":
        return packages_cmd(args)
    if args.cmd == "scans":
        return list_scans(args)
    if args.cmd == "diff":
        return run_diff(args)
    if args.cmd == "serve":
        return serve_cmd(args)
    if args.cmd == "export":
        return export_cmd(args)
    if args.cmd == "mcp":
        return mcp_cmd(args)
    return blast(args)


if __name__ == "__main__":
    sys.exit(main())
