"""S2b - do the five identified fixes actually eliminate the false positives?

S2 classified all 28 false-positive results into five buckets and asserted each
was mechanically fixable. Asserting is not measuring. This re-runs the same 21
failing snippets against the same three releases with a patched extractor and a
patched probe, and reports what survives.

The five fixes:

  F1  declaration parser splits parameters at bracket depth 0, so
      `metadata: Optional[Dict[str, Any]] = None` stops yielding a parameter
      called `Any]]`
  F2  a documented `class X(BaseModel)` block contributes its annotated fields
      as documented parameters, instead of an empty list that makes every real
      field look undocumented
  F3  Enum subclasses are exempt from signature comparison - `(*values)` is an
      artifact of every Enum, not an API surface
  F4  a declaration snippet that contains no import statement never claimed an
      import path, so "not importable as shown" is not a finding about it
  F5  symbol search descends into class namespaces, so a documented method is
      found as a method rather than reported as a missing module attribute

Run:  python spikes/s2b_probe_fixed.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "data" / "runs" / "latest"
OUT = ROOT / "spikes" / "out" / "s2b_probe_fixed.json"

load_dotenv(ROOT.parent / ".env")
load_dotenv(ROOT / ".env")

PACKAGE = "perseus-client"
IMPORT_NAME = "perseus_client"
VERSIONS = ["1.0.0rc15", "1.0.0rc16", "1.0.0rc19"]


# ---------------------------------------------------------------------------
# F1 + F2 - extractor side
# ---------------------------------------------------------------------------
def split_params(text: str) -> list[str]:
    """Split a parameter list on commas at bracket depth 0."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [p.strip() for p in out if p.strip()]


def headers_from_text(code: str) -> list[dict]:
    """F1: bracket-aware parameter splitting."""
    out = []
    for m in re.finditer(
            r"^[ \t]*(?:async\s+)?def\s+(\w+)\s*\(", code, re.M):
        # walk forward to the matching close paren rather than trusting [^)]*
        i, depth = m.end() - 1, 0
        while i < len(code):
            if code[i] in "([{":
                depth += 1
            elif code[i] in ")]}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        inner = code[m.end():i]
        tail = code[i + 1:i + 200]
        ret = ""
        rm = re.match(r"\s*->\s*([^:\n]+):", tail)
        if rm:
            ret = rm.group(1).strip()
        params = []
        for raw in split_params(inner):
            if raw in ("self", "cls"):
                continue
            params.append(raw.split(":")[0].split("=")[0].strip())
        out.append({"name": m.group(1), "params": params,
                    "returns": ret, "kind": "function"})
    return out


def class_fields(node: ast.ClassDef) -> list[str]:
    """F2: annotated class attributes are the documented field list."""
    fields = []
    for sub in node.body:
        if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
            fields.append(sub.target.id)
        elif isinstance(sub, ast.Assign):
            for t in sub.targets:
                if isinstance(t, ast.Name):
                    fields.append(t.id)
    return fields


