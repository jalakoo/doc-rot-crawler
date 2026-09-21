"""Sandbox runner.

Two lanes, because the tiers have different needs:

  structural  ONE sandbox per version, ONE exec, every probe batched into a
              single interpreter. Structural probing is pure introspection, so
              it shares no state and needs no ordering. Measured: 255 probes
              across 3 versions in 21.4s with 3 sandboxes and zero errors,
              against 853.1s and 72 sandboxes for the per-chain shape.

  execute     one sandbox per chain per version, as before, because these
              snippets really do mutate state. Measured: 7 of 8 sequential
              chains in a shared sandbox saw the previous chain's files, so
              they can never share the structural sandbox.

Three things the previous version got wrong, all measured in spike S1/S2:

  * It built an `AsyncDaytona` inside every chain - 72 clients and 72
    connection pools for a 72-chain run. One shared client cut wall clock 4.3x
    and `create` p95 19x (46.1s -> 2.4s). The DaytonaConnectionTimeoutErrors in
    the 2026-09-09 log were this, not the provider.

  * It blocked everything downstream of ANY failure, including a structural
    one. A structural probe mutates nothing, so nothing downstream of it is
    blocked. 32 of 72 blocks were unjustified and masked 3 real findings.

  * It treated an install failure as final. A transient PyPI connection reset
    mid-run is not documentation rot; install retries once.

The concurrency ceiling is an account quota, not a tuning knob:
`Total CPU limit exceeded. Maximum allowed: 10`. Measured error rates: 0% at 8,
16.7% at 12, 37.5% at 16. With the structural lane batched to one sandbox per
version, the quota stops binding in practice.
"""
from __future__ import annotations

import asyncio
import re
import time

from ..config import Config, redact
from ..models import Result, Snippet, Status
from .probe import batched_script, build_targets, parse_batched

# A snippet that dies for want of an API key has told us nothing about whether
# the documentation is correct. Recording that as `fail` would put a red row on
# a page that may be perfectly accurate, so it is reported as `unverified`.
CREDENTIAL_ERROR = re.compile(
    r"(api[_ ]key is not configured|no api key|missing api key|"
    r"authenticationerror|unauthorized|invalid[_ ]api[_ ]key|"
    r"permission denied|\b40[13]\b|"
    # secrets files and vaults the sandbox cannot have
    r"no secrets found|secretnotfound|secrets\.toml|"
    r"credentials not found|could not automatically determine credentials)",
    re.I)

# Reaching the internet and failing is a property of the sandbox, not of the
# documentation. A tutorial that downloads a CSV is not wrong because the
# download was reset.
NETWORK_ERROR = re.compile(
    r"(urlerror|connection reset by peer|connection aborted|"
    r"temporary failure in name resolution|name or service not known|"
    r"max retries exceeded|newconnectionerror|sslerror|"
    r"read timed out|connectiontimeout)", re.I)

# Transient, and about the network rather than the docs.
TRANSIENT_INSTALL = re.compile(
    r"(connection reset|connection aborted|timed? ?out|temporary failure|"
    r"read timed out|proxyerror|incompleteread|502|503|504)", re.I)

QUOTA_ERROR = re.compile(r"(cpu limit exceeded|quota|too many sandboxes)", re.I)

MAX_CONCURRENCY = 8          # account ceiling is 10 CPUs; 8 leaves headroom
INSTALL_ATTEMPTS = 3         # PyPI connection resets are common enough to matter
CREATE_ATTEMPTS = 3          # provider-side disconnects cost a whole version


def _cfg(cfg: Config):
    from daytona import DaytonaConfig
    dcfg = DaytonaConfig(api_key=cfg.daytona_api_key)
    if cfg.daytona_api_url:
        dcfg.api_url = cfg.daytona_api_url
    if cfg.daytona_target:
        dcfg.target = cfg.daytona_target
    return dcfg


async def _new_sandbox(daytona, cfg: Config):
    """Create a sandbox, retrying provider-side blips.

    `DaytonaConnectionError: Server disconnected` and friends are transient and
    cost a whole version when they are not retried - one blip turned 19 real
    findings into 125 blocked results. Quota errors are NOT retried: the
    account ceiling does not move, so retrying only burns time.
    """
    from daytona import CreateSandboxFromSnapshotParams
    params = CreateSandboxFromSnapshotParams(
        language="python",
        env_vars=dict(cfg.sandbox_env),   # prefix already stripped
        ephemeral=True,
    )
    last: Exception | None = None
    for attempt in range(1, CREATE_ATTEMPTS + 1):
        try:
            return await daytona.create(params, timeout=180)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if QUOTA_ERROR.search(msg):
                raise
            last = e
            if attempt < CREATE_ATTEMPTS:
                await asyncio.sleep(2 * attempt)
    raise last if last else RuntimeError("sandbox creation failed")


