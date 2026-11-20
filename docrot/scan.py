"""One scan, end to end: acquire, registry, extract, verify, graph, report.

`run_scan` is the pipeline. `execute_scan` wraps it for the store - it records
status, phase and log as the scan runs and saves the finished run - and is the
one code path behind both `docrot scan` and the dashboard's queue.

A scan may span several docs sites and several repos, and a repo may hold
several packages in several ecosystems. Each package the scan names, or that the
docs mention (`mention_scores`), becomes a verification target with its own
ecosystem and releases; snippets are attributed to the package they exercise
(see `extract.pipeline.attribute`).
"""
from __future__ import annotations

import asyncio
import re
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from . import config as cfgmod
from .models import Extraction, Release, Result, Target
from .store import (
    ECOSYSTEMS,
    PHASES,
    ScanOptions,
    ScanRecord,
    ScanStatus,
    Store,
    now,
    split_package,
)

if TYPE_CHECKING:
    from .acquire.repo import Package

# Ecosystems whose versions are picked across major/minor lines, not just the
# newest few. NuGet needs it to find a release old enough to still compile the
# docs. npm needs it because the busy packages (momentic is past 3.74) cut a
# release every few days and neighbours are nearly identical - the newest N
# would all agree, while the newest of each major line is where renamed or
# removed commands and exports actually show. A standalone CLI is the same
# case: Relate cut 1.1.0 and 1.2.0 days apart, and a subcommand is renamed at
# a minor or major boundary, not between patches. PyPI keeps the plain
# newest-N: its docs usually track the current minor.
SPREAD_ECOSYSTEMS = frozenset({"nuget", "npm", "cli"})

# Most packages one repo contributes when the scan names none of them. A .NET
# monorepo ships 28 (Duende's); every package is a sandbox per version tested,
# so the docs' own mentions choose and this bounds the worst case. Packages the
# scan names are never capped.
MAX_AUTO_PACKAGES = 6

Log = Callable[[str], None]


def _tail(name: str) -> str:
    return name.rsplit("/", 1)[-1]


class ScanError(Exception):
    """A scan that cannot proceed for a reason the user can fix."""


@dataclass
class ScanInputs:
    docs: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    options: ScanOptions = field(default_factory=ScanOptions)

    @classmethod
    def of(cls, rec: ScanRecord) -> ScanInputs:
        return cls(list(rec.docs), list(rec.repos), list(rec.packages), rec.options)


@dataclass
class ScanOutcome:
    report: dict
    extraction: Extraction
    results: list[Result]


def style(msg: str) -> str:
    """Log line colour class, as the scan log panel renders it."""
    s = msg.strip()
    if s.startswith("!") or "fail" in msg.lower():
        return "t-hit"
    if "clean" in msg or "complete" in msg:
        return "t-ok"
    if s.startswith("★") or "!" in msg:
        return "t-gold"
    return "t-sys"


def _source_spec(repo: str, commit: str, clone: Path, code_root: Path) -> str:
    """A pip spec for the repo itself, for a package no registry carries.

    Only for an http(s) remote: the sandbox cannot see a local checkout. The
    subdirectory is where the package metadata actually lives, which in a
    monorepo is not the repo root.
    """
    if not repo.startswith("http") or not commit or commit in ("unknown", "local"):
        return ""
    spec = f"git+{repo.removesuffix('.git')}@{commit}"
    try:
        inside = code_root.resolve().relative_to(clone.resolve()).as_posix()
    except ValueError:
        return spec
    return spec if inside in ("", ".") else f"{spec}#subdirectory={inside}"


def _import_name(package: str, ecosystem: str, found: str = "") -> str:
    """What `import`/`require` calls the package. PyPI distributions swap `-` for
    `_` and NuGet carries its own namespace; an npm package is imported by the
    very name it is published under."""
    if ecosystem in ("npm", "cli"):
        return package
    return found or package.replace("-", "_")


def _short(url: str) -> str:
    p = urlparse(url)
    return (p.netloc + p.path).rstrip("/") if p.netloc else url


# ------------------------------------------------- which packages a repo gives
def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _answers_to(pkg: Package, eco: str, name: str) -> bool:
    """Does the packages entry `eco:name` mean `pkg`? Case and `-`/`_`/`.` do
    not matter, and `wizard` means `@momentic/wizard`: the unscoped tail counts."""
    if eco and eco != pkg.ecosystem:
        return False
    return name.lower() in (pkg.name.lower(), pkg.name.rsplit("/", 1)[-1].lower()) \
        or _norm(name) == _norm(pkg.name)


