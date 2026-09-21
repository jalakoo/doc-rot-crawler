"""S1 - Daytona provisioning throughput and failure rate.

Measures each phase separately (create / install / exec / delete), which the
2026-09-09 pipeline run did not: it recorded only total elapsed, so the
bottleneck behind 853s for 72 sandboxes is still unidentified.

Three strategies:

  A  one sandbox per (chain, version), pip install inside      - the spec's shape
  B  one sandbox per version, chains sequential inside         - reuse
  C  one sandbox from a snapshot with the package baked in     - no install

Budget: hard cap on total sandboxes; every sandbox deleted in a finally; a
sweeper at the end reports anything left behind.

Usage:  python spikes/s1_daytona.py            (full run)
        python spikes/s1_daytona.py --quick    (A only, small n)
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "spikes" / "out" / "s1_daytona.json"

load_dotenv(ROOT.parent / ".env")
load_dotenv(ROOT / ".env")

PACKAGE = "perseus-client"
VERSIONS = ["1.0.0rc15", "1.0.0rc16", "1.0.0rc19"]
BUDGET = 60          # hard cap on sandboxes created by this spike
created = 0

# A realistic structural probe: import the package and introspect a few names.
PROBE = """
import importlib, inspect, json
out = {}
for sym in ["perseus_client", "perseus_client.PerseusClient"]:
    mod, _, attr = sym.rpartition(".")
    try:
        m = importlib.import_module(mod) if mod else importlib.import_module(sym)
        obj = getattr(m, attr) if mod else m
        out[sym] = {"exists": True}
    except Exception as e:
        out[sym] = {"exists": False, "err": type(e).__name__}
