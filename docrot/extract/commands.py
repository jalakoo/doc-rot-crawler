"""Who owns a command a shell snippet runs.

A command line like `relate query ...` is only evidence about a package if that
package really puts `relate` on the PATH. Matching by name alone attributed 330
`relate ...` snippets to `@relate/relate` on the Relate docs - an npm package
whose package.json has no `bin` at all. The commands are the standalone Rust
binary's, and counting them as "structural" for the npm package left them all
unverified.

So ownership is decided from what is known about each target:

  * an npm package whose HEAD package.json was read owns exactly the bins it
    declares (an empty list is an answer: it owns none);
  * a standalone CLI target owns its command name;
  * an npm package with no package.json to read (registry-only) keeps the old
    guess, its unscoped name - but a CLI target of that name wins it;
  * `npx <pkg> ...` and friends always belong to the named npm package.

A command nobody owns is not evidence about any package.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..models import PackageRef, Page
from . import fences, javascript


@dataclass(frozen=True)
class Run:
    """One command line, and the package it exercises."""
    inv: javascript.Invocation
    kind: str          # "npm" | "cli"
    owner: str         # the npm package, or the CLI's command name


def _tail(name: str) -> str:
    return name.rsplit("/", 1)[-1]


class Ownership:
    def __init__(self, npm: Sequence[str] = (), bins: Mapping[str, Sequence[str]] | None = None,
                 cli: Sequence[str] = ()):
        self.npm = tuple(n for n in npm if n)
        self.cli = tuple(c for c in cli if c)
        known = bins or {}
        bare: dict[str, tuple[str, str]] = {}
        for name in self.npm:                     # the guess, for a package nobody has read
            if name not in known:
                bare[_tail(name)] = ("npm", name)
        for command in self.cli:                  # a CLI target beats a guess ...
            bare[command] = ("cli", command)
        for name in self.npm:                     # ... but not a declared bin
            for b in known.get(name, ()):
                bare[b] = ("npm", name)
        self._bare = bare

    @classmethod
    def of(cls, packages: Sequence[PackageRef], bins: Mapping[str, Sequence[str]] | None = None) -> Ownership:
        return cls([p.import_name for p in packages if p.ecosystem == "npm"], bins,
                   [p.package for p in packages if p.ecosystem == "cli"])

    @property
    def active(self) -> bool:
        """Is there any command to look for? Without, shell is read as before."""
        return bool(self.npm or self.cli)

    def runs(self, code: str) -> list[Run]:
        """The command lines in `code` that run a target's command."""
        if not self.active:
            return []
        out = []
        for inv in javascript.invocations(code, {*self.npm, *self._bare}):
            if inv.package:                       # npx and friends name their package
                if inv.package in self.npm:
                    out.append(Run(inv, "npm", inv.package))
                continue
            owner = self._bare.get(inv.bin)
            if owner:
                out.append(Run(inv, *owner))
        return out

    def owner(self, code: str) -> str:
        """The package the first owned command line in `code` belongs to."""
        runs = self.runs(code)
        return runs[0].owner if runs else ""

    def owners(self, code: str) -> set[str]:
        return {r.owner for r in self.runs(code)}


# Command lines of a command the docs must show before a standalone CLI is
# proposed for it. A bare `relate` in prose is English (Relate's docs say it on
# every page); a shell fence that runs it is the tool's own docs. Measured on
# Relate's docs: 412 lines in 330 fences. Two or three lines can be a stray
# example for something else of that name, so the bar is three.
MIN_COMMAND_LINES = 3


def command_lines(pages: Sequence[Page], name: str) -> int:
    """How many command lines in the pages' shell fences run `name` itself.

    `npx name ...` is the npm package of that name, not a standalone binary,
    so a runner line does not count. Only fences are read: prose never counts."""
    total = 0
    for s in fences.parse(list(pages)):
        if fences.family(s.lang) == "shell":
            total += sum(1 for inv in javascript.invocations(s.code, [name]) if not inv.package)
    return total
