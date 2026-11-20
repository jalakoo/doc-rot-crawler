"""CLI lane: check a standalone command-line tool's shell snippets against each release.

For binaries that are not an npm / PyPI / NuGet package (`relate`, `gh`, `rg`):
the docs show `relate query --database ./team.rl`, and the only authority on
whether `query` and `--database` exist is the binary. Same contract as the npm
lane's CLI half - ask `<tool> <words> --help`, judge with the pure rules in
`clihelp` - with the parts a registry package gets for free built here:

  acquire   ONE sandbox per version. The tool's GitHub release asset for the
            sandbox's OS and CPU is downloaded and put on PATH (`acquire.binaries`
            picks it); an explicit install command from the caller is the
            fallback. Nothing is derived from docs text and run.
  pin       A recipe that "installs version X" proves nothing until the binary
            says so: an install script that ignores the pin installs latest, and
            every version of the matrix would then agree with itself. After
            install the binary's `--version` must name the requested release,
            else that version is `blocked` with the reason, never a result.
  ask       Every distinct `--help` (a prefix of each documented command) is run
            ONCE, in one Python process in the sandbox, in parallel. A native
            binary starts in tens of milliseconds; the same ~20 distinct commands
            cost ~28s through npm's Node CLIs.
  hidden    A flag that is not in the help is asked about directly:
            `<cmd> --flag --help`. clap and cobra reject an unknown flag with a
            recognisable error before they print help; a hand-written parser
            (Relate's) prints help regardless, so the binary's own bytes decide:
            a flag that appears nowhere in the executable is not accepted by it.
            A flag that is in the bytes but not in the help is not judged.

Documented commands are never run - only `--help` (and `-h`, `help <words>` when
`--help` gave nothing a human would call help). Findings use the npm lane's
shape (check kinds `bin` / `command` / `flag`) under the key `"cli"`, so the
report renders both alike.

Traps measured on Relate 1.1.0:

  * Each `sandbox.process.exec` is a fresh shell (zsh): a PATH set in one call is
    gone in the next, so every call that needs the binary exports it itself.
  * `relate bogus --help` prints the ROOT help and `relate extension bogus
    --help` prints the extension help: a swallowed word. Relate's usage line is
    `relate [OPTIONS] [--database PATH] [QUERY]`, so a bare word there is a
    legal query; only a word one typo from a command (`querry`) is called wrong
    from the table. Under `extension`, the usage lines spell the actions out
    (`init|install|list|remove|check`) and nothing else fits there.
  * `relate query --bogus --help` prints query's help with exit 0: help wins over
    a bad flag. Asking is not enough there; the binary is searched.
  * `relate run <name> [flags]`: the flags belong to an installed extension, so a
    flag missing from `run`'s help is not judged.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field

from ..acquire import binaries
from ..acquire.binaries import CliRelease, Recipe
from ..acquire.registry import _key
from ..config import Config, redact
from ..extract import javascript as js
from ..models import Result, Snippet, Status
from . import runner
from .clihelp import (
    DETAIL_CAP,
    HELP_CAP,
    MAX_DEPTH,
    CliCheck,
    chain_args,
    help_key,
    judge_cli,
    open_flags,
    unknown_flag_line,
    usable,
)
from .runner import MAX_CONCURRENCY, QUOTA_ERROR, TRANSIENT_INSTALL

ROOT = "/home/daytona/docrot-cli"
BIN_DIR = "$HOME/docrot-bin"
MARKER = "---DOCROT-CLI---"
HELP_MS = 20_000            # per request, so one hanging CLI cannot eat the lane
MAX_FINDINGS = 12
WORKERS = 8

# A download that died on the network says nothing about the release.
TRANSIENT_CLI = re.compile(
    r"(curl: \((?:6|7|18|28|35|52|55|56)\)|could not resolve host|connection (?:reset|timed out)|"
    r"timed? ?out|temporary failure|\b429\b|\b50[234]\b)", re.I)
# The binary was fetched but cannot start here (wrong libc, wrong CPU).
NOT_RUNNABLE = re.compile(r"(exec format error|cannot execute|GLIBC_[\d.]+|no such file or directory|"
                          r"not found|error while loading shared libraries|illegal instruction)", re.I)

# ---------------------------------------------------------------------- probe

# Runs in the sandbox with the stdlib only. Everything it needs is in
# payload.json; everything it learned goes back as one JSON document between
# markers on stdout, whatever the CLIs print.
PROBE_PY = r'''
import json, os, re, shutil, signal, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

MARK = '---DOCROT-CLI---'
here = os.path.dirname(os.path.abspath(__file__))
payload = json.load(open(os.path.join(here, sys.argv[1] if len(sys.argv) > 1 else 'payload.json')))
t0 = time.time()
deadline = t0 + payload.get('deadlineMs', 120000) / 1000.0
out = {'runs': {}, 'strings': {}, 'notes': [], 'ms': 0}

home = os.path.join(here, 'home')
os.makedirs(home, exist_ok=True)
env = dict(os.environ)
env.update(CI='1', NO_COLOR='1', FORCE_COLOR='0', TERM='dumb', DO_NOT_TRACK='1', NO_UPDATE_NOTIFIER='1',
           HOME=home, XDG_CONFIG_HOME=home + '/.config', XDG_DATA_HOME=home + '/.local/share',
           XDG_CACHE_HOME=home + '/.cache')
if payload.get('binDir'):
    env['PATH'] = payload['binDir'] + os.pathsep + env.get('PATH', '')

def run(argv):
    if time.time() > deadline:
        return {'skipped': True}
    cap = payload.get('cap', 60000)
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd=home, env=env, start_new_session=True)
    except OSError as e:
        return {'error': str(e.strerror or e)[:200]}
    timed = False
    try:
        so, se = p.communicate(timeout=payload.get('helpMs', 20000) / 1000.0)
    except subprocess.TimeoutExpired:
        timed = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        so, se = p.communicate()
    a = so.decode('utf-8', 'replace'); b = se.decode('utf-8', 'replace')
    text = a + ('\n' if a and b else '') + b
    return {'code': None if timed else p.returncode, 'timedOut': timed, 'out': text[:cap]}

def go(req):
    out['runs'][req['key']] = run(req['argv'])

try:
    reqs = payload.get('requests') or []
    with ThreadPoolExecutor(max_workers=payload.get('workers', 8)) as pool:
        list(pool.map(go, reqs))
    sf = payload.get('strings') or {}
    if sf.get('flags'):
        path = shutil.which(sf['bin'], path=env['PATH'])
        if path and os.path.getsize(path) <= 600 * 1024 * 1024:
            data = open(path, 'rb').read()
            for f in sf['flags']:
                pat = re.compile(rb'(?<![\w-])' + re.escape(f.encode()) + rb'(?![\w-])')
                out['strings'][f] = bool(pat.search(data))
        else:
            out['notes'].append('the binary could not be read for a flag search')
except Exception as e:
    out['notes'].append('probe failed: ' + str(e)[:200])
out['ms'] = int((time.time() - t0) * 1000)
sys.stdout.write('\n' + MARK + '\n' + json.dumps(out) + '\n' + MARK + '\n')
sys.stdout.flush()
'''


def parse_probe(stdout: str) -> dict | None:
    """The probe document out of stdout; the last fenced block wins."""
    blocks = re.findall(re.escape(MARKER) + r"\n(.*?)\n" + re.escape(MARKER), stdout or "", re.S)
    for raw in reversed(blocks):
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            return doc
    return None


# --------------------------------------------------------------------- plans

@dataclass
class Plan:
    snippet: Snippet
    clis: list[CliCheck] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def plan_snippet(s: Snippet, name: str, bins: tuple[str, ...] = ()) -> Plan:
    """The invocations of this tool in a shell snippet. Anything installed
    under `bins` is the same binary, so it is asked as `name`. `npx name ...`
    is the npm package of that name, not this binary."""
    plan = Plan(s)
    if s.lang.strip().lower() not in js.SHELL_LANGS:
        return plan
    # `export RELATE_AGENT_MODE=1` on an earlier line is the same thing as a prefix
    exported = re.search(rf"(?im)^\s*(?:export\s+)?{re.escape(name.upper().replace('-', '_'))}_\w*=", s.code)
    for inv in js.invocations(s.code, {name, *bins}):
        if inv.package:
            continue
        if exported:
            plan.clis.append(CliCheck(inv, "", f"the snippet sets {name.upper()}_* variables, which can "
                                            "change the commands and flags the CLI offers; not judged"))
        else:
            plan.clis.append(CliCheck(inv, name))
    return plan


def _argv(name: str, words: list[str], suffix: str = "--help") -> list[str]:
    return [name, *words, suffix] if suffix else [name, *words]


def help_requests(plans: list[Plan], name: str) -> list[dict]:
    """Every distinct `--help` the plans need: each prefix of each documented
    command, once. {key, argv}."""
    seen: dict[str, dict] = {}
    for p in plans:
        for c in p.clis:
            if not c.bin:
                continue
            words = chain_args(c.inv)
            for i in range(len(words) + 1):
                key = help_key(name, words[:i])
                seen.setdefault(key, {"key": key, "argv": _argv(name, words[:i])})
    return list(seen.values())


def fallback_requests(plans: list[Plan], name: str, helps: dict) -> list[dict]:
    """`-h` and `help <words>` for the first step of each chain whose `--help`
    gave nothing usable. Later steps are not asked: they hang off it."""
    out: dict[str, dict] = {}
    for p in plans:
        for c in p.clis:
            if not c.bin:
                continue
            words = chain_args(c.inv)
            for i in range(len(words) + 1):
                key = help_key(name, words[:i])
                rec = helps.get(key)
                if rec is None or usable(rec):
                    continue
                out.setdefault(key + "\x1f-h", {"key": key + "\x1f-h", "argv": _argv(name, words[:i], "-h")})
                if i:
                    out.setdefault(key + "\x1fhelp", {"key": key + "\x1fhelp", "argv": [name, "help", *words[:i]]})
                break
    return list(out.values())


def merge_fallbacks(helps: dict, runs: dict) -> dict:
    """Help records with each unusable one replaced by a usable `-h` or
    `help <words>` answer, when there is one. The record says how it was got."""
    merged = dict(helps)
    for fkey, rec in runs.items():
        base, _, how = fkey.rpartition("\x1f")
        if how in ("-h", "help") and not usable(merged.get(base)) and usable(rec):
            merged[base] = {**rec, "via": how}
    return merged


def ask_key(cmd: str, flag: str) -> str:
    return f"ask\x1f{cmd}\x1f{flag}"


def flag_asks(plans: list[Plan], helps: dict, name: str) -> list[tuple[str, str]]:
    """(command, flag) pairs the help does not settle - the ones to ask the CLI
    about. Found by judging with a recorder, so the two cannot disagree."""
    wanted: list[tuple[str, str]] = []

    def record(cmd: str, flag: str) -> None:
        if (cmd, flag) not in wanted:
            wanted.append((cmd, flag))
        return None

    for p in plans:
        for c in p.clis:
            judge_cli(c, helps, name, {}, flag_says=lambda cmd, flag: record(cmd, flag), shown=p.snippet.shows)
    return wanted


def ask_requests(asks: list[tuple[str, str]]) -> list[dict]:
    return [{"key": ask_key(cmd, flag), "argv": [*cmd.split(), flag, "--help"]} for cmd, flag in asks]


def _same(a: str, b: str) -> bool:
    def norm(t: str) -> str:
        return "\n".join(ln.rstrip() for ln in str(t).strip().splitlines())
    return norm(a) == norm(b)


def make_flag_says(doc: dict, name: str):
    """The evidence rule for a flag the help does not list; see the module
    docstring. Returns the callable `judge_cli` takes."""
    helps = doc.get("helps") or {}
    asks = doc.get("asks") or {}
    strings = doc.get("strings") or {}

    def says(cmd: str, flag: str) -> dict | None:
        words = cmd.split()[1:]
        own = helps.get(help_key(name, words)) or {}
        if open_flags(str(own.get("out") or "")):
            return {"state": "unknown",
                    "why": "this command takes flags of its own making (an extension defines them)"}
        rec = asks.get(ask_key(cmd, flag))
        if not usable(rec) and not (rec and rec.get("out")):
            return {"state": "unknown", "why": "the CLI could not be asked (no answer in time)"}
        assert rec is not None
        out = str(rec.get("out") or "")
        line = unknown_flag_line(out, flag)
        if line:
            return {"state": "rejected", "line": line}
        if _same(out, str(own.get("out") or "")):
            seen = strings.get(flag)
            if seen is False:
                return {"state": "absent"}
            return {"state": "unknown",
                    "why": ("`--help` printed without complaint and the binary contains the flag"
                            if seen else "the CLI answers a bad flag with plain help and the binary "
                                         "could not be searched")}
        if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", out):
            return {"state": "accepted", "why": "the CLI named it in its own reply"}
        return {"state": "unknown", "why": "the CLI answered with something else"}

    return says


# ------------------------------------------------------------------- version

_VERSION = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+)+)(?:[-+][0-9A-Za-z.-]+)?(?![\w.]*\w)")


def versions_in(text: str) -> list[str]:
    return [m.group(1) for m in _VERSION.finditer(text or "")]


def check_version(wanted: str, output: str) -> tuple[str, str]:
    """(state, note) for what `--version` printed against the release asked for.

    ok        the wanted version is among those printed
    mismatch  versions of the same shape are printed and none is the one asked
              for: the recipe installed something else (an install script that
              ignores the pin installs latest)
    unknown   it prints no version of that shape (a license's `2.0` is not one),
              so the pin cannot be confirmed
    """
    core = re.match(r"\d+(?:\.\d+)*", wanted.strip().lstrip("v"))
    want = core.group(0) if core else wanted.strip()          # `1.0.0-rc.1` is asked about as 1.0.0
    shape = min(len(want.split(".")), 3)
    found = [v for v in versions_in(output) if len(v.split(".")) >= shape]
    if any(v == want or _key(v) == _key(want) for v in found):
        return "ok", ""
    if found:
        return "mismatch", (f"the installed binary reports {found[0]}, not the requested {want}: "
                            "the recipe did not install the pinned version, so this version cannot be checked")
    return "unknown", f"the binary prints no version, so the pin to {want} could not be confirmed"


def with_aliases(snippet_bins: tuple[str, ...], name: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((name, *snippet_bins)))


# ------------------------------------------------------------------- judging

def _render(findings: list[dict], notes: list[str], probe: dict) -> str:
    keep = findings[:MAX_FINDINGS]
    for f in keep:
        f["detail"] = str(f.get("detail") or "")[:DETAIL_CAP]
    return json.dumps({
        "probe": probe, "cli": keep,
        "failed": [f"{f['subject']}: {f['message']}" for f in keep],
        "notes": [n[:240] for n in notes[:8]]}, indent=1)


def judge(plan: Plan, doc: dict, name: str, probe: dict) -> tuple[Status, str]:
    """(status, Result.stderr) for one structural snippet on one version."""
    helps = doc.get("helps") or {}
    says = make_flag_says(doc, name)
    held, findings, notes = 0, [], list(plan.notes)
    for c in plan.clis:
        h, f, n = judge_cli(c, helps, name, {}, flag_says=says, shown=plan.snippet.shows)
        held += h
        findings += f
        notes += n
    notes += list(doc.get("notes") or [])
    status: Status
    if findings:
        status = "fail"
    elif held:
        status = "pass"
    else:
        status = "unverified"
        if not notes:
            notes = [f"nothing in this snippet could be checked against `{name}`"]
    return status, _render(findings, notes, probe)


def unverified_result(s: Snippet, version: str, probe: dict, why: str) -> Result:
    return Result(id=s.id, page=s.page, version=version, status="unverified", tier=s.tier,
                  stderr=_render([], [why], probe))


# ------------------------------------------------------------------- sandbox

@dataclass
class Prepared:
    ok: bool
    blocked_by: str = ""
    note: str = ""
    recipe: str = ""
    installed: str = ""         # what `--version` printed
    bin_dir: str = ""
    machine: str = ""
    notes: list[str] = field(default_factory=list)


async def _upload(sandbox, files: dict[str, str]) -> None:
    from daytona import FileUpload
    await sandbox.fs.upload_files([
        FileUpload(source=text.encode(), destination=f"{ROOT}/{path}")
        for path, text in files.items()])


async def _install_one(sandbox, k: int, recipe: Recipe, name: str, cfg: Config, bin_dir: str) -> tuple[bool, str]:
    """Run one recipe, retrying transient network failures. (ok, tail of output)"""
    script = f"install-{k}.sh"
    await _upload(sandbox, {script: binaries.install_script(recipe, name, bin_dir)})
    tail = ""
    for attempt in range(1, runner.INSTALL_ATTEMPTS + 1):
        r = await sandbox.process.exec(f"bash {script}", cwd=ROOT, timeout=cfg.install_timeout)
        if r.exit_code == 0:
            return True, ""
        tail = (r.result or "")[-1500:]
        if not (TRANSIENT_INSTALL.search(tail) or TRANSIENT_CLI.search(tail)):
            break
        if attempt < runner.INSTALL_ATTEMPTS:
            await asyncio.sleep(2 * attempt)
    return False, tail


async def _version_output(sandbox, name: str, bin_dir: str) -> tuple[int, str]:
    """What the binary prints for `--version`; `-V` when that
    prints no version at all."""
    code, text = 0, ""
    for flag in ("--version", "-V"):
        r = await sandbox.process.exec(f'export PATH="{bin_dir}:$PATH"; {name} {flag} 2>&1 </dev/null',
                                       cwd=ROOT, timeout=30)
        code, text = r.exit_code, (r.result or "")
        if flag == "--version" and NOT_RUNNABLE.search(text) and code in (126, 127):
            return code, text
        if versions_in(text):
            break
    return code, text


async def _prepare(sandbox, name: str, version: str, rel: CliRelease | None, names: tuple[str, ...],
                   install: str, cfg: Config) -> Prepared:
    """Install `name` at `version`: recipes in order, and the first binary that
    runs decides - if it is not the version asked for the answer is `blocked`."""
    await sandbox.process.exec(f"mkdir -p {ROOT}", timeout=30)
    r = await sandbox.process.exec("uname -sm; echo $HOME", cwd=ROOT, timeout=30)
    lines = (r.result or "").strip().splitlines()
    sysinfo = lines[0] if lines else ""
    home = lines[1].strip() if len(lines) > 1 else "/home/daytona"
    bin_dir = f"{home}/docrot-bin"
    os_name, _, machine = sysinfo.lower().partition(" ")
    arch = binaries.want_arch(machine)
    recipes = binaries.recipes_for(rel, version, names, os_name or "linux", arch, install)
    if not recipes:
        why = (f"the release has no asset for {os_name}/{arch}" if rel and rel.assets else
               "the release has no assets" if rel else "no such release was found")
        return Prepared(False, "install", f"{why}, and no install command was supplied", machine=machine)
    tails: list[str] = []
    for k, recipe in enumerate(recipes):
        ok, tail = await _install_one(sandbox, k, recipe, name, cfg, bin_dir)
        if not ok:
            tails.append(f"{recipe.description}: {tail.strip()[-400:]}")
            continue
        code, text = await _version_output(sandbox, name, bin_dir)
        if code in (126, 127) or (NOT_RUNNABLE.search(text) and not versions_in(text)):
            tails.append(f"{recipe.description}: the binary does not run here: {text.strip()[:300]}")
            continue
        state, note = check_version(version, text)
        if state == "mismatch":
            return Prepared(False, "install", f"{recipe.description}: {note}", recipe.description,
                            text.strip()[:200], bin_dir, machine)
        notes = [note] if state == "unknown" else []
        return Prepared(True, recipe=recipe.description, installed=text.strip().splitlines()[0][:120]
                        if text.strip() else "", bin_dir=bin_dir, machine=machine, notes=notes)
    return Prepared(False, "install", "\n".join(tails)[-1500:], machine=machine)


async def _probe(sandbox, requests: list[dict], strings: dict | None, bin_dir: str,
                 cfg: Config, budget_ms: int) -> tuple[dict | None, str]:
    payload = {"requests": requests, "binDir": bin_dir, "helpMs": HELP_MS, "workers": WORKERS,
               "cap": HELP_CAP, "deadlineMs": budget_ms}
    if strings:
        payload["strings"] = strings
    await _upload(sandbox, {"probe.py": PROBE_PY, "payload.json": json.dumps(payload)})
    r = await sandbox.process.exec("python3 probe.py payload.json", cwd=ROOT, timeout=cfg.probe_timeout)
    return parse_probe(r.result or ""), (r.result or "")[-800:]


@dataclass
class Gathered:
    doc: dict | None
    tail: str = ""
    distinct: int = 0
    asked: int = 0
    probe_s: float = 0.0
    fell_back: int = 0


async def gather(sandbox, plans: list[Plan], name: str, bin_dir: str, cfg: Config) -> Gathered:
    """The probe, in up to three rounds against one sandbox: every `--help`; then
    `-h` / `help <words>` where `--help` gave nothing usable; then the
    flags the help does not settle (plus the binary search). The rounds are
    separate because each depends on judging the one before."""
    t0 = time.perf_counter()
    budget = max(30_000, int(cfg.probe_timeout * 0.8 * 1000))
    flags = sorted({f for p in plans for c in p.clis for f in c.inv.flags if f not in ("--help", "--version")})
    reqs = help_requests(plans, name)
    doc, tail = await _probe(sandbox, reqs, {"bin": name, "flags": flags} if flags else None, bin_dir, cfg, budget)
    g = Gathered(doc, tail, distinct=len(reqs))
    if doc is None:
        return g
    helps = dict(doc.get("runs") or {})
    tried: set[str] = set()
    for _ in range(MAX_DEPTH + 1):      # one round per level of a chain: each hangs off the one above
        fb = [r for r in fallback_requests(plans, name, helps) if r["key"] not in tried]
        if not fb:
            break
        tried.update(r["key"] for r in fb)
        more, _tail = await _probe(sandbox, fb, None, bin_dir, cfg, budget)
        if not more:
            break
        helps = merge_fallbacks(helps, more.get("runs") or {})
        g.fell_back += len(fb)
    asks = flag_asks(plans, helps, name)
    ask_runs: dict = {}
    if asks:
        more, _ = await _probe(sandbox, ask_requests(asks), None, bin_dir, cfg, budget)
        ask_runs = (more or {}).get("runs") or {}
        g.asked = len(asks)
    doc = {"helps": helps, "asks": ask_runs, "strings": doc.get("strings") or {},
           "notes": list(doc.get("notes") or [])}
    g.doc = doc
    g.probe_s = time.perf_counter() - t0
    return g


def _blocked(snippets: list[Snippet], version: str, by: str, note: str) -> list[Result]:
    return [Result(id=s.id, page=s.page, version=version, status="blocked", blocked_by=by,
                   stderr=note, tier=s.tier) for s in snippets]


async def _drop(daytona, sandbox) -> None:
    if sandbox is not None:
        try:
            await daytona.delete(sandbox)
        except Exception:
            pass


def _failed(e: Exception, snippets: list[Snippet], version: str, secrets: list[str], log) -> list[Result]:
    msg = f"{type(e).__name__}: {e}"
    if QUOTA_ERROR.search(msg):
        log(f"    {version}: sandbox quota exceeded - lower --concurrency")
    else:
        log(f"    {version}: sandbox error - {type(e).__name__}")
    return _blocked(snippets, version, "sandbox", redact(msg[:800], secrets))


def _results(plans: list[Plan], g: Gathered, name: str, version: str, probe: dict,
             secrets: list[str]) -> list[Result]:
    out: list[Result] = []
    for p in plans:
        s = p.snippet
        if g.doc is None:
            out.append(Result(id=s.id, page=s.page, version=version, status="blocked", blocked_by="probe",
                              stderr=redact(g.tail or "probe returned no document", secrets), tier=s.tier))
            continue
        status, body = judge(p, g.doc, name, probe)
        out.append(Result(id=s.id, page=s.page, version=version, status=status,
                          stderr=redact(body, secrets), tier=s.tier))
    return out


async def run_version(daytona, snippets: list[Snippet], name: str, version: str, rel: CliRelease | None,
                      names: tuple[str, ...], install: str, cfg: Config, sem: asyncio.Semaphore,
                      log=print) -> list[Result]:
    """Every structural snippet for one version: one sandbox, the binary
    installed once, every `--help` asked once."""
    if not snippets:
        return []
    secrets = cfg.secrets()
    sandbox = None
    try:
        async with sem:
            t0 = time.perf_counter()
            sandbox = await runner._new_sandbox(daytona, cfg)
            t_create = time.perf_counter() - t0

            t0 = time.perf_counter()
            prep = await _prepare(sandbox, name, version, rel, names, install, cfg)
            t_install = time.perf_counter() - t0
            if not prep.ok:
                log(f"    {version}: install blocked - {prep.note.splitlines()[0][:140] if prep.note else ''}")
                return _blocked(snippets, version, "install", redact(prep.note, secrets))

            plans = [plan_snippet(s, name, names) for s in snippets]
            g = await gather(sandbox, plans, name, prep.bin_dir, cfg)
            probe = {"lane": "cli", "tool": name, "version": version, "tag": rel.tag if rel else "",
                     "installed": prep.installed, "recipe": prep.recipe, "machine": prep.machine}
            for n in prep.notes:
                for p in plans:
                    p.notes.append(n)
            log(f"    {version}: create {t_create:.1f}s · install {t_install:.1f}s · probe {g.probe_s:.1f}s "
                f"({len(snippets)} snippets, {g.distinct} distinct --help, {g.asked} flag asks"
                f"{f', {g.fell_back} fallbacks' if g.fell_back else ''}; {prep.installed or 'no version'})")
            return _results(plans, g, name, version, probe, secrets)
    except Exception as e:
        return _failed(e, snippets, version, secrets, log)
    finally:
        await _drop(daytona, sandbox)


def _release_for(index: list[CliRelease], version: str) -> CliRelease | None:
    want = version.strip().lstrip("v")
    for r in index:
        if r.label == want or _key(r.label) == _key(want):
            return r
    return None


async def run_cli_matrix(snippets: list[Snippet], name: str, versions: list[str], cfg: Config, log=print,
                         *, repo_url: str = "", install: str = "", bins: tuple[str, ...] = ()) -> list[Result]:
    """The CLI counterpart of `run_npm_matrix`: one Result per (snippet, version).

    `name` is the command (`relate`); `bins` other names it answers to;
    `repo_url` the GitHub repo whose release assets are the binaries;
    `install` an optional install command, with `{version}` (and `{tag}`),
    run in the sandbox only when no release asset works. Only `structural`
    snippets are probed: a documented command is never executed.
    """
    from daytona import AsyncDaytona

    structural = [s for s in snippets if s.tier == "structural"]
    others = [s for s in snippets if s.tier != "structural"]

    if not cfg.can_execute:
        log("  no DAYTONA_API_KEY - skipping execution, marking unverified")
        return [Result(id=s.id, page=s.page, version=v, status="unverified", tier=s.tier)
                for s in snippets for v in versions]

    names = with_aliases(bins, name)
    index = (await asyncio.to_thread(binaries.lookup, repo_url, name)).releases if repo_url else []
    concurrency = max(1, min(cfg.concurrency, MAX_CONCURRENCY))
    lanes = len(versions) if structural else 0
    log(f"  {lanes} sandboxes: {lanes} cli ({len(structural)} snippets batched per version), "
        f"concurrency {concurrency}")

    out: list[Result] = []
    async with AsyncDaytona(runner._cfg(cfg)) as daytona:
        sem = asyncio.Semaphore(concurrency)
        tasks = [run_version(daytona, structural, name, v, _release_for(index, v), names, install, cfg, sem, log)
                 for v in versions]
        got = await asyncio.gather(*tasks, return_exceptions=True)
        for batch in got:
            if isinstance(batch, BaseException):
                log(f"    task failed - {type(batch).__name__}")
                continue
            out.extend(batch)
        await runner.sweep(daytona, log)

    skip = {"lane": "cli", "tool": name}
    out.extend(unverified_result(s, v, skip, "documented commands are only checked against --help, "
                                              "and this snippet is not a structural one")
               for s in others for v in versions)
    return out