def _key(p: Package) -> str:
    return f"{p.ecosystem}:{p.name}"


def _mentioned_alone(p: Package) -> bool:
    """May a bare mention of `p`'s name count, outside an install or import?

    Only a name that prose and other registries do not also use. `relate-db` on
    PyPI and `@relate/relate` on npm are never English; a bare `relate` or
    `streamlit` is, so those have to turn up in an install command or an import
    first. An unscoped npm name is not safe either - `pydantic-core` is a PyPI
    project's name all over its docs - but a scoped one, or a NuGet id, is."""
    if p.ecosystem == "npm":
        return p.name.startswith("@")
    if p.ecosystem == "nuget":
        return "." in p.name
    return bool(re.search(r"[-_.\d]", p.name))


def _name_re(p: Package) -> re.Pattern:
    """The name as docs spell it: PyPI treats `-`, `_` and `.` alike; npm and
    NuGet names are literal (NuGet ignoring case), so `pydantic_core` is not
    npm's `pydantic-core`. `Duende.IdentityServer` must not match inside
    `Duende.IdentityServer.EntityFramework`."""
    if p.ecosystem == "pypi":
        body = r"[-_.]".join(re.escape(x) for x in re.split(r"[-_.]+", p.name))
    else:
        body = re.escape(p.name)
    return re.compile(rf"(?<![\w@./-]){body}(?![\w-]|\.\w)", re.I)


# Each ecosystem's install commands: a name on such a line is named as *that
# ecosystem's* package.
_INSTALL = {
    "pypi": re.compile(r"\b(?:pip3?\s+install|-m\s+pip\s+install|uv\s+(?:pip\s+install|add|tool\s+install)|"
                       r"poetry\s+add|pdm\s+add|pipx\s+install|conda\s+install)\b", re.I),
    "npm": re.compile(r"\b(?:npm\s+(?:i|install|add)|npx|yarn\s+(?:global\s+)?add|"
                      r"pnpm\s+(?:i|add|install|dlx)|bun\s+(?:add|install)|bunx)\b", re.I),
    "nuget": re.compile(r"\b(?:dotnet\s+add\s+(?:\S+\s+)?package|dotnet\s+tool\s+install|"
                        r"install-package|nuget\s+install|packagereference)\b", re.I),
}
_USING = re.compile(r"^[ \t]*(?:global[ \t]+)?using[ \t]+(?:static[ \t]+)?([A-Za-z_][\w.]*)[ \t]*;",
                    re.M)


def _import_re(pkg: Package) -> re.Pattern | None:
    """How code in the docs reaches `pkg`: a Python `import`/`from` line, a
    JS `from`/`require`/`import()` of the module or one of its subpaths. NuGet
    is counted separately, by namespace."""
    imp = re.escape(pkg.import_name)
    if pkg.ecosystem == "pypi":
        return re.compile(rf"(?:^|[`;>])[ \t]*(?:import|from)[ \t]+{imp}(?:\.\w+)*"
                          r"(?:[ \t]+(?:import|as)\b|[ \t]*,|[ \t]*(?:$|`))", re.M)
    if pkg.ecosystem == "npm":
        return re.compile(rf"(?:\bfrom|\brequire\(|\bimport\(|\bimport)\s*['\"`]{imp}"
                          r"(?:/[^'\"`\s]*)?['\"`]")
    return None


def _namespace_hits(found: list[Package], corpus: str) -> dict[str, int]:
    """`using` directives per NuGet package. The longest root namespace wins:
    `using Duende.IdentityServer.EntityFramework.Stores;` belongs to the
    EntityFramework package, not also to the IdentityServer one it sits under."""
    roots = [p for p in found if p.ecosystem == "nuget"]
    hits: dict[str, int] = {}
    if not roots:
        return hits
    for ns in _USING.findall(corpus):
        low = ns.lower()
        owners = [p for p in roots
                  if low == p.import_name.lower() or low.startswith(p.import_name.lower() + ".")]
        if owners:
            best = max(owners, key=lambda p: len(p.import_name))
            hits[best.name] = hits.get(best.name, 0) + 1   # nuget ids are unique
    return hits


