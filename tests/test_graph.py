from __future__ import annotations

import pytest

from docrot.config import Config
from docrot.graph import GraphStore
from docrot.graph.store import canon
from tests.fakes import make_outcome, make_targets


def test_unreachable_graph_degrades_to_no_ops(cfg):
    logs = []
    g = GraphStore(cfg, logs.append)
    assert not g.ok and "graph unavailable" in logs[0]
    out = make_outcome(["https://docs.a.io"], ["https://github.com/a/alpha"])
    g.load(out.extraction, out.results, make_targets(["https://github.com/a/alpha"]), {})
    assert g.blast_radius("run") == [] and g.drift() == []
    g.close()


def test_canon():
    assert canon("perseus_client.close_async") == "close_async"
    assert canon("declared:Client.run") == "run"
    assert canon("requirements.txt") == "txt"
    assert canon("1.0.0-rc.19") == ""


@pytest.mark.neo4j
def test_multi_package_ingest_against_a_live_graph():
    """Needs the local Neo4j (docker compose up -d). Wipes the graph."""
    from docrot import config as cfgmod
    g = GraphStore(cfgmod.load(), lambda *_: None)
    if not g.ok:
        pytest.skip("neo4j not reachable")
    repos = ["https://github.com/a/alpha", "https://github.com/a/beta"]
    out = make_outcome(["https://docs.a.io"], repos)
    targets = make_targets(repos)
    g.load(out.extraction, out.results, targets, {"alpha": {"run"}, "beta": {"run"}})
    versions = g._run("MATCH (v:Version) RETURN v.package AS p, v.label AS l ORDER BY p, l")
    assert len(versions) == 6 and {v["p"] for v in versions} == {"alpha", "beta"}
    assert g.blast_radius("run")
    g.close()


def test_config_default_is_harmless():
    assert Config().can_execute is False
