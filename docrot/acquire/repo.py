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
import tomllib
from pathlib import Path

from ..config import CACHE
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
          "setup.py", "package.json")
CLONE_TIMEOUT = 600

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


def find_package(path: Path, hint: str = "") -> tuple[str, str, str, Path]:
    """`package_meta` plus the directory that contains the import package.

    Looks at the repo root, then one level down: monorepos keep the package
    beside tooling. Streamlit's root pyproject.toml declares a `streamlit-dev`
    workspace and the published package lives in lib/, so a package named like
    the repo (`hint`) wins over the root; otherwise the root does. Python
    metadata anywhere wins over a package.json, so a frontend folder is never
    mistaken for the package.
    """
    bases = [path]
    if path.is_dir():
        bases += sorted(d for d in path.iterdir()
                        if d.is_dir() and not d.name.startswith((".", "test", "doc")))

    found = []
    for base in bases:
        pp = base / "pyproject.toml"
        if not pp.exists():
            continue
        try:
            data = tomllib.loads(pp.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        proj = data.get("project") or data.get("tool", {}).get("poetry", {}) or {}
        if proj.get("name"):
            found.append((proj["name"], base))
    if found:
        dist, base = next(((d, b) for d, b in found if hint and _norm(d) == _norm(hint)), found[0])
        imp, root = _guess_import(base, dist)
        return dist, imp, "pypi", root

    for base in bases:
        pj = base / "package.json"
        if not pj.exists():
            continue
        try:
            dist = json.loads(pj.read_text(encoding="utf-8")).get("name", "")
        except Exception:
            dist = ""
        if dist:
            return dist, dist, "npm", base
    return "", "", "pypi", path


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


def symbols_at_head(path: Path, import_name: str) -> set[str]:
    """Public symbols defined at HEAD, by AST walk.

    Exact and instant, and it cannot hallucinate a function that isn't there -
    which is why the model is never asked for symbols found in code.
    """
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