def mention_scores(found: list[Package], corpus: str) -> dict[str, int]:
    """How much the docs talk about each package, by `eco:name`.

    Counts, per package: its distribution name where that is distinctive, the
    install commands that name it (the only place a bare name like `streamlit`
    counts as a name), and the import/require/using lines that reach it. A bare
    `relate` in prose is nothing: Relate's docs say it on every page, and it
    cannot tell the Python client from the .NET one.
    """
    lines = corpus.splitlines()
    installs = {eco: "\n".join(x for x in lines if rx.search(x)) for eco, rx in _INSTALL.items()}
    ns = _namespace_hits(found, corpus)
    out: dict[str, int] = {}
    for p in found:
        text = corpus if _mentioned_alone(p) else installs.get(p.ecosystem, "")
        score = len(_name_re(p).findall(text))
        imp = _import_re(p)
        if imp:
            score += len(imp.findall(corpus))
        out[_key(p)] = score + (ns.get(p.name, 0) if p.ecosystem == "nuget" else 0)
    return out


@dataclass
class Choice:
    chosen: list[Package]
    unmentioned: list[Package]
    over_cap: list[Package]


def choose_packages(found: list[Package], scores: dict[str, int],
                    first: Package | None = None, cap: int | None = None) -> Choice:
    """The packages with evidence, best first, at most `cap`. `first`, the
    package a single-package scan has always picked, leads when it is among
    them, so a repo that was verified as one package keeps its primary."""
    cap = MAX_AUTO_PACKAGES if cap is None else cap
    hit = [p for p in found if scores.get(_key(p))]
    if first is not None:
        lead = next((p for p in hit if (p.ecosystem, _norm(p.name)) == (first.ecosystem, _norm(first.name))), None)
        if lead:
            hit = [lead] + [p for p in hit if p is not lead]
    return Choice(hit[:cap], [p for p in found if not scores.get(_key(p))], hit[cap:])


def nothing_found(path: Path, name: str) -> str:
    """What was looked for and what was seen, for a repo that yields no package."""
    msg = f"no PyPI, npm or NuGet package found in {name}"
    from .acquire.detect import other_manifests
    seen = other_manifests(path)
    if seen:
        msg += f"; it does contain {', '.join(seen)} - docrot has no lane for those yet"
    return msg


def _to_check(snips: list) -> str:
    """How many of a package's snippets a lane will actually look at. Unverifiable
    ones are attributed to it too, but no sandbox ever sees them."""
    n = sum(1 for s in snips if s.tier != "unverifiable")
    return f"{n} snippets to check ({len(snips) - n} more attributed to it, not checkable)"


def _listed(pkgs: list[Package], limit: int = 8) -> str:
    names = [_key(p) for p in pkgs]
    return ", ".join(names[:limit]) + (f", and {len(names) - limit} more" if len(names) > limit else "")


def _automatic(path: Path, name: str, corpus: str, found: list[Package],
               scores: dict[str, int], log: Log) -> list[Package]:
    """The packages to verify from a repo the scan names nothing in.

    One package, or none, is what a scan has always done: whatever `find_package`
    picks. Several: every one the docs mention (`mention_scores`), at most
    `MAX_AUTO_PACKAGES`, the legacy pick first when it is among them. When the
    docs mention none, the legacy pick alone - a scan of Streamlit, whose repo
    holds two npm component libraries too, verifies what it always did."""
    from .acquire import repo as repomod
    from .acquire.repo import Package
    dist, imp, eco, root = repomod.find_package(path, hint=name, rank=corpus.count)
    legacy = Package(dist, imp, eco, root) if dist else None
    if len(found) <= 1:
        return [legacy] if legacy else []
    pick = choose_packages(found, scores, first=legacy)
    if not pick.chosen:
        best = legacy or found[0]
        log(f"  {name} holds {len(found)} packages ({_listed(found)}) and the docs mention "
            f"none of them - verifying {_key(best)} only; name others to include them")
        return [best]
    log(f"  {name} holds {len(found)} packages - verifying the {len(pick.chosen)} the docs "
        f"mention: {_listed(pick.chosen)}")
    if pick.unmentioned:
        log(f"  skipped {len(pick.unmentioned)} the docs never mention: "
            f"{_listed(pick.unmentioned)}; name them to include them")
    if pick.over_cap:
        log(f"  skipped {len(pick.over_cap)} past the cap of {MAX_AUTO_PACKAGES} per repo: "
            f"{_listed(pick.over_cap)}; name them to include them")
    return pick.chosen


