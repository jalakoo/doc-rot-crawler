from __future__ import annotations

import json

import pytest

from docrot.models import Extraction, Page, Release, Result, Snippet, Target
from docrot.report import build_report
from tests.fakes import make_outcome


def test_single_source_report_keeps_the_original_shape():
    r = make_outcome(["https://docs.a.io"], ["https://github.com/a/alpha"]).report
    assert r["docs_url"] == "https://docs.a.io" and r["repo_url"] == "https://github.com/a/alpha"
    assert r["package"] == "alpha" and [v["label"] for v in r["versions"]] == ["1.0.0", "1.1.0", "2.0.0"]
    assert [s["name"] for s in r["sections"]] == ["Guide", "About", "Repository docs"]
    assert {row["pkg"] for row in r["timeline"]} == {0}
    assert r["stats"]["versions"] == 3


def test_multi_source_report():
    r = make_outcome(["https://docs.a.io", "https://guides.a.io"],
                     ["https://github.com/a/alpha", "https://github.com/a/beta"]).report
    assert r["docs_urls"] == ["https://docs.a.io", "https://guides.a.io"]
    assert [p["package"] for p in r["packages"]] == ["alpha", "beta"]
    assert r["sources"] == [
        {"kind": "docs", "url": "https://docs.a.io", "pages": 3},
        {"kind": "docs", "url": "https://guides.a.io", "pages": 3},
        {"kind": "repo", "url": "https://github.com/a/alpha", "pages": 1},
        {"kind": "repo", "url": "https://github.com/a/beta", "pages": 1},
    ]
    names = [s["name"] for s in r["sections"]]
    assert "Guide · docs.a.io" in names and "Repository docs · beta" in names
    assert {row["pkg"] for row in r["timeline"]} == {0, 1}
    assert r["stats"]["versions"] == 6


def test_findings_link_to_the_snippets_own_repo():
    r = make_outcome(["https://docs.a.io", "https://guides.a.io"],
                     ["https://github.com/a/alpha", "https://github.com/a/beta"]).report
    fails = [f for s in r["sections"] for p in s["pages"] for f in p["findings"]
             if f["kind"] == "missing_symbol"]
    assert {f["code_url"].split("/blob/")[0] for f in fails} == \
        {"https://github.com/a/alpha", "https://github.com/a/beta"}


def test_versions_of_two_packages_do_not_mix():
    """Same label, different packages: each page's verdict uses its own package."""
    v = [Release(label="1.0.0", released_at="2026-01-01")]
    targets = [Target(package="a", import_name="a", releases=v, versions=v),
               Target(package="b", import_name="b", releases=v, versions=v)]
    pages = [Page(url="https://d.io/x/a", path="/x/a", title="A", origin="https://d.io"),
             Page(url="https://d.io/x/b", path="/x/b", title="B", origin="https://d.io")]
    snips = [Snippet(id="sa", page=pages[0].url, lang="python", code="import a", package="a"),
             Snippet(id="sb", page=pages[1].url, lang="python", code="import b", package="b")]
    results = [Result(id="sa", page=pages[0].url, version="1.0.0", status="pass", package="a"),
               Result(id="sb", page=pages[1].url, version="1.0.0", status="fail", package="b",
                      stderr="b.x: AttributeError: gone")]
    ex = Extraction(package="a", import_name="a", pages=pages, snippets=snips)
    r = build_report(ex, results, targets, ["https://d.io"], [], "sitemap.xml", 1.0)
    rows = {(row["pkg"], row["n"]): row["c"] for row in r["timeline"]}
    assert rows == {(0, "A"): ["pass"], (1, "B"): ["fail"]}


def test_a_page_nothing_ran_against_is_unverified_not_clean():
    """An install failure, or snippets that can never run, must not read as
    "no drift found" - that is the quiet lie the tool exists to avoid."""
    v = [Release(label="1.0", released_at="2026-01-01")]
    t = Target(package="alpha", import_name="alpha", repo_url="https://github.com/a/alpha",
               releases=v, versions=v)
    pages = [Page(url="https://d.io/x/ran", path="/x/ran", title="Ran", origin="https://d.io"),
             Page(url="https://d.io/x/never", path="/x/never", title="Never", origin="https://d.io")]
    snips = [Snippet(id="ok", page=pages[0].url, lang="python", code="import alpha", package="alpha"),
             Snippet(id="skip", page=pages[1].url, lang="text", code="alpha.go()", package="alpha",
                     tier="unverifiable")]
    results = [Result(id="ok", page=pages[0].url, version="1.0", status="pass", package="alpha"),
               Result(id="skip", page=pages[1].url, version="1.0", status="unverified", package="alpha")]
    ex = Extraction(package="alpha", import_name="alpha", pages=pages, snippets=snips)
    r = build_report(ex, results, [t], ["https://d.io"], [], "llms.txt", 1.0)

    by_name = {p["name"]: p for s in r["sections"] for p in s["pages"]}
    assert by_name["Ran"]["st"] == "pass"
    assert by_name["Ran"]["findings"][0]["kind"] == "clean"

    never = by_name["Never"]
    assert never["st"] == "unlit"                       # not counted as a clean page
    [finding] = never["findings"]
    assert finding["kind"] == "unverified" and finding["title"].startswith("NOT VERIFIED")
    assert "unverifiable" in finding["desc"] and finding["damage"] == "NOT COUNTED"
    assert r["stats"]["drifted"] == 0 and r["stats"]["findings"] == 0


