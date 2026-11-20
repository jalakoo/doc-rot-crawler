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


# ------------------------------------------------- several packages per scan
RELATE_DOCS = """\
# Relate

```python
import relate
relate.connect()
```

```js
import { Relate } from "@relate/relate";
new Relate();
```

```csharp
using Relate.Net;
var db = new RelateClient();
```
"""


def _polyglot(root):
    """Relate's shape: one repo, a package in each of PyPI, npm and NuGet."""
    (root / "py" / "relate").mkdir(parents=True)
    (root / "py" / "pyproject.toml").write_text('[project]\nname = "relate-db"\n')
    (root / "py" / "relate" / "__init__.py").write_text("def connect(): ...\n")
    (root / "js").mkdir()
    (root / "js" / "package.json").write_text('{"name": "@relate/relate", "version": "1.0.0"}')
    (root / "js" / "index.js").write_text("export class Relate {}\n")
    (root / "dotnet" / "Relate.Net").mkdir(parents=True)
    (root / "dotnet" / "Relate.Net" / "Relate.Net.csproj").write_text(
        "<Project><PropertyGroup><PackageId>Relate.Net</PackageId></PropertyGroup></Project>")
    (root / "dotnet" / "Relate.Net" / "Client.cs").write_text(
        "namespace Relate.Net { public class RelateClient { public void Run() {} } }\n")
    (root / "README.md").write_text("# relate\n")
    return root


@pytest.fixture
def lanes(offline, monkeypatch, tmp_path):
    """The polyglot repo as the only clone, docs that cover all three of its
    packages, and a recording stand-in for each lane."""
    import docrot.acquire as acq
    import docrot.acquire.repo as repo
    import docrot.verify as verify
    import docrot.verify.dotnet as dotnet

    root = _polyglot(tmp_path / "relate")
    monkeypatch.setattr(repo, "clone", lambda r, log, timeout=600: (root, "abc123", "main"))
    monkeypatch.setattr(repo, "ensure_sources", lambda *a, **kw: True)
    seen = {"docs": RELATE_DOCS, "npm": [], "dotnet": [], "lookups": []}

    async def acquire_site(url, fetcher, max_pages, log):
        return [Page(url=f"{url}/docs/start", path="/docs/start", title="Start", origin=url,
                     text=seen["docs"])], "llms.txt"

    def lanes_result(snips, versions):
        return [Result(id=s.id, page=s.page, version=v, status="pass") for s in snips for v in versions]

    async def run_npm_matrix(snips, package, versions, cfg, log):
        seen["npm"].append((package, len(snips)))
        return lanes_result(snips, versions)

    async def run_dotnet_matrix(snips, package, ns, versions, cfg, log, head=None):
        seen["dotnet"].append((package, len(snips)))
        return lanes_result(snips, versions)

    def releases(package, ecosystem):
        seen["lookups"].append((package, ecosystem))
        return [Release(label="1.0", released_at="2026-01-01")]

    # the GitHub side of a standalone CLI: what its releases say, and the lane
    import docrot.acquire.binaries as binaries
    import docrot.verify.cli as cli_lane
    seen["cli"] = []                   # (name, versions, repo_url, [snippet ids])
    seen["asked"] = []                 # (repo, command) the scan looked up
    seen["github"] = {}                # command -> CliLookup the fake GitHub answers with

    def lookup(repo, name):
        seen["asked"].append((repo, name))
        return seen["github"].get(name) or binaries.CliLookup(
            name, why=f"{repo} has no GitHub releases of {name}")

    async def run_cli_matrix(snips, name, versions, cfg, log, *, repo_url="", install="", bins=()):
        seen["cli"].append((name, versions, repo_url, sorted(s.id for s in snips)))
        return lanes_result(snips, versions)

    monkeypatch.setattr(acq, "acquire_site", acquire_site)
    monkeypatch.setattr(acq, "releases", releases)
    monkeypatch.setattr(verify, "run_npm_matrix", run_npm_matrix)
    monkeypatch.setattr(dotnet, "run_dotnet_matrix", run_dotnet_matrix)
    monkeypatch.setattr(binaries, "lookup", lookup)
    monkeypatch.setattr(cli_lane, "run_cli_matrix", run_cli_matrix)
    seen["root"] = root
    return seen


