"""Version pins a page tells its reader to install.

Only install-shaped text counts: an install command, a requirements line, or a
quoted dependency spec. A bare `== <number>` is far more often code - Pydantic's
docs yielded `0, 1, 1., 10, 123` from `assert x == 1` and friends, and `1.0`
among them sent a 2019 release into the version matrix.
"""
from __future__ import annotations

import re

_NAME = r"[A-Za-z][\w.\-]*(?:\[[^\]\s]*\])?"
_VERSION = r"(\d+\.\d+[\w.\-]*)"

PIN_PATTERNS = [
    # pip install foo==1.2  ·  uv pip install / uv add  ·  poetry add
    re.compile(rf"(?:pip3?\s+install|uv\s+(?:pip\s+install|add)|poetry\s+add)[^\n]*?{_NAME}\s*==\s*{_VERSION}"),
    # a requirements.txt line
    re.compile(rf"^\s*{_NAME}\s*==\s*{_VERSION}\s*(?:#.*)?$", re.M),
    # "foo==1.2" in a pyproject / setup.py dependency list
    re.compile(rf"[\"']{_NAME}\s*==\s*{_VERSION}[\"']"),
]


def find_pins(text: str) -> list[str]:
    found: set[str] = set()
    for pattern in PIN_PATTERNS:
        found.update(v.rstrip(".") for v in pattern.findall(text or ""))
    return sorted(found)
