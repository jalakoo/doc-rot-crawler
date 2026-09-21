from __future__ import annotations

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
