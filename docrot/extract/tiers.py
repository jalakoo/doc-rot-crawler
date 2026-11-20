"""Deterministic tier assignment.

Used on its own when no extraction LLM is configured, and as the prior the
model is allowed to override when one is. Getting this right without a model
matters: it is what keeps the pipeline runnable with zero API keys.
"""
from __future__ import annotations

import re

from ..models import Snippet, Tier
from . import csharp, fences, javascript
from .commands import Ownership
from .completeness import classify

PLACEHOLDER = re.compile(
    r"(your[-_ ]?\w*(key|token|secret|password)|<[a-z-]+>|xxx+|\.\.\.$|path/to/|"
    r"example\.com|localhost:\d+|sk-\w{3,}|api[-_]?key\s*=\s*[\"'](?!os\.)[^\"']{3,})",
    re.I | re.M)
NETWORK = re.compile(
    r"\b(requests\.|httpx\.|urllib|aiohttp|\.post\(|\.get\(|curl |wget )", re.I)
CREDS = re.compile(r"\b([A-Z][A-Z0-9_]*_(?:API_)?(?:KEY|TOKEN|SECRET|PASSWORD))\b")


def assign(snippets: list[Snippet], available_creds: set[str],
           import_name: str | list[str] = "",
           assume_credentialed: bool = False,
           npm_packages: list[str] | None = None, own: Ownership | None = None) -> None:
    """`npm_packages` names the scan's npm packages. Without it JS/TS is
    `unverifiable` and shell is judged by its pip lines alone, as before. `own`
    adds the commands a standalone CLI target (or a declared npm bin) owns."""
    names = [import_name] if isinstance(import_name, str) else list(import_name)
    npm = [n for n in (npm_packages or []) if n]
    own = own or Ownership(npm)
    for s in snippets:
        s.reason = ""
        s.tier = _tier(s, available_creds, names, assume_credentialed, npm, own)


def skip(s: Snippet, reason: str) -> Tier:
    """Mark `s` unverifiable and record why; the one way a tier is given up."""
    s.reason = reason
    return "unverifiable"


def _tier(s: Snippet, creds: set[str], import_names: list[str] | None = None,
          assume_credentialed: bool = False, npm_names: list[str] | None = None,
          own: Ownership | None = None) -> Tier:
    import_names = [n for n in (import_names or []) if n]
    npm = [n for n in (npm_names or []) if n]
    own = own or Ownership(npm)
    if fences.family(s.lang) == "not-code":
        return skip(s, "not-code")

    if s.lang in ("bash", "sh", "shell", "console") or (own.active and s.lang == "zsh"):
        tier = _shell_tier(s.code, own)
        return skip(s, "no-package-use") if tier == "unverifiable" else tier

    if npm and javascript.is_js(s.lang):
        return _js_tier(s, creds, npm, assume_credentialed)

    if csharp.is_csharp(s.lang):
        # Compiled, never run: the .NET lane checks every C# snippet against
        # each release with the compiler.
        s.kind, s.declares = csharp.classify(s.code)
        return skip(s, "fragment") if s.kind == "fragment" else "structural"

    if s.lang not in fences.PY_LANGS:
        # a JS/TS fence in a scan with no npm package has a lane, just not here;
        # zsh outside an npm scan is shell that runs nothing of ours
        reason = {"javascript": "other-language", "shell": "no-package-use"}
        return skip(s, reason.get(fences.family(s.lang), "no-checker"))

    kind, decls = classify(s.code)
    s.kind = kind
    s.declares = decls

    # A signature on display is compared against introspection, never run.
    if kind == "declaration":
        return "structural"

    # A fragment references names it never binds. Running it produces a
    # NameError about the excerpt, not about the documentation.
    if kind == "fragment":
        return "structural" if s.symbols else skip(s, "fragment")

    needed = set(CREDS.findall(s.code))
    if PLACEHOLDER.search(s.code) or needed or NETWORK.search(s.code):
        # promote only when we actually hold every credential it names
        return "execute" if (needed and needed.issubset(creds)) else "structural"

    # A snippet that drives the target package usually needs the service behind
    # it, and its credentials are read inside the library, so they are
    # invisible here. Holding no keys, the honest tier is `structural`.
    #
    # But this rule is blanket, and on a credential-free target it throws away
    # a third of the available evidence: it drove `execute` to 1 snippet of 131
    # on Perseus, while the measured ceiling is 21.4% there and 35.2% on
    # Streamlit, whose snippets (`import streamlit as st; st.write(...)`) run
    # with no key at all. `--assume-credentialed` opts a target out.
    if not creds and not assume_credentialed \
            and any(_touches(s, n) for n in import_names):
        return "structural"

    return "execute"


def _touches(s: Snippet, import_name: str) -> bool:
    # not `sym.split(".")[0]`: npm names hold dots (`socket.io`) and a subpath
    # is a `/` (`momentic/server.start`), so the root is a prefix, not a segment
    if any(sym == import_name or sym.startswith((import_name + ".", import_name + "/"))
           for sym in s.symbols):
        return True
    return bool(re.search(rf"\b(?:import|from)\s+{re.escape(import_name)}\b", s.code))