# ----------------------------------------------------------------- npm findings
NPM_V = [Release(label="1.0.0", released_at="2026-01-01"), Release(label="2.0.0", released_at="2026-06-01")]
NPM_FINDING = {
    "export": {"check": "export", "subject": "acme.gone", "line": 2,
               "message": "'gone' is not exported by 'acme' (at runtime)", "detail": ""},
    "module": {"check": "module", "subject": "acme/server", "line": 1,
               "message": "[ERR_PACKAGE_PATH_NOT_EXPORTED] Package subpath './server' is not defined by \"exports\"",
               "detail": ""},
    "bin": {"check": "bin", "subject": "acme-cli", "line": 1,
            "message": "acme installs no bin named 'acme-cli' (bins: acme)", "detail": ""},
    "command": {"check": "command", "subject": "acme deploy", "line": 1,
                "message": "the CLI does not know the command 'deploy': error: unknown command 'deploy'",
                "detail": "Usage: acme [options] [command]"},
    "flag": {"check": "flag", "subject": "acme run --ci", "line": 1,
             "message": "--ci does not appear in `acme run --help`", "detail": "Usage: acme run"},
    "runtime": {"check": "runtime", "subject": "acme", "line": 3,
                "message": "TypeError: client.go is not a function", "detail": "at snippet-0.mjs:3"},
}
NPM_CODE = {"export": "import { ok, gone } from 'acme';\nconst x = gone();",
            "module": "import { s } from 'acme/server';", "bin": "npx acme-cli go",
            "command": "npx acme deploy", "flag": "npx acme run --ci",
            "runtime": "const c = 1;\nconst d = 2;\nclient.go();"}


def _npm_report(check: str, statuses: list[str], repo: str = "https://github.com/a/acme", pinned=()):
    t = Target(package="acme", import_name="acme", ecosystem="npm", repo_url=repo, branch="main",
               releases=list(NPM_V), versions=list(NPM_V))
    page = Page(url="https://d.io/x/cli", path="/x/cli", title="CLI", origin="https://d.io",
                pinned_versions=list(pinned))
    s = Snippet(id="s1", page=page.url, lang="bash" if check in ("bin", "command", "flag") else "js",
                code=NPM_CODE[check], package="acme", line=40)
    body = json.dumps({"probe": {"lane": "structural"}, "npm": [NPM_FINDING[check]],
                       "failed": [f"{NPM_FINDING[check]['subject']}: {NPM_FINDING[check]['message']}"],
                       "notes": []})
    ok = json.dumps({"probe": {}, "npm": [], "failed": [], "notes": []})
    results = [Result(id="s1", page=page.url, version=v.label, package="acme", status=st,
                      stderr=body if st == "fail" else ok)
               for v, st in zip(NPM_V, statuses, strict=True)]
    ex = Extraction(package="acme", import_name="acme", ecosystem="npm", pages=[page], snippets=[s])
    r = build_report(ex, results, [t], ["https://d.io"], [], "llms.txt", 1.0)
    return [f for sec in r["sections"] for p in sec["pages"] for f in p["findings"]]


KINDS = {"export": "missing_symbol", "module": "missing_symbol", "bin": "missing_symbol",
         "command": "missing_symbol", "flag": "signature", "runtime": "runtime"}


@pytest.mark.parametrize("check", list(KINDS))
def test_npm_failing_on_the_latest_release_only_is_rot(check):
    [f] = [f for f in _npm_report(check, ["pass", "fail"]) if f["kind"] in KINDS.values()]
    assert f["kind"] == KINDS[check]
    assert "1.0.0" in f["desc"] and "2.0.0" in f["desc"] and "no longer" in f["desc"]
    assert f["doc_stamp"] == "1.0.0" and "NEWER" not in f["damage"]
    assert f["code_parts"][0][0] == NPM_FINDING[check]["message"][:300]
    assert f["evidence"].startswith(NPM_FINDING[check]["subject"])
    assert f["code_url"] == "https://github.com/a/acme"          # not <import>/__init__.py
    assert "__init__" not in json.dumps(f) and ".py" not in f["code_url"]


