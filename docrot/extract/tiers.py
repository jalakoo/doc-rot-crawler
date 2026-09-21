"""Deterministic tier assignment.

Used on its own when no extraction LLM is configured, and as the prior the
model is allowed to override when one is. Getting this right without a model
matters: it is what keeps the pipeline runnable with zero API keys.
"""
from __future__ import annotations

import re

from ..models import Snippet, Tier
from .completeness import classify

PLACEHOLDER = re.compile(
    r"(your[-_ ]?\w*(key|token|secret|password)|<[a-z-]+>|xxx+|\.\.\.$|path/to/|"
    r"example\.com|localhost:\d+|sk-\w{3,}|api[-_]?key\s*=\s*[\"'](?!os\.)[^\"']{3,})",
    re.I | re.M)
NETWORK = re.compile(
    r"\b(requests\.|httpx\.|urllib|aiohttp|\.post\(|\.get\(|curl |wget )", re.I)
CREDS = re.compile(r"\b([A-Z][A-Z0-9_]*_(?:API_)?(?:KEY|TOKEN|SECRET|PASSWORD))\b")
PROSE_LANGS = {"text", "mermaid", "diff", "json", "yaml", "yml", "toml",
               "html", "xml", "cypher", "sparql", "sql", ""}


def assign(snippets: list[Snippet], available_creds: set[str],
           import_name: str | list[str] = "",
           assume_credentialed: bool = False) -> None:
    names = [import_name] if isinstance(import_name, str) else list(import_name)
    for s in snippets:
        s.tier = _tier(s, available_creds, names, assume_credentialed)


def _tier(s: Snippet, creds: set[str], import_names: list[str] | None = None,
          assume_credentialed: bool = False) -> Tier:
    import_names = [n for n in (import_names or []) if n]
    if s.lang in PROSE_LANGS:
        return "unverifiable"

    if s.lang in ("bash", "sh", "shell", "console"):
        return "execute" if _pure_install(s.code) else "unverifiable"

    if s.lang not in ("python", "py", "python3"):
        return "unverifiable"

    kind, decls = classify(s.code)
    s.kind = kind
    s.declares = decls

    # A signature on display is compared against introspection, never run.
    if kind == "declaration":
        return "structural"

    # A fragment references names it never binds. Running it produces a
    # NameError about the excerpt, not about the documentation.
    if kind == "fragment":
        return "structural" if s.symbols else "unverifiable"

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
    if any(sym.split(".")[0] == import_name for sym in s.symbols):
        return True
    return bool(re.search(rf"\b(?:import|from)\s+{re.escape(import_name)}\b", s.code))


# A shell snippet only earns the `execute` tier when every line installs named
# packages from the registry. Anything referencing the docs' own working tree
# (`-r requirements.txt`, `pip install .`) or a service we do not run (docker,
# make) would fail in a bare sandbox for reasons that say nothing about the
# documentation - reporting those as rot is a false positive.
INSTALL_LINE = re.compile(
    r"^\s*(?:\$\s*)?(?:pip3?|uv pip|python -m pip)\s+install\s+(?P<args>.+?)\s*$")
LOCAL_TARGET = re.compile(r"(^-r\b|^--requirement\b|^-e\b|^\.$|^\.\.?/|/|\.txt$)")


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