def _ships_bin(targets: list[Target], bins: dict[str, tuple[str, ...]], command: str) -> str:
    """The npm target that already puts `command` on the PATH, or "".

    A package whose HEAD package.json declares the bin owns the command. One
    nobody has read (registry-only) is guessed at by its unscoped name, as the
    extraction does - a Relate-style repo whose `@relate/relate` declares no bin
    is the case this exists to get right."""
    for t in targets:
        if t.ecosystem != "npm":
            continue
        declared = bins.get(t.package)
        if (command in declared) if declared is not None else _tail(t.package) == command:
            return t.package
    return ""


def _github_repo(repo: str) -> bool:
    from .acquire.binaries import github_slug
    return github_slug(repo) is not None


def _cli_homes(repos: list[str], entries: list[tuple[str, str]]) -> tuple[dict[int, list[str]], list[str]]:
    """Which repo each `cli:` entry belongs to, and the entries no repo can take.

    An entry at a repo's own position belongs to that repo (and must be a GitHub
    one); the ones after the last repo - where the form's extra picks land - go
    to the first GitHub repo, because a command has no registry to be found in."""
    homes: dict[int, list[str]] = {}
    refused: list[str] = []
    first = next((i for i, r in enumerate(repos) if _github_repo(r)), None)
    for j, (eco, name) in enumerate(entries):
        if eco != "cli" or not name:
            continue
        i = j if j < len(repos) else first
        if i is None:
            refused.append(f"cli:{name} needs the GitHub repository that publishes its release "
                           "binaries, and the scan has none")
        elif not _github_repo(repos[i]):
            refused.append(f"cli:{name} cannot be taken from {repos[i]}: docrot installs a "
                           "command-line tool from a GitHub repository's releases")
        else:
            homes.setdefault(i, []).append(name)
    return homes, refused


def _cli_target(lookup, name: str, repo: str, branch: str, commit: str) -> Target | None:
    """A CLI target for `name`, or None when the repo's releases cannot supply it."""
    if not lookup.installable:
        return None
    return Target(package=name, import_name=name, ecosystem="cli", repo_url=repo,
                  branch=branch, commit=commit)


async def _auto_cli(repo: str, name: str, pages: list, targets: list[Target],
                    bins: dict[str, tuple[str, ...]], log: Log) -> str:
    """The command to verify as a standalone CLI from `repo`, or "" - and why not, logged.

    Both must hold: the docs show real command lines for a command named like
    the repo (`commands.MIN_COMMAND_LINES` in shell fences; prose does not
    count), and the repo's GitHub releases carry a Linux binary for it. The
    docs are read first so that a repo whose docs show no such commands costs no
    GitHub request - 60 an hour without a token. A GitHub that cannot be asked
    skips the CLI and never fails the scan."""
    from .acquire import binaries
    from .extract.commands import MIN_COMMAND_LINES, command_lines
    if not _github_repo(repo):
        return ""
    shipped = _ships_bin(targets, bins, name)
    if shipped:
        log(f"  {name} is also a command of {shipped}, which ships that bin - its command lines "
            "are checked against that package, not a standalone CLI")
        return ""
    lines = await asyncio.to_thread(command_lines, pages, name)
    if lines < MIN_COMMAND_LINES:
        return ""
    found = await asyncio.to_thread(binaries.lookup, repo, name)
    if not found.installable:
        log(f"  ! the docs show {lines} `{name}` command lines, but {found.why or 'no installable binary was found'}"
            f" - not verifying {name} as a standalone CLI"
            + ("; name it as cli:" + name + " to retry" if found.failed else ""))
        return ""
    log(f"  the docs show {lines} `{name}` command lines and {repo_name_of(repo)}'s releases carry "
        f"a Linux binary for it - verifying {name} as a standalone CLI")
    return name


def repo_name_of(repo: str) -> str:
    from .acquire.binaries import github_slug
    slug = github_slug(repo)
    return f"{slug[0]}/{slug[1]}" if slug else repo


