"""Symbols a snippet actually calls, via Python's ast.

The hard part is not finding names, it is *not* emitting the wrong ones. A
naive attribute walk yields `graph.entities` for a local loop variable and
`len` for a builtin; the probe then tries to import a module called `graph`
and reports a failure that says nothing about the documentation.

So only names whose root is genuinely importable in the snippet are emitted:
imported modules and aliases, and nothing that is locally bound or builtin.
"""
from __future__ import annotations

import ast
import builtins
import re

from ..models import Snippet
from . import csharp, javascript

BUILTINS = set(dir(builtins))
PIP_PKG = re.compile(r"^[A-Za-z][\w.\-]*(\[[\w,\-]+\])?(==[\w.\-]+)?$")
ENV_ASSIGN = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\s*=")


def annotate(snippets: list[Snippet], import_name: str | list[str] = "",
             npm_packages: list[str] | None = None) -> None:
    """Fill each snippet's `symbols`.

    `npm_packages` names the scan's npm packages. Without it nothing here
    changes: JS stays symbol-less and a shell block yields only its `pip:` and
    `env:` tokens, which is what every Python-only scan relied on.
    """
    names = [import_name] if isinstance(import_name, str) else list(import_name)
    names = [n for n in names if n]
    npm = [n for n in (npm_packages or []) if n]
    for s in snippets:
        if s.lang in ("python", "py", "python3"):
            s.symbols = _python(s.code, names)
        elif s.lang in ("bash", "sh", "shell", "console") or (npm and s.lang == "zsh"):
            s.symbols = sorted({*_shell(s.code), *(_npm_shell(s.code, npm) if npm else ())})
        elif npm and javascript.is_js(s.lang):
            s.symbols = _javascript(s.code, npm)
        elif csharp.is_csharp(s.lang):
            s.symbols = _csharp(s.code, names)
        else:
            s.symbols = []


def _bindings(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    """(importable_root -> dotted origin, locally bound names)."""
    imported: dict[str, str] = {}
    local: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                imported[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                local |= _targets(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            local |= _targets(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            local |= _targets(node.target)
        elif isinstance(node, ast.comprehension):
            local |= _targets(node.target)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            local |= _targets(node.optional_vars)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local.add(node.name)
            args = node.args
            for param in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                local.add(param.arg)
        elif isinstance(node, ast.ClassDef):
            local.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)

    # an imported name that is later reassigned is no longer a reliable root
    return {k: v for k, v in imported.items() if k not in local}, local


def _targets(node) -> set[str]:
    out: set[str] = set()
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            out |= _targets(e)
    elif isinstance(node, ast.Starred):
        out |= _targets(node.value)
    return out


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _python(code: str, import_names: str | list[str]) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    imported, local = _bindings(tree)
    found: set[str] = set()

    # the modules themselves are worth probing - a dead import is real rot
    for origin in imported.values():
        found.add(origin)

    for node in ast.walk(tree):
        name = ""
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
        elif isinstance(node, ast.Attribute):
            name = _dotted(node)
        if not name:
            continue

        root = name.split(".")[0]
        if root in local or root in BUILTINS:
            continue                       # local variable or builtin, not a symbol
        if root not in imported:
            continue                       # unresolvable root - do not guess
        if "." in name:
            found.add(imported[root] + name[len(root):])
        else:
            found.add(imported[root])

    names = [import_names] if isinstance(import_names, str) else import_names
    names = [n for n in names if n]
    if names:
        # the root must BE a target package: `startswith` alone let
        # `pydantic_extra_types` through as part of `pydantic`
        found = {f for f in found if f.split(".")[0] in names}
    return sorted(found)


def _shell(code: str) -> list[str]:
    out: set[str] = set()
    for line in code.splitlines():
        m = re.match(r"\s*(?:\$\s*)?(?:pip3?|uv pip)\s+install\s+(.+)", line)
        if m:
            for tok in m.group(1).split():
                tok = tok.strip("\"'")
                if tok.startswith("-") or not PIP_PKG.match(tok):
                    continue
                out.add(f"pip:{tok}")
    for m in ENV_ASSIGN.finditer(code):
        out.add(f"env:{m.group(1)}")
    return sorted(out)


def _csharp(code: str, import_names: list[str]) -> list[str]:
    """Namespaces and qualified names a C# snippet uses, kept only when they
    fall under a target package's root namespace (`Duende.IdentityServer`
    keeps `Duende.IdentityServer.Models`, drops `Duende.Bff`)."""
    used = csharp.namespaces_used(code)
    if not import_names:
        return used
    return [u for u in used
            if any(u == n or u.startswith(n + ".") for n in import_names)]


def _javascript(code: str, npm: list[str]) -> list[str]:
    """`<specifier>.<name>` for what a JS/TS snippet takes from a target
    package, and the bare specifier for a namespace import (`import * as s`).

    The specifier stays whole - `momentic/server.start`, not `momentic.start` -
    because a subpath is a different module with its own exports. Other
    packages and Node builtins are not symbols: there is nothing of ours in them.
    """
    found: set[str] = set()
    for use in javascript.uses(code):
        if javascript.package_of(use.specifier) not in npm:
            continue
        found.add(use.specifier if use.name == "*" else f"{use.specifier}.{use.name}")
    return sorted(found)


# A subcommand is a plain lowercase word. `run.yaml`, `./flows`, URLs and
# `Makefile` are arguments, and probing the CLI for them would report a
# subcommand that was never documented.
SUBCOMMAND = re.compile(r"^[a-z][a-z0-9:-]*$")


def _npm_shell(code: str, npm: list[str]) -> set[str]:
    """`npm:<pkg>[@version]` for every install line and `cli:<bin> [<sub>]` for
    each run of a target package's CLI.

    Every installed package is listed, not just the targets: attribution and
    the tier rule both need to see that a block installs something else."""
    out = {f"npm:{pkg}@{ver}" if ver else f"npm:{pkg}" for pkg, ver in javascript.installs(code)}
    for inv in javascript.invocations(code, npm):
        out.add(f"cli:{inv.bin}")
        if inv.args and SUBCOMMAND.match(inv.args[0]):
            out.add(f"cli:{inv.bin} {inv.args[0]}")
    return out
