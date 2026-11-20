from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from docrot.config import Config
from docrot.models import Snippet
from docrot.verify import runner
from tests.fakes import run_async

NO_NEO4J = ("Traceback (most recent call last):\n  File \"<stdin>\", line 1, in <module>\n"
            "ModuleNotFoundError: No module named 'neo4j'\n")
MOVED = "ImportError: cannot import name 'MCPToolset' from 'pydantic_ai.mcp' (/usr/lib/mcp.py)"


@pytest.mark.parametrize("body", [NO_NEO4J, MOVED, "zsh: no matches found: relate-db[arrow]",
                                  "bash: line 1: relate: command not found"])
def test_a_failure_of_the_sandbox_or_another_package_is_environmental(body):
    assert runner.environmental(body, "relate")


@pytest.mark.parametrize("body", [
    "ModuleNotFoundError: No module named 'relate'",
    "ModuleNotFoundError: No module named 'relate.missing'",
    "ImportError: cannot import name 'Gone' from 'relate' (/x/relate/__init__.py)",
    "AttributeError: module 'relate' has no attribute 'connect_old'"])
def test_a_failure_about_the_package_itself_is_not(body):
    assert not runner.environmental(body, "relate")


class _Sandbox:
    def __init__(self, output: str):
        self.process = SimpleNamespace(code_run=self._run, exec=self._run)
        self.output = output

    async def _run(self, code, timeout=0):
        return SimpleNamespace(exit_code=1, result=self.output)


def _chain_status(monkeypatch, output: str, import_name: str) -> str:
    async def new(daytona, cfg):
        return _Sandbox(output)

    async def install(*a, **k):
        return True, ""

    class Daytona:
        async def delete(self, sandbox):
            pass

    monkeypatch.setattr(runner, "_new_sandbox", new)
    monkeypatch.setattr(runner, "_install", install)
    snip = Snippet(id="s1", page="p", lang="python", code="import x", line=1, tier="execute")
    [r] = run_async(runner.run_chain(Daytona(), [snip], "relate-db", "1.0.0", Config(),
                                       asyncio.Semaphore(1), lambda *_: None, import_name=import_name))
    return r.status


def test_a_snippet_needing_another_package_is_unverified_not_failed(monkeypatch):
    assert _chain_status(monkeypatch, NO_NEO4J, "relate") == "unverified"


def test_a_snippet_that_cannot_import_the_package_still_fails(monkeypatch):
    assert _chain_status(monkeypatch,
                         "ImportError: cannot import name 'Gone' from 'relate'", "relate") == "fail"
