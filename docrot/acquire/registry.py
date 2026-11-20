"""Release lists straight from the package registry. No auth needed."""
from __future__ import annotations

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
        if not files:
            continue
        out.append(Release(label=label, released_at=files[0].get("upload_time", "")[:10]))
    return _sorted(out)


def _npm(package: str) -> list[Release]:
    try:
        r = httpx.get(f"https://registry.npmjs.org/{package}", timeout=30.0)
        r.raise_for_status()
    except httpx.HTTPError:
        return []
    times = r.json().get("time", {})
    out = [
        Release(label=k, released_at=v[:10])
        for k, v in times.items()
        if k not in ("created", "modified")
    ]
    return _sorted(out)


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


def _sorted(rels: list[Release]) -> list[Release]:
    def key(r: Release):
        try:
            return (0, Version(r.label))
        except InvalidVersion:
            return (1, r.label)
    try:
        return sorted(rels, key=key)
    except TypeError:
        return sorted(rels, key=lambda r: r.released_at)


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

    Pre-releases are kept when the project publishes nothing else - excluding
    them would leave some packages (perseus-client, for one) with no versions
    at all.
    """
    if not rels:
        return []

    def is_pre(label: str) -> bool:
        try:
            return Version(label).is_prerelease
        except InvalidVersion:
            return False

    stable = [r for r in rels if not is_pre(r.label)]
    pool = stable if len(stable) >= want else rels
    if spread:
        pool = _spread(pool)

    picked: list[Release] = []
    if mentioned:
        norm = {m.replace("-", "").replace("_", "").lower() for m in mentioned}
        for r in pool:
            if r.label.replace("-", "").replace("_", "").lower() in norm:
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
