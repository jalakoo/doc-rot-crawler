"""Pydantic models. `Extraction.model_json_schema()` feeds the LLM's
structured-output parameter directly, so these definitions are both the
internal contract and the wire schema.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Tier = Literal["execute", "structural", "unverifiable"]
Status = Literal["pass", "fail", "blocked", "unverified"]


class Snippet(BaseModel):
    id: str
    page: str
    lang: str
    code: str
    tier: Tier = "structural"
    requires: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    line: int = 0
    kind: str = "program"                 # program | declaration | fragment
    declares: list[dict] = Field(default_factory=list)
    package: str = ""                     # distribution this snippet exercises


class Claim(BaseModel):
    id: str
    page: str
    text: str
    symbols: list[str] = Field(default_factory=list)


class Page(BaseModel):
    url: str
    path: str
    title: str = ""
    source: str = "site"          # 'site' | 'repo'
    origin: str = ""              # the docs URL or repo it was acquired from
    text: str = ""
    pinned_versions: list[str] = Field(default_factory=list)


class PackageRef(BaseModel):
    """One package a scan verifies against. A scan with several repos has
    several, each with its own releases."""
    package: str
    import_name: str
    ecosystem: str = "pypi"


class Extraction(BaseModel):
    # `package` / `import_name` / `ecosystem` name the primary package, kept so
    # extraction.json from single-package runs still loads.
    package: str
    import_name: str
    ecosystem: str = "pypi"
    packages: list[PackageRef] = Field(default_factory=list)
    pages: list[Page] = Field(default_factory=list)
    snippets: list[Snippet] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)


class LLMSnippetJudgement(BaseModel):
    """The narrow slice the model is actually asked for."""
    id: str
    tier: Tier
    requires: list[str] = Field(default_factory=list)


class LLMJudgement(BaseModel):
    snippets: list[LLMSnippetJudgement] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)


class Result(BaseModel):
    id: str
    page: str
    version: str
    status: Status
    stderr: str = ""
    blocked_by: str | None = None
    tier: Tier = "structural"
    package: str = ""


class Release(BaseModel):
    label: str
    released_at: str = ""


class Finding(BaseModel):
    """One docs-vs-code disagreement, as rendered in the UI."""
    kind: str                     # stale_pin | signature | missing_symbol | runtime | blocked | clean | prose
    snippet: str = ""             # the snippet this is about; empty for page-level findings
    line: int = 0                 # line in the page text, where known
    label: str                    # short chip label
    title: str
    desc: str
    page: str
    doc_where: str = ""
    doc_url: str = ""
    doc_stamp_label: str = "docs reflect"
    doc_stamp: str = ""
    code_where: str = ""
    code_url: str = ""
    code_stamp_label: str = "code is at"
    code_stamp: str = ""
    doc_parts: list[list[str]] = Field(default_factory=list)     # [text, same|del|ins|void]
    code_parts: list[list[str]] = Field(default_factory=list)
    damage: str = ""
    days_behind: int = 0
    evidence: str = ""
    chips: list[list[str]] = Field(default_factory=list)         # [css class, label]
    sprite: str = "slime"


class Target(PackageRef):
    """A package as verified: where its code lives, its registry releases, and
    the versions actually tested. The report and graph need all of it, per
    package, once a scan can span several repos."""
    repo_url: str = ""
    branch: str = ""
    commit: str = ""
    releases: list[Release] = Field(default_factory=list)
    versions: list[Release] = Field(default_factory=list)
    # pip spec used when the package is not on a registry: the repo at the
    # scanned commit, e.g. git+https://host/org/repo@sha#subdirectory=core
    install_spec: str = ""

    @property
    def latest(self) -> Release:
        return self.versions[-1] if self.versions else Release(label="HEAD")

    @property
    def labels(self) -> list[str]:
        return [v.label for v in self.versions]
