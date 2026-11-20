"""Which versions a scan verifies against: ordering and pre-release rules, with
every registry replaced by canned JSON."""
from __future__ import annotations

import httpx

from docrot.acquire import registry
from docrot.models import Release


def _rels(*labels):
    return [Release(label=v, released_at=f"2026-01-{i + 1:02d}") for i, v in enumerate(labels)]


def _labels(rels):
    return [r.label for r in rels]


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _serve(monkeypatch, data):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp(data))
    monkeypatch.setattr(httpx, "Client", lambda **kw: _Client(data))


class _Client:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        return _Resp(self.data)


def _npm(times, tags=None, versions=None):
    data = {"time": {"created": "2026-01-01T00:00:00Z", "modified": "2026-09-01T00:00:00Z", **times},
            "dist-tags": tags or {}}
    if versions is not None:
        data["versions"] = {v: {} for v in versions}
    return data


def test_a_stage_build_is_never_chosen_while_stable_releases_exist(monkeypatch):
    _serve(monkeypatch, _npm({"1.0.0": "2026-01-02T00:00:00Z", "1.1.0": "2026-02-02T00:00:00Z",
                              "1.2.0": "2026-03-02T00:00:00Z", "0.0.0-stage": "2026-09-01T00:00:00Z"}))
    rels = registry.releases("@relate/relate", "npm")
    assert rels[-1].label == "1.2.0"                              # not the newest by date or by text
    assert _labels(registry.pick_versions(rels, 3)) == ["1.0.0", "1.1.0", "1.2.0"]


def test_a_package_with_only_prereleases_still_gets_a_version(monkeypatch):
    _serve(monkeypatch, {"items": [{"items": [
        {"catalogEntry": {"version": "1.0.0-preview.1", "published": "2026-05-01T00:00:00Z"}}]}]})
    rels = registry.releases("Relate.Net", "nuget")
    assert _labels(registry.pick_versions(rels, 3)) == ["1.0.0-preview.1"]
    older = _rels("1.0.0-beta.1", "1.0.0-rc.1", "0.0.0-stage")
    assert registry.pick_versions(older, 1)[0].label == "1.0.0-rc.1"       # the newest prerelease


def test_a_version_the_docs_pin_is_kept_even_when_it_is_a_prerelease():
    rels = _rels("1.0.0", "1.1.0", "2.0.0", "2.1.0-rc.1")
    picked = registry.pick_versions(rels, 3, mentioned=["2.1.0-rc.1"])
    assert "2.1.0-rc.1" in _labels(picked)
    assert "2.1.0-rc.1" not in _labels(registry.pick_versions(rels, 3))


def test_npm_dist_tags_latest_is_the_newest_stable(monkeypatch):
    _serve(monkeypatch, _npm({"1.0.0": "2026-01-02T00:00:00Z", "1.1.0": "2026-02-02T00:00:00Z",
                              "2.0.0": "2026-03-02T00:00:00Z"}, tags={"latest": "1.1.0", "next": "2.0.0"}))
    rels = registry.releases("pkg", "npm")
    assert _labels(rels) == ["1.0.0", "1.1.0"]
    assert registry.pick_versions(rels, 1)[0].label == "1.1.0"


def test_unpublished_npm_versions_are_not_releases(monkeypatch):
    _serve(monkeypatch, _npm({"1.0.0": "2026-01-02T00:00:00Z", "1.1.0": "2026-02-02T00:00:00Z",
                              "unpublished": {"time": "2026-03-01", "versions": ["1.1.0"]}},
                             versions=["1.0.0"]))
    assert _labels(registry.releases("pkg", "npm")) == ["1.0.0"]


def test_pypi_yanked_releases_are_dropped(monkeypatch):
    f = {"upload_time": "2026-01-01T00:00:00"}
    _serve(monkeypatch, {"releases": {"1.0": [f], "1.1": [{**f, "yanked": True}], "1.2": []}})
    assert _labels(registry.releases("pkg")) == ["1.0"]


def test_ordering_is_numeric_not_textual():
    rels = _rels("1.9.0", "1.10.0", "1.2.0", "0.0.0-stage", "1.10.0-rc.2", "1.10.0-rc.10", "1.10.0-beta.1")
    assert _labels(registry._sorted(rels)) == ["0.0.0-stage", "1.2.0", "1.9.0", "1.10.0-beta.1",
                                               "1.10.0-rc.2", "1.10.0-rc.10", "1.10.0"]
    assert registry.pick_versions(_rels("1.9.0", "1.10.0", "1.8.0"), 1)[0].label == "1.10.0"


def test_prerelease_and_build_tags_are_recognised():
    for label in ("0.0.0-stage", "1.1.1-local", "1.0.0-rc.1", "2.0.0b1", "1.0.0.dev3", "1.0.0-preview.1",
                  "1.0.0+20260101"):
        assert registry.is_prerelease(label), label
    for label in ("1.0.0", "2024.1", "1.0.0.post1", "1.2.3.4"):
        assert not registry.is_prerelease(label), label


def test_spread_still_walks_back_by_major_line():
    rels = _rels("7.3.4", "7.4.12", "8.0.6", "8.0.8", "9.0.0-rc.1")
    assert _labels(registry.pick_versions(rels, 3, spread=True)) == ["7.3.4", "7.4.12", "8.0.8"]
