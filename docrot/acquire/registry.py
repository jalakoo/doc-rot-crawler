"""Release lists straight from the package registry. No auth needed."""
from __future__ import annotations

import re

import httpx
from packaging.version import InvalidVersion, Version

from ..models import Release


def releases(package: str, ecosystem: str = "pypi") -> list[Release]:
    if ecosystem == "npm":
        return _npm(package)
    if ecosystem == "nuget":
        return _nuget(package)
    return _pypi(package)


def _pypi(package: str) -> list[Release]:
    try:
        r = httpx.get(f"https://pypi.org/pypi/{package}/json", timeout=30.0)
        r.raise_for_status()
    except httpx.HTTPError:
        return []
    out = []
    for label, files in r.json().get("releases", {}).items():
        # A yanked release is withdrawn - pip skips it unless pinned exactly -
        # so it is never the "current" one a scan should verify against.
        if not files or all(f.get("yanked") for f in files):
            continue
        out.append(Release(label=label, released_at=files[0].get("upload_time", "")[:10]))
    return _sorted(out)


def _npm(package: str) -> list[Release]:
    try:
        r = httpx.get(f"https://registry.npmjs.org/{package}", timeout=30.0)
        r.raise_for_status()
    except httpx.HTTPError:
        return []
    data = r.json()
    times = data.get("time", {})
    # `time` also lists versions since unpublished (and an `unpublished` entry
    # that is an object, not a date); `versions` is what can still be installed.
    live = set(data.get("versions") or times)
    out = [
        Release(label=k, released_at=v[:10])
        for k, v in times.items()
        if k in live and k not in ("created", "modified") and isinstance(v, str)
    ]
    return _with_latest(_sorted(out), (data.get("dist-tags") or {}).get("latest", ""))


def _with_latest(rels: list[Release], latest: str) -> list[Release]:
    """`dist-tags.latest` is what `npm install pkg` resolves, so it is the
    newest stable release even when sorting disagrees: a `2.0.0` published
    under the `next` tag sorts above it but is not what users get.

    Stable releases above `latest` are dropped; pre-releases are left to
    `pick_versions`, which excludes them anyway."""
    labels = [r.label for r in rels]
    if latest not in labels or is_prerelease(latest):
        return rels
    above = labels[labels.index(latest) + 1:]
    drop = {label for label in above if not is_prerelease(label)}
    return [r for r in rels if r.label not in drop]


NUGET_REGISTRATION = "https://api.nuget.org/v3/registration5-gz-semver2/{id}/index.json"


def _nuget(package: str) -> list[Release]:
    """Listed versions with their publish dates, from the registration index.

    The index inlines its pages for small packages and links them for large
    ones, so a linked page is fetched on demand. Unlisted versions carry a
    1900 publish date and are dropped: NuGet will not resolve them by default.
    """
    try:
        with httpx.Client(timeout=30.0) as c:
            r = c.get(NUGET_REGISTRATION.format(id=package.lower()))
            r.raise_for_status()
            out = []
            for page in r.json().get("items", []):
                items = page.get("items")
                if items is None:
                    pr = c.get(page["@id"])
                    pr.raise_for_status()
                    items = pr.json().get("items", [])
                for it in items:
                    entry = it.get("catalogEntry", {})
                    published = entry.get("published", "")[:10]
                    if entry.get("listed") is False or published.startswith("1900"):
                        continue
                    out.append(Release(label=entry.get("version", ""), released_at=published))
    except (httpx.HTTPError, KeyError, ValueError):
        return []
    return _sorted([r for r in out if r.label])


_SEMVER = re.compile(r"^v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$")
_PRE_RANK = {"a": "alpha", "b": "beta", "rc": "rc"}


def _ids(tag: str) -> tuple:
    """Semver pre-release identifiers: numeric ones compare as numbers and sort
    before alphanumeric ones, which compare as text."""
    return tuple((0, int(i)) if i.isdigit() else (1, i.lower()) for i in tag.split("."))


def _release(parts) -> tuple:
    out = list(parts)
    while out and out[-1] == 0:          # 1.0 == 1.0.0 == 1.0.0.0
        out.pop()
    return tuple(out)


