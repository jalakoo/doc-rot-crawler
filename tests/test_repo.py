from __future__ import annotations

import json
import os
from pathlib import Path
from typing import ClassVar

from docrot.acquire.repo import acquire_repo, package_meta, repo_name, symbols_at_head


def _repo(tmp_path, name="alpha"):
    root = tmp_path / name
    (root / name).mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    (root / name / "__init__.py").write_text("def go(x): ...\nclass Client:\n    def run(self): ...\n")
    (root / "README.md").write_text("# Alpha\npip install alpha==1.2.3\n")
    return root


def test_repo_name():
    assert repo_name("https://github.com/org/pydantic-core.git") == "pydantic-core"
    assert repo_name("git@github.com:org/thing.git") == "thing"
    assert repo_name("/tmp/local/") == "local"


def test_acquire_repo_single_keeps_plain_urls(tmp_path):
    pages = acquire_repo(_repo(tmp_path), log=lambda *_: None, origin="https://github.com/o/alpha")
    assert [p.url for p in pages] == ["repo://README.md"]
    assert pages[0].origin == "https://github.com/o/alpha"
    assert pages[0].pinned_versions == ["1.2.3"]


def test_acquire_repo_namespaced(tmp_path):
    pages = acquire_repo(_repo(tmp_path), log=lambda *_: None, namespace="alpha")
    assert [p.url for p in pages] == ["repo://alpha/README.md"]


def test_package_meta_and_symbols(tmp_path):
    root = _repo(tmp_path)
    assert package_meta(root) == ("alpha", "alpha", "pypi")
    assert symbols_at_head(root, "alpha") == {"go", "Client", "Client.run"}


def _git(*args, cwd):
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})


def test_clone_is_sparse_refreshes_and_recovers_from_partial(tmp_path, monkeypatch):
    import docrot.acquire.repo as repo
    monkeypatch.setattr(repo, "CACHE", tmp_path / "cache")
    origin = _repo(tmp_path, "alpha")
    (origin / "big.bin").write_bytes(b"\0" * 2048)
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("add", ".", cwd=origin)
    _git("commit", "-qm", "one", cwd=origin)
    url = f"file://{origin}"

    path, sha1, branch = repo.clone(url, log=lambda *_: None)
    assert branch == "main" and (path / "README.md").exists()
    assert not (path / "big.bin").exists()                   # outside the sparse set

    (origin / "README.md").write_text("# changed\n")
    _git("commit", "-qam", "two", cwd=origin)
    path2, sha2, _ = repo.clone(url, log=lambda *_: None)
    assert path2 == path and sha2 != sha1
    assert (path / "README.md").read_text() == "# changed\n"

    import shutil
    shutil.rmtree(path / ".git")                             # an interrupted clone
    _, sha3, _ = repo.clone(url, log=lambda *_: None)
    assert sha3 == sha2


def test_clone_failure_is_a_clone_error(tmp_path, monkeypatch):
    import pytest

    import docrot.acquire.repo as repo
    monkeypatch.setattr(repo, "CACHE", tmp_path / "cache")
    with pytest.raises(repo.CloneError):
        repo.clone(f"file://{tmp_path}/nope", log=lambda *_: None)
    assert not list((tmp_path / "cache").glob("*.partial"))


def test_find_package_in_a_monorepo(tmp_path):
    from docrot.acquire.repo import find_package
    root = tmp_path / "mono"
    (root / "frontend").mkdir(parents=True)
    (root / "frontend" / "package.json").write_text('{"name": "@mono/frontend"}')
    (root / "pyproject.toml").write_text('[project]\nname = "mono-dev"\n')   # a dev workspace
    (root / "lib" / "mono").mkdir(parents=True)
    (root / "lib" / "pyproject.toml").write_text('[project]\nname = "mono"\n')
    (root / "lib" / "mono" / "__init__.py").write_text("def hello(): ...\n")
    assert find_package(root, hint="mono") == ("mono", "mono", "pypi", root / "lib")
    assert find_package(root)[0] == "mono-dev"            # no hint: the root wins

    core = tmp_path / "core"
    (core / "python" / "fast_core").mkdir(parents=True)
    (core / "pyproject.toml").write_text('[project]\nname = "fast-core"\n')
    (core / "python" / "fast_core" / "__init__.py").write_text("")
    assert find_package(core) == ("fast-core", "fast_core", "pypi", core / "python")


