"""The dashboard: the UI plus a small JSON API over the scan store.

    GET    /api/scans                   every scan, with status and run history
    POST   /api/scans                   create a scan and queue its first run
    GET    /api/scans/{id}              one scan
    PATCH  /api/scans/{id}              rename
    POST   /api/scans/{id}/runs         queue another run
    POST   /api/imports                 adopt an exported run (JSON Lines or SARIF)
    GET    /api/scans/{id}/report       the newest report (?run=<id> for another)
    GET    /api/scans/{id}/findings.jsonl   the same run as JSON Lines, for agents
    GET    /api/scans/{id}/findings.sarif   the same run as SARIF 2.1.0, for CI

Bound to 127.0.0.1: it can start sandboxes and spend API credit on request,
and it has no authentication. Two guards keep the rest of the web out of it:
a page on another origin cannot POST to it (CSRF), and a hostname that resolves
to 127.0.0.1 cannot reach it either (DNS rebinding). See `guard`.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .report.ingest import BadExport
from .store import AlreadyImported, RunSummary, ScanOptions, ScanRecord, ScanStatus, Store
from .worker import AlreadyActive, ScanWorker

UI = Path(__file__).resolve().parent / "ui"

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
WRITES = {"POST", "PUT", "PATCH", "DELETE"}

# The dashboard loads its own scripts and styles, plus two Google fonts. With
# script-src 'self' an injected element cannot carry an inline handler, which is
# the last line of defence behind escaping a finding's text.
CSP = ("default-src 'self'; script-src 'self'; "
       "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
ASSETS = ("styles.css", "lib.js", "app.js")
ASSET_REF = re.compile(r'((?:src|href)=")(' + "|".join(map(re.escape, ASSETS)) + r')(")')


def render_index() -> str:
    """index.html with every local asset URL carrying a hash of its content.

    The dashboard replaced a page served by Python's static file server, which
    sent no Cache-Control, so browsers kept `app.js` and `styles.css` - the same
    names the dashboard uses - for hours. Versioned URLs mean a new page can
    never run a stale script, today or after the next upgrade.
    """
    html = (UI / "index.html").read_text(encoding="utf-8")

    def version(m: re.Match) -> str:
        digest = hashlib.sha1((UI / m.group(2)).read_bytes(), usedforsecurity=False).hexdigest()[:10]
        return f"{m.group(1)}{m.group(2)}?v={digest}{m.group(3)}"

    return ASSET_REF.sub(version, html)


def dashboard_url(port: int, path: str = "") -> str:
    """A URL no browser has cached: the old single-report page lived at `/`."""
    return f"http://127.0.0.1:{port}/?t={int(time.time())}#/{path.lstrip('#/')}"


class UIFiles(StaticFiles):
    """The dashboard has no build step, so asset names never change. Without
    this, a browser keeps serving yesterday's app.js after an upgrade; with it,
    every load revalidates against the ETag and still gets a 304 when unchanged."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class ScanView(ScanRecord):
    status: ScanStatus
    runs: list[RunSummary]


class CreateScan(BaseModel):
    name: str = ""
    docs: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)
    options: ScanOptions = Field(default_factory=ScanOptions)


class UpdateScan(BaseModel):
    name: str


def _hostname(value: str) -> str:
    """Host or Origin header down to its hostname, port and scheme removed."""
    value = (value or "").strip().rsplit("//", 1)[-1]
    if value.startswith("["):                       # [::1]:8080
        return value.split("]")[0] + "]"
    return value.split(":")[0]


