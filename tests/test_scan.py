"""The pipeline with every network edge replaced: sites, git, registry,
sandboxes and the graph."""
from __future__ import annotations

import pytest

from docrot import scan as scanmod
from docrot.models import Page, Release, Result
from docrot.scan import ScanError, ScanInputs, execute_scan, run_scan
from docrot.store import PHASES, ScanOptions
from tests.fakes import fake_runner
from tests.fakes import run_async as run


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Patch the pipeline's I/O. Returns a dict recording what was called."""
    calls = {"matrix": [], "graph": None}

    async def acquire_site(url, fetcher, max_pages, log):
        return [Page(url=f"{url}/docs/guide/start", path="/docs/guide/start", title="Start",
                     origin=url, text="```python\nimport alpha\nalpha.go()\n```\n"
                                      "```python\nimport beta\nbeta.run()\n```\n")], "llms.txt"

    def clone(repo, log, timeout=600):
        name = repo.rstrip("/").split("/")[-1]
        root = tmp_path / name
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
        (root / name / "__init__.py").write_text("def go(): ...\ndef run(): ...\n")
        (root / "README.md").write_text("# readme\n")
        return root, "abc123", "main"

    def releases(package, ecosystem):
        return [Release(label="1.0", released_at="2026-01-01"),
                Release(label="2.0", released_at="2026-06-01")]

    async def run_matrix(snips, package, import_name, versions, cfg, log, install_spec=""):
        calls["matrix"].append((package, sorted(s.id for s in snips), versions, install_spec))
        return [Result(id=s.id, page=s.page, version=v, status="pass") for s in snips for v in versions]

    class Graph:
        def __init__(self, cfg, log): ...
        def load(self, ex, results, targets, head):
            calls["graph"] = ([t.package for t in targets], sorted(head))
        def close(self): ...

    import docrot.acquire as acq
    import docrot.acquire.repo as repo
    import docrot.graph as graph
    import docrot.verify as verify
    monkeypatch.setattr(acq, "acquire_site", acquire_site)
    monkeypatch.setattr(acq, "releases", releases)
    monkeypatch.setattr(repo, "clone", clone)
    monkeypatch.setattr(verify, "run_matrix", run_matrix)
    monkeypatch.setattr(graph, "GraphStore", Graph)
    return calls


def test_run_scan_multi_source(offline, cfg):
    phases, lines = [], []
    inp = ScanInputs(docs=["https://a.io", "https://b.io"],
                     repos=["https://github.com/o/alpha", "https://github.com/o/beta"],
                     options=ScanOptions(versions=2))
    out = run(run_scan(inp, cfg, lines.append, phases.append))

    assert phases == list(range(len(PHASES)))
    assert [t["package"] for t in out.report["packages"]] == ["alpha", "beta"]
    # each package verified once, against only its own snippets
    assert [(pkg, len(ids)) for pkg, ids, _, _ in offline["matrix"]] == [("alpha", 2), ("beta", 2)]
    assert {r.package for r in out.results} == {"alpha", "beta"}
    assert offline["graph"] == (["alpha", "beta"], ["alpha", "beta"])
    assert {p.url for p in out.extraction.pages if p.source == "repo"} == \
        {"repo://alpha/README.md", "repo://beta/README.md"}
    assert any("scan complete" in line for line in lines)


def test_run_scan_needs_a_package(offline, cfg):
    with pytest.raises(ScanError, match="no package to verify"):
        run(run_scan(ScanInputs(docs=["https://a.io"]), cfg, lambda m: None))


def test_run_scan_docs_only_with_explicit_package(offline, cfg):
    out = run(run_scan(ScanInputs(docs=["https://a.io"], packages=["alpha"]),
                               cfg, lambda m: None))
    assert out.report["package"] == "alpha"