@pytest.mark.parametrize("check", list(KINDS))
def test_npm_failing_only_on_older_releases_documents_something_newer(check):
    [f] = [f for f in _npm_report(check, ["fail", "pass"]) if f["kind"] in KINDS.values()]
    assert f["title"] == "REQUIRES A NEWER RELEASE" and f["damage"] == "NEWER API"
    assert "only exists on 2.0.0" in f["desc"]


@pytest.mark.parametrize("check", list(KINDS))
def test_npm_failing_everywhere_never_held(check):
    [f] = [f for f in _npm_report(check, ["fail", "fail"]) if f["kind"] in KINDS.values()]
    assert f["kind"] == KINDS[check] and "1.0.0, 2.0.0" in f["desc"]
    assert f["doc_stamp"] == "no release tested"


def test_npm_titles_and_voice_per_check():
    def title(check, statuses):
        return next(f for f in _npm_report(check, statuses) if f["kind"] in KINDS.values())["title"]
    assert title("command", ["pass", "fail"]) == "DOCUMENTS A COMMAND THE CLI NO LONGER HAS"
    assert title("flag", ["pass", "fail"]) == "DOCUMENTS A FLAG THE CLI DOES NOT ACCEPT"
    assert title("export", ["pass", "fail"]) == "EXPORT REMOVED OR RENAMED"
    assert title("module", ["pass", "fail"]) == "MODULE PATH REMOVED OR MOVED"
    assert title("runtime", ["pass", "fail"]) == "SNIPPET FAILS WHEN RUN"


def test_a_flag_absent_everywhere_is_not_claimed_to_have_been_removed():
    [f] = [f for f in _npm_report("flag", ["fail", "fail"]) if f["kind"] == "signature"]
    assert "absent from the CLI's own" in f["desc"] and "--help" in f["desc"]
    assert "removed" not in f["desc"] and "no longer" not in f["desc"]
    assert f["label"] == "not in --help" and f["damage"] == "-1 FLAG"


def test_npm_finding_shows_the_offending_line_and_links_npm_when_there_is_no_repo():
    [f] = [f for f in _npm_report("export", ["pass", "fail"], repo="") if f["kind"] == "missing_symbol"]
    assert f["doc_parts"] == [["const x = gone();", "del"]]
    assert f["doc_where"].endswith("line 2")
    assert f["code_url"] == "https://www.npmjs.com/package/acme/v/2.0.0"
    assert f["code_where"] == "node import · 2.0.0"


def test_npm_cli_findings_name_the_cli_help_as_the_source():
    [f] = [f for f in _npm_report("command", ["pass", "fail"]) if f["kind"] == "missing_symbol"]
    assert f["code_where"] == "acme --help · 2.0.0" and f["damage"] == "-1 COMMAND"
    assert "unknown command 'deploy'" in f["evidence"] and "Usage: acme" in f["evidence"]


def test_an_npm_page_with_nothing_failing_reads_clean_without_python_paths():
    [f] = _npm_report("export", ["pass", "pass"])
    assert f["kind"] == "clean" and ".py" not in f["code_url"] and "__init__" not in f["code_where"]
    assert "CLI commands and flags" in f["code_parts"][0][0]


def test_an_npm_stale_pin_is_written_with_at_not_double_equals():
    finds = _npm_report("export", ["pass", "pass"], pinned=["1.0.0"])
    [pin] = [f for f in finds if f["kind"] == "stale_pin"]
    assert pin["code_where"] == "package.json / registry"
    assert pin["doc_parts"][0][0].endswith("acme@") and pin["doc_parts"][1] == ["1.0.0", "del"]
    assert pin["code_parts"][1] == ["2.0.0", "ins"] and "==" not in json.dumps(pin["doc_parts"])


def test_a_pass_record_with_an_empty_npm_list_falls_through_to_the_generic_path():
    t = Target(package="acme", import_name="acme", ecosystem="npm", releases=list(NPM_V), versions=list(NPM_V))
    page = Page(url="https://d.io/x/y", path="/x/y", title="Y", origin="https://d.io")
    s = Snippet(id="s1", page=page.url, lang="js", code="client.go()", package="acme")
    empty = json.dumps({"probe": {}, "npm": [], "failed": [], "notes": []})
    results = [Result(id="s1", page=page.url, version="2.0.0", package="acme", status="fail", stderr=empty)]
    ex = Extraction(package="acme", import_name="acme", ecosystem="npm", pages=[page], snippets=[s])
    r = build_report(ex, results, [t], ["https://d.io"], [], "llms.txt", 1.0)
    [f] = [f for sec in r["sections"] for p in sec["pages"] for f in p["findings"]]
    assert f["kind"] == "runtime" and f["title"] == "SNIPPET FAILS WHEN RUN"