def _key(label: str) -> tuple:
    """Version precedence for any registry's label, in one comparable shape.

    PEP 440 where the label parses, semver (npm, NuGet) where it does not -
    `0.0.0-stage` is not PEP 440 and used to sort after every real release.
    Within a release: dev < pre-release < final < post-release. A label that
    is neither sorts oldest rather than newest."""
    try:
        v = Version(label)
    except InvalidVersion:
        m = _SEMVER.match(label.strip())
        if not m:
            return ((-1,), (0, ((1, label),)))
        pre = (1, _ids(m.group(2))) if m.group(2) else (2, ())
        return (_release(int(p) for p in m.group(1).split(".")), pre)
    phase: tuple
    if v.pre:
        phase = (1, ((1, _PRE_RANK[v.pre[0]]), (0, v.pre[1])))
    elif v.dev is not None:
        phase = (0, ((0, v.dev),))
    elif v.post is not None:
        phase = (3, ((0, v.post),))
    else:
        phase = (2, ())
    return (_release(v.release), phase)


def is_prerelease(label: str) -> bool:
    """A release `pip`/`npm`/`nuget` would not pick by default, or one that is
    not a real release at all: -rc, -beta, -preview, .dev, and build tags such
    as `0.0.0-stage` or `1.1.1-local` (the Relate registries list both)."""
    try:
        v = Version(label)
        return v.is_prerelease or v.is_devrelease or v.local is not None
    except InvalidVersion:
        m = _SEMVER.match(label.strip())
        return not m or bool(m.group(2) or m.group(3))


def _sorted(rels: list[Release]) -> list[Release]:
    return sorted(rels, key=lambda r: _key(r.label))


def pick_versions(rels: list[Release], want: int = 3,
                  mentioned: list[str] | None = None,
                  spread: bool = False) -> list[Release]:
    """Current release, one back, and one predating the oldest version the
    docs mention.

    `spread` walks back by major version instead - the newest release of each
    major line, then of each minor. The .NET lane judges drift by comparing
    versions, so it needs one old enough to still compile what the docs say:
    three patches of one minor (8.0.6, 8.0.7, 8.0.8) agree on almost
    everything and hide the rot a 7.x release would expose.

    Pre-releases and non-release builds (`0.0.0-stage`) are never chosen while
    a stable release exists, unless the docs pin that exact version. A project
    that publishes nothing else keeps them - excluding them would leave some
    packages (perseus-client, `Relate.Net`) with no versions at all.
    """
    if not rels:
        return []

    pinned = {_squash(m) for m in mentioned or []}
    stable = [r for r in rels if not is_prerelease(r.label) or _squash(r.label) in pinned]
    pool = _sorted(stable) if stable else _sorted(rels)
    if spread:
        pool = _spread(pool)

    picked: list[Release] = []
    if mentioned:
        for r in pool:
            if _squash(r.label) in pinned:
                picked.append(r)

    for r in reversed(pool):
        if len(picked) >= want:
            break
        if r not in picked:
            picked.append(r)

    if len(picked) < want and len(pool) > want:
        oldest = pool[0]
        if oldest not in picked:
            picked.append(oldest)

    seen, out = set(), []
    for r in picked:
        if r.label not in seen:
            seen.add(r.label)
            out.append(r)
    if spread:
        # Maintenance lines ship on their own schedule - 7.4.12 went out the
        # same day as 8.0.8 - so the release date does not say which is latest.
        return _sorted(out)[-want:]
    return sorted(out, key=lambda r: r.released_at or "")[:want]


def _squash(label: str) -> str:
    return label.replace("-", "").replace("_", "").lower()


def _spread(pool: list[Release]) -> list[Release]:
    """`pool` reordered so that `pick_versions`, which takes from the end,
    takes the newest release of each major line first, then of each minor,
    then the rest - each group newest first."""
    def line(r: Release, depth: int) -> tuple:
        try:
            return Version(r.label).release[:depth]
        except InvalidVersion:
            return (r.label,)

    seen: set[str] = set()
    order: list[Release] = []
    for depth in (1, 2, 3):
        newest: dict[tuple, Release] = {}
        for r in pool:                       # pool is ascending: last one wins
            newest[line(r, depth)] = r
        for r in sorted(newest.values(), key=lambda r: pool.index(r), reverse=True):
            if r.label not in seen:
                seen.add(r.label)
                order.append(r)
    order += [r for r in reversed(pool) if r.label not in seen]
    return list(reversed(order))
