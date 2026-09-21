"""S1d - strategy D: one sandbox per version, ALL structural probes in a
single exec.

S1 established that a sandbox costs ~1.7s to create and ~2s to install into,
and that the account ceiling is 10 concurrent CPUs. S2b established that
structural probes are pure introspection. Put those together and the whole
per-chain fan-out collapses: every structural probe for a given version can
run inside one interpreter, in one exec, in one sandbox.

That turns 72 sandboxes into 3, and 255 round trips into 3.

Measured against the same corpus the 2026-09-09 run used, so the numbers are
directly comparable to its 853s.

Usage:  python spikes/s1d_batched.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUN = ROOT / "data" / "runs" / "latest"
OUT = ROOT / "spikes" / "out" / "s1d_batched.json"

load_dotenv(ROOT.parent / ".env")
load_dotenv(ROOT / ".env")

from spikes.s2b_probe_fixed import declares_for, kwargs_used  # noqa: E402

PACKAGE, IMPORT_NAME = "perseus-client", "perseus_client"
VERSIONS = ["1.0.0rc15", "1.0.0rc16", "1.0.0rc19"]


def batched_script(targets: list[dict], package: str) -> str:
    """One interpreter, every snippet, one JSON document out.

    Each snippet is isolated in its own try/except so one exploding probe
    cannot take the batch with it - the property that made per-snippet execs
    feel necessary in the first place.
    """
    payload = json.dumps({"targets": targets, "package": package})
    return f'''
import importlib, inspect, json, enum, traceback

_cfg = json.loads({payload!r})
targets = _cfg["targets"]
package = _cfg["package"]

def resolve(dotted):
    mod, _, attr = dotted.rpartition(".")
    if not mod:
        return importlib.import_module(dotted)
    try:
        m = importlib.import_module(mod)
    except ModuleNotFoundError:
        parts = mod.split(".")
        m = importlib.import_module(parts[0])
        for p in parts[1:]:
            m = getattr(m, p)
    return getattr(m, attr)

_cache = {{}}
def find_anywhere(name):
    if name in _cache:
        return _cache[name]
    res = ("", "")
    try:
        root = importlib.import_module(package)
        seen, queue = set(), [root]
        while queue:
            mod = queue.pop(0)
            mname = getattr(mod, "__name__", "")
            if mname in seen:
                continue
            seen.add(mname)
            if hasattr(mod, name):
                res = ("%s.%s" % (mname, name), "module"); break
            hit = False
            for attr in dir(mod):
                if attr.startswith("_"):
                    continue
                try:
                    sub = getattr(mod, attr)
                except Exception:
                    continue
                if inspect.ismodule(sub) and getattr(sub, "__name__", "").startswith(package):
                    queue.append(sub)
                elif inspect.isclass(sub) and hasattr(sub, name):
                    res = ("%s.%s.%s" % (mname, attr, name), "method"); hit = True; break
            if hit:
                break
            if len(seen) > 60:
                break
    except Exception:
        pass
    _cache[name] = res
    return res


def probe_one(t):
    out, failed, notes = {{}}, [], []
    for sym in t["symbols"]:
        try:
            obj = resolve(sym)
            rec = {{"exists": True}}
            try:
                sig = inspect.signature(obj)
                rec["sig"] = str(sig)
                short = sym.split(".")[-1]
                anykw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
                for name in t["kwargs"].get(short, []):
                    if name not in sig.parameters and not anykw:
                        failed.append("%s() has no parameter '%s' -- signature is %s"
                                      % (short, name, sig))
            except (TypeError, ValueError):
                pass
            out[sym] = rec
        except Exception as e:
            where, kind = find_anywhere(sym.split(".")[-1])
            if kind == "method":
                out[sym] = {{"exists": True, "as_method": where}}
                notes.append("%s is a method (%s)" % (sym, where))
            else:
                out[sym] = {{"exists": False, "err": type(e).__name__}}
                failed.append("%s: %s: %s" % (sym, type(e).__name__, str(e)[:160]))

    for d in t["declares"]:
        name = d["name"]
        obj = None
        for cand in ([package + "." + name] if package else []) + [name]:
            try:
                obj = resolve(cand); break
            except Exception:
                continue
        if obj is None:
            where, kind = find_anywhere(name)
            if where and kind == "method":
                notes.append("documented %s() is a method at %s" % (name, where)); continue
            if where:
                if t["claims_import"]:
                    failed.append("documented %s is imported from the package root "
                                  "but lives at %s" % (name, where))
                else:
                    notes.append("%s lives at %s" % (name, where))
                continue
            failed.append("documented %s() does not exist in this release" % name)
            continue
        if inspect.isclass(obj) and issubclass(obj, enum.Enum):
            actual = list(obj.__members__)
            missing = [p for p in d["params"] if p not in actual]
            if missing:
                failed.append("%s: documented member(s) %s do not exist -- actual "
                              "members are %s" % (name, ", ".join(missing), actual))
            continue
        try:
            sig = inspect.signature(obj)
        except (TypeError, ValueError):
            continue
        actual = [p for p in sig.parameters if p not in ("self", "cls")]
        documented = [p.lstrip("*") for p in d["params"] if p not in ("self", "cls")]
        if not documented:
            notes.append("%s: docs printed no parameter list" % name); continue
        missing = [p for p in documented if p not in actual]
        added = [p for p in actual if p not in documented
                 and sig.parameters[p].default is inspect.Parameter.empty]
        if missing:
            failed.append("%s(): docs document parameter(s) %s that do not exist -- "
                          "actual signature is %s" % (name, ", ".join(missing), sig))
        if added:
            notes.append("%s(): required parameter(s) %s are not documented"
                         % (name, ", ".join(added)))
    return {{"probe": out, "failed": failed, "notes": notes}}


results = {{}}
for t in targets:
    try:
        results[t["id"]] = probe_one(t)
    except Exception:
        results[t["id"]] = {{"failed": ["probe crashed: " + traceback.format_exc()[-200:]],
                             "notes": [], "probe": {{}}}}

print("---DOCROT---")
print(json.dumps(results))
'''.strip()


def cfg():
    from daytona import DaytonaConfig
    return DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"])


async def run_version(version: str, targets: list[dict]) -> dict:
    from daytona import AsyncDaytona, CreateSandboxFromSnapshotParams

    rec = {"version": version, "create": None, "install": None,
           "exec": None, "results": 0, "error": None}
    async with AsyncDaytona(cfg()) as d:
        sandbox = None
        try:
            t = time.perf_counter()
            sandbox = await d.create(
                CreateSandboxFromSnapshotParams(language="python", ephemeral=True),
                timeout=180)
            rec["create"] = round(time.perf_counter() - t, 2)

            t = time.perf_counter()
            r = await sandbox.process.exec(
                f'pip install --quiet "{PACKAGE}=={version}"', timeout=180)
            rec["install"] = round(time.perf_counter() - t, 2)
            if r.exit_code != 0:
                rec["error"] = "InstallFailed"
                return rec

            t = time.perf_counter()
            r = await sandbox.process.code_run(batched_script(targets, IMPORT_NAME),
                                               timeout=180)
            rec["exec"] = round(time.perf_counter() - t, 2)

            body = (r.result or "")
            marker = body.find("---DOCROT---")
            if marker < 0:
                rec["error"] = "NoMarker"
                rec["tail"] = body[-400:]
                return rec
            payload = json.loads(body[marker + len("---DOCROT---"):].strip())
            rec["results"] = len(payload)
            rec["failing"] = sum(1 for v in payload.values() if v["failed"])
            rec["payload"] = payload
            return rec
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            return rec
        finally:
            if sandbox is not None:
                try:
                    await d.delete(sandbox)
                except Exception:
                    pass


async def main() -> None:
    extraction = json.loads((RUN / "extraction.json").read_text())
    snippets = [s for s in extraction["snippets"] if s["tier"] != "unverifiable"]

    targets = []
    for s in snippets:
        code = s["code"]
        targets.append({
            "id": s["id"],
            "symbols": [x for x in s.get("symbols", [])
                        if not x.startswith(("pip:", "env:"))],
            "kwargs": kwargs_used(code),
            "declares": (declares_for(code) if s.get("kind") == "declaration" else []),
            "claims_import": bool(re.search(r"^\s*(from|import)\s", code, re.M)),
        })

    print(f"strategy D: {len(targets)} probes x {len(VERSIONS)} versions "
          f"= {len(VERSIONS)} sandboxes, {len(VERSIONS)} execs\n")

    t0 = time.perf_counter()
    recs = await asyncio.gather(*[run_version(v, targets) for v in VERSIONS],
                                return_exceptions=True)
    wall = time.perf_counter() - t0

    recs = [r if isinstance(r, dict) else {"error": type(r).__name__} for r in recs]
    summary = {
        "wall": round(wall, 1),
        "sandboxes": len(VERSIONS),
        "probes_per_version": len(targets),
        "total_probes": len(targets) * len(VERSIONS),
        "per_version": [{k: r.get(k) for k in
                         ("version", "create", "install", "exec", "results",
                          "failing", "error")} for r in recs],
        "baseline_2026_09_09_wall": 853.1,
    }
    ok = [r for r in recs if not r.get("error")]
    if ok:
        summary["speedup_vs_baseline"] = round(853.1 / wall, 1)
        summary["ms_per_probe"] = round(
            sum(r["exec"] for r in ok) / sum(r["results"] for r in ok) * 1000, 1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"summary": summary, "records": recs}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