def _as_npm(monkeypatch):
    """Make every cloned repo look like an npm package, and record the npm lane."""
    import docrot.acquire.repo as repo
    import docrot.verify as verify
    real = repo.find_package
    monkeypatch.setattr(repo, "find_package", lambda p, **kw: (*real(p)[:2], "npm", real(p)[3]))
    monkeypatch.setattr(repo, "ensure_sources", lambda *a, **kw: True)
    npm_calls = []

    async def run_npm_matrix(snips, package, versions, cfg, log):
        npm_calls.append((package, len(snips), versions))
        return [Result(id=s.id, page=s.page, version=v, status="pass") for s in snips for v in versions]

    monkeypatch.setattr(verify, "run_npm_matrix", run_npm_matrix)
    return npm_calls


def test_npm_targets_go_to_the_npm_lane(offline, cfg, monkeypatch):
    npm_calls = _as_npm(monkeypatch)
    out = run(run_scan(ScanInputs(docs=["https://a.io"], repos=["https://github.com/o/alpha"]),
                       cfg, lambda m: None))
    assert offline["matrix"] == []                      # not the pip lane
    assert [(p, n) for p, n, _ in npm_calls] == [("alpha", 2)]
    assert {r.status for r in out.results} == {"pass"}


def test_npm_sources_are_widened_before_reading_symbols(offline, cfg, monkeypatch):
    import docrot.acquire.repo as repo
    _as_npm(monkeypatch)
    order = []
    monkeypatch.setattr(repo, "ensure_sources", lambda path, eco, log, timeout: order.append(("ensure", eco, timeout)))
    real = repo.symbols_at_head
    monkeypatch.setattr(repo, "symbols_at_head", lambda *a: order.append(("symbols",)) or real(*a))
    run(run_scan(ScanInputs(docs=["https://a.io"], repos=["https://github.com/o/alpha"]),
                 cfg, lambda m: None))
    assert order[0] == ("ensure", "npm", cfg.clone_timeout) and ("symbols",) in order


def test_an_ecosystem_with_no_lane_stays_unverified(offline, cfg, monkeypatch):
    import docrot.acquire.repo as repo
    real = repo.find_package
    monkeypatch.setattr(repo, "find_package", lambda p, **kw: (*real(p)[:2], "cargo", real(p)[3]))
    lines = []
    out = run(run_scan(ScanInputs(docs=["https://a.io"], repos=["https://github.com/o/alpha"]),
                       cfg, lines.append))
    assert offline["matrix"] == []
    assert {r.status for r in out.results} == {"unverified"}
    assert any("no verification lane for cargo" in line for line in lines)


def test_a_named_package_on_a_repo_without_metadata_takes_the_chosen_ecosystem(
        offline, cfg, monkeypatch, tmp_path):
    import docrot.acquire.repo as repo
    _as_npm(monkeypatch)
    docs_only = tmp_path / "cli"
    docs_only.mkdir()
    (docs_only / "README.md").write_text("# docs\n")
    monkeypatch.setattr(repo, "clone", lambda r, log, timeout=600: (docs_only, "abc123", "main"))
    monkeypatch.setattr(repo, "find_package", lambda p, **kw: ("", "", "pypi", p))
    out = run(run_scan(ScanInputs(docs=["https://a.io"], repos=["https://github.com/o/cli"],
                                  packages=["my-cli"], options=ScanOptions(ecosystem="npm")),
                       cfg, lambda m: None))
    [t] = out.report["packages"]
    assert (t["package"], t["ecosystem"]) == ("my-cli", "npm")
    assert offline["matrix"] == []                      # never verified as a PyPI package


def test_a_repo_less_npm_package_keeps_its_name_as_the_import(offline, cfg, monkeypatch):
    seen = {}
    import docrot.extract as extract_pkg
    real = extract_pkg.extract

    def spy(pages, targets, *a, **kw):
        seen.update({t.package: t.import_name for t in targets})
        return real(pages, targets, *a, **kw)

    monkeypatch.setattr(extract_pkg, "extract", spy)
    _as_npm(monkeypatch)
    run(run_scan(ScanInputs(docs=["https://a.io"], packages=["my-cli"],
                            options=ScanOptions(ecosystem="npm")), cfg, lambda m: None))
    assert seen == {"my-cli": "my-cli"}


def test_the_missing_package_message_names_both_the_field_and_the_flag(offline, cfg):
    with pytest.raises(ScanError) as e:
        run(run_scan(ScanInputs(docs=["https://a.io"]), cfg, lambda m: None))
    assert "package field" in str(e.value) and "--package" in str(e.value)


