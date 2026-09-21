"""Diff two runs, so a scheduled re-check reports news rather than status.

A nightly scan that prints the same twenty failures every night trains you to
ignore it. The signal is the *transition*: a page that was clean yesterday and
fails today usually means a release shipped a breaking change this morning.

Diffed on `(page, snippet_id, version) -> status`. Only `pass -> fail` and
`pass -> blocked` are news. Everything else is suppressed:

  * `unverifiable` churn - the tier means "we never tested this", so a change
    in it is a change in classification, not in the documentation
  * `unverified` in either direction - a missing credential or a reset
    connection is a property of the sandbox
  * `blocked -> fail` and back - both mean "no verdict was reached"
  * anything whose output matches a configured flake pattern

At ~30s per verification pass this is cheap enough to run hourly.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# A status that carries no verdict about the documentation.
NO_VERDICT = {"unverified", "unverifiable"}

DEFAULT_FLAKE = re.compile(
    r"(rate.?limit|429\b|timed? ?out|connection reset|temporarily unavailable|"
    r"service unavailable|50[234]\b)", re.I)


def _key(row: dict) -> tuple:
    return (row.get("page", ""), row["id"], row["version"])


def load_results(path: Path) -> dict[tuple, dict]:
    rows = json.loads(Path(path).read_text())
    return {_key(r): r for r in rows}


def diff(previous: dict[tuple, dict], current: dict[tuple, dict],
         flake: re.Pattern = DEFAULT_FLAKE) -> dict:
    """What is newly broken, newly fixed, and newly present."""
    regressions, fixes, appeared, disappeared = [], [], [], []

    for key, now in current.items():
        before = previous.get(key)
        if before is None:
            if now["status"] == "fail":
                appeared.append(_entry(key, None, now))
            continue

        was, is_ = before["status"], now["status"]
        if was == is_:
            continue
        if was in NO_VERDICT or is_ in NO_VERDICT:
            continue                      # no verdict either side
        if is_ in ("fail", "blocked") and flake.search(now.get("stderr") or ""):
            continue                      # known-flaky output
        if was == "pass" and is_ in ("fail", "blocked"):
            regressions.append(_entry(key, before, now))
        elif was in ("fail", "blocked") and is_ == "pass":
            fixes.append(_entry(key, before, now))

    for key, before in previous.items():
        if key not in current and before["status"] == "fail":
            disappeared.append(_entry(key, before, None))

    return {
        "regressions": regressions,      # the signal
        "fixes": fixes,
        "appeared": appeared,            # new snippets that fail
        "disappeared": disappeared,      # failing snippets the docs removed
        "counts": {"regressions": len(regressions), "fixes": len(fixes),
                   "appeared": len(appeared), "disappeared": len(disappeared)},
    }


def _entry(key: tuple, before: dict | None, now: dict | None) -> dict:
    page, sid, version = key
    detail = ""
    if now and now.get("stderr"):
        try:
            payload = json.loads(now["stderr"])
            failed = (payload.get("failed") if isinstance(payload, dict) else None) or []
            detail = str(failed[0])[:200] if failed else ""
        except (ValueError, TypeError):
            detail = (now["stderr"] or "")[-200:]
    return {
        "page": page, "id": sid, "version": version,
        "was": before["status"] if before else None,
        "now": now["status"] if now else None,
        "detail": detail,
    }


def exit_code(d: dict) -> int:
    """0 when nothing got worse. Non-zero is what a cron job alerts on."""
    return 1 if d["counts"]["regressions"] or d["counts"]["appeared"] else 0
