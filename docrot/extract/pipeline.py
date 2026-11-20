"""Extraction stage: pages in, validated Extraction out.

Writes extraction.json and stops calling the model. Everything downstream
reads that file, which makes the rest of the pipeline deterministic and
replayable offline.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from ..config import Config
from ..models import Claim, Extraction, PackageRef, Page, Snippet
from . import claims as claims_mod
from . import csharp, fences, javascript, llm, symbols, tiers


def extract(pages: list[Page], packages: Sequence[PackageRef], cfg: Config,
            log=print, assume_credentialed: bool = False,
            head_symbols: set[str] | None = None) -> Extraction:
    if not packages:
        raise ValueError("extract needs at least one package")
    primary = packages[0]
    import_names = [p.import_name for p in packages]

    snips = fences.parse(pages)
    log(f"  {len(snips)} code fences parsed")

    npm = [p.import_name for p in packages if p.ecosystem == "npm" and p.import_name]
    symbols.annotate(snips, import_names, npm_packages=npm)
    tiers.assign(snips, set(cfg.sandbox_env.keys()), import_names,
                 assume_credentialed, npm_packages=npm)
    attribute(snips, packages)
    match_languages(snips, packages)
    prune_declares(snips, head_symbols or set())

    # Lexical claim binding: no embedding service (S4: precision 1.00,
    # recall 0.97). The model, where configured, only adds claims - it never
    # touches tiers (S3).
    found = claims_mod.extract(pages, head_symbols or set())
    corpus = "\n\n".join(p.text for p in pages)
    model_claims = llm.apply(llm.judge(snips, corpus, cfg, log), snips)

    seen = {c.text for c in found}
    fresh = ground(([c for c in model_claims if c.text not in seen]), pages,
                   head_symbols or set())
    claims = found + fresh
    log(f"  {len(claims)} claims bound to symbols"
        + (f" ({len(found)} lexical + {len(claims) - len(found)} model)"
           if model_claims else ""))

    counts: dict[str, int] = {}
    for s in snips:
        counts[s.tier] = counts.get(s.tier, 0) + 1
    log("  tiers: " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    return Extraction(
        package=primary.package, import_name=primary.import_name,
        ecosystem=primary.ecosystem, packages=list(packages),
        pages=pages, snippets=snips, claims=claims,
    )


MIN_OVERLAP = 0.6          # share of a claim's words the page must also use


def ground(claims: list[Claim], pages: list[Page], head_symbols: set[str]) -> list[Claim]:
    """Tie model-written claims back to a real page and real symbols.

    The model is handed one corpus excerpt, so whatever it writes in `page` is
    a guess - usually a page *title*. Ingested as given, those became Page
    nodes keyed by title ("Configuration"), 38 of them in a 77-page scan, and
    the claims hung off pages that do not exist.

    A claim is placed on the page it is actually about: the page quoting the
    sentence, or - because the model paraphrases rather than quotes, measured
    at 0 of 97 verbatim - the page sharing most of its words. Below
    `MIN_OVERLAP` the claim is dropped rather than guessed at. Only symbols the
    code defines survive, the rule the lexical pass already uses.
    """
    index = [(p, " ".join((p.text or "").split())) for p in pages]
    words = [(p, _words(text)) for p, text in index]
    kept = []
    for claim in claims:
        needle = " ".join((claim.text or "").split())
        if not needle:
            continue
        page = next((p for p, text in index if needle in text), None)
        if page is None:
            said = _words(needle)
            scored = [(len(said & seen) / max(1, len(said)), p) for p, seen in words]
            score, page = max(scored, key=lambda x: x[0]) if scored else (0.0, None)
            if page is None or score < MIN_OVERLAP:
                continue                   # not clearly about any page: drop it
        symbols = [s for s in claim.symbols if s.split(".")[-1] in head_symbols] \
            if head_symbols else claim.symbols
        kept.append(claim.model_copy(update={"page": page.url, "symbols": symbols}))
    return kept


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


def prune_declares(snippets: list[Snippet], head_symbols: set[str]) -> None:
    """Keep only documented declarations the code actually defines at HEAD.

    A declaration block with no import is read as a signature on display and
    looked up in the package. On a tutorial-heavy corpus most are the reader's
    own code - Pydantic's `class Model(BaseModel): ...` examples produced
    "documents a function that does not exist" by the dozen. A name defined
    neither at HEAD nor in any tested release was never package API; one that
    exists at HEAD still gets checked against every release, so removed and
    unreleased API keep their findings.

    Without a HEAD symbol table (no repo in the scan) nothing is pruned.
    """
    if not head_symbols:
        return
    tails = {h.rsplit(".", 1)[-1] for h in head_symbols}
    for s in snippets:
        # A C# snippet's declares are its own types, which the .NET lane
        # needs whole: they are what a compiler error cannot be about.
        if not s.declares or csharp.is_csharp(s.lang) or javascript.is_js(s.lang):
            continue
        s.declares = [d for d in s.declares if d.get("name") in tails]
        if s.kind == "declaration" and not s.declares and not s.symbols \
                and s.tier == "structural":
            s.tier = "unverifiable"


def attribute(snippets: list[Snippet], packages: Sequence[PackageRef]) -> None:
    """Which package each snippet is verified against.

    The first package the snippet names wins: an imported symbol root, an
    `import x` line, a `pip install x` token, or - for npm - a module
    specifier, an `npm install x` token or a CLI it runs. A snippet naming none
    of them goes to the primary package, which is what a single-package scan
    always did.
    """
    by_import = {p.import_name: p.package for p in packages}
    by_dist = {_norm(p.package): p.package for p in packages}
    npm = {p.import_name: p.package for p in packages if p.ecosystem == "npm"}
    for s in snippets:
        s.package = _owner(s, by_import, by_dist, npm) or packages[0].package


def _owner(s: Snippet, by_import: dict[str, str], by_dist: dict[str, str],
           npm: dict[str, str] | None = None) -> str:
    npm = npm or {}
    if npm and javascript.is_js(s.lang):
        # Specifiers, not symbol strings: `socket.io.Server` cannot be told
        # apart from `socket` + `io.Server` once it is joined with dots.
        for use in javascript.uses(s.code):
            if (pkg := javascript.package_of(use.specifier)) in npm:
                return npm[pkg]
        return ""
    for sym in s.symbols:
        if sym.startswith("pip:"):
            name = re.split(r"[\[=<>~!]", sym[4:], maxsplit=1)[0]
            if _norm(name) in by_dist:
                return by_dist[_norm(name)]
        elif sym.startswith(("npm:", "cli:")):
            if pkg := _npm_token_owner(sym, npm):
                return pkg
        elif imp := _root(sym, by_import, set(npm)):
            return by_import[imp]
    for imp, pkg in by_import.items():
        if re.search(rf"\b(?:import|from|using)\s+{re.escape(imp)}\b", s.code):
            return pkg
    return ""


def _npm_token_owner(sym: str, npm: dict[str, str]) -> str:
    """The scan's npm package an `npm:<pkg>[@ver]` or `cli:<bin> [sub]` token names."""
    if sym.startswith("npm:"):
        name = sym[4:]
        at = name.find("@", 1)             # a scope's own `@` is not a version
        return npm.get(name[:at] if at > 0 else name, "")
    bin_ = sym[4:].split(" ", 1)[0]
    for imp, pkg in npm.items():
        if bin_ in (imp, imp.rsplit("/", 1)[-1]):
            return pkg
    return ""