DENIED = ("git@github.com: Permission denied (publickey).\n"
          "fatal: Could not read from remote repository.\n\n"
          "Please make sure you have the correct access rights\n"
          "and the repository exists.\n")


class FakeGit:
    """Stands in for subprocess.Popen: records how git was launched.

    `rewritten` mimics a url.<base>.insteadOf rule sending an https URL to ssh,
    which then fails unless the user's config is ignored (`plain`).
    """

    calls: ClassVar[list] = []
    rewritten: ClassVar[bool] = False
    private: ClassVar[bool] = False

    def __init__(self, args, **kw):
        FakeGit.calls.append((args, kw))
        self.args, self.returncode = args, 0
        self.stdout, self.stderr = "", ""
        plain = kw["env"].get("GIT_CONFIG_GLOBAL") == os.devnull
        rewritten_now = FakeGit.rewritten and not plain
        network = any(a in args for a in ("clone", "ls-remote", "fetch")) or "reset" in args

        if "--get-url" in args:
            self.stdout = "git@github.com:o/r\n" if FakeGit.rewritten else "https://github.com/o/r\n"
        elif network and (rewritten_now or FakeGit.private):
            self.returncode, self.stderr = 128, DENIED
        elif "ls-remote" in args:
            self.stdout = "ref: refs/heads/main\tHEAD\nabc123\tHEAD\n"
        elif "clone" in args:
            Path(args[-1]).mkdir(parents=True)              # git would have made it

    def communicate(self, timeout=None):
        return self.stdout, self.stderr


def _fake_git(monkeypatch, tmp_path, rewritten=False, private=False):
    import docrot.acquire.repo as repo
    monkeypatch.setattr(repo, "CACHE", tmp_path / "cache")
    FakeGit.calls = []
    FakeGit.rewritten, FakeGit.private = rewritten, private
    monkeypatch.setattr(repo.subprocess, "Popen", FakeGit)
    return repo


def test_git_never_waits_for_a_prompt(tmp_path, monkeypatch):
    """A scan runs unattended: an SSH passphrase or username prompt would block
    the queue on a console nobody is watching."""
    import subprocess

    repo = _fake_git(monkeypatch, tmp_path)
    repo.clone("https://github.com/o/r", log=lambda *_: None)

    assert FakeGit.calls, "no git ran"
    for args, kw in FakeGit.calls:
        assert kw["stdin"] is subprocess.DEVNULL          # nothing to type into
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "BatchMode=yes" in kw["env"]["GIT_SSH_COMMAND"]
        assert kw["env"]["SSH_ASKPASS_REQUIRE"] == "never"
        assert kw["env"]["PATH"]                          # the rest of the environment survives
        assert kw["start_new_session"] is True            # so a timeout kills ssh too


def test_authentication_failure_explains_itself(tmp_path, monkeypatch):
    import pytest

    repo = _fake_git(monkeypatch, tmp_path, rewritten=True, private=True)
    with pytest.raises(repo.CloneError) as e:
        repo.clone("https://github.com/o/r", log=lambda *_: None)

    message = str(e.value)
    assert message.startswith("git@github.com: Permission denied (publickey)")
    assert "never prompts" in message and "ssh-add" in message
    assert "git contacts it as git@github.com:o/r" in message      # the rewritten URL
    assert ".." not in message
    assert not list((tmp_path / "cache").glob("*.partial"))


def test_a_hung_git_takes_its_children_with_it(tmp_path, monkeypatch):
    """git's ssh child must not outlive the timeout, still holding the terminal."""
    import subprocess
    import time

    import pytest

    import docrot.acquire.repo as repo
    marker = tmp_path / "child-alive"
    script = f"!sh -c 'touch {marker}; sleep 30'"
    t0 = time.perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        repo._run(["-c", f"alias.slow={script}", "slow"], 2)
    assert time.perf_counter() - t0 < 10
    assert marker.exists(), "the child never started, so this proves nothing"
    time.sleep(0.5)
    # the child is identifiable by the marker path in its command line
    survivors = subprocess.run(["pgrep", "-f", str(marker)], capture_output=True, text=True)
    assert survivors.stdout.strip() == "", "git's child outlived the timeout"