def _scan(**kw):
    kw.setdefault("docs", ["https://a.io"])
    kw.setdefault("repos", ["https://github.com/o/relate"])
    return ScanInputs(**kw)


def _targets(out):
    return [(t["package"], t["ecosystem"]) for t in out.report["packages"]]


def test_a_polyglot_repo_yields_a_target_per_package_each_in_its_own_lane(
        offline, lanes, cfg, monkeypatch):
    import docrot.extract as extract_pkg
    imports = {}
    real = extract_pkg.extract

    def spy(pages, targets, *a, **kw):
        imports.update({t.package: t.import_name for t in targets})
        return real(pages, targets, *a, **kw)

    monkeypatch.setattr(extract_pkg, "extract", spy)
    lines = []
    out = run(run_scan(_scan(), cfg, lines.append))

    assert _targets(out) == [("relate-db", "pypi"), ("@relate/relate", "npm"),
                             ("Relate.Net", "nuget")]
    assert imports == {"relate-db": "relate", "@relate/relate": "@relate/relate",
                       "Relate.Net": "Relate.Net"}
    assert [m[0] for m in offline["matrix"]] == ["relate-db"]
    assert [p for p, _ in lanes["npm"]] == ["@relate/relate"]
    assert [p for p, _ in lanes["dotnet"]] == ["Relate.Net"]
    assert any("relate-db (pypi) · import relate · " in line and "public symbols at HEAD" in line
               for line in lines)


def test_the_primary_is_the_package_a_single_package_scan_picked(offline, lanes, cfg):
    """Perseus, Streamlit and the rest verified `find_package`'s pick; widening
    must not hand extraction's fallback attribution to a different package."""
    out = run(run_scan(_scan(), cfg, lambda m: None))
    assert out.extraction.package == "relate-db"
    assert out.extraction.packages[0].package == "relate-db"


def test_automatic_selection_skips_packages_the_docs_never_mention(offline, lanes, cfg):
    lanes["docs"] = "```python\nimport relate\nrelate.connect()\n```\n"
    lines = []
    out = run(run_scan(_scan(), cfg, lines.append))
    assert _targets(out) == [("relate-db", "pypi")]
    skipped = next(line for line in lines if "skipped" in line)
    assert "never mention" in skipped and "npm:@relate/relate" in skipped \
        and "nuget:Relate.Net" in skipped and "name them to include them" in skipped
    assert lanes["npm"] == [] and lanes["dotnet"] == []


def test_a_bare_word_in_prose_is_not_a_mention(offline, lanes, cfg):
    """`relate` and `Relate` are on every page of Relate's docs."""
    lanes["docs"] = ("Relate lets you relate things. Relate is fast, and relate is simple.\n\n"
                     "```python\nimport relate\n```\n")
    out = run(run_scan(_scan(), cfg, lambda m: None))
    assert _targets(out) == [("relate-db", "pypi")]


def test_an_install_command_is_a_mention(offline, lanes, cfg):
    lanes["docs"] = "```bash\nnpm install @relate/relate\npip install relate-db\n```\n"
    out = run(run_scan(_scan(), cfg, lambda m: None))
    assert _targets(out) == [("relate-db", "pypi"), ("@relate/relate", "npm")]


def test_a_repo_whose_docs_mention_nothing_verifies_its_single_best_package(offline, lanes, cfg):
    lanes["docs"] = "Nothing about packages here.\n"
    lines = []
    out = run(run_scan(_scan(), cfg, lines.append))
    assert _targets(out) == [("relate-db", "pypi")]
    assert any("mention none of them" in line and "name others to include them" in line
               for line in lines)


def test_automatic_selection_is_capped_per_repo(offline, lanes, cfg, monkeypatch):
    monkeypatch.setattr(scanmod, "MAX_AUTO_PACKAGES", 2)
    lines = []
    out = run(run_scan(_scan(), cfg, lines.append))
    assert len(out.report["packages"]) == 2
    assert out.report["packages"][0]["package"] == "relate-db"
    assert any("past the cap of 2" in line for line in lines)