# `socket.io` is a package, not an attribute `io` of `socket`
_NAME_TAILS = frozenset({"io", "js", "ts"})


def _root(sym: str, by_import: dict[str, str], npm: set[str] | None = None) -> str:
    """The import name `sym` falls under. .NET root namespaces are dotted and
    nest - `Duende.IdentityServer.EntityFramework` belongs to that package,
    not to `Duende.IdentityServer` - so the longest match wins.

    An npm name also owns its subpaths (`momentic/server.start`), and since its
    own name may hold a dot, `socket.io` is never read as `socket` plus `io`."""
    npm = npm or set()
    hits = []
    for imp in by_import:
        if sym == imp or sym.startswith(imp + "."):
            if imp in npm and sym[len(imp) + 1:].split(".", 1)[0] in _NAME_TAILS:
                continue
            hits.append(imp)
        elif imp in npm and sym.startswith(imp + "/"):
            hits.append(imp)
    return max(hits, key=len) if hits else ""


LANGUAGE = {"pypi": "python", "npm": "javascript", "nuget": "csharp"}


def _family(lang: str) -> str:
    """python / csharp / javascript, or '' for what is neutral (shell, prose)."""
    if csharp.is_csharp(lang):
        return "csharp"
    if lang in ("python", "py", "python3"):
        return "python"
    return "javascript" if javascript.is_js(lang) else ""


def match_languages(snippets: list[Snippet], packages: Sequence[PackageRef]) -> None:
    """A snippet can only be checked against a package in its own language.

    A C# block attributed to a Python package - or a Python block to a NuGet
    one - lands there because it named no package at all. Probing one against
    the other reports nothing true, so it is marked unverifiable instead.

    Shell is neutral, but not free: its lane is its package's ecosystem. A
    block attributed to an npm package must install or run that package, and
    one carrying only npm tokens stays out of a PyPI lane.
    """
    eco = {p.package: p.ecosystem for p in packages}
    npm = {p.import_name: p.package for p in packages if p.ecosystem == "npm"}
    for s in snippets:
        if s.tier == "unverifiable":
            continue
        ecosystem = eco.get(s.package, "pypi")
        want = LANGUAGE.get(ecosystem, "")
        lang = _family(s.lang)
        if lang:
            if lang != want:
                s.tier = "unverifiable"
        elif want == "csharp":
            s.tier = "unverifiable"
        elif want == "javascript":
            if not _names_npm(s, s.package, npm):
                s.tier = "unverifiable"
        elif _names_npm_only(s):
            s.tier = "unverifiable"


def _names_npm(s: Snippet, package: str, npm: dict[str, str]) -> bool:
    return any(_npm_token_owner(sym, npm) == package for sym in s.symbols
               if sym.startswith(("npm:", "cli:")))


def _names_npm_only(s: Snippet) -> bool:
    """npm tokens and no pip token: a block for another ecosystem's lane."""
    npm_side = any(sym.startswith(("npm:", "cli:")) for sym in s.symbols)
    return npm_side and not any(sym.startswith("pip:") for sym in s.symbols)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()