def test_reason_picks_the_useful_line():
    from docrot.acquire.repo import _reason
    assert _reason("fatal: repository 'x' not found\n", 128) == "fatal: repository 'x' not found"
    assert _reason("warning: noise\nremote: Repository not found.\nfatal: could not read\n", 128) \
        == "remote: Repository not found."
    assert _reason("", 3) == "git exited 3"


def test_a_url_rewritten_to_ssh_falls_back_to_the_url_as_typed(tmp_path, monkeypatch):
    """GitHub has no anonymous ssh, so `url.insteadOf` breaks a public repo the
    user asked for over https. The typed URL needs no key."""
    repo = _fake_git(monkeypatch, tmp_path, rewritten=True)
    said = []
    path, _sha, branch = repo.clone("https://github.com/o/r", log=said.append)

    assert branch == "main" and path.exists()
    assert any("rewrites this URL" in line and "as typed" in line for line in said)
    # every step that touches the network ignores the rewrite, not just the clone
    network = [(args, kw) for args, kw in FakeGit.calls
               if any(a in args for a in ("clone", "fetch")) or "reset" in args]
    assert network and all(kw["env"]["GIT_CONFIG_GLOBAL"] == os.devnull for _, kw in network)


def test_a_private_repo_still_reports_the_auth_failure(tmp_path, monkeypatch):
    """The fallback must not turn "you need credentials" into something vaguer."""
    import pytest

    repo = _fake_git(monkeypatch, tmp_path, rewritten=True, private=True)
    with pytest.raises(repo.CloneError) as e:
        repo.clone("https://github.com/o/r", log=lambda *_: None)
    assert e.value.auth is True
    assert "Permission denied" in str(e.value) and "ssh-add" in str(e.value)


def test_no_rewrite_means_no_second_attempt(tmp_path, monkeypatch):
    import pytest

    repo = _fake_git(monkeypatch, tmp_path, private=True)
    with pytest.raises(repo.CloneError):
        repo.clone("https://github.com/o/r", log=lambda *_: None)
    assert not any(kw["env"].get("GIT_CONFIG_GLOBAL") == os.devnull for _, kw in FakeGit.calls)


# ----------------------------------------------------------------------- npm

def _write(root, rel, text):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def _npm_tree(tmp_path):
    root = tmp_path / "momentic"
    _write(root, "package.json", '{"name": "momentic"}')
    _write(root, "src/index.ts", "export function run() {}\nexport class Client { go() {} }\n"
                                 "export default function main() {}\n")
    _write(root, "src/server.js", "exports.start = () => {}\n")
    _write(root, "src/types.d.ts", "export interface Config { a: number }\n")
    _write(root, "src/index.test.ts", "export const fromTest = 1\n")
    _write(root, "src/__tests__/x.ts", "export const fromTestDir = 1\n")
    _write(root, "src/vendor.min.js", "exports.minified = 1\n")
    _write(root, "src/huge.js", "exports.huge = 1\n" + "// pad\n" * 80_000)
    _write(root, "node_modules/dep/index.js", "exports.dep = 1\n")
    _write(root, "dist/index.js", "exports.built = 1\n")
    _write(root, "test/fixtures/a.js", "exports.fixture = 1\n")
    return root


def test_npm_symbols_come_from_sources_and_skip_everything_else(tmp_path):
    syms = symbols_at_head(_npm_tree(tmp_path), "momentic", "npm")
    assert syms == {"run", "Client", "Client.go", "start", "Config"}  # no `default`, no tests, no bundles


def test_npm_symbols_without_src_read_the_root_and_never_raise(tmp_path):
    root = tmp_path / "flat"
    _write(root, "package.json", '{"name": "flat"}')
    _write(root, "index.js", "module.exports = { alpha() {}, beta: 1 }\n")
    _write(root, "node_modules/x/index.js", "exports.no = 1\n")
    assert symbols_at_head(root, "flat", "npm") == {"alpha", "beta"}
    assert symbols_at_head(tmp_path / "missing", "x", "npm") == set()