def _shell_tier(code: str, own: Ownership) -> Tier:
    """Shell: install blocks run, CLI invocations are probed, the rest is skipped.

    Installing the scan's own packages tells us the documented install works.
    Installing anything else says nothing about these docs, and running a
    documented command is never safe, so the lane asks the CLI's own `--help`
    instead (`structural`) - but only for a command a target really owns
    (`commands.Ownership`): `relate query` is the Rust binary's, not the npm
    package's that merely shares the name.
    """
    if own.npm:
        installed = {pkg for pkg, _ in javascript.installs(code)}
        if installed and installed <= set(own.npm) and javascript.pure_install(code):
            return "execute"
    if own.runs(code):
        return "structural"
    return "execute" if _pure_install(code) else "unverifiable"


# JS cannot be parsed here, so "runs on its own" is argued from what is absent.
# Network needs the service behind it, and a browser global has no browser to
# run in; either would fail for reasons that say nothing about the documentation.
JS_NETWORK = re.compile(
    r"\b(fetch\s*\(|axios\b|https?\.(?:request|get)\s*\(|XMLHttpRequest\b|new\s+WebSocket\b)|\.listen\s*\(")
BROWSER = re.compile(r"(?<![\w$.])(document|window|localStorage|sessionStorage|navigator)\s*\.")
# `</div>`, `<Foo />`, `<>`: a TS generic (`Array<string>`, `<T>(x)`) has none of these
JSX = re.compile(r"</[A-Za-z>]|<[A-Za-z][\w.]*(?:\s[^<>]*)?/>|<>")
JSX_LANGS = {"jsx", "tsx"}


def _js_tier(s: Snippet, creds: set[str], npm: list[str], assume_credentialed: bool) -> Tier:
    """A JS/TS snippet is `structural` when it takes anything from a target
    package, `unverifiable` when it takes nothing (no import of ours to check).
    It earns `execute` only when it looks like a whole program needing nothing
    we lack; where unsure the answer is `structural`."""
    uses = [u for u in javascript.uses(s.code) if javascript.package_of(u.specifier) in npm]
    if not uses:
        return skip(s, "no-package-use")
    code = javascript.mask(s.code)       # a `<div>` or `fetch(` inside a string is not markup or a call
    if s.lang.lower() in JSX_LANGS or JSX.search(code) or BROWSER.search(code):
        return "structural"
    if all(u.type_only for u in uses):
        return "structural"              # types are erased: there is nothing to run
    needed = set(CREDS.findall(s.code))
    if PLACEHOLDER.search(s.code) or needed or JS_NETWORK.search(code):
        return "execute" if (needed and needed.issubset(creds)) else "structural"
    # same blanket rule as Python: a snippet driving the package usually needs
    # the service behind it, with credentials read inside the library
    if not creds and not assume_credentialed:
        return "structural"
    return "execute"


# A shell snippet only earns the `execute` tier when every line installs named
# packages from the registry. Anything referencing the docs' own working tree
# (`-r requirements.txt`, `pip install .`) or a service we do not run (docker,
# make) would fail in a bare sandbox for reasons that say nothing about the
# documentation - reporting those as rot is a false positive.
INSTALL_LINE = re.compile(
    r"^\s*(?:\$\s*)?(?:pip3?|uv pip|python -m pip)\s+install\s+(?P<args>.+?)\s*$")
# `relate_db-<version>-<platform>.whl`: a file the reader has built or downloaded, or a
# placeholder the reader fills in - neither is something the sandbox can fetch.
LOCAL_TARGET = re.compile(r"(^-r\b|^--requirement\b|^-e\b|^\.$|^\.\.?/|/|\.txt$|\.(?:whl|zip|tar\.gz)$|<[^<>\s]+>)")


def _pure_install(code: str) -> bool:
    lines = [ln for ln in code.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return False
    for ln in lines:
        if any(tok in ln for tok in ("&&", ";", "|", "docker", "make ", "cd ")):
            return False
        m = INSTALL_LINE.match(ln)
        if not m:
            return False
        for tok in m.group("args").split():
            tok = tok.strip("\"'")
            if tok.startswith("--"):
                continue
            if LOCAL_TARGET.search(tok):
                return False
    return True


def chains(snippets: list[Snippet]) -> list[list[Snippet]]:
    """Cut the `requires` DAG into chains that must share one sandbox.

    Snippets on the same page run in document order by default: earlier steps
    routinely establish state (an install, a client, a written file) that later
    ones consume.
    """
    by_page: dict[str, list[Snippet]] = {}
    for s in snippets:
        by_page.setdefault(s.page, []).append(s)

    out: list[list[Snippet]] = []
    for group in by_page.values():
        group.sort(key=lambda s: (s.line, s.id))
        runnable = [s for s in group if s.tier != "unverifiable"]
        if runnable:
            out.append(runnable)
    return out
