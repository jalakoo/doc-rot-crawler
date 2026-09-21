"""Structural probe generation.

The tier that carries most of the value: no credentials, no network beyond the
install, and it catches the majority of real rot, because API surfaces change
far more often than behaviour does. Measured on two corpora, 68-71% of snippets
are structural-eligible.

Two checks run inside the sandbox:

  1. resolution   - does the symbol still import, and does the signature accept
                    the keywords the snippet actually passes?
  2. declaration  - where reference docs *print* a signature, compare the
                    documented parameter list against the introspected one.

Every probe for a version runs in ONE interpreter, in ONE exec, in ONE sandbox.
Structural probing is pure introspection: it mutates nothing, shares no state,
and needs no ordering, so the per-chain fan-out the runner used to do bought
nothing and cost 40x (spike S1: 853s -> 21.4s).

Five fixes from spike S2, which took the false-positive rate from 59.6% to 0%
without losing a single true positive. Three live here; F1 and F2 live in
`extract/completeness.py` because they are extraction-side:

  F3  Enum classes are compared by MEMBERS, never by signature.
      `inspect.signature(SomeEnum)` is `(*values)` for every Enum ever written,
      so comparing it to documented members is meaningless. (5 FPs)
  F4  A declaration block with no import statement never claimed an import
      path, so "not importable as shown" is not a finding about it. (6 FPs)
  F5  Symbol search descends into class namespaces, so a documented method is
      found as a method rather than reported as a missing module attr. (2 FPs)

And one trap, found the hard way: the payload is embedded as a JSON *string*
parsed inside the sandbox. Interpolating `json.dumps` output straight into
Python source dies with `NameError: name 'false' is not defined` the moment a
value is a boolean or null - the previous probe only survived because its
payload happened to contain none.
"""
from __future__ import annotations

import ast
import json
import re

from ..models import Snippet

IMPORT_LINE = re.compile(r"^\s*(from|import)\s", re.M)