def test_the_cap_never_applies_to_packages_the_scan_names(offline, lanes, cfg, monkeypatch):
    monkeypatch.setattr(scanmod, "MAX_AUTO_PACKAGES", 1)
    out = run(run_scan(_scan(packages=["relate-db", "npm:@relate/relate", "Relate.Net"]),
                       cfg, lambda m: None))
    assert len(out.report["packages"]) == 3


def test_naming_one_package_restricts_its_repo_to_it(offline, lanes, cfg):
    out = run(run_scan(_scan(packages=["Relate.Net"]), cfg, lambda m: None))
    assert _targets(out) == [("Relate.Net", "nuget")]


def test_a_package_is_matched_by_unscoped_tail_case_and_any_position(offline, lanes, cfg):
    """`--repo A --repo B --package relate` finds it in whichever repo has it, and
    `relate` means `@relate/relate` as `wizard` means `@momentic/wizard`."""
    out = run(run_scan(_scan(packages=["", "RELATE_DB", "npm:relate"]), cfg, lambda m: None))
    assert _targets(out) == [("relate-db", "pypi"), ("@relate/relate", "npm")]


def test_a_prefix_picks_between_packages_that_share_a_name(offline, lanes, cfg):
    """`relate` answers to both `relate-db`'s import and `@relate/relate`'s tail;
    the prefix says which was meant."""
    out = run(run_scan(_scan(packages=["npm:relate"]), cfg, lambda m: None))
    assert _targets(out) == [("@relate/relate", "npm")]


def test_a_named_package_the_repo_lacks_is_verified_from_the_registry_with_the_repo_linked(
        offline, lanes, cfg):
    out = run(run_scan(_scan(packages=["npm:relate-extras"]), cfg, lambda m: None))
    [t] = out.report["packages"]
    assert (t["package"], t["ecosystem"]) == ("relate-extras", "npm")
    assert out.extraction.packages[0].repo_url == "https://github.com/o/relate"
    assert lanes["npm"] and offline["matrix"] == []


def test_a_registry_only_package_with_a_prefix_never_asks_the_registries(offline, lanes, cfg):
    out = run(run_scan(ScanInputs(docs=["https://a.io"], packages=["nuget:Some.Lib"],
                                  options=ScanOptions(ecosystem="auto")), cfg, lambda m: None))
    assert _targets(out) == [("Some.Lib", "nuget")]
    assert lanes["lookups"] == [("Some.Lib", "nuget")]       # only the release listing itself


def _on(monkeypatch, *registries):
    """Make a name exist on exactly these registries."""
    import docrot.acquire as acq
    seen = []

    def releases(package, ecosystem):
        seen.append((package, ecosystem))
        return [Release(label="1.0", released_at="2026-01-01")] if ecosystem in registries else []

    monkeypatch.setattr(acq, "releases", releases)
    return seen


def test_auto_uses_the_one_registry_that_has_a_registry_only_name(offline, lanes, cfg, monkeypatch):
    _on(monkeypatch, "npm")
    out = run(run_scan(ScanInputs(docs=["https://a.io"], packages=["momentic"],
                                  options=ScanOptions(ecosystem="auto")), cfg, lambda m: None))
    assert _targets(out) == [("momentic", "npm")]


def test_auto_stops_when_several_registries_have_the_name_and_shows_the_prefix(
        offline, lanes, cfg, monkeypatch):
    _on(monkeypatch, "pypi", "npm")
    with pytest.raises(ScanError) as e:
        run(run_scan(ScanInputs(docs=["https://a.io"], packages=["relate"],
                                options=ScanOptions(ecosystem="auto")), cfg, lambda m: None))
    msg = str(e.value)
    assert "pypi and npm" in msg and "pypi:relate" in msg and "npm:relate" in msg


def test_auto_stops_when_no_registry_has_the_name(offline, lanes, cfg, monkeypatch):
    _on(monkeypatch)
    with pytest.raises(ScanError, match=r"no package to verify.*nothing-here.*PyPI, npm or NuGet"):
        run(run_scan(ScanInputs(docs=["https://a.io"], packages=["nothing-here"],
                                options=ScanOptions(ecosystem="auto")), cfg, lambda m: None))


