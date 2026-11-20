"""Repository acquisition: shallow clone, docs harvest, AST symbol table."""
from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CACHE
from ..extract import javascript
from ..extract.csharp import NAMESPACE_DECL
from ..models import Page
from .pins import find_pins

DOC_GLOBS = ("README.md", "README.rst", "CHANGELOG.md",
             "docs/**/*.md", "docs/**/*.mdx", "examples/**/*.md")


def repo_name(repo: str) -> str:
    """`https://github.com/org/name.git` -> `name`; a local path -> its folder."""
    return repo.rstrip("/").split("/")[-1].split(":")[-1].removesuffix(".git") or "repo"


class CloneError(RuntimeError):
    """The repo could not be fetched. The message says why, for the scan log."""

    def __init__(self, message: str, auth: bool = False):
        super().__init__(message)
        self.auth = auth        # failed on credentials, rather than anything else


# What a scan reads from a repo: code for the symbol table, markdown for docs,
# and the metadata that names the package. Everything else - test snapshots,
# images, frontend bundles - is never fetched.
SPARSE = ("*.py", "*.md", "*.mdx", "*.rst", "pyproject.toml", "setup.cfg",
          "setup.py", "package.json", "*.cs", "*.csproj")
CLONE_TIMEOUT = 600

# JS/TS sources are fetched only for an npm repo, and only after `find_package`
# has said so (`ensure_sources`). On every clone they would pull in the
# frontends of Python repos - the very thing the sparse set exists to avoid.
JS_EXTS = ("js", "mjs", "cjs", "jsx", "ts", "mts", "cts", "tsx")
# Whatever a JS repo vendors or builds is never read, and is where the bulk is.
# Each negation is scoped to JS files, so a README under `dist/` still arrives.
JS_SKIP_DIRS = ("node_modules", "dist", "build", "out", "coverage")
JS_SPARSE = (*(f"*.{e}" for e in JS_EXTS),
             *(f"!**/{d}/**/*.{e}" for d in JS_SKIP_DIRS for e in JS_EXTS),
             "!*.min.js", "!*.bundle.js")

# Scans run unattended - from the dashboard's worker thread, or a cron job.
# Git must never stop to ask: a passphrase prompt for an SSH key, or a
# username prompt for a private repo, blocks the whole scan queue on a console
# nobody is watching. Fail immediately instead, and say what to do.
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",              # no username/password prompt
    "GIT_ASKPASS": "",                       # ...and no GUI prompt either
    "SSH_ASKPASS_REQUIRE": "never",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
}
AUTH_ERROR = re.compile(
    r"(permission denied|could not read username|could not read password|authentication failed|"
    r"host key verification failed|enter passphrase|repository not found|access rights)", re.I)