def _workspace(tmp_path):
    root = tmp_path / "momentic-mono"
    _write(root, "package.json", '{"name": "monorepo-root", "private": true, '
                                 '"workspaces": ["packages/*", "tools/*"]}')
    _write(root, "packages/wizard/package.json", '{"name": "@momentic/wizard"}')
    _write(root, "packages/cli/package.json", '{"name": "momentic"}')
    _write(root, "packages/internal/package.json", '{"name": "internal", "private": true}')
    _write(root, "tools/gen/package.json", '{"name": "gen-tool"}')
    return root


def test_find_package_prefers_a_workspace_member_over_a_private_root(tmp_path):
    from docrot.acquire.repo import find_package
    root = _workspace(tmp_path)
    assert find_package(root, hint="momentic") == ("momentic", "momentic", "npm", root / "packages/cli")
    assert find_package(root, hint="wizard")[:3] == ("@momentic/wizard", "@momentic/wizard", "npm")
    assert find_package(root, hint="momentic-mono", want="gen-tool")[3] == root / "tools/gen"
    assert find_package(root, hint="momentic", want="@momentic/wizard")[0] == "@momentic/wizard"
    # nothing matches: what it always did
    assert find_package(root, hint="other")[0] == "monorepo-root"


def test_find_package_for_a_plain_npm_repo_is_unchanged(tmp_path):
    from docrot.acquire.repo import find_package
    root = _npm_tree(tmp_path)
    assert find_package(root, hint="momentic") == ("momentic", "momentic", "npm", root)
    private = tmp_path / "p"
    _write(private, "package.json", '{"name": "app", "private": true}')
    assert find_package(private, hint="other")[0] == "app"


def _npm_origin(tmp_path):
    origin = tmp_path / "momentic"
    _write(origin, "package.json", '{"name": "momentic"}')
    _write(origin, "README.md", "# Momentic\n")
    _write(origin, "src/index.ts", "export function run() {}\n")
    _write(origin, "node_modules/dep/index.js", "exports.dep = 1\n")
    _write(origin, "dist/index.js", "exports.built = 1\n")
    _write(origin, "dist/README.md", "# built docs\n")
    _write(origin, "app.min.js", "exports.m = 1\n")
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("add", "-f", ".", cwd=origin)
    _git("commit", "-qm", "one", cwd=origin)
    return origin


