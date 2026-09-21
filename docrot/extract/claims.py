"""Prose claims, and binding them to symbols.

A page with zero snippets can still rot: if it states that `build_graph`
accepts a list of file paths and `build_graph` no longer exists, the page is
wrong even though there is nothing to execute. Claims are what let such a page
inherit a failure, via `Claim -[:ABOUT]-> Symbol`.

The spec provisioned a self-hosted embedding endpoint for this. Spike S4
measured the lexical fallback instead - identifier-shaped tokens matched
against the symbol table - and got **precision 1.000, recall 0.968** over 255
claims against 103 symbols. That is above the bar set for keeping embeddings,
so the endpoint is gone: no service, no config, no failure mode.

One caveat S4 made explicit: it measured claim *linking* (given a claim, does
it attach to the right symbol), not claim *discovery* (are the claims worth
linking found at all). Discovery here is deliberately shallow - a sentence
mentioning something identifier-shaped. An extraction model, where one is
configured, can add claims this misses; it never replaces the linking.
"""
from __future__ import annotations

import re

from ..models import Claim, Page

# Identifier-shaped tokens: a backticked span, snake_case, or CamelCase.
# `[\w]` rather than `[a-z_]` on the snake_case branch is deliberate - excluding
# digits was the sole cause of all three recall misses S4 found
# (`save_to_neo4j`, `save_to_neo4j_async`).
IDENT = re.compile(
    r"`([A-Za-z_][\w.]*)`"
    r"|\b([a-z_][\w]*_[\w]+)\b"
    r"|\b([A-Z][a-z]+[A-Z]\w*)\b")

SENTENCE = re.compile(r"(?<=[.!?])\s+")
FENCE = re.compile(r"```.*?```", re.S)

MIN_LEN, MAX_LEN = 30, 400


def extract(pages: list[Page], symbols: set[str]) -> list[Claim]:
    """Prose sentences that name a symbol the package actually defines."""
    out: list[Claim] = []
    for page in pages:
        # drop fenced code so the claims are genuinely prose
        prose = FENCE.sub(" ", page.text or "")
        for i, sentence in enumerate(SENTENCE.split(prose)):
            sentence = " ".join(sentence.split())
            if not (MIN_LEN <= len(sentence) <= MAX_LEN):
                continue
            bound = link(sentence, symbols)
            if bound:
                out.append(Claim(id=f"{_slug(page.path)}-c{i}", page=page.url,
                                 text=sentence, symbols=bound))
    return out


def link(text: str, symbols: set[str]) -> list[str]:
    """Symbols this sentence is about. Lexical - no embedding service."""
    hits = set()
    for m in IDENT.finditer(text):
        for group in m.groups():
            if not group:
                continue
            tail = group.split(".")[-1]
            if tail in symbols:
                hits.add(tail)
    return sorted(hits)


def _slug(path: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (path or "").lower()).strip("-")
    return s[-40:] or "page"