print(json.dumps(out))
"""


def cfg():
    from daytona import DaytonaConfig
    c = DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"])
    if os.getenv("DAYTONA_API_URL"):
        c.api_url = os.environ["DAYTONA_API_URL"]
    if os.getenv("DAYTONA_TARGET"):
        c.target = os.environ["DAYTONA_TARGET"]
    return c


def budget_ok() -> bool:
    return created < BUDGET


async def one_sandbox(daytona, version: str, n_exec: int = 1,
                      snapshot: str | None = None) -> dict:
    """Create, install, exec n times, delete. Every phase timed independently."""
    global created
    from daytona import CreateSandboxFromSnapshotParams

    rec: dict = {"version": version, "n_exec": n_exec, "snapshot": snapshot,
                 "create": None, "install": None, "exec": [], "delete": None,
                 "error": None, "install_ok": None}
    sandbox = None
    try:
        if not budget_ok():
            rec["error"] = "BudgetExhausted"
            return rec
        created += 1

        params = CreateSandboxFromSnapshotParams(language="python", ephemeral=True)
        if snapshot:
            params.snapshot = snapshot

        t = time.perf_counter()
        sandbox = await daytona.create(params, timeout=180)
        rec["create"] = time.perf_counter() - t

        if not snapshot:
            t = time.perf_counter()
            r = await sandbox.process.exec(
                f'pip install --quiet "{PACKAGE}=={version}"', timeout=180)
            rec["install"] = time.perf_counter() - t
            rec["install_ok"] = r.exit_code == 0
            if r.exit_code != 0:
                rec["error"] = "InstallFailed"
                rec["install_tail"] = (r.result or "")[-300:]
                return rec
        else:
            rec["install_ok"] = True

        for _ in range(n_exec):
            t = time.perf_counter()
            r = await sandbox.process.code_run(PROBE, timeout=45)
            rec["exec"].append(time.perf_counter() - t)
            rec.setdefault("exec_exit", []).append(r.exit_code)

        return rec

    except Exception as e:
        rec["error"] = type(e).__name__
        rec["error_msg"] = str(e)[:200]
        return rec
    finally:
        if sandbox is not None:
            try:
                t = time.perf_counter()
                await daytona.delete(sandbox)
                rec["delete"] = time.perf_counter() - t
            except Exception as e:
                rec["delete_error"] = type(e).__name__


async def strategy_a(concurrency: int, n: int, shared_client: bool = False) -> dict:
    """One sandbox per unit of work, `concurrency` of them in flight.

    `shared_client` toggles the variable the 09-09 runner got wrong by default:
    it constructs a fresh AsyncDaytona (and therefore a fresh connection pool)
    inside every chain task - 72 clients for 72 chains. A shared client is the
    obvious alternative and the prime suspect behind the connection timeouts.
    """
    from daytona import AsyncDaytona

    sem = asyncio.Semaphore(concurrency)

    if shared_client:
        async with AsyncDaytona(cfg()) as d:
            async def guarded(i):
                async with sem:
                    return await one_sandbox(d, VERSIONS[i % len(VERSIONS)])
            t = time.perf_counter()
            recs = await asyncio.gather(*[guarded(i) for i in range(n)],
                                        return_exceptions=True)
            wall = time.perf_counter() - t
    else:
        async def guarded(i):
            async with sem:
                async with AsyncDaytona(cfg()) as d:
                    return await one_sandbox(d, VERSIONS[i % len(VERSIONS)])
        t = time.perf_counter()
        recs = await asyncio.gather(*[guarded(i) for i in range(n)],
                                    return_exceptions=True)
        wall = time.perf_counter() - t

    out = [r if isinstance(r, dict) else {"error": type(r).__name__} for r in recs]
    return {"strategy": "A-shared" if shared_client else "A",
            "concurrency": concurrency, "n": n, "wall": wall, "records": out}


async def strategy_b(n_chains: int = 8) -> dict:
    """One sandbox per version; chains run sequentially inside it.

    Also tests state safety: chain k writes a marker file, chain k+1 checks
    whether the previous chain's state is visible to it.
    """
    from daytona import AsyncDaytona, CreateSandboxFromSnapshotParams

    async def per_version(version: str) -> dict:
        global created
        rec = {"version": version, "create": None, "install": None,
               "chains": [], "error": None, "state_leak": None, "delete": None}
        sandbox = None
        async with AsyncDaytona(cfg()) as d:
            try:
                if not budget_ok():
                    rec["error"] = "BudgetExhausted"
                    return rec
                created += 1

                t = time.perf_counter()
                sandbox = await d.create(
                    CreateSandboxFromSnapshotParams(language="python", ephemeral=True),
                    timeout=180)
                rec["create"] = time.perf_counter() - t

                t = time.perf_counter()
                r = await sandbox.process.exec(
                    f'pip install --quiet "{PACKAGE}=={version}"', timeout=180)
                rec["install"] = time.perf_counter() - t
                if r.exit_code != 0:
                    rec["error"] = "InstallFailed"
                    return rec

                leaks = 0
                for k in range(n_chains):
                    t = time.perf_counter()
                    r = await sandbox.process.code_run(PROBE, timeout=45)
                    rec["chains"].append({"t": time.perf_counter() - t,
                                          "exit": r.exit_code})
                    # state-safety check: does chain k see chain k-1's marker?
                    chk = await sandbox.process.exec(
                        "test -f /tmp/docrot_marker && echo LEAK || echo CLEAN; "
                        "touch /tmp/docrot_marker", timeout=20)
                    if "LEAK" in (chk.result or ""):
                        leaks += 1
                rec["state_leak"] = leaks
                return rec
            except Exception as e:
                rec["error"] = type(e).__name__
                rec["error_msg"] = str(e)[:200]
                return rec
            finally:
                if sandbox is not None:
                    try:
                        t = time.perf_counter()
                        await d.delete(sandbox)
                        rec["delete"] = time.perf_counter() - t
                    except Exception:
                        pass

    t = time.perf_counter()
    recs = await asyncio.gather(*[per_version(v) for v in VERSIONS],
                                return_exceptions=True)
    wall = time.perf_counter() - t
    out = [r if isinstance(r, dict) else {"error": type(r).__name__} for r in recs]
    return {"strategy": "B", "n_chains": n_chains, "wall": wall, "records": out}


async def strategy_c() -> dict:
    """Snapshot with the package preinstalled - removes the install phase.

    Guarded: snapshot build may not be permitted on this plan. A failure here
    is itself a result, not a crash.
    """
    from daytona import AsyncDaytona, CreateSnapshotParams, Image, Resources

    name = f"docrot-perseus-{VERSIONS[-1].replace('.', '-')}"
    rec: dict = {"strategy": "C", "snapshot": name, "build": None,
                 "error": None, "records": []}

    async with AsyncDaytona(cfg()) as d:
        try:
            img = (Image.debian_slim("3.12")
                   .pip_install([f"{PACKAGE}=={VERSIONS[-1]}"]))
            t = time.perf_counter()
            await d.snapshot.create(
                CreateSnapshotParams(name=name, image=img,
                                     resources=Resources(cpu=1, memory=1)),
                on_logs=lambda s: None, timeout=600)
            rec["build"] = time.perf_counter() - t
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            # snapshot may already exist from a previous run - try using it
            if "already exists" not in str(e).lower():
                return rec

        try:
            recs = await asyncio.gather(*[
                one_sandbox(d, VERSIONS[-1], snapshot=name) for _ in range(4)
            ], return_exceptions=True)
            rec["records"] = [r if isinstance(r, dict) else {"error": type(r).__name__}
                              for r in recs]
        except Exception as e:
            rec["error"] = (rec["error"] or "") + f" | use: {type(e).__name__}"
    return rec


async def sweep() -> dict:
    """Anything left behind is a leak - the spec's §14 failure mode."""
    from daytona import AsyncDaytona
    try:
        async with AsyncDaytona(cfg()) as d:
            live = [s async for s in d.list()]
            return {"live_sandboxes": len(live),
                    "ids": [getattr(s, "id", "?") for s in live][:20]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:150]}"}


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1))))
    return round(xs[k], 2)


