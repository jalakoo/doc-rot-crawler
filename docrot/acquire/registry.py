"""Release lists straight from the package registry. No auth needed."""
from __future__ import annotations

import httpx
from packaging.version import InvalidVersion, Version

from ..models import Release


def releases(package: str, ecosystem: str = "pypi") -> list[Release]:
    if ecosystem == "npm":
        return _npm(package)
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
                  mentioned: list[str] | None = None) -> list[Release]:
    """Current release, one back, and one predating the oldest version the
    docs mention.

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
    return sorted(out, key=lambda r: r.released_at or "")[:want]