def test_ensure_sources_widens_a_cache_clone_once(tmp_path, monkeypatch):
    import docrot.acquire.repo as repo
    monkeypatch.setattr(repo, "CACHE", tmp_path / "cache")
    url = f"file://{_npm_origin(tmp_path)}"
    path, _, _ = repo.clone(url, log=lambda *_: None)
    assert not (path / "src/index.ts").exists()               # clone alone fetches no JS
    assert symbols_at_head(path, "momentic", "npm") == set()

    assert repo.ensure_sources(path, "npm", log=lambda *_: None) is True
    assert (path / "src/index.ts").exists()
    assert (path / "dist/README.md").exists()                 # docs are never excluded
    assert not (path / "node_modules").exists() and not (path / "dist/index.js").exists()
    assert not (path / "app.min.js").exists()
    assert symbols_at_head(path, "momentic", "npm") == {"run"}

    calls = []
    real = repo._run_git
    monkeypatch.setattr(repo, "_run_git", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    assert repo.ensure_sources(path / "src", "npm", log=lambda *_: None) is True    # idempotent
    assert calls == []

    path2, _, _ = repo.clone(url, log=lambda *_: None)         # a refresh keeps them
    assert path2 == path and (path / "src/index.ts").exists()


def test_ensure_sources_leaves_other_ecosystems_and_local_paths_alone(tmp_path, monkeypatch):
    import docrot.acquire.repo as repo
    monkeypatch.setattr(repo, "CACHE", tmp_path / "cache")
    origin = _npm_origin(tmp_path)
    path, _, _ = repo.clone(f"file://{origin}", log=lambda *_: None)
    assert repo.ensure_sources(path, "pypi") is False
    assert repo.ensure_sources(path, "nuget") is False
    assert not (path / "src/index.ts").exists()
    assert repo.ensure_sources(origin, "npm") is False         # the user's own tree
    assert repo.ensure_sources(tmp_path / "nowhere", "npm") is False


def test_a_failed_widening_is_logged_not_raised(tmp_path, monkeypatch):
    import docrot.acquire.repo as repo
    monkeypatch.setattr(repo, "CACHE", tmp_path / "cache")
    path, _, _ = repo.clone(f"file://{_npm_origin(tmp_path)}", log=lambda *_: None)

    def boom(*a, **k):
        raise repo.CloneError("git sparse-checkout of x timed out after 1s")
    monkeypatch.setattr(repo, "_run_git", boom)
    said = []
    assert repo.ensure_sources(path, "npm", log=said.append) is False
    assert "timed out" in said[0]


# ------------------------------------------------- every package in a checkout
def _relate(tmp_path):
    """Relate's shape: three real packages, and every decoy its repo holds."""
    root = tmp_path / "relate"
    _write(root, "surfaces/relate-python/pyproject.toml",
           '[build-system]\nrequires = ["maturin"]\n[project]\nname = "relate-db"\n'
           '[tool.maturin]\nmodule-name = "relate._native"\npython-source = "python"\n')
    _write(root, "surfaces/relate-python/python/relate/__init__.py", "")
    _write(root, "surfaces/relate-python/python/tests/test_x.py", "")
    _write(root, "clients/javascript/package.json", json.dumps({
        "name": "@relate/relate",
        "optionalDependencies": {"@relate/relate-linux-x64": "file:platforms/linux-x64",
                                 "@relate/relate-darwin-arm64": "file:platforms/darwin-arm64"}}))
    for plat in ("linux-x64", "darwin-arm64"):
        _write(root, f"clients/javascript/platforms/{plat}/package.json",
               json.dumps({"name": f"@relate/relate-{plat}"}))
    _write(root, "docs/api/bolt/examples/websocket/package.json", '{"name": "ws-example"}')
    pkg = "<PackageId>Relate.Net</PackageId><Version>1.1.1-local</Version>"
    _csproj(root / "clients/dotnet/src/Relate.Net/Relate.Net.csproj", pkg)
    _csproj(root / "clients/dotnet/tests/Relate.Net.Tests/Relate.Net.Tests.csproj")
    _csproj(root / "docs/api/dotnet/examples/Relate.Examples.csproj", "<OutputType>Exe</OutputType>")
    _write(root, "crates/core/Cargo.toml", "[package]\nname = 'relate-core'\n")
    return root


def _csproj(path, body=""):
    _write(path.parent, path.name, f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>{body}'
                                    "</PropertyGroup></Project>")


def test_find_packages_returns_exactly_the_three_relate_packages(tmp_path):
    from docrot.acquire.repo import find_packages
    root = _relate(tmp_path)
    got = find_packages(root, hint="relate")
    assert [(p.name, p.import_name, p.ecosystem, p.root.relative_to(root).as_posix()) for p in got] == [
        ("@relate/relate", "@relate/relate", "npm", "clients/javascript"),    # named like the repo
        ("relate-db", "relate", "pypi", "surfaces/relate-python/python"),     # then shallower
        ("Relate.Net", "Relate.Net", "nuget", "clients/dotnet/src/Relate.Net"),
    ]
    assert [p.name for p in find_packages(root)] == ["@relate/relate", "relate-db", "Relate.Net"]  # by depth


def test_find_packages_ranks_by_how_often_the_docs_mention_a_name(tmp_path):
    from docrot.acquire.repo import find_packages
    docs = "Relate.Net Relate.Net relate-db"
    got = find_packages(_relate(tmp_path), hint="other", rank=docs.count)
    assert [p.name for p in got] == ["Relate.Net", "relate-db", "@relate/relate"]


def test_find_package_keeps_the_python_distribution_name_apart_from_the_import(tmp_path):
    from docrot.acquire.repo import find_package
    root = _relate(tmp_path)
    assert find_package(root, hint="relate") == (
        "relate-db", "relate", "pypi", root / "surfaces/relate-python/python")
    assert find_package(root, want="Relate.Net")[:3] == ("Relate.Net", "Relate.Net", "nuget")
    assert find_package(root, want="@relate/relate")[2] == "npm"


def _py(tmp_path, name, pyproject, *dirs):
    root = tmp_path / name
    _write(root, "pyproject.toml", pyproject)
    for d in dirs:
        _write(root, f"{d}/__init__.py", "")
    return root


def test_import_name_comes_from_build_metadata(tmp_path):
    from docrot.acquire.repo import find_packages
    cases = {
        "maturin": (_py(tmp_path, "m", '[project]\nname = "relate-db"\n[tool.maturin]\n'
                        'module-name = "relate._native"\npython-source = "python"\n', "python/relate"),
                    ("relate", "python")),
        "hatch": (_py(tmp_path, "h", '[project]\nname = "acme-sdk"\n[tool.hatch.build.targets.wheel]\n'
                      'packages = ["src/acme"]\n', "src/acme"), ("acme", "src")),
        "poetry": (_py(tmp_path, "p", '[tool.poetry]\nname = "acme-sdk"\npackages = '
                       '[{include = "acme", from = "lib"}]\n', "lib/acme"), ("acme", "lib")),
        "setuptools package-dir": (_py(tmp_path, "s", '[project]\nname = "acme-sdk"\n'
                                       '[tool.setuptools]\npackages = ["acme", "acme.sub"]\n'
                                       '[tool.setuptools.package-dir]\n"" = "pysrc"\n', "pysrc/acme"),
                                   ("acme", "pysrc")),
        "setuptools find": (_py(tmp_path, "f", '[project]\nname = "acme-sdk"\n[tool.setuptools.packages.find]\n'
                                'where = ["src"]\n', "src/acme"), ("acme", "src")),
        "flit": (_py(tmp_path, "l", '[project]\nname = "acme-sdk"\n[tool.flit.module]\nname = "acme"\n',
                     "src/acme"), ("acme", "src")),
        "no metadata, a guess": (_py(tmp_path, "g", '[project]\nname = "acme-sdk"\n', "acme_sdk"),
                                 ("acme_sdk", "")),
    }
    for label, (root, (imp, sub)) in cases.items():
        (pkg,) = find_packages(root)
        assert (pkg.import_name, pkg.root) == (imp, root / sub if sub else root), label


def test_a_pyproject_that_only_configures_tools_is_not_a_package(tmp_path):
    from docrot.acquire.repo import find_packages
    root = _py(tmp_path, "r", '[tool.ruff]\nline-length = 100\n')
    assert find_packages(root) == []
    dev = tmp_path / "dev"
    _write(dev, "pyproject.toml", '[project]\nname = "mono-dev"\nversion = "0.0.0"\n')
    _write(dev, "lib/pyproject.toml", '[project]\nname = "mono"\n')
    _write(dev, "lib/mono/__init__.py", "")
    _write(dev, "lib/other/__init__.py", "")           # a stray package must not make it one
    assert [p.name for p in find_packages(dev)] == ["mono"]


def test_a_private_npm_workspace_root_yields_its_public_members(tmp_path):
    from docrot.acquire.repo import find_packages
    got = find_packages(_workspace(tmp_path))
    assert sorted(p.name for p in got) == ["@momentic/wizard", "gen-tool", "momentic"]


def test_npm_platform_packages_are_dropped_even_when_not_listed_with_file_paths(tmp_path):
    from docrot.acquire.repo import find_packages
    root = tmp_path / "bin"
    _write(root, "package.json", json.dumps({"name": "tool", "optionalDependencies": {"tool-linux": "1.0.0"}}))
    _write(root, "npm/linux/package.json", '{"name": "tool-linux"}')
    _write(root, "npm/inside/package.json", '{"name": "tool-inside"}')   # under a public package
    assert [p.name for p in find_packages(root)] == ["tool"]


def test_duende_style_client_samples_stay_out_but_a_client_library_with_a_package_id_is_in(tmp_path):
    from docrot.acquire.repo import find_packages
    root = tmp_path / "products"
    _csproj(root / "identity-server/src/IdentityServer/IdentityServer.csproj",
            "<PackageId>Duende.IdentityServer</PackageId>")
    _csproj(root / "identity-server/clients/src/ConsoleCode/ConsoleCode.csproj")
    _csproj(root / "identity-server/clients/src/Constants/Constants.csproj")           # a library, unpacked
    _csproj(root / "clients/dotnet/src/Acme.Client/Acme.Client.csproj", "<PackageId>Acme.Client</PackageId>")
    _csproj(root / "clients/dotnet/samples/Demo/Demo.csproj", "<PackageId>Acme.Demo</PackageId>")
    assert sorted(p.name for p in find_packages(root)) == ["Acme.Client", "Duende.IdentityServer"]


def test_the_walk_skips_dependencies_and_build_output(tmp_path):
    from docrot.acquire.repo import find_packages
    root = tmp_path / "r"
    _write(root, "package.json", '{"name": "real"}')
    for skipped in ("node_modules/dep", "vendor/dep", "third_party/dep", ".venv/dep", "dist/dep",
                    "build/dep", "target/dep"):
        _write(root, f"{skipped}/package.json", '{"name": "bad"}')
        _write(root, f"{skipped}/pyproject.toml", '[project]\nname = "bad"\n')
    assert [p.name for p in find_packages(root)] == ["real"]


def test_the_walk_is_bounded_and_finds_the_shallow_packages_first(tmp_path, monkeypatch):
    import docrot.acquire.repo as repo
    root = tmp_path / "big"
    _write(root, "pkg/package.json", '{"name": "near"}')
    for i in range(40):
        _write(root, f"deep/a{i}/b/c/package.json", f'{{"name": "far{i}"}}')
    monkeypatch.setattr(repo, "MAX_WALK_DIRS", 8)
    names = [p.name for p in repo.find_packages(root)]
    assert "near" in names and len(names) < 41


def test_find_package_compatibility_for_single_result_callers(tmp_path):
    from docrot.acquire.repo import find_package
    root = tmp_path / "mono"
    _write(root, "frontend/package.json", '{"name": "@mono/frontend"}')
    _write(root, "lib/pyproject.toml", '[project]\nname = "mono"\n')
    _write(root, "lib/mono/__init__.py", "")
    assert find_package(root)[:3] == ("mono", "mono", "pypi")                # python beats a frontend
    assert find_package(tmp_path / "empty") == ("", "", "pypi", tmp_path / "empty")
    deep = tmp_path / "deep"
    _write(deep, "a/b/package.json", '{"name": "deep-pkg"}')                 # past the old one-level search
    assert find_package(deep)[:3] == ("deep-pkg", "deep-pkg", "npm")


def test_concurrent_clones_of_one_repo_never_overlap_but_different_repos_may(tmp_path, monkeypatch):
    import threading
    import time

    import docrot.acquire.repo as repo
    inside, peak, lock = {}, {}, threading.Lock()

    def slow(url, log, timeout):
        with lock:
            inside[url] = inside.get(url, 0) + 1
            peak[url] = max(peak.get(url, 0), inside[url])
        time.sleep(0.05)
        with lock:
            inside[url] -= 1
        return tmp_path, "sha", "main"

    monkeypatch.setattr(repo, "_clone_remote", slow)
    urls = ["https://x.io/a/one"] * 3 + ["https://x.io/a/two"]
    threads = [threading.Thread(target=repo.clone, args=(u, print, 5)) for u in urls]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak == {"https://x.io/a/one": 1, "https://x.io/a/two": 1}


def test_other_manifests_come_from_git_when_a_sparse_clone_has_none_on_disk(tmp_path, monkeypatch):
    import docrot.acquire.repo as repo
    from docrot.acquire.detect import other_manifests
    monkeypatch.setattr(repo, "CACHE", tmp_path / "cache")
    origin = _repo(tmp_path, "alpha")
    (origin / "Cargo.toml").write_text('[package]\nname = "alpha"\n')
    (origin / "tools").mkdir()
    (origin / "tools" / "go.mod").write_text("module x\n")
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("add", ".", cwd=origin)
    _git("commit", "-qm", "one", cwd=origin)
    path, _, _ = repo.clone(f"file://{origin}", log=lambda *_: None)
    assert not (path / "Cargo.toml").exists()
    assert other_manifests(path) == ["Cargo.toml", "go.mod"]