def summarize(run: dict) -> dict:
    recs = run.get("records", [])
    ok = [r for r in recs if not r.get("error")]
    creates = [r.get("create") for r in recs if r.get("create")]
    installs = [r.get("install") for r in recs if r.get("install")]
    execs = [t for r in recs for t in (r.get("exec") or [])]
    errs: dict[str, int] = {}
    for r in recs:
        if r.get("error"):
            errs[r["error"]] = errs.get(r["error"], 0) + 1
    return {
        "n": len(recs), "ok": len(ok),
        "error_rate": round(1 - len(ok) / max(len(recs), 1), 3),
        "errors": errs,
        "wall": round(run.get("wall", 0), 1),
        "create_p50": pct(creates, 50), "create_p95": pct(creates, 95),
        "install_p50": pct(installs, 50), "install_p95": pct(installs, 95),
        "exec_p50": pct(execs, 50), "exec_p95": pct(execs, 95),
        "create_mean": round(statistics.mean(creates), 2) if creates else None,
        "install_mean": round(statistics.mean(installs), 2) if installs else None,
    }


async def main():
    quick = "--quick" in sys.argv
    results: dict = {"package": PACKAGE, "versions": VERSIONS, "runs": []}

    # (concurrency, n, shared_client)
    plan = ([(1, 3, False), (8, 8, False), (8, 8, True)] if quick else
            [(1, 3, False), (4, 8, False), (8, 8, False),
             (8, 8, True), (16, 16, True)])

    for conc, n, shared in plan:
        if not budget_ok():
            print(f"  budget exhausted before A c={conc}")
            break
        tag = "A-shared" if shared else "A"
        print(f"{tag}: concurrency={conc} n={n} ...", flush=True)
        run = await strategy_a(conc, n, shared_client=shared)
        run["summary"] = summarize(run)
        print("   ", json.dumps(run["summary"]), flush=True)
        results["runs"].append(run)

    if not quick and budget_ok():
        print("B: one sandbox per version, 8 chains sequential ...", flush=True)
        b = await strategy_b()
        chain_ts = [c["t"] for r in b["records"] for c in r.get("chains", [])]
        b["summary"] = {
            "wall": round(b["wall"], 1),
            "sandboxes": len(b["records"]),
            "errors": [r.get("error") for r in b["records"] if r.get("error")],
            "create_p50": pct([r.get("create") for r in b["records"]], 50),
            "install_p50": pct([r.get("install") for r in b["records"]], 50),
            "chain_exec_p50": pct(chain_ts, 50),
            "state_leaks": [r.get("state_leak") for r in b["records"]],
        }
        print("   ", json.dumps(b["summary"]), flush=True)
        results["runs"].append(b)

    if not quick and budget_ok():
        print("C: snapshot with package preinstalled ...", flush=True)
        c = await strategy_c()
        c["summary"] = summarize(c) | {"build": c.get("build"),
                                       "error": c.get("error")}
        print("   ", json.dumps(c["summary"], default=str), flush=True)
        results["runs"].append(c)

    results["sandboxes_created"] = created
    results["sweep"] = await sweep()
    print("sweep:", json.dumps(results["sweep"]), flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nwrote {OUT}  ({created} sandboxes created)")


if __name__ == "__main__":
    asyncio.run(main())
