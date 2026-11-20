"""The standalone-CLI ecosystem as the rest of docrot sees it: the GitHub lookup
and its rate limit, `cli:` validation, the token setting, the report's wording,
and `docrot packages`. The lane itself is tests/test_cli_lane.py; selection
inside a scan is tests/test_scan.py."""
from __future__ import annotations

import json

import httpx
import pytest

from docrot import config as cfgmod
from docrot import settings
from docrot.acquire import binaries as b
from docrot.acquire.repo import npm_bins
from docrot.models import Extraction, Page, Release, Result, Snippet, Target
from docrot.report import build_report
from docrot.store import align_packages, clean_packages, split_package


# ------------------------------------------------------------ the GitHub lookup
def _rel(tag, assets, at="2026-11-01T00:00:00Z"):
    return {"tag_name": tag, "published_at": at, "draft": False, "prerelease": False,
            "assets": [{"name": a, "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{a}",
                        "size": 1} for a in assets]}


class Resp:
    def __init__(self, data=None, status=200, headers=None, text=""):
        self.data, self.status_code, self.headers, self.text = data, status, headers or {}, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://api.github.com"),
                                        response=httpx.Response(self.status_code, headers=self.headers,
                                                                text=self.text))

    def json(self):
        return self.data


def _github(monkeypatch, answer):
    calls = []

    def get(url, **kw):
        calls.append((url, kw["headers"]))
        return answer(url) if callable(answer) else answer

    monkeypatch.setattr(httpx, "get", get)
    return calls


def test_a_repo_with_a_linux_binary_in_its_recent_releases_is_installable(monkeypatch):
    calls = _github(monkeypatch, Resp([_rel("v1.1.0", ["relate-linux-amd64"]), _rel("v1.2.0", ["relate-linux-amd64"])]))
    got = b.lookup("https://github.com/relatedb/relate", "relate")
    assert got.installable and [r.label for r in got.releases] == ["1.1.0", "1.2.0"] and got.why == ""
    assert [r.label for r in got.rows()] == ["1.1.0", "1.2.0"] and len(calls) == 1


def test_lookup_asks_github_once_per_repo_and_command_however_often_it_is_asked(monkeypatch):
    calls = _github(monkeypatch, Resp([_rel("v1.0.0", ["relate-linux-amd64"])]))
    for _ in range(4):
        b.lookup("https://github.com/relatedb/relate", "relate")
        b.lookup("https://github.com/relatedb/relate/", "relate")
    assert len(calls) == 1


def test_a_repo_whose_releases_carry_only_other_products_is_not_installable_and_says_so(monkeypatch):
    _github(monkeypatch, Resp([_rel("v1.0.0", ["relate-1.0.0.jar", "relate-relate-1.0.0.tgz", "SHA256SUMS"])]))
    got = b.lookup("https://github.com/o/relate", "relate")
    assert not got.installable and not got.failed
    assert "none of the newest 1 releases of relate carries a Linux binary" in got.why


def test_a_repo_with_no_releases_is_not_a_failure(monkeypatch):
    _github(monkeypatch, Resp([]))
    got = b.lookup("https://github.com/o/relate", "relate")
    assert not got.installable and not got.failed and "has no GitHub releases of relate" in got.why


def test_an_exhausted_rate_limit_is_a_failure_that_names_the_token_and_is_not_remembered(monkeypatch):
    _github(monkeypatch, Resp(status=403, headers={"x-ratelimit-remaining": "0"}, text="API rate limit exceeded"))
    got = b.lookup("https://github.com/o/relate", "relate")
    assert got.failed and not got.installable and "rate limit" in got.why and "GITHUB_TOKEN" in got.why
    calls = _github(monkeypatch, Resp([_rel("v1.0.0", ["relate-linux-amd64"])]))
    assert b.lookup("https://github.com/o/relate", "relate").installable      # asked again, and answered
    assert len(calls) == 1