def test_auto_treats_an_unpublished_package_linked_to_a_repo_as_pypi_from_source(
        offline, lanes, cfg, monkeypatch):
    """switch-core is on no registry; its repo is what gets installed."""
    _on(monkeypatch)
    out = run(run_scan(_scan(packages=["switch-core-x"], options=ScanOptions(ecosystem="auto")),
                       cfg, lambda m: None))
    assert _targets(out) == [("switch-core-x", "pypi")]


def test_a_scoped_name_under_auto_is_npm_without_a_lookup(offline, lanes, cfg, monkeypatch):
    seen = _on(monkeypatch)
    out = run(run_scan(ScanInputs(docs=["https://a.io"], packages=["@acme/tool"],
                                  options=ScanOptions(ecosystem="auto")), cfg, lambda m: None))
    assert _targets(out) == [("@acme/tool", "npm")]
    assert ("@acme/tool", "pypi") not in seen


def test_an_old_style_record_still_means_the_same(offline, cfg, monkeypatch, tmp_path):
    """momentic-532a75: ecosystem npm, one positional package, a repo that holds
    only docs. The package comes from npm, linked to the repo, never from PyPI,
    and no registry is asked which one it is on."""
    import docrot.acquire as acq
    import docrot.acquire.repo as repo
    npm_calls = _as_npm(monkeypatch)
    docs_only = tmp_path / "cli"
    docs_only.mkdir()
    (docs_only / "README.md").write_text("# docs\n")
    monkeypatch.setattr(repo, "clone", lambda r, log, timeout=600: (docs_only, "abc123", "main"))
    monkeypatch.setattr(repo, "find_package", lambda p, **kw: ("", "", "pypi", p))
    lookups = []
    monkeypatch.setattr(acq, "releases", lambda p, e: lookups.append((p, e)) or
                        [Release(label="1.0.0", released_at="2026-01-01")])
    out = run(run_scan(ScanInputs(docs=["https://a.io"], repos=["https://github.com/o/cli"],
                                  packages=["momentic"], options=ScanOptions(ecosystem="npm")),
                       cfg, lambda m: None))
    [t] = out.report["packages"]
    assert (t["package"], t["ecosystem"]) == ("momentic", "npm")
    assert out.extraction.packages[0].repo_url == "https://github.com/o/cli"
    assert lookups == [("momentic", "npm")] and npm_calls and offline["matrix"] == []


def test_a_package_two_repos_both_hold_is_verified_once_with_both_heads(offline, cfg):
    out = run(run_scan(ScanInputs(docs=["https://a.io"],
                                  repos=["https://github.com/o/alpha", "https://x.io/p/alpha"]),
                       cfg, lambda m: None))
    assert [t["package"] for t in out.report["packages"]] == ["alpha"]
    assert len(offline["matrix"]) == 1


def test_the_same_name_on_two_registries_is_verified_once(offline, lanes, cfg):
    """Results, snippets and the graph are keyed by package name."""
    lines = []
    out = run(run_scan(_scan(packages=["relate-db", "npm:relate-db"]), cfg, lines.append))
    assert _targets(out) == [("relate-db", "pypi")]
    assert any("is on pypi and npm" in line for line in lines)


def test_a_repo_with_no_package_says_what_was_looked_for_and_what_was_seen(
        offline, cfg, monkeypatch, tmp_path):
    import docrot.acquire.repo as repo
    rust = tmp_path / "relate"
    (rust / "crates").mkdir(parents=True)
    (rust / "crates" / "Cargo.toml").write_text("[package]\nname='core'\n")
    (rust / "go.mod").write_text("module relate\n")
    (rust / "README.md").write_text("# relate\n")
    monkeypatch.setattr(repo, "clone", lambda r, log, timeout=600: (rust, "abc123", "main"))
    lines = []
    with pytest.raises(ScanError) as e:
        run(run_scan(ScanInputs(docs=["https://a.io"], repos=["https://github.com/o/relate"]),
                     cfg, lines.append))
    want = ("no PyPI, npm or NuGet package found in relate; it does contain Cargo.toml, go.mod "
            "- docrot has no lane for those yet")
    assert want in str(e.value) and "--package" in str(e.value)
    assert any(want in line for line in lines)


