from __future__ import annotations

import pytest

from docrot import cli
from docrot import config as cfgmod
from docrot.store import Store
from tests.fakes import fake_runner


def test_parser_accepts_several_sources():
    args = cli.parser().parse_args([
        "scan", "--docs-url", "https://a.io", "https://b.io",
        "--repo", "https://github.com/o/x", "--repo", "https://github.com/o/y",
        "--name", "Both"])
    assert args.docs_url == ["https://a.io", "https://b.io"]
    assert args.repo == ["https://github.com/o/x", "https://github.com/o/y"]


def test_scan_requires_a_source():
    with pytest.raises(SystemExit):
        cli.main(["scan"])


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "DATA", tmp_path / "data")
    monkeypatch.setattr(cfgmod, "RUNS", tmp_path / "data" / "runs")
    import docrot.scan as scanmod
    real = scanmod.execute_scan
    monkeypatch.setattr(scanmod, "execute_scan",
                        lambda store, scan_id, echo=None: real(
                            store, scan_id, echo=echo, runner=fake_runner(),
                            cfg=cfgmod.Config(neo4j_uri="bolt://127.0.0.1:1")))
    return tmp_path / "data"


def test_scan_records_into_the_store_and_reuses_the_scan(data):
    argv = ["scan", "--docs-url", "https://docs.a.io", "--repo", "https://github.com/a/alpha"]
    assert cli.main([*argv, "--name", "Alpha"]) == 1      # findings -> exit 1
    assert cli.main(argv) == 1
    store = Store(data)
    [rec] = store.all()
    assert rec.name == "Alpha" and len(store.runs(rec.id)) == 2


def test_scan_rejects_bad_sources(data):
    assert cli.main(["scan", "--docs-url", "not-a-url"]) == 2


def test_diff_and_scans_commands(data, capsys):
    argv = ["scan", "--docs-url", "https://docs.a.io", "--repo", "https://github.com/a/alpha"]
    cli.main(argv)
    assert cli.main(["diff"]) == 0
    assert "need two runs" in capsys.readouterr().out
    cli.main(argv)
    assert cli.main(["diff"]) == 0
    assert "no change" in capsys.readouterr().out
    assert cli.main(["scans"]) == 0
    assert "docs.a.io" in capsys.readouterr().out