def test_a_dead_network_and_a_server_error_are_failures_not_missing_binaries(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    got = b.lookup("https://github.com/o/relate", "relate")
    assert got.failed and "could not reach GitHub" in got.why
    _github(monkeypatch, Resp(status=502))
    assert b.lookup("https://github.com/o/relate", "relate").failed


def test_a_repo_github_does_not_have_is_an_answer_not_a_failure(monkeypatch):
    _github(monkeypatch, Resp(status=404))
    got = b.lookup("https://github.com/o/nope", "nope")
    assert not got.failed and not got.installable and "no public repository" in got.why


def test_a_repo_that_is_not_on_github_is_never_asked_about(monkeypatch):
    calls = _github(monkeypatch, Resp([]))
    got = b.lookup("https://gitlab.com/o/r", "r")
    assert calls == [] and not got.installable and "not a GitHub repository" in got.why


def test_the_token_goes_to_github_only_as_a_bearer_header_and_the_settings_one_wins(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    calls = _github(monkeypatch, Resp([]))
    b.lookup("https://github.com/o/a", "a")
    assert calls[-1][1]["Authorization"] == "Bearer from-env"
    b.use_token("from-settings")
    b.lookup("https://github.com/o/b", "b")
    assert calls[-1][1]["Authorization"] == "Bearer from-settings"


# ---------------------------------------------------------------- validation
def test_cli_is_a_valid_prefix_and_normalises_like_the_others():
    assert split_package("cli:relate") == ("cli", "relate")
    assert clean_packages(["cli: relate", "npm:x"]) == ["cli:relate", "npm:x"]


@pytest.mark.parametrize("entry", ["cli:", "cli:@scope/x", "cli:has space", "cli:a/b"])
def test_a_cli_entry_must_be_a_command_name(entry):
    with pytest.raises(ValueError) as e:
        clean_packages([entry])
    assert "names no package" in e.value.args[0][0] or "not a command name" in e.value.args[0][0]


def test_a_cli_entry_without_a_github_repo_is_refused_where_the_repos_are_known():
    with pytest.raises(ValueError) as e:
        align_packages(["/srv/x"], ["cli:relate"])
    assert "needs the GitHub repository that publishes its release binaries" in e.value.args[0][0]
    with pytest.raises(ValueError):
        align_packages([], ["cli:relate"])
    assert align_packages(["https://github.com/o/relate"], ["cli:relate"]) == ["cli:relate"]
    assert align_packages(["https://github.com/o/relate"], ["npm:x", "cli:relate"]) == ["npm:x", "cli:relate"]


def test_a_stored_scan_with_a_cli_package_round_trips(store):
    rec = store.create([], ["https://github.com/o/relate"], packages=["cli:relate"])
    assert store.get(rec.id).packages == ["cli:relate"]


def test_the_registry_ecosystem_option_has_no_cli_choice():
    from docrot.store import ECOSYSTEMS, ScanOptions
    assert "cli" not in ECOSYSTEMS
    with pytest.raises(ValueError):
        ScanOptions(ecosystem="cli")


# ------------------------------------------------------------- package.json bins
@pytest.mark.parametrize("meta, want", [
    ({"name": "@relate/relate"}, ()),
    ({"name": "@relate/relate", "bin": "cli.js"}, ("relate",)),
    ({"name": "tool", "bin": {"a": "a.js", "b": "b.js"}}, ("a", "b")),
])
def test_bins_are_what_package_json_declares_and_none_is_an_answer(tmp_path, meta, want):
    (tmp_path / "package.json").write_text(json.dumps(meta))
    assert npm_bins(tmp_path) == want


def test_no_readable_package_json_is_unknown_not_empty(tmp_path):
    assert npm_bins(tmp_path) is None
    (tmp_path / "package.json").write_text("{not json")
    assert npm_bins(tmp_path) is None


# ------------------------------------------------------------------ settings
def test_github_token_is_an_optional_secret_that_never_enters_a_sandbox_and_is_redacted(monkeypatch):
    field = settings.BY_KEY["GITHUB_TOKEN"]
    assert field.secret and "rate limit" in field.hint and "never sent into a sandbox" in field.hint
    monkeypatch.setattr(cfgmod, "base_env", lambda env_file=None: {"SANDBOX_ENV_X": "1"})
    settings.save({"GITHUB_TOKEN": "ghp_secret_token_value"})
    cfg = cfgmod.load()
    assert cfg.github_token == "ghp_secret_token_value"
    assert "ghp_secret_token_value" not in cfg.sandbox_env.values() and "GITHUB_TOKEN" not in cfg.sandbox_env
    assert "ghp_secret_token_value" in cfg.secrets()
    assert "ghp" not in json.dumps(settings.describe({}))


def test_the_token_falls_back_to_the_environment_and_gh_token(monkeypatch):
    monkeypatch.setattr(cfgmod, "base_env", lambda env_file=None: {"GH_TOKEN": "from-gh"})
    assert cfgmod.load().github_token == "from-gh"


def test_the_sample_env_documents_the_token():
    from pathlib import Path
    text = (Path(cfgmod.ROOT) / ".env.sample").read_text()
    assert "GITHUB_TOKEN" in text and "rate limit" in text


# -------------------------------------------------------------------- report
def _report(ecosystem="cli", pinned=(), code="relate query a", statuses=("pass", "pass")):
    labels = [Release(label="1.1.0", released_at="2026-11-17"), Release(label="1.2.0", released_at="2026-11-20")]
    t = Target(package="relate", import_name="relate", ecosystem=ecosystem, repo_url="https://github.com/relatedb/relate",
               branch="main", releases=list(labels), versions=list(labels))
    page = Page(url="https://d.io/x", path="/x", origin="https://d.io", pinned_versions=list(pinned))
    s = Snippet(id="s1", page=page.url, lang="bash", code=code, package="relate", line=3)
    results = [Result(id="s1", page=page.url, version=v.label, package="relate", status=st,
                      stderr=json.dumps({"probe": {"lane": "cli"}, "cli": [], "failed": [], "notes": []}))
               for v, st in zip(labels, statuses, strict=True)]
    ex = Extraction(package="relate", import_name="relate", ecosystem=ecosystem, pages=[page], snippets=[s])
    r = build_report(ex, results, [t], ["https://d.io"], [], "llms.txt", 1.0)
    return [f for sec in r["sections"] for p in sec["pages"] for f in p["findings"]]


def test_a_clean_cli_page_says_help_was_read_and_nothing_ran_and_links_the_releases():
    [f] = _report()
    blob = json.dumps(f)
    assert f["kind"] == "clean" and "--help" in blob and "nothing was executed" in blob
    assert f["code_url"] == "https://github.com/relatedb/relate/releases"
    assert "__init__" not in blob and "introspected" not in blob and "npmjs" not in blob


def test_a_stale_cli_pin_names_github_releases_not_pyproject_or_a_registry():
    [f] = [f for f in _report(pinned=["1.1.0"], code="relate --version  # 1.1.0") if f["kind"] == "stale_pin"]
    assert f["code_where"] == "GitHub releases" and f["code_url"] == "https://github.com/relatedb/relate/releases"
    assert f["evidence"].startswith("GitHub releases  relate") and "==" not in json.dumps(f["doc_parts"])
    assert f["doc_parts"][1] == ["1.1.0", "del"] and f["code_parts"][1] == ["1.2.0", "ins"]


def test_a_stale_pin_for_the_other_ecosystems_keeps_its_wording():
    [f] = [f for f in _report("pypi", pinned=["1.1.0"], code="pip install relate==1.1.0") if f["kind"] == "stale_pin"]
    assert f["code_where"] == "pyproject.toml / registry" and f["evidence"].startswith("registry  relate")
