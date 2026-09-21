"""Deterministic code-fence extraction. The model is never asked to find
these - mistune parses them exactly, with no hallucinated line numbers.
"""
from __future__ import annotations

import re

import mistune

from ..models import Page, Snippet

PY_LANGS = {"python", "py", "python3"}
SHELL_LANGS = {"bash", "sh", "shell", "console", "zsh"}


def parse(pages: list[Page]) -> list[Snippet]:
    md = mistune.create_markdown(renderer=None)
    out: list[Snippet] = []
    used: set[str] = set()
    for page in pages:
        n = 0
        slug = _unique_slug(page, used)
        try:
            tokens = md(page.text)
        except Exception:
            tokens = []
        for tok in _walk(tokens):
            if tok.get("type") != "block_code":
                continue
            code = (tok.get("raw") or "").rstrip()
            if not code.strip():
                continue
            info = (tok.get("attrs") or {}).get("info") or ""
            lang = info.strip().split()[0].lower() if info.strip() else _sniff(code)
            n += 1
            out.append(Snippet(
                id=f"{slug}-{n:02d}",
                page=page.url,
                lang=lang,
                code=code,
                line=_line_of(page.text, code),
            ))
    return out


def _walk(tokens):
    for t in tokens or []:
        if not isinstance(t, dict):
            continue
        yield t
        kids = t.get("children")
        if isinstance(kids, list):
            yield from _walk(kids)


def _sniff(code: str) -> str:
    if re.search(r"^\s*(import |from \w+ import|def |class )", code, re.M):
        return "python"
    if re.search(r"^\s*(pip|npm|uv|git|curl|export|docker)\b", code, re.M):
        return "bash"
    if code.lstrip().startswith(("{", "[")):
        return "json"
    return "text"


def _line_of(text: str, code: str) -> int:
    head = code.strip().splitlines()[0] if code.strip() else ""
    if not head:
        return 0
    idx = text.find(head)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else 0


def _unique_slug(page: Page, used: set[str]) -> str:
    """The id prefix for a page's snippets.

    Ids are keyed on the page path, which is unique within one site but not
    across two (`/quickstart` on both) or across two repos (`README.md`). The
    first page keeps the plain slug, so single-source ids are unchanged and old
    runs still diff; a later collision is qualified by where it came from.
    """
    slug = _slug(page.path)
    if slug in used:
        where = page.url.split("://", 1)[-1].split("/", 1)[0]
        slug = _slug(f"{where}-{page.path}")
        base, i = slug, 2
        while slug in used:
            slug, i = f"{base}-{i}", i + 1
    used.add(slug)
    return slug


def _slug(path: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return s[-40:] or "page"