def test_export_command(data, capsys, tmp_path):
    import json
    assert cli.main(["export"]) == 3                         # nothing scanned yet
    capsys.readouterr()
    cli.main(["scan", "--docs-url", "https://docs.a.io", "--repo", "https://github.com/a/alpha"])
    capsys.readouterr()

    assert cli.main(["export"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["type"] == "scan" and all(r["type"] == "finding" for r in lines[1:])

    out = tmp_path / "f.jsonl"
    assert cli.main(["export", "-o", str(out), "--include-clean"]) == 0
    assert "wrote" in capsys.readouterr().out
    assert len(out.read_text().splitlines()) > len(lines)
    assert cli.main(["export", "--run", "20000101T000000"]) == 3


def test_export_sarif(data, capsys, tmp_path):
    import json
    cli.main(["scan", "--docs-url", "https://docs.a.io", "--repo", "https://github.com/a/alpha"])
    capsys.readouterr()
    assert cli.main(["export", "--format", "sarif"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == "2.1.0" and doc["runs"][0]["results"]

    out = tmp_path / "f.sarif"
    assert cli.main(["export", "--format", "sarif", "-o", str(out)]) == 0
    assert "wrote" in capsys.readouterr().out and json.loads(out.read_text())["runs"]


def test_package_flag_takes_prefixed_names_repeated_or_listed():
    args = cli.parser().parse_args([
        "scan", "--repo", "https://github.com/relatedb/relate",
        "--package", "pypi:relate-db", "npm:@relate/relate", "--package", "nuget:Relate.Net"])
    assert args.package == ["pypi:relate-db", "npm:@relate/relate", "nuget:Relate.Net"]


def test_ecosystem_defaults_to_auto_and_accepts_the_three_registries():
    p = cli.parser()
    assert p.parse_args(["scan", "--repo", "./x"]).ecosystem == "auto"
    for eco in ("pypi", "npm", "nuget", "auto"):
        assert p.parse_args(["scan", "--repo", "./x", "--ecosystem", eco]).ecosystem == eco
    with pytest.raises(SystemExit):
        p.parse_args(["scan", "--repo", "./x", "--ecosystem", "cargo"])


def test_scan_help_explains_automatic_selection(capsys):
    with pytest.raises(SystemExit):
        cli.main(["scan", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "the docs mention" in text and "at most 6 per repo" in text
    assert "docrot packages" in text and "npm:@relate/relate" in text


def test_scan_records_the_packages_and_ecosystem_it_was_given(data):
    argv = ["scan", "--docs-url", "https://docs.a.io", "--repo", "https://github.com/a/alpha",
            "--package", "npm:@a/alpha", "--ecosystem", "nuget"]
    cli.main(argv)
    [rec] = Store(data).all()
    assert rec.packages == ["npm:@a/alpha"] and rec.options.ecosystem == "nuget"


def test_scan_rejects_an_unknown_package_prefix(data, capsys):
    assert cli.main(["scan", "--docs-url", "https://docs.a.io",
                     "--package", "cargo:serde"]) == 2
    assert "unknown ecosystem prefix 'cargo:'" in capsys.readouterr().out
    assert Store(data).all() == []


def _relate(tmp_path):
    root = tmp_path / "relate"
    (root / "py" / "relate").mkdir(parents=True)
    (root / "py" / "pyproject.toml").write_text('[project]\nname = "relate-db"\n')
    (root / "py" / "relate" / "__init__.py").write_text("")
    (root / "js").mkdir()
    (root / "js" / "package.json").write_text('{"name": "@relate/relate", "version": "1"}')
    return root


@pytest.fixture
def cloned(monkeypatch, tmp_path):
    import docrot.acquire.repo as repo
    from docrot.acquire import binaries
    paths = {}
    monkeypatch.setattr(repo, "clone", lambda r, log, timeout=600: (paths[r], "abc123", "main"))
    # no test asks GitHub: a repo whose releases carry no binary
    monkeypatch.setattr(binaries, "lookup", lambda r, n: binaries.CliLookup(n, why="offline"))
    return paths


def test_packages_lists_what_a_repo_holds_with_the_name_to_pass_to_scan(cloned, tmp_path, capsys):
    cloned["https://github.com/relatedb/relate"] = _relate(tmp_path)
    assert cli.main(["packages", "https://github.com/relatedb/relate"]) == 0
    out = capsys.readouterr().out
    assert "pypi:relate-db" in out and "npm:@relate/relate" in out
    assert "py" in out and "js" in out and "relate" in out
    assert "--package eco:name" in out            # two packages: say how to choose


def test_packages_says_what_it_saw_when_a_repo_holds_none(cloned, tmp_path, capsys):
    rust = tmp_path / "rusty"
    rust.mkdir()
    (rust / "Cargo.toml").write_text("[package]\nname='x'\n")
    cloned["./rusty"] = rust
    assert cli.main(["packages", "./rusty"]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "no PyPI, npm or NuGet package found in rusty" in out and "Cargo.toml" in out


def test_packages_reports_a_repo_it_cannot_fetch(monkeypatch, capsys):
    import docrot.acquire.repo as repo

    def refuse(r, log, timeout=600):
        raise repo.CloneError("not found")

    monkeypatch.setattr(repo, "clone", refuse)
    assert cli.main(["packages", "https://github.com/o/nope"]) == 3
    assert "couldn't fetch https://github.com/o/nope" in capsys.readouterr().out


def test_packages_lists_the_standalone_cli_the_releases_carry(cloned, tmp_path, capsys, monkeypatch):
    from docrot.acquire import binaries
    cloned["https://github.com/relatedb/relate"] = _relate(tmp_path)
    monkeypatch.setattr(binaries, "lookup", lambda r, n: binaries.CliLookup(
        n, [binaries.CliRelease("1.2.0", "v1.2.0")], installable=True))
    assert cli.main(["packages", "https://github.com/relatedb/relate"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "cli:relate" in out and "GitHub release binaries (latest 1.2.0)" in out


def test_packages_says_why_when_github_could_not_be_asked(cloned, tmp_path, capsys, monkeypatch):
    from docrot.acquire import binaries
    cloned["https://github.com/relatedb/relate"] = _relate(tmp_path)
    monkeypatch.setattr(binaries, "lookup", lambda r, n: binaries.CliLookup(
        n, why=binaries.RATE_LIMITED, failed=True))
    assert cli.main(["packages", "https://github.com/relatedb/relate"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "No standalone relate CLI listed" in out and "GITHUB_TOKEN" in out and "cli:relate" not in out


def test_scan_help_explains_cli_packages(capsys):
    with pytest.raises(SystemExit):
        cli.main(["scan", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "cli:relate" in text and "GitHub release binaries" in text