def _callee(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _kwargs_used(code: str) -> dict[str, list[str]]:
    """Keyword arguments the snippet actually passes, by callee name.

    Parsed, not regexed. A regex stopping at the first `)` attributes a nested
    call's keywords to the enclosing function:
    `st.pydeck_chart(pdk.Deck(map_style=..., layers=...))` reported three
    parameters `pydeck_chart` never accepted.
    """
    out: dict[str, list[str]] = {}
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = _callee(node.func)
        if not fn:
            continue
        names = [k.arg for k in node.keywords if k.arg]
        if names:
            out.setdefault(fn, [])
            out[fn].extend(n for n in names if n not in out[fn])
    return out


def build_targets(snippets: list[Snippet], import_name: str) -> list[dict]:
    """One probe descriptor per snippet, ready to be batched into a script."""
    targets = []
    for s in snippets:
        syms = [x for x in s.symbols if not x.startswith(("pip:", "env:"))]
        if import_name and not any(x.split(".")[0] == import_name for x in syms):
            syms.append(import_name)
        claims_import = bool(IMPORT_LINE.search(s.code))
        # A reference page prints a signature with no imports around it. A
        # tutorial imports the package and then defines its own helpers - those
        # names live in the snippet, not in the package, so looking them up
        # reports `load_data()` and `long_running_function()` as missing API.
        declares = ([] if claims_import
                    else (s.declares if s.kind == "declaration" else []))
        # A decorated definition is the docs' own example code. Reference pages
        # display an undecorated signature; a tutorial writes
        # `@st.cache_data def load_data(nrows)`, whose name lives in the
        # snippet and was being reported as missing package API.
        declares = [d for d in declares if not d.get("decorated")]
        targets.append({
            "id": s.id,
            "symbols": syms,
            "kwargs": _kwargs_used(s.code),
            "declares": declares,
            # F4: only a snippet that actually imports a name can be wrong
            # about where that name lives.
            "claims_import": claims_import,
        })
    return targets


MARKER = "---DOCROT---"


def batched_script(targets: list[dict], import_name: str) -> str:
    """Every probe for one version, in one script.

    Each probe is isolated in its own try/except, so one exploding probe cannot
    take the batch with it - the property that made per-snippet execs feel
    necessary in the first place.
    """
    payload = json.dumps({"targets": targets, "package": import_name})
    return f'''
import importlib, inspect, json, enum, pkgutil, traceback

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


_seen_cache = {{}}


def find_anywhere(name):
    """F5: search module AND class namespaces. A documented method resolved
    only against modules looks like a removed attribute."""
    if name in _seen_cache:
        return _seen_cache[name]
    res = ("", "")
    try:
        root = importlib.import_module(package)
        seen, queue = set(), [root]
        # Packages that load submodules lazily (pydantic.json_schema) do not
        # list them in dir(), so walk the package path as well. Private
        # modules and optional-dependency import errors are skipped.
        for info in pkgutil.walk_packages(getattr(root, "__path__", []), package + "."):
            if len(queue) > 120:
                break
            if any(part.startswith("_") for part in info.name.split(".")[1:]):
                continue
            try:
                queue.append(importlib.import_module(info.name))
            except Exception:
                continue
        while queue:
            mod = queue.pop(0)
            mname = getattr(mod, "__name__", "")
            if mname in seen:
                continue
            seen.add(mname)
            try:
                found = hasattr(mod, name)
            except Exception:        # a module __getattr__ that raises ImportError
                found = False
            if found:
                res = ("%s.%s" % (mname, name), "module")
                break
            hit = False
            for attr in dir(mod):
                if attr.startswith("_"):
                    continue
                try:
                    sub = getattr(mod, attr)
                except Exception:
                    continue
                if inspect.ismodule(sub) and \\
                        getattr(sub, "__name__", "").startswith(package):
                    queue.append(sub)
                elif inspect.isclass(sub) and hasattr(sub, name):
                    res = ("%s.%s.%s" % (mname, attr, name), "method")
                    hit = True
                    break
            if hit or len(seen) > 150:
                break
    except Exception:
        pass
    _seen_cache[name] = res
    return res


def probe_one(t):
    out, failed, notes = {{}}, [], []

    # --- 1. resolution + keywords actually passed -------------------------
    for sym in t["symbols"]:
        try:
            obj = resolve(sym)
            rec = {{"exists": True}}
            try:
                sig = inspect.signature(obj)
                rec["sig"] = str(sig)
                short = sym.split(".")[-1]
                anykw = any(p.kind == p.VAR_KEYWORD
                            for p in sig.parameters.values())
                for name in t["kwargs"].get(short, []):
                    if name not in sig.parameters and not anykw:
                        rec.setdefault("bad_kwargs", []).append(name)
                        failed.append(
                            "%s() has no parameter '%s' -- signature is %s"
                            % (short, name, sig))
            except (TypeError, ValueError):
                pass
            out[sym] = rec
        except Exception as e:
            where, kind = find_anywhere(sym.split(".")[-1])
            if kind == "method":
                out[sym] = {{"exists": True, "as_method": where}}
                notes.append("%s is a method (%s), not a module attribute"
                             % (sym, where))
            else:
                out[sym] = {{"exists": False, "err": type(e).__name__,
                             "msg": str(e)[:200]}}
                failed.append("%s: %s: %s"
                              % (sym, type(e).__name__, str(e)[:160]))

    # --- 2. documented signature vs introspected signature ----------------
    for d in t["declares"]:
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
                # F4: a declaration never claimed an import path
                if t["claims_import"]:
                    out["declared:" + name] = {{"exists": True, "moved_to": where}}
                    failed.append("documented %s is imported from the package "
                                  "root but lives at %s" % (name, where))
                else:
                    out["declared:" + name] = {{"exists": True, "located": where}}
                    notes.append("%s lives at %s" % (name, where))
                continue
            out["declared:" + name] = {{"exists": False}}
            failed.append("documented %s() does not exist in this release" % name)
            continue

        # F3: Enum signature comparison is meaningless - compare members.
        if inspect.isclass(obj) and issubclass(obj, enum.Enum):
            documented = list(d["params"])
            actual = list(obj.__members__)
            missing = [p for p in documented if p not in actual]
            rec = {{"exists": True, "enum": True,
                    "documented": documented, "actual": actual}}
            if missing:
                rec["missing"] = missing
                failed.append("%s: documented member(s) %s do not exist -- "
                              "actual members are %s"
                              % (name, ", ".join(missing), actual))
            out["declared:" + name] = rec
            continue

        try:
            sig = inspect.signature(obj)
        except (TypeError, ValueError):
            continue

        actual = [p for p in sig.parameters if p not in ("self", "cls")]
        # `*args` / `**data` are catch-alls whose names callers never type, and
        # bare `*` or `/` are separators, not parameters. Pydantic documents
        # `__init__(**data)` over a `(*args, **kwargs)` implementation.
        documented = [p for p in d["params"]
                      if p not in ("self", "cls", "/") and not p.startswith("*")]

        # Nothing printed means nothing to compare against.
        if not documented:
            out["declared:" + name] = {{"exists": True, "actual": actual,
                                        "no_documented_params": True,
                                        "sig": str(sig)}}
            notes.append("%s: docs printed no parameter list" % name)
            continue

        missing = [p for p in documented if p not in actual]
        added = [p for p in actual if p not in documented
                 and sig.parameters[p].default is inspect.Parameter.empty]

        rec = {{"exists": True, "documented": documented, "actual": actual,
                "sig": str(sig), "target": target}}
        if missing:
            rec["missing"] = missing
            failed.append("%s(): docs document parameter(s) %s that do not "
                          "exist -- actual signature is %s"
                          % (name, ", ".join(missing), sig))
        if added:
            # A required parameter the docs omit is a completeness gap, not a
            # page that has rotted. Reported as a note, never as a failure -
            # this branch alone produced 9 of the 28 false positives.
            rec["undocumented_required"] = added
            notes.append("%s(): required parameter(s) %s are not documented"
                         % (name, ", ".join(added)))
        out["declared:" + name] = rec

    return {{"probe": out, "failed": failed, "notes": notes}}


results = {{}}
for t in targets:
    try:
        results[t["id"]] = probe_one(t)
    except Exception:
        results[t["id"]] = {{"probe": {{}}, "notes": [],
                             "failed": ["probe crashed: "
                                        + traceback.format_exc()[-300:]]}}

print({MARKER!r})
print(json.dumps(results))
'''.strip()


def parse_batched(stdout: str) -> dict:
    """Pull the probe payload out of a sandbox's stdout.

    `pip` and imports write to the same stream, so the payload is delimited
    rather than assumed to start at the first brace.
    """
    idx = (stdout or "").rfind(MARKER)
    if idx < 0:
        raise ValueError("probe marker missing from sandbox output")
    return json.loads(stdout[idx + len(MARKER):].strip())
