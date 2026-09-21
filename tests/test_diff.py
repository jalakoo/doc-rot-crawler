from __future__ import annotations

from docrot.report.diff import diff, exit_code


def _rows(**statuses):
    return {("p", sid, "1.0"): {"page": "p", "id": sid, "version": "1.0", "status": st,
                                "stderr": ""} for sid, st in statuses.items()}


def test_only_transitions_are_news():
    d = diff(_rows(a="pass", b="fail", c="pass", d="unverified", gone="fail"),
             _rows(a="fail", b="pass", c="pass", d="fail", new="fail"))
    assert [r["id"] for r in d["regressions"]] == ["a"]
    assert [r["id"] for r in d["fixes"]] == ["b"]
    assert [r["id"] for r in d["appeared"]] == ["new"]
    assert [r["id"] for r in d["disappeared"]] == ["gone"]
    assert exit_code(d) == 1


def test_flaky_output_is_suppressed():
    prev = _rows(a="pass")
    now = _rows(a="fail")
    now[("p", "a", "1.0")]["stderr"] = "HTTP 503 service unavailable"
    d = diff(prev, now)
    assert d["counts"]["regressions"] == 0 and exit_code(d) == 0
