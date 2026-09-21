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