def _run(args: list[str], timeout: int, plain: bool = False) -> subprocess.CompletedProcess:
    """git, with prompts disabled and no terminal to prompt on.

    Its own session, so a timeout can kill the whole group: killing git alone
    leaves the ssh it spawned running, still holding the connection - and, on a
    prompt, the terminal.

    `plain` also ignores the user's git config, which is how a URL rewritten by
    `url.<base>.insteadOf` is fetched as it was typed. Only ever a fallback:
    that config also holds the credential helpers a private repo needs.
    """
    env = {**os.environ, **GIT_ENV}
    if plain:
        env["GIT_CONFIG_GLOBAL"] = env["GIT_CONFIG_SYSTEM"] = os.devnull
    proc = subprocess.Popen(["git", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, stdin=subprocess.DEVNULL,
                            env=env, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _effective_url(repo: str, plain: bool = False) -> str:
    """What git will really contact: `url.<base>.insteadOf` rewrites happen
    silently, and turn an https URL into an ssh one that needs a key."""
    try:
        out = _run(["ls-remote", "--get-url", repo], 30, plain=plain).stdout.strip()
    except Exception:
        return repo
    return out or repo


# One lock per cached clone. `clone` builds in `<dest>.partial` and renames it
# into place, so two clones of one new repo delete each other's work in progress,
# and a fetch racing a fetch trips over git's own index.lock. Every caller - a
# scan's worker thread, a detect from the dashboard or an agent - goes through
# `clone`, so the lock lives there rather than being something callers remember.
_clone_locks: dict[str, threading.Lock] = {}
_clone_guard = threading.Lock()


def clone_lock(repo: str) -> threading.Lock:
    with _clone_guard:
        return _clone_locks.setdefault(repo.strip(), threading.Lock())


def clone(repo: str, log=print, timeout: int = CLONE_TIMEOUT) -> tuple[Path, str, str]:
    """Shallow, sparse clone - or refresh of the cached one. Returns
    (path, commit_sha, default_branch).

    The default branch is resolved from the remote rather than assumed -
    projects on a release train often default to something other than `main`.

    Blobless and sparse because a full checkout is dominated by files a scan
    never reads: Streamlit's took over 300s and timed out, the sparse one 20s.
    A cached clone is fetched again on every scan, so a re-run checks today's
    HEAD rather than whatever was current when the cache was first filled.
    """
    if Path(repo).exists():
        dest = Path(repo).resolve()
        sha = _git(dest, "rev-parse", "HEAD") or "local"
        return dest, sha[:12], _git(dest, "rev-parse", "--abbrev-ref", "HEAD") or "?"

    with clone_lock(repo):
        return _clone_remote(repo, log, timeout)


def _clone_remote(repo: str, log, timeout: int) -> tuple[Path, str, str]:
    key = hashlib.sha1(repo.encode(), usedforsecurity=False).hexdigest()[:8]
    dest = CACHE / f"repo-{repo_name(repo)}-{key}"

    branch, plain = "HEAD", False
    for attempt in (False, True):
        try:
            r = _run(["ls-remote", "--symref", repo, "HEAD"], 60, plain=attempt)
        except Exception:
            break
        if r.returncode == 0:
            plain = attempt
            m = re.search(r"ref:\s+refs/heads/(\S+)\s+HEAD", r.stdout)
            if m:
                branch = m.group(1)
            break
        # An https URL that git rewrites to ssh (url.<base>.insteadOf) needs a
        # key even when the repo is public - GitHub has no anonymous ssh. The
        # URL as typed usually needs nothing, so try that before giving up.
        rewritten = _effective_url(repo) != repo
        if not (attempt is False and AUTH_ERROR.search(r.stderr or "") and rewritten
                and repo.startswith("http")):
            break
        log(f"  ! your git config rewrites this URL to {_effective_url(repo)}, which needs a key "
            "- retrying it as typed")

    if (dest / ".git").exists():
        try:
            _run_git(["-C", str(dest), "fetch", "--quiet", "--depth", "1",
                      "--filter=blob:none", "origin", branch], timeout, repo, plain=plain)
            _run_git(["-C", str(dest), "reset", "--hard", "--quiet", "FETCH_HEAD"], timeout, repo,
                     plain=plain)
        except CloneError as e:
            log(f"  ! could not update the cached clone of {repo} ({e}) - using it as is")
    else:
        shutil.rmtree(dest, ignore_errors=True)          # partial, from an interrupted run
        tmp = dest.with_name(dest.name + ".partial")
        shutil.rmtree(tmp, ignore_errors=True)
        log(f"  cloning {repo} @ {branch}")
        try:
            cmd = ["clone", "--quiet", "--depth", "1", "--filter=blob:none", "--no-checkout"]
            if branch != "HEAD":
                cmd += ["--branch", branch]
            # every step, not just the clone: a blobless checkout fetches file
            # contents on demand, and would be rewritten to ssh all over again
            _run_git([*cmd, repo, str(tmp)], timeout, repo, plain=plain)
            _run_git(["-C", str(tmp), "sparse-checkout", "set", "--no-cone", *SPARSE], timeout,
                     repo, plain=plain)
            _run_git(["-C", str(tmp), "reset", "--hard", "--quiet", "HEAD"], timeout, repo,
                     plain=plain)
        except CloneError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        tmp.rename(dest)

    sha = _git(dest, "rev-parse", "HEAD") or "unknown"
    if branch not in ("main", "HEAD"):
        log(f"  ! default branch is {branch}, not main")
    return dest, sha[:12], branch


def _reason(output: str, returncode: int) -> str:
    """The one line worth showing. git puts the cause first and advice after, so
    the last line is usually "and the repository exists.."."""
    lines = [ln.strip() for ln in (output or "").strip().splitlines() if ln.strip()]
    for match in (AUTH_ERROR.search, lambda ln: ln.startswith(("fatal:", "error:"))):
        hit = next((ln for ln in lines if match(ln)), None)
        if hit:
            return hit
    return lines[-1] if lines else f"git exited {returncode}"


def _run_git(args: list[str], timeout: int, repo: str, plain: bool = False) -> None:
    try:
        r = _run(args, timeout, plain=plain)
    except subprocess.TimeoutExpired as e:
        raise CloneError(f"git {args[0] if args[0] != '-C' else args[2]} of {repo} "
                         f"timed out after {timeout}s") from e
    if r.returncode == 0:
        return
    message = _reason(r.stderr or r.stdout, r.returncode)
    auth = bool(AUTH_ERROR.search(r.stderr or ""))
    if auth:
        url = _effective_url(repo, plain=plain)
        where = f" (git contacts it as {url})" if url != repo else ""
        message = message.rstrip(".") + (
            f". docrot never prompts, so this failed rather than waiting{where}. "
            "Load your SSH key (ssh-add --apple-use-keychain ~/.ssh/id_ed25519) "
            "or check that this account can read the repo.")
    raise CloneError(message, auth=auth)


def _git(path: Path, *args: str) -> str:
    try:
        return _run(["-C", str(path), *args], 30).stdout.strip()
    except Exception:
        return ""


def acquire_repo(path: Path, log=print, namespace: str = "",
                 origin: str = "") -> list[Page]:
    """Markdown docs checked into the repo.

    `namespace` prefixes the page URL. It is set only when a scan has several
    repos - two READMEs would otherwise share `repo://README.md` - and left empty
    for one, so single-repo page keys (and `docrot diff` history) are unchanged.
    """
    pages = []
    prefix = f"{namespace}/" if namespace else ""
    for pattern in DOC_GLOBS:
        for f in sorted(path.glob(pattern)):
            if not f.is_file() or f.stat().st_size > 400_000:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(f.relative_to(path))
            pages.append(Page(
                url=f"repo://{prefix}{rel}", path=rel, title=rel, source="repo",
                origin=origin or str(path), text=text, pinned_versions=find_pins(text),
            ))
    log(f"  {len(pages)} doc files in repo")
    return pages


def package_meta(path: Path) -> tuple[str, str, str]:
    """(distribution_name, import_name, ecosystem) from project metadata.

    Distribution and import names routinely differ - perseus-client imports as
    perseus_client - and both are needed: one to install, one to probe.
    """
    dist, imp, eco, _ = find_package(path)
    return dist, imp, eco


@dataclass(frozen=True)
class Package:
    name: str          # distribution name on its registry: 'relate-db', '@relate/relate', 'Relate.Net'
    import_name: str   # what code imports: 'relate', '@relate/relate', 'Relate.Net' (root namespace)
    ecosystem: str     # 'pypi' | 'npm' | 'nuget'
    root: Path         # the directory to read its source from
    dev: bool = field(default=False, compare=False, repr=False)   # a dev-workspace pyproject

    def as_tuple(self) -> tuple[str, str, str, Path]:
        return self.name, self.import_name, self.ecosystem, self.root


def find_package(path: Path, hint: str = "", want: str = "",
                 rank=None) -> tuple[str, str, str, Path]:
    """`package_meta` plus the directory that contains the import package: the
    one package to verify, where `find_packages` lists them all.

    Python metadata anywhere wins over a package.json, so a frontend folder is
    never mistaken for the package. Streamlit's root pyproject.toml declares a
    `streamlit-dev` workspace and the published package lives in lib/, so a
    package named like the repo (`hint`) wins over the root; otherwise the
    root does. `want` (`--package`) names the package to verify, in whatever
    ecosystem it is.

    A .NET monorepo holds many packages: `rank` - a score per package id, in
    practice how often the docs mention it - picks one, shortest id on a tie.
    """
    if not path.is_dir():
        return "", "", "pypi", path
    found = _collect(path)
    if want and (hit := next((p for p in _ordered(found, hint) if _answers_to(p, want)), None)):
        return hit.as_tuple()

    pypi = [p for p in _ordered(found, hint) if p.ecosystem == "pypi"]
    if pypi:
        return pypi[0].as_tuple()

    dotnet = nuget_packages(path)
    if dotnet:
        pick = next((p for p in dotnet if want and _norm(p.id) == _norm(want)), None) \
            or next((p for p in dotnet if hint and _norm(p.id) == _norm(hint)), None) \
            or max(dotnet, key=lambda p: ((rank(p.id) if rank else 0), -len(p.id)))
        return pick.id, pick.namespace, "nuget", pick.project.parent

    found_npm = _npm_package(path, _bases(path), hint, want)
    if found_npm:
        return (found_npm[0], found_npm[0], "npm", found_npm[1])
    npm = [p for p in _ordered(found, hint) if p.ecosystem == "npm"]
    if npm:                                   # deeper than the old one-level search saw
        return npm[0].as_tuple()
    return "", "", "pypi", path


def find_packages(path: Path, hint: str = "", rank=None) -> list[Package]:
    """Every publishable package in a checkout, at any depth, in all three
    ecosystems, best first.

    Relate's one repo holds `relate-db` (surfaces/relate-python), `@relate/relate`
    (clients/javascript) and `Relate.Net` (clients/dotnet/src/Relate.Net); a
    search that returned one package, from the root or one level down, found
    none of them. Order: a name matching `hint` (the repo name) first, then
    `rank(name)` descending - how often the docs mention it - then the shallower
    path, then the name. De-duplicated by (ecosystem, normalised name).

    Not packages: test, example, sample, template, benchmark and fixture trees;
    private npm packages (workspace roots, apps); npm platform sub-packages of
    another package; pyprojects that only configure tools or a dev workspace;
    the .NET projects `nuget_packages` rejects.
    """
    if not path.is_dir():
        return []
    return _ordered([p for p in _collect(path) if not p.dev], hint, rank)


def _bases(path: Path) -> list[Path]:
    bases = [path]
    if path.is_dir():
        bases += sorted(d for d in path.iterdir()
                        if d.is_dir() and not d.name.startswith((".", "test", "doc")))
    return bases


def _answers_to(p: Package, wanted: str) -> bool:
    """`--package wizard` means `@momentic/wizard`: the unscoped name counts."""
    low = wanted.lower()
    return low in (p.name.lower(), p.name.rsplit("/", 1)[-1].lower()) \
        or _norm(p.name) == _norm(wanted)


def _ordered(pkgs: list[Package], hint: str = "", rank=None) -> list[Package]:
    """De-duplicated (a name published from two folders is one package; the
    shallower folder is the one that is read) and ranked as `find_packages`
    documents."""
    def depth(p: Package) -> int:
        return len(p.root.parts)

    best: dict[tuple[str, str], Package] = {}
    for p in sorted(pkgs, key=lambda p: (depth(p), str(p.root))):
        best.setdefault((p.ecosystem, _norm(p.name)), p)

    def key(p: Package):
        named = bool(hint) and _answers_to(p, hint)
        return (not named, -(rank(p.name) if rank else 0), depth(p), p.name.lower())

    return sorted(best.values(), key=key)


# ------------------------------------------------------------------ the walk
# Directories nothing publishable lives in: VCS, dependencies, build output.
# `vendor` and `third_party` hold other people's packages.
WALK_SKIP = frozenset({"node_modules", "vendor", "third_party", "build", "dist", "out", "target",
                       "bin", "obj", "venv", "env", "site-packages", "__pycache__"})
# Trees that only demonstrate or exercise a package, whatever the ecosystem.
DECOY_DIR = re.compile(r"(^|[._-])(tests?|testing|__tests__|samples?|examples?|templates?|"
                       r"benchmarks?|fixtures?|playground|demos?|e2e)($|[._-])", re.I)
# A Python or npm manifest under these is the docs site's, not the package's.
DOC_DIRS = frozenset({"docs", "doc", "documentation", "website"})
# Past this the walk stops: it is breadth-first, so what is found is the
# shallowest part of a repo, and a Streamlit-sized checkout stays fast.
MAX_WALK_DIRS = 20_000
MANIFESTS = frozenset({"pyproject.toml", "setup.cfg", "package.json"})


def _walk(path: Path) -> list[Path]:
    """Every manifest (pyproject.toml, setup.cfg, package.json, *.csproj) in
    the checkout, shallowest first, never entering what `WALK_SKIP` and
    `DECOY_DIR` name, dot-directories or symlinks, and visiting at most
    `MAX_WALK_DIRS` directories."""
    found: list[Path] = []
    queue = [path]
    visited = 0
    while queue and visited < MAX_WALK_DIRS:
        here = queue.pop(0)
        visited += 1
        try:
            entries = sorted(os.scandir(here), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if not (e.name.startswith(".") or e.name in WALK_SKIP
                            or DECOY_DIR.search(e.name)):
                        queue.append(Path(e.path))
                elif e.name in MANIFESTS or e.name.endswith(".csproj"):
                    found.append(Path(e.path))
            except OSError:
                continue
    return found


def _collect(path: Path) -> list[Package]:
    """Every package the walk finds, before ordering and the dev-workspace
    filter. npm and Python manifests inside a docs folder are not packages."""
    manifests = _walk(path)
    pkgs: list[Package] = []
    for m in manifests:
        rel = m.parent.relative_to(path).parts
        in_docs = bool(DOC_DIRS & {part.lower() for part in rel})
        if m.name == "pyproject.toml" and not in_docs:
            pkgs += _python_package(m.parent)
        elif m.name == "setup.cfg" and not in_docs and not (m.parent / "pyproject.toml").exists():
            pkgs += _setup_cfg_package(m.parent)
    pkgs += _npm_packages([m.parent for m in manifests if m.name == "package.json"
                           and not DOC_DIRS & {x.lower() for x in m.parent.relative_to(path).parts}])
    pkgs += [Package(p.id, p.namespace, "nuget", p.project.parent)
             for p in _nuget_from([m for m in manifests if m.suffix == ".csproj"], path)]
    return pkgs


# ---------------------------------------------------------------------- npm
def _npm_packages(dirs: list[Path]) -> list[Package]:
    """Public npm packages among `dirs`, less each one's platform sub-packages.

    `private` is a workspace root or an app, which nobody installs. A package
    named in another's `optionalDependencies` (by name, or by a `file:` path)
    is that package's per-platform binary, not something to verify; and one
    living under another package's directory is dropped too, unless that
    package declares `workspaces` - then its members are what it publishes.
    """
    manifests = {d: _package_json(d) for d in dirs}
    manifests = {d: m for d, m in manifests.items() if isinstance(m.get("name"), str) and m["name"]}
    sub_names: set[str] = set()
    sub_dirs: set[Path] = set()
    for d, m in manifests.items():
        opt = m.get("optionalDependencies")
        for name, spec in (opt.items() if isinstance(opt, dict) else []):
            sub_names.add(name)
            if isinstance(spec, str) and spec.startswith("file:"):
                sub_dirs.add((d / spec[5:]).resolve())
    out = []
    for d, m in manifests.items():
        if m.get("private") or m["name"] in sub_names or d.resolve() in sub_dirs:
            continue
        if any(o != d and o in d.parents and not o_m.get("private") and not o_m.get("workspaces")
               for o, o_m in manifests.items()):
            continue
        out.append(Package(m["name"], m["name"], "npm", d))
    return out


# ------------------------------------------------------------------- python
def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _python_package(base: Path) -> list[Package]:
    """The package a pyproject.toml declares - none when it only configures
    tools, which is a pyproject with no `[project]` or poetry name."""
    data = _read_toml(base / "pyproject.toml")
    proj = data.get("project") or data.get("tool", {}).get("poetry", {}) or {}
    name = proj.get("name")
    if not isinstance(name, str) or not name:
        return []
    imp, root = _python_import(base, name, data)
    return [Package(name, imp, "pypi", root, dev=_is_dev_workspace(base, name, data))]


def _setup_cfg_package(base: Path) -> list[Package]:
    """A pre-pyproject package: `[metadata] name` in setup.cfg. setup.py is not
    read - its `name=` is arbitrary code, and a regex over it would take the
    first `name=` of any call as the package."""
    import configparser
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(base / "setup.cfg", encoding="utf-8")
    except (configparser.Error, OSError):
        return []
    name = cp.get("metadata", "name", fallback="").strip()
    if not name:
        return []
    imp, root = _guess_import(base, name)
    return [Package(name, imp, "pypi", root)]


def _is_dev_workspace(base: Path, name: str, data: dict) -> bool:
    """A pyproject that declares no package of its own: Streamlit's root is
    `streamlit-dev` 0.0.0 whose only dependency is the real package, which lives
    in lib/. Build metadata names no module, no folder is named for it (the
    directory heuristic would still find some stray package, `e2e_playwright`),
    and it is named or versioned as development scaffolding, or is a uv
    workspace root."""
    if _declared_import(base, data):
        return False
    candidate = name.replace("-", "_").replace(".", "_")
    if any((r / candidate).is_dir() or (r / f"{candidate}.py").is_file()
           for r in (base, base / "src", base / "python")):
        return False
    dev_named = bool(re.search(r"[-_.]dev$", name, re.I)) \
        or (data.get("project") or {}).get("version") == "0.0.0"
    return dev_named or "workspace" in (data.get("tool", {}).get("uv") or {})


def _python_import(base: Path, dist: str, data: dict) -> tuple[str, Path]:
    """(import_name, directory containing it). The build metadata says where the
    code is; the directory heuristics are the fallback.

    Relate's `relate-db` imports as `relate`: `[tool.maturin] module-name =
    "relate._native"` and `python-source = "python"`. The distribution name
    cannot be turned into it - and an unrelated `relate` is on PyPI, so the
    import name must never be turned back into the distribution name either."""
    named = _declared_import(base, data)
    if named:
        name, root = named
        if (root / name).is_dir() or (root / f"{name}.py").is_file():
            return name, root
    guess, root = _guess_import(base, dist)
    if named and not (root / guess).is_dir():
        return named
    return guess, root


def _top(name: str) -> str:
    return name.replace("/", ".").split(".")[0]


def _declared_import(base: Path, data: dict) -> tuple[str, Path] | None:
    tool = data.get("tool", {})
    # maturin: module-name's first segment, under python-source when set
    maturin = tool.get("maturin") or {}
    if isinstance(maturin.get("module-name"), str):
        return _top(maturin["module-name"]), base / maturin.get("python-source", "")
    # flit
    flit = (tool.get("flit") or {}).get("module") or {}
    if isinstance(flit.get("name"), str):
        top = _top(flit["name"])
        return top, next((r for r in (base, base / "src") if (r / top).exists()), base)
    # uv's build backend
    uv = (tool.get("uv") or {}).get("build-backend") or {}
    if isinstance(uv.get("module-name"), str):
        return _top(uv["module-name"]), base / uv.get("module-root", "src")
    # hatch: packages = ["src/foo"]
    wheel = (((tool.get("hatch") or {}).get("build") or {}).get("targets") or {}).get("wheel") or {}
    hatch = [p for p in wheel.get("packages") or [] if isinstance(p, str)]
    if hatch:
        folder, _, top = hatch[0].rstrip("/").rpartition("/")
        return top, base / folder
    # poetry: packages = [{include = "foo", from = "src"}]
    for entry in (tool.get("poetry") or {}).get("packages") or []:
        if isinstance(entry, dict) and isinstance(entry.get("include"), str):
            return _top(entry["include"]), base / entry.get("from", "")
    return _setuptools_import(base, tool.get("setuptools") or {})


def _setuptools_import(base: Path, st: dict) -> tuple[str, Path] | None:
    """`packages` (a list, or `find` with `where`) and `package-dir`'s root."""
    pkg_dir = st.get("package-dir") or {}
    root = base / (pkg_dir.get("") or "") if isinstance(pkg_dir, dict) else base
    packages = st.get("packages")
    if isinstance(packages, list):
        top = next((_top(p) for p in packages if isinstance(p, str)), "")
        return (top, root) if top else None
    find = packages.get("find") if isinstance(packages, dict) else None
    if isinstance(find, dict):
        where = find.get("where") or [""]
        root = base / (where[0] if isinstance(where, list) and where else "")
        top = next((d.name for d in sorted(root.iterdir()) if d.is_dir()
                    and (d / "__init__.py").exists() and not d.name.startswith((".", "test"))),
                   "") if root.is_dir() else ""
        return (top, root) if top else None
    return None


def _package_json(directory: Path) -> dict:
    try:
        data = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _workspace_members(root: Path, manifest: dict) -> list[tuple[str, Path]]:
    """(name, dir) of each publishable package a workspace root holds: the
    `workspaces` globs, plus the `packages/*` and `apps/*` convention."""
    ws = manifest.get("workspaces")
    if isinstance(ws, dict):
        ws = ws.get("packages")
    patterns = [p for p in (ws if isinstance(ws, list) else []) if isinstance(p, str)
                and not p.startswith("!")]
    patterns += ["packages/*", "apps/*"]
    out: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for pattern in patterns:
        try:
            dirs = sorted(root.glob(pattern.rstrip("/")))
        except (ValueError, NotImplementedError, OSError):
            continue
        for d in dirs:
            if d in seen or not d.is_dir() or "node_modules" in d.parts:
                continue
            seen.add(d)
            data = _package_json(d)
            if data.get("name") and not data.get("private"):
                out.append((data["name"], d))
    return out


def _npm_package(path: Path, bases: list[Path], hint: str, want: str) -> tuple[str, Path] | None:
    """(name, directory) of the npm package to verify, or None.

    The first package.json at the root or one level down is what a plain repo
    wants. A workspace root, though, is `"private": true` and named for the
    monorepo - nothing anyone installs - so when a member matches `want` or the
    repo's name (`hint`) the member wins. `@momentic/wizard` answers to `wizard`.
    """
    roots = [(b, _package_json(b)) for b in bases if (b / "package.json").exists()]
    roots = [(b, m) for b, m in roots if m.get("name")]
    if not roots:
        return None

    def matches(name: str, wanted: str) -> bool:
        return bool(wanted) and wanted.lower() in (name.lower(), name.rsplit("/", 1)[-1].lower())

    members: list[tuple[str, Path]] = []
    for base, manifest in roots:
        members += _workspace_members(base, manifest)
    named = [(manifest["name"], base) for base, manifest in roots] + members
    if want and (hit := next((m for m in named if matches(m[0], want)), None)):
        return hit
    first_dir, first = roots[0]
    if first.get("private") and members \
            and (hit := next((m for m in members if matches(m[0], hint)), None)):
        return hit
    return first["name"], first_dir


NOT_A_PACKAGE_WORDS = ("tests?", "testing", "samples?", "examples?", "templates?", "benchmarks?",
                       "performance", "playground", "demo", "hosts?", "migrations", "shared")


def _folder_re(words) -> re.Pattern:
    return re.compile(rf"(^|[/\\._-])({'|'.join(words)})($|[/\\._-])", re.I)


# Where a project is never a package. `clients` is the exception: Duende's
# `clients/` folders hold sample console apps and APIs, but Relate's
# `clients/dotnet/src/Relate.Net` is the product, a client library. So `clients`
# disqualifies a project only when it states no `<PackageId>` - a sample app
# never packs itself, a library headed for NuGet always does.
LIBRARY_FOLDERS = _folder_re(NOT_A_PACKAGE_WORDS)
NOT_A_PACKAGE = _folder_re((*NOT_A_PACKAGE_WORDS, "clients"))


@dataclass
class NugetPackage:
    id: str
    namespace: str            # the root namespace its code declares
    project: Path


def nuget_packages(path: Path) -> list[NugetPackage]:
    """Every packable project in a repo: a library outside test, sample and
    template folders, not marked `<IsPackable>false`. Its id is the PackageId,
    else the assembly name."""
    if not path.is_dir():
        return []
    return _nuget_from([m for m in _walk(path) if m.suffix == ".csproj"], path)


def _nuget_from(projects: list[Path], path: Path) -> list[NugetPackage]:
    out = []
    for proj in sorted(projects):
        rel = proj.relative_to(path).as_posix()
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        try:
            xml = proj.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"<IsPackable>\s*false\s*</IsPackable>", xml, re.I) \
                or re.search(r"<OutputType>\s*(Win)?Exe\s*</OutputType>", xml, re.I):
            continue
        explicit = _prop(xml, "PackageId")
        if LIBRARY_FOLDERS.search(folder) or (not explicit and NOT_A_PACKAGE.search(folder)):
            continue
        name = explicit or _prop(xml, "AssemblyName") or proj.stem
        if "$(" in name:
            continue
        ns = _prop(xml, "RootNamespace")
        out.append(NugetPackage(name, ns if ns and "$(" not in ns else _declared_root(proj, name),
                                proj))
    return out


def _declared_root(project: Path, name: str) -> str:
    """The root namespace as the code spells it. `Duende.BFF` the assembly
    declares `namespace Duende.Bff.*`, and snippets are attributed to a package
    by the namespaces they use, so the assembly's casing matches none of them."""
    for f in project.parent.rglob("*.cs"):
        if {"obj", "bin"} & set(f.relative_to(project.parent).parts):
            continue
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        for ns in NAMESPACE_DECL.findall(head):
            if ns.lower() == name.lower() or ns.lower().startswith(name.lower() + "."):
                return ns[:len(name)]
    return name


def _prop(xml: str, name: str) -> str:
    m = re.search(rf"<{name}>\s*([^<]+?)\s*</{name}>", xml)
    return m.group(1) if m else ""


def dotnet_sources(project: Path) -> list[Path]:
    """The .cs files a project compiles: its own folder, and the folders of the
    projects it references in the same repo, transitively. IdentityServer's
    `Client` and `ApiScope` live in the Storage project it references."""
    seen: set[Path] = set()
    queue = [project.resolve()]
    files: list[Path] = []
    while queue:
        proj = queue.pop()
        if proj in seen or not proj.exists():
            continue
        seen.add(proj)
        files += [f for f in proj.parent.rglob("*.cs")
                  if not {"obj", "bin"} & set(f.relative_to(proj.parent).parts)]
        xml = proj.read_text(encoding="utf-8", errors="replace")
        for ref in re.findall(r'<ProjectReference\s+Include="([^"]+)"', xml):
            queue.append((proj.parent / ref.replace("\\", "/")).resolve())
    return files


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _guess_import(path: Path, dist: str) -> tuple[str, Path]:
    """(import_name, directory containing it). Checks the flat, `src/` and
    `python/` layouts before falling back to any package directory."""
    candidate = dist.replace("-", "_").replace(".", "_")
    for root in (path, path / "src", path / "python"):
        if (root / candidate / "__init__.py").exists():
            return candidate, root
    for d in sorted(path.iterdir()):
        if d.is_dir() and (d / "__init__.py").exists() and not d.name.startswith((".", "test")):
            return d.name, path
    return candidate, path


def dotnet_surface(code_root: Path) -> dict:
    """A .NET package's public API at HEAD, in `extract.csharp.surface` shape."""
    from ..extract.csharp import surface
    proj = next(iter(sorted(code_root.glob("*.csproj"))), None)
    files = dotnet_sources(proj) if proj else list(code_root.rglob("*.cs"))
    texts = []
    for f in files:
        try:
            texts.append(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return surface(texts)


def npm_bins(root: Path) -> tuple[str, ...] | None:
    """The command names the package at `root` declares in package.json's
    `bin`: a string is the package's own (unscoped) name, an object's keys are
    the names. `()` is an answer - the package ships no command - and `None`
    means there is no readable package.json to ask. Ownership of a command
    line (`extract.commands`) rests on the difference: `@relate/relate` has no
    `bin`, and the `relate` its docs run is a different program."""
    try:
        meta = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    bin_ = meta.get("bin")
    if isinstance(bin_, str):
        return (str(meta.get("name") or "").rsplit("/", 1)[-1],) if meta.get("name") else ()
    if isinstance(bin_, dict):
        return tuple(str(k) for k in bin_)
    return ()


def symbols_at_head(path: Path, import_name: str, ecosystem: str = "pypi") -> set[str]:
    """Public symbols defined at HEAD, by AST walk.

    For a .NET package, the types and `Type.Member` names its source declares.
    For an npm package, the names its JS/TS sources export (`npm_symbols`).

    Exact and instant, and it cannot hallucinate a function that isn't there -
    which is why the model is never asked for symbols found in code.
    """
    if ecosystem == "nuget":
        api = dotnet_surface(path)
        return set(api["types"]) | {f"{t}.{m}" for t, ms in api["members"].items() for m in ms}
    if ecosystem == "npm":
        return npm_symbols(path)
    out: set[str] = set()
    pkg = path / import_name
    root = pkg if pkg.is_dir() else path
    for f in root.rglob("*.py"):
        if any(p in f.parts for p in ("test", "tests", ".venv", "build")):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name.startswith("_"):
                    continue
                out.add(node.name)
                if isinstance(node, ast.ClassDef):
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                           and not sub.name.startswith("_"):
                            out.add(f"{node.name}.{sub.name}")
    return out


# Directories whose JS is never the package's own API: dependencies, build
# output, and the tests and fixtures that import the package like a user would.
NPM_SKIP_DIRS = frozenset({"node_modules", "dist", "build", "out", "coverage", "test", "tests",
                           "__tests__", "__mocks__", "fixtures", "__fixtures__", "e2e", "examples",
                           "example", "docs", "benchmark", "benchmarks", "vendor"})
NPM_SOURCE = re.compile(r"\.(?:[cm]?[jt]sx?)$")
NPM_NOT_SOURCE = re.compile(r"\.(?:test|spec|min|bundle|stories)\.|\.config\.[cm]?[jt]s$")
NPM_MAX_BYTES = 512_000           # past this it is a bundle, not hand-written source
NPM_MAX_FILES = 3000


def npm_symbols(path: Path) -> set[str]:
    """Names an npm package exports at HEAD: the union of `exports_of` over its
    JS/TS sources, preferring `src/` when there is one.

    A union rather than the entry point's exports, because re-exports through
    an index file cannot be followed lexically; a name that is exported
    anywhere in the package is one a doc is allowed to mention. `default` is
    dropped: every module has one and none is a name anyone documents.
    Never raises - a repo that cannot be read has no symbols.
    """
    try:
        src = path / "src"
        files = _npm_files(src) or _npm_files(path)
        out: set[str] = set()
        for f in files:
            with contextlib.suppress(Exception):
                out |= javascript.exports_of(f.read_text(encoding="utf-8", errors="replace"))
        out.discard("default")
        return out
    except Exception:
        return set()


def _npm_files(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir():
        return found
    for here, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in NPM_SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if not NPM_SOURCE.search(name) or NPM_NOT_SOURCE.search(name):
                continue
            f = Path(here) / name
            try:
                if f.stat().st_size > NPM_MAX_BYTES:
                    continue
            except OSError:
                continue
            found.append(f)
            if len(found) >= NPM_MAX_FILES:
                return found
    return found


def ensure_sources(path: Path, ecosystem: str, log=print, timeout: int = CLONE_TIMEOUT) -> bool:
    """Make sure an npm repo's JS/TS sources are checked out, before
    `symbols_at_head`. True when they are there, False when nothing was done
    or it could not be done.

    `clone` fetches no JS, so a cache clone of an npm repo has none. This widens
    that one clone's sparse set (`JS_SPARSE`) with `git sparse-checkout add`,
    through the same git plumbing, timeout and no-prompt handling as `clone`.
    It is idempotent - a clone already widened is left alone - and works on a
    cache clone made before this existed. Nothing but `ecosystem == "npm"` and
    a cache clone is touched: a local path is the user's own tree, complete
    already, and Python and .NET scans never fetch JS.

    `path` is the clone or any directory inside it (scan passes `code_root`
    as readily as `clone`'s path). A failure is logged, not raised: the docs
    were fetched, so the scan goes on with an empty symbol table.
    """
    if ecosystem != "npm":
        return False
    dest = _cache_clone(path)
    if dest is None:
        return False
    if all(p in _git(dest, "sparse-checkout", "list").splitlines() for p in JS_SPARSE[:len(JS_EXTS)]):
        return True
    origin = _git(dest, "remote", "get-url", "origin") or str(dest)
    for plain in (False, True):
        try:
            _run_git(["-C", str(dest), "sparse-checkout", "add", *JS_SPARSE], timeout, origin,
                     plain=plain)
            return True
        except CloneError as e:
            if not (e.auth and not plain):
                log(f"  ! could not fetch the JS/TS sources of {repo_name(origin)} ({e})")
                return False
    return False


def _cache_clone(path: Path) -> Path | None:
    """The cache clone `path` is, or is inside - None for any other directory."""
    try:
        cache = Path(CACHE).resolve()
        here = path.resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if d == cache or cache not in d.parents:
            return None
        if (d / ".git").exists():
            return d
    return None
