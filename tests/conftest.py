from __future__ import annotations

import pytest

from docrot import settings
from docrot.config import Config
from docrot.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "data")


@pytest.fixture
def cfg() -> Config:
    """No credentials: nothing reaches Daytona, OpenRouter or Neo4j."""
    return Config(neo4j_uri="bolt://127.0.0.1:1")


@pytest.fixture(autouse=True)
def keychain():
    """Tests never touch the real OS keychain."""
    backend = settings.MemoryBackend()
    settings.use(backend)
    yield backend
    settings.use(None)


@pytest.fixture(autouse=True)
def github_forgotten():
    """`binaries.lookup` remembers answers for ten minutes; a test must not
    inherit another's, nor keep a token."""
    from docrot.acquire import binaries
    binaries.forget()
    binaries.use_token("")
    yield
    binaries.forget()
    binaries.use_token("")
