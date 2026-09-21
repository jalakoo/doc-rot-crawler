"""One shared HTTP client, with disk caching so reruns hit no network."""
from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from ..config import CACHE


def _key(url: str) -> Path:
    return CACHE / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".cache")


class Fetcher:
    def __init__(self, user_agent: str, use_cache: bool = True):
        self.use_cache = use_cache
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": user_agent},
        )

    async def get(self, url: str) -> tuple[int, str]:
        """Returns (status_code, text). Cached responses report 200."""
        cached = _key(url)
        if self.use_cache and cached.exists():
            return 200, cached.read_text(encoding="utf-8", errors="replace")
        try:
            r = await self.client.get(url)
        except httpx.HTTPError:
            return 0, ""
        if r.status_code == 200 and self.use_cache:
            cached.write_text(r.text, encoding="utf-8", errors="replace")
        return r.status_code, r.text

    async def ok(self, url: str) -> bool:
        """Status-code check only.

        Many docs hosts serve a full HTML body with a 404, so probes must
        branch on the status code and never on whether a body came back.
        """
        code, _ = await self.get(url)
        return code == 200

    async def aclose(self) -> None:
        await self.client.aclose()