def test_npm_versions_are_spread_across_major_lines(offline, cfg, monkeypatch):
    """The newest three of a package that ships daily are three near-identical
    patches; the newest of each major line is where the docs have rotted."""
    import docrot.acquire as acq
    npm_calls = _as_npm(monkeypatch)
    labels = ["1.0.0", "1.0.1", "2.0.0", "2.0.1", "3.0.0", "3.0.1"]
    monkeypatch.setattr(acq, "releases", lambda p, e: [Release(label=v, released_at="2026-01-01")
                                                       for v in labels])
    run(run_scan(ScanInputs(docs=["https://a.io"], repos=["https://github.com/o/alpha"],
                            options=ScanOptions(versions=3)), cfg, lambda m: None))
    assert npm_calls[0][2] == ["1.0.1", "2.0.1", "3.0.1"]


def test_execute_scan_records_a_run(store, cfg):
    rec = store.create(["https://docs.a.io"], ["https://github.com/a/alpha"])
    echoed = []
    st = execute_scan(store, rec.id, echo=echoed.append, runner=fake_runner(), cfg=cfg)
    assert st.state == "done" and st.phase == len(PHASES) - 1
    assert store.status(rec.id).state == "done"
    runs = store.runs(rec.id)
    assert len(runs) == 1 and runs[0].stats["findings"] > 0
    assert store.report(rec.id)["log"] == st.log
    assert any("scan complete" in m for m in echoed)


def test_execute_scan_failure_is_recorded_not_raised(store, cfg):
    rec = store.create(["https://docs.a.io"], [])
    st = execute_scan(store, rec.id, runner=fake_runner(fail="no pages acquired"), cfg=cfg)
    assert st.state == "failed" and st.error == "no pages acquired"
    assert store.runs(rec.id) == []


def test_execute_scan_survives_a_crash(store, cfg):
    rec = store.create(["https://docs.a.io"], [])

    def boom(inp, cfg, log, phase):
        raise RuntimeError("kaput")

    st = execute_scan(store, rec.id, runner=boom, cfg=cfg)
    assert st.state == "failed" and "RuntimeError: kaput" in st.error


def test_style_classes():
    assert scanmod.style("  ! default branch") == "t-hit"
    assert scanmod.style("  660 results · 39 failing") == "t-hit"
    assert scanmod.style("› scan complete — 0 findings") == "t-ok"
    assert scanmod.style("  graph loaded") == "t-sys"


def test_a_package_with_no_releases_is_installed_from_its_repo(offline, cfg, monkeypatch):
    """switch-core is not on PyPI, so `pip install switch-core` finds nothing and
    the whole scan comes back unverified. The repo itself is installable."""
    import docrot.acquire as acq
    monkeypatch.setattr(acq, "releases", lambda package, ecosystem: [])

    lines = []
    out = run(run_scan(ScanInputs(docs=["https://a.io"], repos=["https://github.com/o/alpha"]),
                       cfg, lines.append))

    [(package, _ids, versions, spec)] = offline["matrix"]
    assert package == "alpha" and versions == ["HEAD"]
    assert spec == "git+https://github.com/o/alpha@abc123"
    assert any("installing the repo itself at abc123" in line for line in lines)
    assert out.report["stats"]["versions"] == 1


def test_source_install_points_at_the_package_inside_a_monorepo(tmp_path):
    from docrot.scan import _source_spec
    clone = tmp_path / "repo"
    (clone / "core").mkdir(parents=True)
    assert _source_spec("https://github.com/o/r.git", "abc123", clone, clone / "core") == \
        "git+https://github.com/o/r@abc123#subdirectory=core"
    assert _source_spec("https://github.com/o/r", "abc123", clone, clone) == \
        "git+https://github.com/o/r@abc123"
    # a local checkout the sandbox cannot see, and an unknown commit
    assert _source_spec("/local/path", "abc123", clone, clone) == ""
    assert _source_spec("https://github.com/o/r", "local", clone, clone) == ""
