"""Scans and their runs, on disk.

    data/scans/<scan>/scan.json          name, sources, options
    data/scans/<scan>/status.json        the current or last attempt: state,
                                         phase, log, error
    data/scans/<scan>/runs/<run>/        run.json, report.json, results.json,
                                         extraction.json, findings.jsonl

Plain JSON written atomically. The CLI and the dashboard server both read and
write it, and neither should need a database running to see the other's scans.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import shutil
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

State = Literal["idle", "queued", "running", "done", "failed"]


class AlreadyImported(Exception):
    """This run is already in the store."""
PHASES = ["acquiring", "registry", "extracting", "verifying", "graph", "report"]
KEEP_RUNS = 30          # an hourly cron would otherwise grow without bound
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
NAME_MAX = 60


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class ScanOptions(BaseModel):
    ecosystem: Literal["pypi", "npm"] = "pypi"
    versions: int = Field(3, ge=1, le=10)
    max_pages: int | None = Field(None, ge=1, le=2000)
    assume_credentialed: bool = False
    no_cache: bool = False
    extraction_models: list[str] = Field(default_factory=list)


class ScanRecord(BaseModel):
    id: str
    name: str
    # set when the scan came from an exported file rather than a scan here
    imported_from: str = ""
    docs: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)
    options: ScanOptions = Field(default_factory=ScanOptions)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


class ScanStatus(BaseModel):
    state: State = "idle"
    phase: int = 0
    queued_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    log: list[list[str]] = Field(default_factory=list)   # [style, message]


class RunSummary(BaseModel):
    id: str
    at: str
    elapsed: float = 0.0
    stats: dict = Field(default_factory=dict)
    # pages per state (fail, drift, block, pass, unlit): the dashboard card's
    # page bar, without fetching the whole report for every card
    page_states: dict[str, int] = Field(default_factory=dict)


def page_states(report: dict) -> dict[str, int]:
    counts = {k: 0 for k in ("fail", "drift", "block", "pass", "unlit")}
    for sec in report.get("sections", []):
        for p in sec.get("pages", []):
            counts[p.get("st", "unlit")] = counts.get(p.get("st", "unlit"), 0) + 1
    return counts


# ----------------------------------------------------------------- validation

def is_docs_url(value: str) -> bool:
    p = urlparse(value)
    return p.scheme in ("http", "https") and "." in (p.hostname or "")


def is_repo(value: str) -> bool:
    """A git remote (https, ssh, scp-style) or a local path."""
    if re.match(r"^git@[\w.-]+:[\w.-]+/[\w.-]+$", value):
        return True
    if re.match(r"^(https?|ssh)://", value):
        return is_docs_url(re.sub(r"^ssh:", "https:", value))
    return bool(re.match(r"^(/|\.{1,2}/|~/)", value))


def clean_sources(docs: list[str], repos: list[str]) -> tuple[list[str], list[str]]:
    """Trimmed, de-duplicated and validated. Raises ValueError listing every
    problem, in the same words the dashboard shows beside each field."""
    def tidy(values):
        out: list[str] = []
        for v in values or []:
            v = (v or "").strip()
            if v and v not in out:
                out.append(v)
        return out

    docs, repos = tidy(docs), tidy(repos)
    problems = [f"Docs URL {i} isn't an http(s) address."
                for i, u in enumerate(docs, 1) if not is_docs_url(u)]
    problems += [f"Repository {i} isn't a git URL or a local path (/…, ./…, ~/…)."
                 for i, u in enumerate(repos, 1) if not is_repo(u)]
    if not problems and not docs and not repos:
        problems.append("Add at least one docs URL or repository — "
                        "docrot needs something to read.")
    if problems:
        raise ValueError(problems)
    return docs, repos


def default_name(docs: list[str], repos: list[str]) -> str:
    if docs:
        p = urlparse(docs[0])
        return ((p.hostname or "") + p.path.rstrip("/"))[:NAME_MAX]
    return repos[0].rstrip("/").split("/")[-1].split(":")[-1].removesuffix(".git")[:NAME_MAX]


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError(["Name can't be empty."])
    if len(name) > NAME_MAX:
        raise ValueError([f"Name must be {NAME_MAX} characters or fewer."])
    return name


# ---------------------------------------------------------------------- store

class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.scans_dir = self.root / "scans"
        self.scans_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------- helpers
    def _dir(self, scan_id: str) -> Path:
        if not ID_RE.match(scan_id or ""):
            raise KeyError(scan_id)
        return self.scans_dir / scan_id

    @staticmethod
    def _write(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        text = data if isinstance(data, str) else json.dumps(data, indent=1)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _read(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # --------------------------------------------------------------- scans
    def all(self) -> list[ScanRecord]:
        out = []
        for d in sorted(self.scans_dir.iterdir()):
            data = self._read(d / "scan.json") if d.is_dir() else None
            if data:
                out.append(ScanRecord.model_validate(data))
        return out

    def get(self, scan_id: str) -> ScanRecord | None:
        try:
            data = self._read(self._dir(scan_id) / "scan.json")
        except KeyError:
            return None
        return ScanRecord.model_validate(data) if data else None

    def create(self, docs: list[str], repos: list[str], name: str = "",
               packages: list[str] | None = None, options: ScanOptions | None = None,
               imported_from: str = "") -> ScanRecord:
        if imported_from:
            # an import carries whatever sources the file names, including none
            docs, repos = [d for d in docs if d], [r for r in repos if r]
            name = clean_name(name or imported_from)
        else:
            docs, repos = clean_sources(docs, repos)
            name = clean_name(name) if (name or "").strip() else default_name(docs, repos)
        with self._lock:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "scan"
            scan_id = f"{slug}-{secrets.token_hex(3)}"
            while (self.scans_dir / scan_id).exists():
                scan_id = f"{slug}-{secrets.token_hex(3)}"
            rec = ScanRecord(id=scan_id, name=name, docs=docs, repos=repos,
                             imported_from=imported_from,
                             packages=[p.strip() for p in packages or [] if p.strip()],
                             options=options or ScanOptions())
            self._write(self._dir(scan_id) / "scan.json", rec.model_dump())
            return rec

    def update(self, scan_id: str, **changes) -> ScanRecord:
        with self._lock:
            rec = self.get(scan_id)
            if rec is None:
                raise KeyError(scan_id)
            if "name" in changes:
                changes["name"] = clean_name(changes["name"])
            rec = rec.model_copy(update={**changes, "updated_at": now()})
            self._write(self._dir(scan_id) / "scan.json", rec.model_dump())
            return rec

    def find_by_sources(self, docs: list[str], repos: list[str]) -> ScanRecord | None:
        """The scan watching exactly these sources, in any order."""
        want = (sorted(docs), sorted(repos))
        return next((r for r in self.all()
                     if (sorted(r.docs), sorted(r.repos)) == want), None)

    # -------------------------------------------------------------- status
    def status(self, scan_id: str) -> ScanStatus:
        data = self._read(self._dir(scan_id) / "status.json")
        return ScanStatus.model_validate(data) if data else ScanStatus()

    def set_status(self, scan_id: str, status: ScanStatus) -> None:
        with self._lock:
            self._write(self._dir(scan_id) / "status.json", status.model_dump())

    def recover(self) -> list[str]:
        """Scans left queued or running by a process that is gone.

        Called when the server starts: nothing else is working on them, so
        leaving them `running` would show a spinner forever."""
        stuck = []
        for rec in self.all():
            st = self.status(rec.id)
            if st.state in ("queued", "running"):
                st.state, st.finished_at = "failed", now()
                st.error = "Interrupted — the server stopped before this scan finished."
                st.log.append(["t-hit", "! interrupted: the server stopped"])
                self.set_status(rec.id, st)
                stuck.append(rec.id)
        return stuck

    # ---------------------------------------------------------------- runs
    def runs(self, scan_id: str) -> list[RunSummary]:
        d = self._dir(scan_id) / "runs"
        if not d.exists():
            return []
        out = []
        for run in sorted(d.iterdir()):
            data = self._read(run / "run.json") if run.is_dir() else None
            if data:
                out.append(RunSummary.model_validate(data))
        return out

    def run_dir(self, scan_id: str, run_id: str) -> Path:
        if not re.match(r"^\d{8}T\d{6}(-\d+)?$", run_id or ""):
            raise KeyError(run_id)
        return self._dir(scan_id) / "runs" / run_id

    def save_run(self, scan_id: str, report: dict, results: list[dict],
                 extraction: str | dict, at: str | None = None) -> RunSummary:
        with self._lock:
            at = at or report.get("generated_at") or now()
            base = dt.datetime.fromisoformat(at).strftime("%Y%m%dT%H%M%S")
            run_id, i = base, 2
            while (self._dir(scan_id) / "runs" / run_id).exists():
                run_id, i = f"{base}-{i}", i + 1
            d = self.run_dir(scan_id, run_id)
            self._write(d / "report.json", report)
            self._write(d / "results.json", results)
            self._write(d / "extraction.json", extraction)
            summary = RunSummary(id=run_id, at=at, elapsed=report.get("elapsed", 0.0),
                                 stats=report.get("stats", {}),
                                 page_states=page_states(report))
            self._write(d / "run.json", summary.model_dump())
            try:
                self._write(d / "findings.jsonl", self.export_text(scan_id, run_id))
            except Exception:           # never lose a finished run to its export;
                pass                    # `docrot export` regenerates and shows the error
            for old in self.runs(scan_id)[:-KEEP_RUNS]:
                shutil.rmtree(self.run_dir(scan_id, old.id), ignore_errors=True)
            return summary

    def load_run(self, scan_id: str, run_id: str | None = None
                 ) -> tuple[RunSummary, dict, dict, list[dict]] | None:
        """(summary, report, extraction, results) for a run - the newest by default."""
        runs = self.runs(scan_id)
        run = next((r for r in runs if r.id == run_id), None) if run_id else (runs[-1] if runs else None)
        if run is None:
            return None
        d = self.run_dir(scan_id, run.id)
        return (run, self._read(d / "report.json") or {}, self._read(d / "extraction.json") or {},
                self._read(d / "results.json") or [])

    def export(self, scan_id: str, run_id: str | None = None, include_clean: bool = False):
        """JSON Lines records for a run (see report/export.py), or None if no such run."""
        from .report import export

        rec, loaded = self.get(scan_id), self.load_run(scan_id, run_id)
        if rec is None or loaded is None:
            return None
        run, report, extraction, results = loaded
        return export.records(rec.model_dump(), run.model_dump(), report, extraction, results,
                              include_clean=include_clean)

    def export_text(self, scan_id: str, run_id: str | None = None, include_clean: bool = False,
                    fmt: str = "jsonl") -> str:
        """A run as JSON Lines (`jsonl`) or SARIF 2.1.0 (`sarif`)."""
        import io
        import json as jsonlib

        from .report import export, sarif

        lines = self.export(scan_id, run_id, include_clean)
        if lines is None:
            raise KeyError(run_id or scan_id)
        if fmt == "sarif":
            return jsonlib.dumps(sarif.document(lines), indent=1) + "\n"
        if fmt != "jsonl":
            raise ValueError(f"unknown export format: {fmt}")
        buf = io.StringIO()
        export.write(lines, buf)
        return buf.getvalue()

    def report(self, scan_id: str, run_id: str | None = None) -> dict | None:
        runs = self.runs(scan_id)
        if not runs:
            return None
        run = run_id or runs[-1].id
        return self._read(self.run_dir(scan_id, run) / "report.json")

    # -------------------------------------------------------------- import
    def import_run(self, text: str, filename: str = "", name: str = "") -> tuple[ScanRecord, RunSummary]:
        """Adopt an exported run (JSON Lines or SARIF) as a scan the UI can show.

        A file whose sources match an existing scan adds a run to it, so an
        export from elsewhere lands in the history of the scan it belongs to;
        anything else becomes a new scan. Raises `BadExport` for a file this
        cannot read, and `AlreadyImported` for a run already present.
        """
        import json as jsonlib

        from .report import ingest

        run = ingest.to_run(ingest.read(text), filename)
        docs, repos = run["scan"]["docs"], run["scan"]["repos"]
        with self._lock:
            rec = self.find_by_sources(docs, repos) if (docs or repos) else None
            if rec is not None:
                # the same run twice: same moment, or the same original run id
                already = next((r for r in self.runs(rec.id)
                                if r.at == run["report"].get("generated_at")
                                or ((self.report(rec.id, r.id) or {}).get("imported") or {}).get("run")
                                == (run["run"] or None)), None)
                if already is not None:
                    raise AlreadyImported(
                        f"{rec.name} already has this run ({already.id}).")
            else:
                rec = self.create(docs, repos, name=name or run["scan"]["name"],
                                  options=ScanOptions(**(run["scan"].get("options") or {})),
                                  imported_from=filename or "an uploaded file")
            summary = self.save_run(rec.id, run["report"], run["results"],
                                    jsonlib.dumps(run["extraction"], indent=1))
            self.set_status(rec.id, ScanStatus(state="done", finished_at=summary.at,
                                               log=run["report"].get("log", [])))
            return rec, summary

    # -------------------------------------------------------------- legacy
    def import_legacy(self, runs_dir: Path) -> ScanRecord | None:
        """Adopt the pre-dashboard `data/runs/` archive as one scan.

        Only when no scans exist yet, so it happens once. The old archiver
        copied `report.json` before the new one was written, so an archived
        directory holds the *previous* run's report next to its own results;
        each run takes its report from the next directory, and the newest from
        `latest/`.
        """
        runs_dir = Path(runs_dir)
        latest = runs_dir / "latest"
        if self.all() or not (latest / "report.json").exists():
            return None
        head = self._read(latest / "report.json") or {}
        archived = sorted(d for d in runs_dir.iterdir()
                          if d.is_dir() and d.name != "latest"
                          and (d / "results.json").exists())
        report_dirs = [*archived[1:], latest]
        docs = [head["docs_url"]] if head.get("docs_url") else []
        repos = [head["repo_url"]] if head.get("repo_url") else []
        if not docs and not repos:
            return None
        rec = self.create(docs, repos, name=head.get("package") or "")
        for run, rep_dir in zip(archived, report_dirs, strict=True):
            report = self._read(rep_dir / "report.json")
            results = self._read(run / "results.json")
            extraction = (run / "extraction.json").read_text(encoding="utf-8") \
                if (run / "extraction.json").exists() else "{}"
            if report and results is not None:
                self.save_run(rec.id, report, results, extraction)
        last = self.runs(rec.id)
        if last:
            self.set_status(rec.id, ScanStatus(state="done", finished_at=last[-1].at))
        return rec