async def _install(sandbox, package: str, version: str, cfg: Config,
                   install_spec: str = "") -> tuple[bool, str]:
    """Install, retrying transient network failures with a short backoff.

    PyPI resets connections often enough to matter: it happened in spike S1 and
    again on the first run of the ported runner, taking out one whole version.
    Immediate retries hit the same reset, so back off between attempts.

    `install_spec` installs the repo itself - what a package with no published
    release needs, and the difference between checking its docs and reporting
    the whole scan unverified.
    """
    if install_spec and version.startswith("HEAD"):
        spec = install_spec
    else:
        spec = f"{package}=={version}" if not version.startswith("HEAD") else package
    last = ""
    for attempt in range(1, INSTALL_ATTEMPTS + 1):
        r = await sandbox.process.exec(
            f'pip install --quiet "{spec}"', timeout=cfg.install_timeout)
        if r.exit_code == 0:
            return True, ""
        last = (r.result or "")[-1500:]
        if not TRANSIENT_INSTALL.search(last):
            break          # a real resolution failure; retrying cannot help
        if attempt < INSTALL_ATTEMPTS:
            await asyncio.sleep(2 * attempt)
    return False, last


# ---------------------------------------------------------------------------
# structural lane
# ---------------------------------------------------------------------------
async def run_structural(daytona, snippets: list[Snippet], package: str,
                         import_name: str, version: str, cfg: Config,
                         sem: asyncio.Semaphore, log=print,
                         install_spec: str = "") -> list[Result]:
    """Every structural probe for one version, in one sandbox, in one exec.

    Semaphore-guarded like the execute lane: a structural sandbox costs exactly
    the same CPU against the account quota as any other.
    """
    if not snippets:
        return []

    secrets = cfg.secrets()
    targets = build_targets(snippets, import_name)
    sandbox = None
    try:
      async with sem:
        t0 = time.perf_counter()
        sandbox = await _new_sandbox(daytona, cfg)
        t_create = time.perf_counter() - t0

        t0 = time.perf_counter()
        ok, tail = await _install(sandbox, package, version, cfg, install_spec)
        t_install = time.perf_counter() - t0
        if not ok:
            log(f"    {version}: install failed")
            note = redact(tail, secrets)
            return [Result(id=s.id, page=s.page, version=version,
                           status="blocked", blocked_by="install",
                           stderr=note, tier=s.tier) for s in snippets]

        t0 = time.perf_counter()
        r = await sandbox.process.code_run(
            batched_script(targets, import_name), timeout=cfg.probe_timeout)
        t_exec = time.perf_counter() - t0
        log(f"    {version}: create {t_create:.1f}s · install {t_install:.1f}s "
            f"· probe {t_exec:.1f}s ({len(targets)} symbols)")

        payload = parse_batched(r.result or "")

        out: list[Result] = []
        for s in snippets:
            rec = payload.get(s.id)
            if rec is None:
                out.append(Result(id=s.id, page=s.page, version=version,
                                  status="blocked", blocked_by="probe",
                                  stderr="probe returned no record",
                                  tier=s.tier))
                continue
            body = redact(_render(rec), secrets)
            status: Status
            if rec["failed"]:
                status = ("unverified"
                          if CREDENTIAL_ERROR.search(body)
                          or NETWORK_ERROR.search(body) else "fail")
            else:
                status = "pass"
            # No blocking here, ever: a structural probe mutates no state, so
            # one failure says nothing about the next snippet on the page.
            out.append(Result(id=s.id, page=s.page, version=version,
                              status=status, stderr=body, tier=s.tier))
        return out

    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if QUOTA_ERROR.search(msg):
            log(f"    {version}: sandbox quota exceeded - lower --concurrency")
        else:
            log(f"    {version}: sandbox error - {type(e).__name__}")
        note = redact(msg[:800], secrets)
        return [Result(id=s.id, page=s.page, version=version, status="blocked",
                       blocked_by="sandbox", stderr=note, tier=s.tier)
                for s in snippets]
    finally:
        if sandbox is not None:
            try:
                await daytona.delete(sandbox)
            except Exception:
                pass


def _render(rec: dict) -> str:
    """Probe output as the report already expects to read it."""
    import json
    return json.dumps({"probe": rec.get("probe", {}),
                       "failed": rec.get("failed", []),
                       "notes": rec.get("notes", [])}, indent=1)