def declares_for(code: str) -> list[dict]:
    """Recompute `declares` with F1 + F2 applied."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return headers_from_text(code)

    decls: list[dict] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = n.args
            params = [p.arg for p in (*a.posonlyargs, *a.args)]
            params += [p.arg for p in a.kwonlyargs]
            decls.append({"name": n.name,
                          "params": [p for p in params if p not in ("self", "cls")],
                          "returns": ast.unparse(n.returns) if n.returns else "",
                          "kind": "function"})
        elif isinstance(n, ast.ClassDef):
            bases = [ast.unparse(b) for b in n.bases]
            decls.append({"name": n.name, "params": class_fields(n),
                          "returns": "", "kind": "class",
                          "is_enum": any("Enum" in b for b in bases)})
    return decls


# ---------------------------------------------------------------------------
# F3 + F4 + F5 - probe side
# ---------------------------------------------------------------------------
def probe_script(symbols: list[str], kwargs: dict, declared: list[dict],
                 package: str, claims_import: bool) -> str:
    # Embed as a JSON *string* parsed at runtime. Interpolating json.dumps()
    # output directly into Python source breaks the moment a value is a
    # boolean or null: `false` and `null` are not Python literals.
    payload = json.dumps({"symbols": symbols, "kwargs": kwargs,
                          "declared": declared, "package": package,
                          "claims_import": claims_import})
    return f'''
import importlib, inspect, json, enum

_cfg = json.loads({payload!r})
symbols       = _cfg["symbols"]
kwargs        = _cfg["kwargs"]
declared      = _cfg["declared"]
package       = _cfg["package"]
claims_import = _cfg["claims_import"]

out, failed, notes = {{}}, [], []


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


def find_anywhere(name):
    """F5: descend into class namespaces too, so a documented method is found
    as a method rather than reported as a missing module attribute."""
    if not package:
        return "", ""
    try:
        root = importlib.import_module(package)
    except Exception:
        return "", ""
    seen, queue = set(), [root]
    while queue:
        mod = queue.pop(0)
        mname = getattr(mod, "__name__", "")
        if mname in seen:
            continue
        seen.add(mname)
        if hasattr(mod, name):
            return "%s.%s" % (mname, name), "module"
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
                return "%s.%s.%s" % (mname, attr, name), "method"
        if len(seen) > 60:
            break
    return "", ""


# --- 1. resolution + keywords actually passed -------------------------------
for sym in symbols:
    try:
        obj = resolve(sym)
        rec = {{"exists": True}}
        try:
            sig = inspect.signature(obj)
            rec["sig"] = str(sig)
            short = sym.split(".")[-1]
            accepts_any = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
            for name in kwargs.get(short, []):
                if name not in sig.parameters and not accepts_any:
                    rec.setdefault("bad_kwargs", []).append(name)
                    failed.append("%s() has no parameter '%s' -- signature is %s"
                                  % (short, name, sig))
        except (TypeError, ValueError):
            pass
        out[sym] = rec
    except Exception as e:
        where, kind = find_anywhere(sym.split(".")[-1])
        if kind == "method":
            # F5: documented as a bare name but really a method - the docs are
            # not wrong about its existence, only about its scope
            out[sym] = {{"exists": True, "as_method": where}}
            notes.append("%s is a method (%s), not a module attribute" % (sym, where))
        else:
            out[sym] = {{"exists": False, "err": type(e).__name__, "msg": str(e)[:200]}}
            failed.append("%s: %s: %s" % (sym, type(e).__name__, str(e)[:160]))


# --- 2. documented signature vs introspected signature ----------------------
for d in declared:
    name = d["name"]
    target, obj = None, None
    for cand in ([package + "." + name] if package else []) + [name]:
        try:
            obj = resolve(cand)
            target = cand
            break
        except Exception:
            continue

    if obj is None:
        where, kind = find_anywhere(name)
        if where and kind == "method":
            out["declared:" + name] = {{"exists": True, "as_method": where}}
            notes.append("documented %s() is a method at %s" % (name, where))
            continue
        if where:
            # F4: a declaration block never claimed an import path. Only a
            # snippet that actually imports the name can be wrong about where
            # it lives.
            if claims_import:
                out["declared:" + name] = {{"exists": True, "moved_to": where}}
                failed.append("documented %s is imported from the package root "
                              "but lives at %s" % (name, where))
            else:
                out["declared:" + name] = {{"exists": True, "located": where}}
                notes.append("%s lives at %s" % (name, where))
            continue
        out["declared:" + name] = {{"exists": False}}
        failed.append("documented %s() does not exist in this release" % name)
        continue

    # F3: Enum signature comparison is meaningless - inspect.signature returns
    # (*values) for every Enum class ever written.
    if inspect.isclass(obj) and issubclass(obj, enum.Enum):
        documented = [p for p in d["params"]]
        actual = [m for m in obj.__members__]
        missing = [p for p in documented if p not in actual]
        rec = {{"exists": True, "enum": True,
                "documented": documented, "actual": actual}}
        if missing:
            rec["missing"] = missing
            failed.append("%s: documented member(s) %s do not exist -- actual "
                          "members are %s" % (name, ", ".join(missing), actual))
        out["declared:" + name] = rec
        continue

    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        continue

    actual = [p for p in sig.parameters if p not in ("self", "cls")]
    documented = [p.lstrip("*") for p in d["params"] if p not in ("self", "cls")]

    # F2: with no documented parameter list at all there is nothing to compare.
    if not documented:
        out["declared:" + name] = {{"exists": True, "no_documented_params": True,
                                    "actual": actual, "sig": str(sig)}}
        notes.append("%s: docs printed no parameter list; nothing to compare" % name)
        continue

    missing = [p for p in documented if p not in actual]
    added = [p for p in actual if p not in documented
             and sig.parameters[p].default is inspect.Parameter.empty]

    rec = {{"exists": True, "documented": documented, "actual": actual,
            "sig": str(sig), "target": target}}
    if missing:
        rec["missing"] = missing
        failed.append("%s(): docs document parameter(s) %s that do not exist -- "
                      "actual signature is %s" % (name, ", ".join(missing), sig))
    if added:
        # still reported, but as a note: an undocumented required parameter is
        # a completeness gap, not a page that has rotted
        rec["undocumented_required"] = added
        notes.append("%s(): required parameter(s) %s are not documented"
                     % (name, ", ".join(added)))
    out["declared:" + name] = rec


print(json.dumps({{"probe": out, "failed": failed, "notes": notes}}, indent=1))
if failed:
    raise SystemExit(1)
'''.strip()


CALL = re.compile(r"(\w+)\s*\(\s*([^)]*)\)", re.S)


def kwargs_used(code: str) -> dict:
    out: dict[str, list[str]] = {}
    for m in CALL.finditer(code):
        fn, args = m.group(1), m.group(2)
        names = re.findall(r"(\w+)\s*=", args)
        if names:
            out.setdefault(fn, [])
            out[fn].extend(n for n in names if n not in out[fn])
    return out


# ---------------------------------------------------------------------------
def cfg():
    from daytona import DaytonaConfig
    return DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"])


async def run_version(version: str, targets: list[dict]) -> list[dict]:
    """One sandbox per version, all snippets probed sequentially inside it.

    Structural probes mutate no state, so reuse is safe here - which is exactly
    the property S1's strategy B measured.
    """
    from daytona import AsyncDaytona, CreateSandboxFromSnapshotParams

    recs = []
    async with AsyncDaytona(cfg()) as d:
        sandbox = None
        try:
            sandbox = await d.create(
                CreateSandboxFromSnapshotParams(language="python", ephemeral=True),
                timeout=180)
            r = await sandbox.process.exec(
                f'pip install --quiet "{PACKAGE}=={version}"', timeout=180)
            if r.exit_code != 0:
                return [{"id": t["id"], "version": version, "status": "install-failed"}
                        for t in targets]

            for t in targets:
                script = probe_script(t["symbols"], t["kwargs"], t["declares"],
                                      IMPORT_NAME, t["claims_import"])
                res = await sandbox.process.code_run(script, timeout=45)
                recs.append({"id": t["id"], "version": version,
                             "status": "pass" if res.exit_code == 0 else "fail",
                             "stderr": (res.result or "")[-3000:]})
            return recs
        except Exception as e:
            return [{"id": t["id"], "version": version, "status": "error",
                     "stderr": f"{type(e).__name__}: {e}"[:300]} for t in targets]
        finally:
            if sandbox is not None:
                try:
                    await d.delete(sandbox)
                except Exception:
                    pass


async def main() -> None:
    results = json.loads((RUN / "results.json").read_text())
    extraction = json.loads((RUN / "extraction.json").read_text())
    snippets = {s["id"]: s for s in extraction["snippets"]}

    failing_ids = sorted(set(r["id"] for r in results if r["status"] == "fail"))
    targets = []
    for sid in failing_ids:
        s = snippets[sid]
        code = s["code"]
        targets.append({
            "id": sid,
            "symbols": [x for x in s.get("symbols", [])
                        if not x.startswith(("pip:", "env:"))],
            "kwargs": kwargs_used(code),
            "declares": (declares_for(code) if s.get("kind") == "declaration" else []),
            "claims_import": bool(re.search(r"^\s*(from|import)\s", code, re.M)),
        })

    print(f"re-probing {len(targets)} previously-failing snippets "
          f"across {len(VERSIONS)} versions ({len(VERSIONS)} sandboxes)\n")

    t0 = time.perf_counter()
    batches = await asyncio.gather(*[run_version(v, targets) for v in VERSIONS],
                                   return_exceptions=True)
    wall = time.perf_counter() - t0

    recs = [r for b in batches if isinstance(b, list) for r in b]

    # --- compare against the old verdicts ---------------------------------
    old = json.loads((ROOT / "spikes" / "out" / "s2_probe_precision.json").read_text())
    verdict = old["snippet_verdicts"]

    before = {(r["id"], r["version"]): r["status"]
              for r in results if r["status"] in ("pass", "fail")}
    changed, still_failing, fp_cleared, tp_kept, tp_lost = [], [], 0, 0, 0

    for r in recs:
        key = (r["id"], r["version"])
        was = before.get(key)
        v = verdict.get(r["id"])
        if was == "fail" and r["status"] == "pass":
            changed.append({**{k: r[k] for k in ("id", "version")},
                            "was": "fail", "now": "pass", "verdict": v})
            if v == "FP":
                fp_cleared += 1
            elif v == "TP":
                tp_lost += 1
        elif was == "fail" and r["status"] == "fail":
            still_failing.append({"id": r["id"], "version": r["version"],
                                  "verdict": v})
            if v == "TP":
                tp_kept += 1

    fp_results_before = sum(1 for r in results if r["status"] == "fail"
                            and verdict.get(r["id"]) == "FP")
    tp_results_before = sum(1 for r in results if r["status"] == "fail"
                            and verdict.get(r["id"]) == "TP")
    fp_after = sum(1 for r in still_failing if r["verdict"] == "FP")

    summary = {
        "wall": round(wall, 1),
        "sandboxes": len(VERSIONS),
        "snippets_reprobed": len(targets),
        "results": len(recs),
        "fp_results_before": fp_results_before,
        "fp_results_after": fp_after,
        "fp_cleared": fp_cleared,
        "tp_results_before": tp_results_before,
        "tp_results_still_failing": tp_kept,
        "tp_lost": tp_lost,
        "fp_rate_before": round(fp_results_before /
                                max(fp_results_before + tp_results_before, 1), 3),
        "fp_rate_after": round(fp_after / max(fp_after + tp_kept, 1), 3),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"summary": summary, "records": recs, "changed": changed,
         "still_failing": still_failing}, indent=2))

    print(json.dumps(summary, indent=2))
    if still_failing:
        print("\nstill failing:")
        for s in still_failing[:15]:
            print(f"  {s['verdict']:<9} {s['id']} @ {s['version']}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