def test_a_repo_with_no_package_and_nothing_else_says_only_what_was_looked_for(tmp_path):
    (tmp_path / "README.md").write_text("# docs\n")
    assert scanmod.nothing_found(tmp_path, "docs") == "no PyPI, npm or NuGet package found in docs"


def test_a_net_monorepo_is_capped_but_a_named_package_beyond_the_cap_is_kept(
        offline, cfg, monkeypatch, tmp_path):
    import docrot.acquire as acq
    import docrot.acquire.repo as repo
    import docrot.verify.dotnet as dotnet
    root = tmp_path / "products"
    for n in range(9):
        d = root / "src" / f"Acme.P{n}"
        d.mkdir(parents=True)
        (d / f"Acme.P{n}.csproj").write_text(
            f"<Project><PropertyGroup><PackageId>Acme.P{n}</PackageId></PropertyGroup></Project>")
        (d / "A.cs").write_text(f"namespace Acme.P{n} {{ public class C{n} {{}} }}\n")
    monkeypatch.setattr(repo, "clone", lambda r, log, timeout=600: (root, "abc123", "main"))
    ran = []

    async def run_dotnet_matrix(snips, package, ns, versions, cfg, log, head=None):
        ran.append(package)
        return []

    monkeypatch.setattr(dotnet, "run_dotnet_matrix", run_dotnet_matrix)
    monkeypatch.setattr(acq, "releases", lambda p, e: [Release(label="1.0", released_at="2026-01-01")])

    async def acquire_site(url, fetcher, max_pages, log):
        text = "".join(f"```csharp\nusing Acme.P{n};\nvar c = new C{n}();\n```\n" for n in range(9))
        return [Page(url=f"{url}/d", path="/d", title="D", origin=url, text=text)], "llms.txt"

    monkeypatch.setattr(acq, "acquire_site", acquire_site)
    out = run(run_scan(ScanInputs(docs=["https://a.io"], repos=["https://github.com/o/products"]),
                       cfg, lambda m: None))
    assert len(out.report["packages"]) == scanmod.MAX_AUTO_PACKAGES

    named = run(run_scan(ScanInputs(docs=["https://a.io"], repos=["https://github.com/o/products"],
                                    packages=["Acme.P8"]), cfg, lambda m: None))
    assert [t["package"] for t in named.report["packages"]] == ["Acme.P8"]


# ------------------------------------------------------- standalone CLI targets
COMMANDS = "```bash\nrelate query \"MATCH (n) RETURN n\"\nrelate clone acme/graph\nrelate serve --port 80\n```\n"


def _github(lanes, *labels, installable=True, **kw):
    """The repo's GitHub releases: `labels` are versions, newest last."""
    from docrot.acquire.binaries import CliLookup, CliRelease
    lanes["github"]["relate"] = CliLookup(
        "relate", [CliRelease(label=v, tag=f"v{v}", released_at=f"2026-0{i + 1}-01")
                   for i, v in enumerate(labels)], installable=installable, **kw)


def test_the_docs_running_the_command_and_a_release_binary_propose_a_cli_target(offline, lanes, cfg):
    lanes["docs"] = RELATE_DOCS + COMMANDS
    _github(lanes, "1.0.0", "1.1.0", "1.2.0")
    lines = []
    out = run(run_scan(_scan(options=ScanOptions(versions=2)), cfg, lines.append))

    assert _targets(out) == [("relate-db", "pypi"), ("@relate/relate", "npm"),
                             ("Relate.Net", "nuget"), ("relate", "cli")]
    assert out.extraction.package == "relate-db"            # a package stays the primary
    name, versions, repo, ids = lanes["cli"][0]
    assert (name, versions, repo) == ("relate", ["1.1.0", "1.2.0"], "https://github.com/o/relate")
    assert len(ids) == 1                                    # the three-line fence
    assert not any(p == "relate" for p, _ in lanes["npm"])  # and the npm lane never sees it
    assert set(lanes["asked"]) == {("https://github.com/o/relate", "relate")}   # one repo, one command
    assert any("3 `relate` command lines" in line and "standalone CLI" in line for line in lines)
    assert any(line.strip() == "relate (cli) · 3 releases · testing 1.1.0, 1.2.0" for line in lines)


