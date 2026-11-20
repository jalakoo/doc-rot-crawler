from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from docrot import config as cfgmod
from docrot import settings
from docrot.server import create_app
from docrot.worker import ScanWorker

ENV = {"DAYTONA_API_KEY": "env-daytona-key", "DOCROT_MAX_PAGES": "40",
       "SANDBOX_ENV_PERSEUS_API_KEY": "env-perseus"}


@pytest.fixture
def env(monkeypatch):
    """A fixed stand-in for the shell plus `.env`."""
    monkeypatch.setattr(cfgmod, "base_env", lambda env_file=None: dict(ENV))
    return ENV


@pytest.fixture
def client(store, env):
    worker = ScanWorker(store, lambda s, i: None)
    with TestClient(create_app(store, worker), base_url="http://127.0.0.1:8080") as c:
        yield c
    worker.stop()


def field(data, key):
    return next(f for f in data["fields"] if f["key"] == key)


def test_saved_values_override_env_and_removal_falls_back(env):
    assert cfgmod.load().daytona_api_key == "env-daytona-key"
    settings.save({"DAYTONA_API_KEY": "keychain-key", "DOCROT_MAX_PAGES": "7",
                   "EXTRACTION_MODELS": "a/b, c/d"})
    cfg = cfgmod.load()
    assert cfg.daytona_api_key == "keychain-key" and cfg.max_pages == 7
    assert cfg.extraction_models == ["a/b", "c/d"]

    settings.save({"DAYTONA_API_KEY": None, "DOCROT_MAX_PAGES": ""})
    cfg = cfgmod.load()
    assert cfg.daytona_api_key == "env-daytona-key" and cfg.max_pages == 40


def test_sandbox_credentials_are_added_to_those_in_env(env):
    settings.save({"SANDBOX_ENV_OPENAI_API_KEY": "sk-kc", "SANDBOX_ENV_PERSEUS_API_KEY": "kc-perseus"})
    cfg = cfgmod.load()
    assert cfg.sandbox_env == {"OPENAI_API_KEY": "sk-kc", "PERSEUS_API_KEY": "kc-perseus"}
    assert "sk-kc" in cfg.secrets()          # redacted from captured output


def test_everything_lives_in_one_keychain_item_and_empties_clean(keychain):
    settings.save({"NEO4J_PASSWORD": "pw"})
    assert json.loads(keychain.text) == {"NEO4J_PASSWORD": "pw"}
    settings.save({"NEO4J_PASSWORD": None})
    assert keychain.text is None


@pytest.mark.parametrize("changes, message", [
    ({"PATH": "/tmp"}, "not a setting"),
    ({"SANDBOX_ENV_lower": "x"}, "not a setting"),
    ({"DOCROT_CONCURRENCY": "0"}, "whole number"),
    ({"DOCROT_CONCURRENCY": "lots"}, "whole number"),
    ({"DAYTONA_API_KEY": "a\nb"}, "single line"),
])
def test_bad_entries_are_refused_and_nothing_is_saved(keychain, changes, message):
    with pytest.raises(ValueError) as e:
        settings.save({"NEO4J_DATABASE": "ok", **changes})
    assert message in e.value.args[0][0]
    assert keychain.text is None


def test_a_corrupt_or_missing_keychain_never_stops_a_scan(keychain, env):
    keychain.text = "{not json"
    assert settings.overrides() == {}

    class Locked(settings.MemoryBackend):
        def read(self):
            raise settings.Unavailable("locked")

    settings.use(Locked())
    assert cfgmod.load().daytona_api_key == "env-daytona-key"
    assert settings.describe(ENV)["available"] is False
    with pytest.raises(settings.Unavailable):
        settings.save({"NEO4J_DATABASE": "x"})


def test_int_defaults_shown_match_the_real_defaults():
    cfg = cfgmod.Config()
    shown = {f.key: f.default for f in settings.FIELDS}
    assert int(shown["DOCROT_MAX_PAGES"]) == cfg.max_pages
    assert int(shown["DOCROT_CONCURRENCY"]) == cfg.concurrency
    assert int(shown["DOCROT_SNIPPET_TIMEOUT"]) == cfg.snippet_timeout
    assert int(shown["DOCROT_INSTALL_TIMEOUT"]) == cfg.install_timeout
    assert int(shown["DOCROT_BUILD_TIMEOUT"]) == cfg.build_timeout
    assert shown["DOCROT_USER_AGENT"] == cfg.user_agent
    assert shown["DOCROT_NODE_IMAGE"] == cfg.node_image == ""     # blank means Daytona's default
    assert shown["EXTRACTION_MODELS"].split(",") == cfgmod.DEFAULT_EXTRACTION_MODELS


def test_api_reports_sources_but_never_a_secret(client):
    data = client.get("/api/settings").json()
    assert data["available"] and data["backend"] == "memory"
    assert field(data, "DAYTONA_API_KEY") | {"hint": ""} == {
        "key": "DAYTONA_API_KEY", "label": "Daytona API key", "group": "sandbox", "secret": True,
        "kind": "text", "hint": "", "source": "env", "set": True, "value": "", "fallback": ""}
    assert field(data, "DOCROT_MAX_PAGES")["fallback"] == "40"
    assert field(data, "NEO4J_URI")["source"] == "default"
    assert data["sandbox"] == [{"key": "SANDBOX_ENV_PERSEUS_API_KEY", "name": "PERSEUS_API_KEY",
                                "source": "env"}]

    r = client.put("/api/settings", json={"values": {
        "DAYTONA_API_KEY": "kc-secret-1", "NEO4J_URI": "neo4j+s://x.io",
        "SANDBOX_ENV_OPENAI_API_KEY": "kc-secret-2"}})
    assert r.status_code == 200
    data = r.json()
    assert field(data, "DAYTONA_API_KEY")["source"] == "keychain"
    assert field(data, "NEO4J_URI")["value"] == "neo4j+s://x.io"
    assert [c["source"] for c in data["sandbox"]] == ["keychain", "env"]
    for text in (r.text, client.get("/api/settings").text):
        assert "kc-secret" not in text and "env-daytona-key" not in text and "env-perseus" not in text


def test_api_validation_and_guards(client):
    r = client.put("/api/settings", json={"values": {"HOME": "/x", "DOCROT_MAX_PAGES": "-1"}})
    assert r.status_code == 422 and len(r.json()["detail"]) == 2
    r = client.put("/api/settings", json={"values": {"NEO4J_DATABASE": "x"}},
                   headers={"Origin": "https://evil.example"})
    assert r.status_code == 403

    class Gone(settings.MemoryBackend):
        def read(self):
            raise settings.Unavailable("no system keychain was found on this machine")

    settings.use(Gone())
    assert client.put("/api/settings", json={"values": {"NEO4J_DATABASE": "x"}}).status_code == 503
    assert client.get("/api/settings").json()["available"] is False


def test_node_image_can_be_saved_and_blank_means_the_default_snapshot(env):
    assert cfgmod.load().node_image == ""
    settings.save({"DOCROT_NODE_IMAGE": "node:22"})
    assert cfgmod.load().node_image == "node:22"
