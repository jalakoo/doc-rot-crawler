from __future__ import annotations

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
