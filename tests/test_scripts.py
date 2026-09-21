"""The shell entry points.

Only their argument handling is exercised: running ./stop.sh for real would
stop a dashboard the developer is using.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script", ["start.sh", "stop.sh"])
def test_scripts_are_valid_bash_and_executable(script):
    path = ROOT / script
    assert path.stat().st_mode & 0o111, f"{script} is not executable"
    subprocess.run(["bash", "-n", str(path)], check=True, capture_output=True)


def test_stop_help_explains_itself_without_stopping_anything():
    r = subprocess.run(["./stop.sh", "--help"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "--all-cache" in r.stdout and "stop.sh" in r.stdout
    # help must short-circuit: nothing was stopped, nothing was deleted
    assert "stopping" not in r.stdout and "removing" not in r.stdout


def test_stop_rejects_an_unknown_option():
    r = subprocess.run(["./stop.sh", "--nope"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "unknown option" in r.stderr
    assert r.stdout == ""
