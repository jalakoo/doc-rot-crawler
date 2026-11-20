"""Which packages does a repo hold? The question a scan asks before it can verify
anything, answered up front so the dashboard and agents can ask it first.

A polyglot monorepo ships several packages (Relate: PyPI, npm and NuGet), and a
scan that is not told which to verify can only guess. `detect` clones - or
refreshes the cached clone of - a repo and lists what `find_packages` finds,
plus the ecosystems docrot has no checker for, so the answer can say "also
contains Rust and Go" instead of leaving the user wondering why nothing
matched.
"""
from __future__ import annotations

import os
from pathlib import Path

from .repo import CLONE_TIMEOUT, CloneError, _git, clone, find_packages, repo_name

# manifest file name (or suffix) -> ecosystem docrot cannot verify yet
OTHER_MANIFESTS = {"Cargo.toml": "rust", "go.mod": "go", "pom.xml": "java",
                   "build.gradle": "java", "build.gradle.kts": "java",
                   "Package.swift": "swift", "composer.json": "php", "Gemfile": "ruby",
                   "mix.exs": "elixir", "pubspec.yaml": "dart", "DESCRIPTION": "r",
                   "cabal.project": "haskell", "dune-project": "ocaml"}
OTHER_SUFFIXES = {".gemspec": "ruby"}
# Where a stray manifest is not the project's own: vendored code, fixtures, samples.
NOT_PROJECT = frozenset({"node_modules", "vendor", "third_party", "third-party", "test", "tests",
                         "testdata", "fixtures", "example", "examples", "sample", "samples",
                         "benchmark", "benchmarks", "docs", "target", "build", "dist", "out",
                         "bin", "obj", "venv", "env", "site-packages", "__pycache__"})
MAX_LISTING = 200_000       # bytes of `git ls-tree` output read: a monorepo's tree is large


def _quiet(*_args) -> None:
    """Cloning logs through a callback; nothing may reach stdout (MCP's wire)."""


def detect(repo: str, log=_quiet, timeout: int = CLONE_TIMEOUT) -> dict:
    """What `repo` holds. Never raises for a bad repo: the result is then
    `{"repo", "error"}` and says what to do."""
    repo = (repo or "").strip()
    try:
        path, commit, branch = clone(repo, log, timeout)
        found = find_packages(path, repo_name(repo))
        return {
            "repo": repo, "commit": commit, "branch": branch,
            "packages": [{"name": p.name, "import_name": p.import_name, "ecosystem": p.ecosystem,
                          "path": _relative(p.root, path), "spec": f"{p.ecosystem}:{p.name}"}
                         for p in found],
            "other": other_ecosystems(path),
        }
    except CloneError as e:
        return {"repo": repo, "error": str(e)}
    except Exception as e:      # a detect must never be a 500: the user can still name a package by hand
        return {"repo": repo, "error": f"{type(e).__name__}: {e}"[:300]}


def _relative(root: Path, base: Path) -> str:
    try:
        rel = root.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return ""
    return "" if rel == "." else rel


def _ecosystem_of(name: str) -> str | None:
    if name in OTHER_MANIFESTS:
        return OTHER_MANIFESTS[name]
    return next((eco for suffix, eco in OTHER_SUFFIXES.items() if name.endswith(suffix)), None)


def other_manifests(path: Path) -> list[str]:
    """Names of other ecosystems' manifests in the top two levels of a checkout,
    sorted.

    Both the disk and git's tree are read. A cache clone is sparse and blobless
    - it holds no Cargo.toml - but git still has its tree; a local path is the
    user's own working tree, whose uncommitted files only the disk has."""
    seen: dict[str, None] = {}
    for rel in [*_walk_names(path), *_tree_names(path)]:
        parts = rel.split("/")
        if len(parts) > 2 or any(p.lower() in NOT_PROJECT or p.startswith(".") for p in parts[:-1]):
            continue
        if _ecosystem_of(parts[-1]):
            seen.setdefault(parts[-1])
    return sorted(seen)


def other_ecosystems(path: Path) -> list[str]:
    """Ecosystems with no checker whose manifest `other_manifests` finds."""
    return sorted({eco for name in other_manifests(path) if (eco := _ecosystem_of(name))})


def _tree_names(path: Path) -> list[str]:
    out = _git(path, "ls-tree", "-r", "--name-only", "HEAD")[:MAX_LISTING]
    return out.splitlines()


def _walk_names(path: Path) -> list[str]:
    names: list[str] = []
    try:
        for top in os.scandir(path):
            if top.is_file():
                names.append(top.name)
            elif top.is_dir(follow_symlinks=False) and not top.name.startswith("."):
                if top.name.lower() in NOT_PROJECT:
                    continue
                names += [f"{top.name}/{e.name}" for e in os.scandir(top.path) if e.is_file()]
    except OSError:
        pass
    return names