def test_two_command_lines_are_not_enough_and_cost_no_github_request(offline, lanes, cfg):
    lanes["docs"] = RELATE_DOCS + "```bash\nrelate query a\nrelate clone b\n```\n"
    _github(lanes, "1.0.0")
    out = run(run_scan(_scan(), cfg, lambda m: None))
    assert ("relate", "cli") not in _targets(out)
    assert lanes["asked"] == []


def test_prose_about_the_command_is_not_a_command_line(offline, lanes, cfg):
    lanes["docs"] = RELATE_DOCS + "Run relate query, relate clone and relate serve. `relate query` is fast.\n"
    _github(lanes, "1.0.0")
    out = run(run_scan(_scan(), cfg, lambda m: None))
    assert ("relate", "cli") not in _targets(out) and lanes["asked"] == []


def test_a_github_that_cannot_be_asked_skips_the_cli_and_never_fails_the_scan(offline, lanes, cfg):
    from docrot.acquire.binaries import RATE_LIMITED, CliLookup
    lanes["docs"] = RELATE_DOCS + COMMANDS
    lanes["github"]["relate"] = CliLookup("relate", why=RATE_LIMITED, failed=True)
    lines = []
    out = run(run_scan(_scan(), cfg, lines.append))
    assert ("relate", "cli") not in _targets(out) and lanes["cli"] == []
    assert len(out.report["packages"]) == 3
    skipped = next(line for line in lines if "not verifying relate as a standalone CLI" in line)
    assert "rate limit" in skipped and "GITHUB_TOKEN" in skipped and "cli:relate" in skipped


def test_a_repo_with_no_installable_binary_is_not_proposed_and_says_why(offline, lanes, cfg):
    lanes["docs"] = RELATE_DOCS + COMMANDS
    _github(lanes, "1.0.0", installable=False,
            why="none of the newest 1 releases of relate carries a Linux binary docrot can install")
    lines = []
    out = run(run_scan(_scan(), cfg, lines.append))
    assert ("relate", "cli") not in _targets(out)
    assert any("Linux binary docrot can install" in line for line in lines)


def test_an_npm_package_that_declares_the_bin_owns_the_command_and_no_cli_is_proposed(
        offline, lanes, cfg):
    (lanes["root"] / "js" / "package.json").write_text(
        '{"name": "@relate/relate", "version": "1.0.0", "bin": {"relate": "cli.js"}}')
    lanes["docs"] = RELATE_DOCS + COMMANDS
    _github(lanes, "1.0.0")
    lines = []
    out = run(run_scan(_scan(), cfg, lines.append))
    assert ("relate", "cli") not in _targets(out) and lanes["asked"] == []
    assert any("ships that bin" in line and "@relate/relate" in line for line in lines)
    assert lanes["npm"] and lanes["npm"][0][1] >= 1         # the commands went to the npm lane


def test_the_commands_of_an_npm_package_without_a_bin_are_not_structural_for_it(offline, lanes, cfg):
    """330 `relate ...` snippets were counted as `@relate/relate`'s."""
    lanes["docs"] = RELATE_DOCS + COMMANDS
    out = run(run_scan(_scan(), cfg, lambda m: None))      # no CLI target: GitHub knows nothing
    cmd = next(s for s in out.extraction.snippets if s.code.startswith("relate query"))
    assert (cmd.tier, cmd.reason) == ("unverifiable", "no-package-use")


def test_naming_cli_adds_it_without_the_docs_proof_and_restricts_the_repo(offline, lanes, cfg):
    lanes["docs"] = RELATE_DOCS
    _github(lanes, "1.0.0", "1.1.0")
    out = run(run_scan(_scan(packages=["cli:relate"]), cfg, lambda m: None))
    assert _targets(out) == [("relate", "cli")]             # naming restricts: no automatic packages
    assert out.extraction.package == "relate"               # nothing else to be primary
    assert lanes["npm"] == [] and lanes["dotnet"] == []