def create_app(store: Store, worker: ScanWorker) -> FastAPI:
    app = FastAPI(title="docrot", docs_url="/api/docs", redoc_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Refuse anything that did not come from this machine's own dashboard.

        A page on another site can POST here without a preflight (a text/plain
        body, or no body at all), which would let it start scans that cost
        sandbox time and model credit. It cannot forge Origin, and it cannot
        make the browser send our Host either, so both are checked.
        """
        if _hostname(request.headers.get("host", "")) not in LOCAL_HOSTS:
            return JSONResponse({"detail": ["docrot serves 127.0.0.1 only."]}, status_code=403)
        origin = request.headers.get("origin")
        if request.method in WRITES and origin and _hostname(origin) not in LOCAL_HOSTS:
            return JSONResponse({"detail": [f"Refused a write from {origin}."]}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    def view(rec: ScanRecord) -> ScanView:
        return ScanView(**rec.model_dump(), status=store.status(rec.id),
                        runs=store.runs(rec.id))

    def found(scan_id: str) -> ScanRecord:
        rec = store.get(scan_id)
        if rec is None:
            raise HTTPException(404, "no such scan")
        return rec

    @app.get("/api/scans", response_model=list[ScanView])
    def list_scans():
        return [view(r) for r in store.all()]

    @app.post("/api/scans", response_model=ScanView, status_code=201)
    def create_scan(body: CreateScan):
        try:
            rec = store.create(body.docs, body.repos, name=body.name,
                               packages=body.packages, options=body.options)
        except ValueError as e:
            raise HTTPException(422, e.args[0]) from e
        worker.enqueue(rec.id)
        return view(rec)

    @app.post("/api/imports", response_model=ScanView, status_code=201)
    async def import_export(request: Request, filename: str = "", name: str = ""):
        """Body is the exported file itself: JSON Lines or SARIF."""
        text = (await request.body()).decode("utf-8", "replace")
        try:
            rec, _ = store.import_run(text, filename=filename, name=name)
        except BadExport as e:
            raise HTTPException(422, [str(e)]) from e
        except AlreadyImported as e:
            raise HTTPException(409, [str(e)]) from e
        except ValueError as e:
            raise HTTPException(422, e.args[0] if isinstance(e.args[0], list) else [str(e)]) from e
        return view(rec)

    @app.get("/api/scans/{scan_id}", response_model=ScanView)
    def get_scan(scan_id: str):
        return view(found(scan_id))

    @app.patch("/api/scans/{scan_id}", response_model=ScanView)
    def update_scan(scan_id: str, body: UpdateScan):
        found(scan_id)
        try:
            return view(store.update(scan_id, name=body.name))
        except ValueError as e:
            raise HTTPException(422, e.args[0]) from e

    @app.post("/api/scans/{scan_id}/runs", response_model=ScanView, status_code=202)
    def rerun(scan_id: str):
        rec = found(scan_id)
        try:
            worker.enqueue(scan_id)
        except AlreadyActive as e:
            raise HTTPException(409, "this scan is already queued or running") from e
        return view(rec)

    @app.get("/api/scans/{scan_id}/report")
    def report(scan_id: str, run: str | None = None):
        found(scan_id)
        try:
            data = store.report(scan_id, run)
        except KeyError as e:
            raise HTTPException(404, "no such run") from e
        if data is None:
            raise HTTPException(404, "no finished run yet")
        return data

    def _export(scan_id: str, run: str | None, include_clean: bool, fmt: str, media: str):
        found(scan_id)
        loaded = store.load_run(scan_id, run)
        if loaded is None:
            raise HTTPException(404, "no such run" if run else "no finished run yet")
        run_id = loaded[0].id
        text = store.export_text(scan_id, run_id, include_clean=include_clean, fmt=fmt)
        return Response(text, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="{scan_id}-{run_id}.{fmt}"'})

    @app.get("/api/scans/{scan_id}/findings.jsonl")
    def findings_jsonl(scan_id: str, run: str | None = None, include_clean: bool = False):
        return _export(scan_id, run, include_clean, "jsonl", "application/x-ndjson")

    @app.get("/api/scans/{scan_id}/findings.sarif")
    def findings_sarif(scan_id: str, run: str | None = None, include_clean: bool = False):
        return _export(scan_id, run, include_clean, "sarif", "application/sarif+json")

    @app.get("/", include_in_schema=False)
    def index():
        return HTMLResponse(render_index(), headers={"Cache-Control": "no-store"})

    @app.get("/report.json", include_in_schema=False)
    @app.get("/data/report.json", include_in_schema=False)
    def legacy_report():
        # Only a cached copy of the pre-dashboard page asks for this. Tell the
        # browser to drop its cache for this origin, so one reload lands on the
        # dashboard instead of that page's "No scan found".
        return JSONResponse(
            {"detail": "The single-report page was replaced by the dashboard - reload the page."},
            status_code=410, headers={"Clear-Site-Data": '"cache"', "Cache-Control": "no-store"})

    app.mount("/", UIFiles(directory=UI), name="ui")
    return app


def _open_when_ready(port: int, url: str, timeout: float = 30.0) -> None:
    """Open the browser once the port accepts connections. A fixed delay raced
    startup, which takes about a second, and could land on connection refused."""
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.2)


def serve(store: Store, port: int = 8080, open_browser: bool = True,
          path: str = "") -> None:
    import uvicorn

    from .scan import execute_scan

    stuck = store.recover()
    worker = ScanWorker(store, lambda s, scan_id: execute_scan(s, scan_id))
    for scan_id in stuck:
        print(f"  ! {scan_id} was interrupted by the last shutdown - marked failed")
    url = dashboard_url(port, path)
    print(f"\n  dashboard:  {url}\n  ctrl-c to stop\n")
    if open_browser:
        threading.Thread(target=_open_when_ready, args=(port, url), daemon=True).start()
    try:
        uvicorn.run(create_app(store, worker), host="127.0.0.1", port=port,
                    log_level="warning")
    finally:
        worker.stop()