# ---------------------------------------------------------------------------
# execute lane
# ---------------------------------------------------------------------------
async def run_chain(daytona, chain: list[Snippet], package: str, version: str,
                    cfg: Config, sem: asyncio.Semaphore, log=print,
                    install_spec: str = "") -> list[Result]:
    """One sandbox per chain: these snippets establish state for each other."""
    secrets = cfg.secrets()
    async with sem:
        sandbox = None
        try:
            sandbox = await _new_sandbox(daytona, cfg)

            ok, tail = await _install(sandbox, package, version, cfg, install_spec)
            if not ok:
                log(f"    {version}: install failed")
                note = redact(tail, secrets)
                return [Result(id=s.id, page=s.page, version=version,
                               status="blocked", blocked_by="install",
                               stderr=note, tier=s.tier) for s in chain]

            results: list[Result] = []
            blocked_by: str | None = None
            for s in chain:
                if blocked_by:
                    results.append(Result(id=s.id, page=s.page, version=version,
                                          status="blocked",
                                          blocked_by=blocked_by, tier=s.tier))
                    continue

                if s.lang in ("bash", "sh", "shell", "console"):
                    r = await sandbox.process.exec(
                        s.code, timeout=cfg.snippet_timeout)
                else:
                    r = await sandbox.process.code_run(
                        s.code, timeout=cfg.snippet_timeout)

                body = redact((r.result or "")[-2000:], secrets)
                status: Status
                if r.exit_code == 0:
                    status = "pass"
                elif CREDENTIAL_ERROR.search(body) or NETWORK_ERROR.search(body):
                    status = "unverified"
                else:
                    status = "fail"

                results.append(Result(id=s.id, page=s.page, version=version,
                                      status=status, stderr=body, tier=s.tier))
                if status == "fail":
                    # only here: an execute snippet really does establish state
                    blocked_by = s.id
            return results

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if QUOTA_ERROR.search(msg):
                log(f"    {version}: sandbox quota exceeded - lower --concurrency")
            else:
                log(f"    {version}: sandbox error - {type(e).__name__}")
            note = redact(msg[:800], secrets)
            return [Result(id=s.id, page=s.page, version=version,
                           status="blocked", blocked_by="sandbox",
                           stderr=note, tier=s.tier) for s in chain]
        finally:
            if sandbox is not None:
                try:
                    await daytona.delete(sandbox)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
async def sweep(daytona, log=print) -> int:
    """Reclaim anything a crashed task left behind."""
    try:
        live = [sb async for sb in daytona.list()]
    except Exception:
        return 0
    reclaimed = 0
    for sb in live:
        try:
            await daytona.delete(sb)
            reclaimed += 1
        except Exception:
            pass
    if reclaimed:
        log(f"  swept {reclaimed} leaked sandbox(es)")
    return reclaimed


async def run_matrix(snippets: list[Snippet], package: str, import_name: str,
                     versions: list[str], cfg: Config, log=print,
                     install_spec: str = "") -> list[Result]:
    from daytona import AsyncDaytona

    from ..extract.tiers import chains

    structural = [s for s in snippets if s.tier == "structural"]
    executable = [s for s in snippets if s.tier == "execute"]
    skipped = [s for s in snippets if s.tier == "unverifiable"]

    if not cfg.can_execute:
        log("  no DAYTONA_API_KEY - skipping execution, marking unverified")
        return [Result(id=s.id, page=s.page, version=v, status="unverified",
                       tier=s.tier)
                for s in snippets for v in versions]

    exec_chains = chains(executable) if executable else []
    concurrency = max(1, min(cfg.concurrency, MAX_CONCURRENCY))
    lanes = len(versions) if structural else 0       # run_structural skips an empty lane
    sandboxes = lanes + len(exec_chains) * len(versions)
    log(f"  {sandboxes} sandboxes: {lanes} structural "
        f"({len(structural)} probes batched per version)"
        + (f" + {len(exec_chains)} execute chains x {len(versions)}"
           if exec_chains else "")
        + f", concurrency {concurrency}")

    out: list[Result] = []
    # One client for the whole run. Never one per unit of work.
    async with AsyncDaytona(_cfg(cfg)) as daytona:
        sem = asyncio.Semaphore(concurrency)

        tasks = [run_structural(daytona, structural, package, import_name,
                                v, cfg, sem, log, install_spec) for v in versions]
        tasks += [run_chain(daytona, c, package, v, cfg, sem, log, install_spec)
                  for v in versions for c in exec_chains]

        got = await asyncio.gather(*tasks, return_exceptions=True)
        for batch in got:
            if isinstance(batch, BaseException):   # CancelledError is not an Exception
                log(f"    task failed - {type(batch).__name__}")
                continue
            out.extend(batch)

        await sweep(daytona, log)

    out.extend(Result(id=s.id, page=s.page, version=v, status="unverified",
                      tier=s.tier)
               for s in skipped for v in versions)
    return out