def test_a_named_cli_goes_after_the_package_named_with_it(offline, lanes, cfg):
    _github(lanes, "1.0.0")
    out = run(run_scan(_scan(packages=["relate-db", "cli:relate"]), cfg, lambda m: None))
    assert _targets(out) == [("relate-db", "pypi"), ("relate", "cli")]


def test_a_cli_entry_after_the_last_repo_goes_to_the_first_github_repo(offline, lanes, cfg):
    _github(lanes, "1.0.0")
    out = run(run_scan(_scan(repos=["https://github.com/o/relate"],
                             packages=["npm:@relate/relate", "cli:relate"]), cfg, lambda m: None))
    assert _targets(out) == [("@relate/relate", "npm"), ("relate", "cli")]


def test_a_cli_on_a_repo_that_is_not_github_is_refused_by_name_and_the_rest_goes_on(offline, lanes, cfg):
    lines = []
    out = run(run_scan(_scan(repos=["https://gitlab.com/o/relate"], packages=["cli:relate"]),
                       cfg, lines.append))
    assert ("relate", "cli") not in _targets(out)
    refusal = next(line for line in lines if "cli:relate cannot be taken from https://gitlab.com/o/relate" in line)
    assert "GitHub repository's releases" in refusal


def test_a_cli_that_is_all_the_scan_asked_for_and_cannot_be_had_fails_it(offline, lanes, cfg):
    with pytest.raises(ScanError) as e:
        run(run_scan(_scan(repos=[], docs=["https://a.io"], packages=["cli:relate"]), cfg, lambda m: None))
    assert "needs the GitHub repository that publishes its release binaries" in str(e.value)


def test_a_cli_with_no_repo_at_all_fails_the_scan_saying_exactly_that(offline, lanes, cfg):
    with pytest.raises(ScanError) as e:
        run(run_scan(ScanInputs(docs=["https://a.io"], packages=["cli:relate"]), cfg, lambda m: None))
    assert "needs the GitHub repository that publishes its release binaries" in str(e.value)


def test_a_named_cli_the_releases_cannot_supply_fails_alone_and_is_skipped_beside_others(
        offline, lanes, cfg):
    """Nothing else to verify: the scan says so. Beside a package: it logs and goes on."""
    with pytest.raises(ScanError) as e:
        run(run_scan(_scan(packages=["cli:relate"]), cfg, lambda m: None))
    assert "cli:relate not verified: https://github.com/o/relate has no GitHub releases of relate" in str(e.value)
    lines = []
    out = run(run_scan(_scan(packages=["relate-db", "cli:relate"]), cfg, lines.append))
    assert _targets(out) == [("relate-db", "pypi")]
    assert any("! cli:relate not verified: " in line for line in lines)


def test_a_cli_named_like_a_package_already_in_the_scan_is_skipped_not_merged(offline, lanes, cfg):
    """Results, snippets and the graph are keyed by package name alone."""
    _github(lanes, "1.0.0")
    lines = []
    out = run(run_scan(_scan(packages=["relate-db", "pypi:relate", "cli:relate"]), cfg, lines.append))
    assert ("relate", "cli") not in _targets(out)
    assert any("cli:relate not verified: relate (pypi) already goes by that name" in line for line in lines)


def test_a_cli_target_has_no_head_symbols_and_the_graph_is_told_so(offline, lanes, cfg):
    _github(lanes, "1.0.0")
    run(run_scan(_scan(packages=["relate-db", "cli:relate"]), cfg, lambda m: None))
    packages, head = offline["graph"]
    assert packages == ["relate-db", "relate"] and "relate" in head


def test_cli_versions_are_spread_across_major_lines(offline, lanes, cfg):
    _github(lanes, "1.0.0", "1.1.0", "1.1.1", "2.0.0", "2.0.1")
    run(run_scan(_scan(packages=["cli:relate"], options=ScanOptions(versions=2)), cfg, lambda m: None))
    assert lanes["cli"][0][1] == ["1.1.1", "2.0.1"]       # the newest of each major line, oldest first