def pick_ecosystem(name: str, prefix: str, default: str,
                   on: Callable[[str, str], bool], linked: bool = False) -> str:
    """The registry a package that no repo describes is on.

    A prefix (`npm:name`) decides. Otherwise the scan's ecosystem does, unless
    it is `auto`: then the registries are asked. One that has the name wins;
    several is an error naming them, none is an error unless the package is
    linked to a repo, which can be installed from source (the docs-only-repo and
    not-yet-published cases) and is treated as PyPI like before."""
    if prefix:
        return prefix
    if default != "auto":
        return default
    if name.startswith("@"):                  # only npm has scopes
        return "npm"
    hits = [e for e in ECOSYSTEMS if on(name, e)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        syntax = " or ".join(f"{e}:{name}" for e in hits)
        raise ScanError(f"{name} exists on {' and '.join(hits)} - say which one: "
                        f"--package {syntax} (the package field takes the same form)")
    if linked:
        return "pypi"
    raise ScanError(f"no package to verify - {name} was not found on PyPI, npm or NuGet. "
                    f"Check the spelling, name its registry ({' / '.join(e + ':' + name for e in ECOSYSTEMS)}) "
                    "or add the repo it lives in")


async def run_scan(inp: ScanInputs, cfg: cfgmod.Config, log: Log = print,
                   phase: Callable[[int], None] = lambda i: None) -> ScanOutcome:
    from .acquire import acquire_repo, acquire_site, binaries, pick_versions, releases
    from .acquire.http import Fetcher
    from .acquire.repo import (
        CloneError,
        clone,
        dotnet_surface,
        ensure_sources,
        find_packages,
        npm_bins,
        repo_name,
        symbols_at_head,
    )
    from .extract import extract
    from .graph import GraphStore
    from .report import build_report
    from .verify import run_matrix, run_npm_matrix
    from .verify.cli import run_cli_matrix
    from .verify.dotnet import Surface, run_dotnet_matrix

    binaries.use_token(cfg.github_token)
    opts = inp.options
    if opts.extraction_models:
        # a per-scan choice wins over EXTRACTION_MODELS
        cfg.extraction_models = [m.strip() for m in opts.extraction_models if m.strip()]
    started = time.time()
    if cfg.can_llm:
        # EXTRACTION_MODELS drops empty entries, so `,vendor/model` silently
        # discards whatever preceded the comma. Say out loud what resolved.
        log(f"  extraction models: {', '.join(cfg.extraction_models)}")

    # ------------------------------------------------------------ acquire
    phase(0)
    fetcher = Fetcher(cfg.user_agent, use_cache=not opts.no_cache)
    pages, strategies = [], []
    try:
        for url in inp.docs:
            site_pages, how = await acquire_site(url, fetcher, opts.max_pages or cfg.max_pages, log)
            where = f" · {_short(url)}" if len(inp.docs) > 1 else ""
            log(f"  {len(site_pages)} pages via {how}{where}")
            pages.extend(site_pages)
            strategies.append(how)
    finally:
        await fetcher.aclose()

    targets: list[Target] = []
    head: dict[str, set[str]] = {}
    api: dict[str, Surface] = {}               # .NET packages' public API at HEAD
    bins: dict[str, tuple[str, ...]] = {}      # what each npm target's package.json declares
    prefetched: dict[tuple[str, str], list[Release]] = {}
    site_pages = list(pages)
    site_text = "\n".join(p.text for p in pages)
    multi = len(inp.repos) > 1
    entries = [split_package(p) for p in inp.packages]       # (ecosystem or "", name)
    # a `cli:` entry names a command, not a package: it never takes a package's slot
    cli_for, refused = _cli_homes(inp.repos, entries)
    entries = [("", "") if eco == "cli" else (eco, name) for eco, name in entries]

    def known(name: str, eco: str) -> bool:
        if (eco, name) not in prefetched:
            prefetched[(eco, name)] = releases(name, eco)
        return bool(prefetched[(eco, name)])

    async def ecosystem_of(name: str, prefix: str, linked: bool) -> str:
        return await asyncio.to_thread(pick_ecosystem, name, prefix, opts.ecosystem, known, linked)

    def add(t: Target, syms: set[str], surface: Surface | None, where: str = "") -> None:
        again = next((x for x in targets if _norm(x.package) == _norm(t.package)), None)
        if again:
            if again.ecosystem == t.ecosystem:
                head[again.package] |= syms
            else:
                # results, snippets and the graph are all keyed by package name
                log(f"  ! {t.package} is on {again.ecosystem} and {t.ecosystem}; only the "
                    f"{again.ecosystem} one is verified and the {t.ecosystem} one is skipped "
                    "(a scan keys its results by package name alone)")
            return
        targets.append(t)
        head[t.package] = syms
        if surface:
            api[t.package] = surface
        log(f"  {t.package} ({t.ecosystem}) · import {t.import_name} · "
            f"{len(syms)} public symbols at HEAD ({t.commit}){where}")

    # ---- acquire every repo and see what each holds
    plans = []
    claimed: set[int] = set()                  # packages entries some repo answered
    for repo in inp.repos:
        try:
            path, commit, branch = await asyncio.to_thread(clone, repo, log, cfg.clone_timeout)
        except CloneError as e:
            raise ScanError(f"couldn't fetch {repo}: {e}") from e
        name = repo_name(repo)
        repo_pages = acquire_repo(path, log, namespace=name if multi else "", origin=repo)
        pages.extend(repo_pages)
        # What the docs say decides which packages matter. A repo-only scan has
        # no site, so the repo's own docs stand in.
        corpus = site_text or "\n".join(p.text for p in repo_pages)
        docs_pages = site_pages or repo_pages
        found = await asyncio.to_thread(find_packages, path, name)
        scores: dict[str, int] = {}
        if len(found) > 1:
            # ranked by the docs' own mentions: ties and the order of what is
            # chosen come from `find_packages`, which takes the score by name
            scores = mention_scores(found, corpus)
            by_name = {p.name: scores[_key(p)] for p in found}
            found = await asyncio.to_thread(find_packages, path, name,
                                            lambda n, m=by_name: m.get(n, 0))
        here = [(j, p) for p in found for j, (eco, want) in enumerate(entries)
                if want and _answers_to(p, eco, want)]
        claimed |= {j for j, _ in here}
        # in the order they were named: the first is the primary, as for
        # `--package Duende.IdentityServer Duende.BFF`
        named = list(dict.fromkeys(p for _, p in sorted(here, key=lambda x: x[0])))
        plans.append((repo, path, commit, branch, corpus, found, scores, named, docs_pages))

    # ---- turn each repo into targets
    cli_targets: list[Target] = []
    skipped_cli: list[str] = list(refused)
    for i, (repo, path, commit, branch, corpus, found, scores, named, docs_pages) in enumerate(plans):
        name = repo_name(repo)
        where = f" · {name}" if multi else ""
        pos = entries[i] if i < len(entries) and entries[i][1] else None
        unmatched = pos if pos and i not in claimed else None
        chosen = named
        if not chosen and not unmatched and not cli_for.get(i):
            chosen = _automatic(path, name, corpus, found, scores, log)
        if any(p.ecosystem == "npm" for p in chosen):
            # a cached sparse clone holds only docs until the JS/TS sources are fetched
            await asyncio.to_thread(ensure_sources, path, "npm", log, cfg.clone_timeout)
        for pkg in chosen:
            imp = _import_name(pkg.name, pkg.ecosystem, pkg.import_name)
            syms = symbols_at_head(pkg.root, imp, pkg.ecosystem)
            if pkg.ecosystem == "npm" and (declared := npm_bins(pkg.root)) is not None:
                bins[pkg.name] = declared
            add(Target(package=pkg.name, import_name=imp, ecosystem=pkg.ecosystem,
                       repo_url=repo, branch=branch, commit=commit,
                       install_spec=(_source_spec(repo, commit, path, pkg.root)
                                     if pkg.ecosystem == "pypi" else "")),
                syms, Surface.of(dotnet_surface(pkg.root)) if pkg.ecosystem == "nuget" else None,
                where)
        if unmatched:
            # The user's word for what this repo is. A docs-only repo has no
            # metadata to say, so the prefix or the scan's ecosystem does.
            eco = await ecosystem_of(unmatched[1], unmatched[0], linked=True)
            if eco == "npm":
                await asyncio.to_thread(ensure_sources, path, eco, log, cfg.clone_timeout)
            imp = _import_name(unmatched[1], eco)
            add(Target(package=unmatched[1], import_name=imp, ecosystem=eco, repo_url=repo,
                       branch=branch, commit=commit,
                       install_spec=_source_spec(repo, commit, path, path) if eco == "pypi" else ""),
                symbols_at_head(path, imp, eco), None, where)
        elif not chosen and not cli_for.get(i):
            log(f"  ! {nothing_found(path, name)}. Its docs are read, nothing is verified "
                "from it; name its package (the package field, or --package) to check the "
                "docs against it")
        # ---- a standalone CLI of this repo: named, or proposed from the docs and the releases
        wanted = list(cli_for.get(i, []))
        if not wanted and not named and not unmatched:
            auto = await _auto_cli(repo, name, docs_pages, targets, bins, log)
            wanted = [auto] if auto else []
        for cmd in wanted:
            found_cli = await asyncio.to_thread(binaries.lookup, repo, cmd)
            t = _cli_target(found_cli, cmd, repo, branch, commit)
            if t is None:
                skipped_cli.append(f"cli:{cmd} not verified: {found_cli.why or 'no installable binary found'}")
            else:
                cli_targets.append(t)
    # an entry no repo answered is a package straight from its registry
    for j, (eco, want) in enumerate(entries):
        if want and j not in claimed and j >= len(inp.repos):
            eco = await ecosystem_of(want, eco, linked=False)
            add(Target(package=want, import_name=_import_name(want, eco), ecosystem=eco),
                set(), None)

    # standalone CLIs come last: the primary target of a scan, which names its
    # report and its graph, is a package whenever there is one
    for t in cli_targets:
        clash = next((x for x in targets if _norm(x.package) == _norm(t.package)), None)
        if clash:
            skipped_cli.append(f"cli:{t.package} not verified: {clash.package} ({clash.ecosystem}) already "
                               "goes by that name, and a scan keys its results by package name")
            continue
        head[t.package] = set()
        targets.append(t)
        log(f"  {t.package} (cli) · command {t.package} · release binaries of "
            f"{repo_name_of(t.repo_url)} ({t.commit})")
    for msg in skipped_cli:
        log(f"  ! {msg}")
    if not targets:
        looked = "; ".join(nothing_found(path, repo_name(repo)) for repo, path, *_ in plans)
        why = "; ".join(skipped_cli + ([looked] if looked else []))
        raise ScanError("no package to verify - " + (why + ". " if why else "") +
                        "Name one (the package field beside a repo, or --package on the "
                        "command line) or use a repo that has package metadata")
    if not pages:
        raise ScanError("no pages acquired")

    # ----------------------------------------------------------- registry
    phase(1)
    mentioned = sorted({p for pg in pages for p in pg.pinned_versions})
    for t in targets:
        if t.ecosystem == "cli":
            t.releases = (await asyncio.to_thread(binaries.lookup, t.repo_url, t.package)).rows()
        else:
            t.releases = await asyncio.to_thread(releases, t.package, t.ecosystem)
        if not t.releases and t.ecosystem == "cli":
            log(f"  ! {t.package} has no releases on GitHub - nothing can be verified")
        elif not t.releases:
            if t.install_spec:
                log(f"  ! {t.package} has no releases on {t.ecosystem} - installing the repo "
                    f"itself at {t.commit}")
            else:
                log(f"  ! {t.package} has no releases on {t.ecosystem}, and no repo to install "
                    "from - nothing can be verified")
        t.versions = pick_versions(t.releases, opts.versions, mentioned,
                                   spread=t.ecosystem in SPREAD_ECOSYSTEMS) or [Release(label="HEAD")]
        who = f" · {t.package}" if len(targets) > 1 else ""
        if t.ecosystem == "cli":
            log(f"  {t.package} (cli) · {len(t.releases)} releases · testing {', '.join(t.labels)}")
        else:
            log(f"  {len(t.releases)} releases · testing {', '.join(t.labels)}{who}")
    if mentioned:
        log(f"  ! docs mention versions: {', '.join(mentioned[:6])}")

    # --------------------------------------------------------- extraction
    phase(2)
    all_head = set().union(*head.values()) if head else set()
    ex = await asyncio.to_thread(
        extract, pages, targets, cfg, log,
        assume_credentialed=opts.assume_credentialed, head_symbols=all_head, bins=bins)

    # ------------------------------------------------------- verification
    phase(3)
    results: list[Result] = []
    for t in targets:
        snips = [s for s in ex.snippets if s.package == t.package]
        if not snips:
            continue
        if t.ecosystem == "nuget":
            if len(targets) > 1:
                log(f"  {t.package}: {_to_check(snips)}")
            got = await run_dotnet_matrix(snips, t.package, t.package, t.labels, cfg, log,
                                          head=api.get(t.package))
        elif t.ecosystem == "npm":
            if len(targets) > 1:
                log(f"  {t.package}: {_to_check(snips)}")
            got = await run_npm_matrix(snips, t.package, t.labels, cfg, log)
        elif t.ecosystem == "cli":
            if len(targets) > 1:
                log(f"  {t.package}: {_to_check(snips)}")
            got = await run_cli_matrix(snips, t.package, t.labels, cfg, log, repo_url=t.repo_url)
        elif t.ecosystem != "pypi":
            log(f"  ! {t.package}: no verification lane for {t.ecosystem} packages yet - "
                "marked unverified")
            got = [Result(id=s.id, page=s.page, version=v, status="unverified",
                          tier=s.tier) for s in snips for v in t.labels]
        else:
            if len(targets) > 1:
                log(f"  {t.package}: {_to_check(snips)}")
            got = await run_matrix(snips, t.package, t.import_name, t.labels, cfg, log,
                                   install_spec=t.install_spec)
        for r in got:
            r.package = t.package
        results.extend(got)
    fails = sum(1 for r in results if r.status == "fail")
    log(f"  {len(results)} results · {fails} failing")

    # -------------------------------------------------------------- graph
    phase(4)
    store = GraphStore(cfg, log)
    try:
        await asyncio.to_thread(store.load, ex, results, targets, head)
    finally:
        store.close()

    # ------------------------------------------------------------- report
    phase(5)
    strategy = " + ".join(dict.fromkeys(s for s in strategies if s)) or "none"
    report = build_report(ex, results, targets, inp.docs, inp.repos, strategy,
                          time.time() - started)
    log(f"› scan complete — {report['stats']['findings']} findings on "
        f"{report['stats']['drifted']} pages, {report['elapsed']}s")
    return ScanOutcome(report=report, extraction=ex, results=results)


Runner = Callable[[ScanInputs, cfgmod.Config, Log, Callable[[int], None]], ScanOutcome]


def _default_runner(inp, cfg, log, phase) -> ScanOutcome:
    return asyncio.run(run_scan(inp, cfg, log, phase))


def execute_scan(store: Store, scan_id: str, echo: Log | None = None,
                 runner: Runner = _default_runner,
                 cfg: cfgmod.Config | None = None) -> ScanStatus:
    """Run one scan synchronously, recording its progress in the store.

    Blocking - the CLI calls it directly, the dashboard from its worker thread.
    Never raises for a failed scan; the returned status says what happened.
    """
    rec = store.get(scan_id)
    if rec is None:
        raise KeyError(scan_id)
    prev = store.status(scan_id)
    st = ScanStatus(state="running", phase=0, queued_at=prev.queued_at, started_at=now())
    store.set_status(scan_id, st)

    def log(msg: str = "") -> None:
        st.log.append([style(msg), msg])
        if echo:
            echo(msg)
        store.set_status(scan_id, st)

    def phase(i: int) -> None:
        st.phase = max(0, min(i, len(PHASES) - 1))
        if echo:
            echo(f"\n› {PHASES[st.phase]}")
        store.set_status(scan_id, st)

    try:
        outcome = runner(ScanInputs.of(rec), cfg or cfgmod.load(), log, phase)
    except ScanError as e:
        st.error = str(e)
        log(f"! {e}")
    except Exception as e:           # a crash must not leave the scan `running`
        st.error = f"{type(e).__name__}: {e}"[:500]
        log(f"! scan crashed - {st.error}")
        for line in traceback.format_exc().strip().splitlines()[-4:]:
            st.log.append(["t-sys", "  " + line])
    else:
        outcome.report["log"] = st.log
        store.save_run(scan_id, outcome.report,
                       [r.model_dump() for r in outcome.results],
                       outcome.extraction.model_dump_json(indent=1))
        st.state, st.finished_at = "done", now()
        store.set_status(scan_id, st)
        return st

    st.state, st.finished_at = "failed", now()
    store.set_status(scan_id, st)
    return st
