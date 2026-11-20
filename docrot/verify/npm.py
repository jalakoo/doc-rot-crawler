"""npm lane: check a package's JS/TS and shell snippets against each release.

Two lanes, like the Python one, for the same reason (structural probing shares
no state, execution does):

  structural  ONE sandbox per version, ONE Node process, everything batched.
              The probe loads every module the docs import and looks for every
              name they take from it; for shell snippets it asks the package's
              own CLI (`<bin> --help`) whether the command and flags the docs
              show exist. Nothing a snippet says is ever run.

  execute     one sandbox per chain per version: a pure `npm install` is run,
              a JS snippet is syntax-checked and then run.

Sandbox: Daytona's default snapshot (what the Python lane uses) already ships
Node 24 and npm 11 under nvm, measured 2026-11, so no image is built. Node >= 20
is required; `cfg.node_image` (when the integration step adds it) overrides.

What this lane is careful about - every rule below is a false "your docs are
wrong" that costs far more than a missed finding, so each one errs towards
`unverified` with a note:

  * Failing to LOAD a module says nothing about the docs unless the error is
    about the specifier itself. A package that needs a browser global, an env
    var or a native binary at import time throws something else entirely; a
    transitive dependency that is missing says nothing either. Only
    ERR_MODULE_NOT_FOUND / ERR_PACKAGE_PATH_NOT_EXPORTED whose message names
    the probe as the importer count (a transitive failure names a file inside
    node_modules). And `import()` is stricter than `require()` (extensionless
    subpaths, `exports` maps with only a "require" condition) while CJS-era
    docs say `require('pkg/sub')`, so a specifier is only missing when
    `require.resolve` fails too.
  * TypeScript types do not exist at runtime: `import { Config } from 'pkg'`
    with `Config` an interface is correct. A name missing at runtime is looked
    up in the package's shipped `.d.ts` files before it is called missing.
  * CJS interop: a bundler-era `import { x } from 'cjs-pkg'` finds `x` on
    `module.exports`, which ESM exposes as `default`. Accepted.
  * Help text is the CLI's own word. A word is a subcommand only if the CLI
    treats it as one; it is a missing subcommand only if the CLI says so
    (unknown-command errors), and a word whose help is byte-identical to its
    parent's was swallowed as a positional argument. Anything else -
    a timeout, a login wall, no output - is unverified.
  * Node's type stripping does not elide unused imports, so a TS snippet that
    imports an interface without `import type` dies at runtime with "does not
    provide an export named". That is the TS toolchain, not the docs.
  * Packages print on import and sometimes call `process.exit`. The probe
    document is fenced by markers, `process.exit` is turned into a throw while
    modules load, and the probe exits explicitly so a stray handle cannot hang
    the lane.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from dataclasses import dataclass, field

from ..config import Config, redact
from ..extract import javascript as js
from ..extract.javascript import Invocation, Use
from ..models import Result, Snippet, Status
from . import runner
from .runner import (
    CREDENTIAL_ERROR,
    INSTALL_ATTEMPTS,
    MAX_CONCURRENCY,
    NETWORK_ERROR,
    QUOTA_ERROR,
    TRANSIENT_INSTALL,
)

IMAGE = ""              # "" is Daytona's default snapshot, which has Node 24
ROOT = "/home/daytona/docrot-npm"
MARKER = "---DOCROT-NPM---"
MIN_NODE = 20

IMPORT_MS = 20_000      # per module, so one hanging import cannot eat the lane
HELP_MS = 20_000        # per `--help`
MAX_DEPTH = 4           # subcommand words walked per invocation
HELP_CAP = 60_000       # chars of help kept per command
MAX_FINDINGS = 12
DETAIL_CAP = 300

# Transient npm failures, on top of runner.TRANSIENT_INSTALL.
TRANSIENT_NPM = re.compile(
    r"(ECONNRESET|ETIMEDOUT|ESOCKETTIMEDOUT|EAI_AGAIN|socket hang up|"
    r"npm (?:ERR!|error) network|\b429\b|ECONNREFUSED|EPIPE)", re.I)

# A snippet reaching for the network or a secret has told us nothing about
# whether the docs are right.
NETWORK_RUN = re.compile(
    r"(ECONNREFUSED|ENOTFOUND|ECONNRESET|ETIMEDOUT|EAI_AGAIN|fetch failed|"
    r"socket hang up|getaddrinfo|UND_ERR_\w+|EACCES|EPERM)", re.I)
CREDENTIAL_RUN = re.compile(
    r"(\b(?:api[_ -]?key|access[_ -]?token|auth(?:entication)?|credentials?|token|secret)\b"
    r"[^\n]{0,50}\b(?:required|missing|not set|not found|invalid|undefined|must be)\b|"
    r"\bmissing (?:required )?(?:env|environment)|environment variable[^\n]{0,40}"
    r"(?:not set|missing|required)|\b40[13]\b|unauthori[sz]ed|forbidden)", re.I)

# ---------------------------------------------------------------------- probe

# Runs in the sandbox, as ESM, from the work dir, so resolution uses its
# node_modules. Everything it needs is in payload.json; everything it learned
# goes back as one JSON document between markers on stdout.
PROBE_JS = r"""
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const MARK = '---DOCROT-NPM---';
const self = fileURLToPath(import.meta.url);
const here = path.dirname(self);
const payload = JSON.parse(fs.readFileSync(path.resolve(here, process.argv[2] || 'payload.json'), 'utf8'));
const req = createRequire(self);
const deadline = Date.now() + (payload.deadlineMs || 120000);
const out = { node: process.version, modules: {}, uses: {}, helps: {}, sourceFlags: [], notes: [] };

// Packages print to stdout on import, so the document is fenced.
const realExit = process.exit.bind(process);
let emitted = false;
function emit() {
  if (emitted) return;
  emitted = true;
  const buf = Buffer.from('\n' + MARK + '\n' + JSON.stringify(out) + '\n' + MARK + '\n');
  let off = 0;
  while (off < buf.length) {
    try { off += fs.writeSync(1, buf, off); } catch (e) { if (e.code !== 'EAGAIN') break; }
  }
}
process.on('exit', emit);
const note = (m) => { if (out.notes.length < 10) out.notes.push(String(m).slice(0, 300)); };
process.on('uncaughtException', (e) => note('uncaught while probing: ' + String((e && e.message) || e)));
process.on('unhandledRejection', (e) => note('unhandled rejection while probing: ' + String((e && e.message) || e)));

function clean(msg) {
  let s = String(msg || '').split('\n')[0];
  s = s.split(pathToFileURL(self).href).join('the probe').split(self).join('the probe');
  s = s.split(here + '/').join('');
  s = s.replace(/\s+imported from (the probe|\S*probe\.mjs)/, '');
  return s.slice(0, 400);
}
const describe = (e) => (e && e.code ? '[' + e.code + '] ' : '') + clean(e && e.message);

function withTimeout(p, ms) {
  let t;
  const timer = new Promise((_, rej) => {
    t = setTimeout(() => rej(Object.assign(new Error('timed out after ' + ms + 'ms'), { code: 'DOCROT_TIMEOUT' })), ms);
  });
  return Promise.race([p, timer]).finally(() => clearTimeout(t));
}

// Only an error about OUR specifier is evidence: the ESM resolver names its
// importer, and a failure inside the package names a file in node_modules.
const RESOLUTION = new Set(['ERR_MODULE_NOT_FOUND', 'MODULE_NOT_FOUND', 'ERR_PACKAGE_PATH_NOT_EXPORTED']);
function ours(e) {
  if (!e || !RESOLUTION.has(e.code)) return false;
  const m = String(e.message);
  return m.includes(self) || m.includes(pathToFileURL(self).href);
}

function cjsNamespace(m) {
  const ns = {};
  if (m !== null && (typeof m === 'object' || typeof m === 'function')) {
    for (const k of Object.keys(m)) ns[k] = true;
  }
  ns.default = m;
  return ns;
}

async function loadOnce(spec) {
  if (spec.endsWith('.json')) {
    // import() wants an attribute for JSON; docs say require('pkg/package.json')
    try { req.resolve(spec); } catch (e) {
      return { state: e.code === 'ERR_PACKAGE_PATH_NOT_EXPORTED' ? 'not_exported' : 'missing', message: describe(e) };
    }
    try { return { state: 'ok', ns: cjsNamespace(req(spec)), via: 'require' }; }
    catch (e) { return { state: 'error', message: describe(e) }; }
  }
  let err;
  try {
    return { state: 'ok', ns: await withTimeout(import(spec), payload.importMs) };
  } catch (e) { err = e; }
  if (err && err.code === 'DOCROT_TIMEOUT') return { state: 'timeout', message: err.message };
  if (ours(err)) {
    let resolved = true;
    try { req.resolve(spec); } catch (e) { resolved = false; }
    if (resolved) {
      // import() is stricter than require(); docs written for CJS are not wrong.
      try { return { state: 'ok', ns: cjsNamespace(req(spec)), via: 'require' }; }
      catch (e) { return { state: 'error', message: describe(e) }; }
    }
    return { state: err.code === 'ERR_PACKAGE_PATH_NOT_EXPORTED' ? 'not_exported' : 'missing',
             message: describe(err) };
  }
  return { state: 'error', message: describe(err) };
}

const modules = new Map();
async function load(spec) {
  if (!modules.has(spec)) {
    // a package that calls process.exit() on import must not end the probe
    process.exit = (c) => { throw new Error('process.exit(' + c + ') called while importing'); };
    try { modules.set(spec, await loadOnce(spec)); }
    finally { process.exit = realExit; }
  }
  return modules.get(spec);
}

// ---- type declarations ---------------------------------------------------
let declCache = null;
function declarations() {
  if (declCache) return declCache;
  const pkg = payload.package;
  const roots = [path.join(here, 'node_modules', ...pkg.split('/'))];
  roots.push(path.join(here, 'node_modules', '@types', pkg.replace(/^@/, '').replace('/', '__')));
  let files = 0, bytes = 0, text = '';
  for (const root of roots) {
    const stack = [root];
    while (stack.length && files < payload.maxDecl && bytes < payload.maxDeclBytes) {
      const dir = stack.pop();
      let entries = [];
      try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { continue; }
      for (const d of entries) {
        const p = path.join(dir, d.name);
        if (d.isDirectory()) { if (d.name !== 'node_modules') stack.push(p); }
        else if (/\.d\.[mc]?ts$/.test(d.name) && files < payload.maxDecl && bytes < payload.maxDeclBytes) {
          try { const t = fs.readFileSync(p, 'utf8'); files++; bytes += t.length; text += '\n' + t; } catch (e) {}
        }
      }
    }
  }
  return (declCache = { files, text });
}
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
function declared(name) {
  const d = declarations();
  if (!d.files) return null;
  const n = esc(name);
  const edge = '(?<![\\w$])' + n + '(?![\\w$])';
  return new RegExp('\\b(?:interface|type|class|enum|function|const|let|var|namespace|declare\\s+\\w+)\\s+' + n + '(?![\\w$])').test(d.text)
      || new RegExp('export\\s*(?:type\\s*)?\\{[^}]*' + edge + '[^}]*\\}').test(d.text);
}

function checkUse(ns, u) {
  const name = u.name;
  if (name === '*') return { state: 'ok' };
  if (name === 'default') return { state: 'default' in ns ? 'ok' : 'missing' };
  const bare = (name.startsWith('default.') ? name.slice(8) : name).split('.')[0];
  if (!/^[A-Za-z_$][\w$]*$/.test(bare)) return { state: 'unknown', why: 'not an identifier' };
  const def = ns.default;
  const obj = def !== null && def !== undefined ? Object(def) : null;
  if (!u.typeOnly && ((bare in ns) || (obj && bare in obj))) return { state: 'ok' };
  if (u.typeOnly && ((bare in ns) || (obj && bare in obj))) return { state: 'ok' };
  const d = declared(bare);
  if (d === true) return { state: 'types' };
  if (d === false) return { state: 'missing', declarations: true };
  return u.typeOnly ? { state: 'unknown', why: 'type-only import and the package ships no declarations' }
                    : { state: 'missing', declarations: false };
}

// ---- the CLI's own help --------------------------------------------------
const helps = {};
function runHelp(key, bin, args) {
  if (helps[key]) return helps[key];
  if (Date.now() > deadline) return (helps[key] = { skipped: true });
  const r = spawnSync(path.join(here, 'node_modules', '.bin', bin), [...args, '--help'], {
    cwd: here, encoding: 'utf8', timeout: payload.helpMs, killSignal: 'SIGKILL', maxBuffer: 8 * 1024 * 1024,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, CI: '1', NO_COLOR: '1', FORCE_COLOR: '0', TERM: 'dumb', NO_UPDATE_NOTIFIER: '1',
           DO_NOT_TRACK: '1', npm_config_update_notifier: 'false' },
  });
  const timedOut = !!(r.error && r.error.code === 'ETIMEDOUT');
  const o = (r.stdout || '') + (r.stdout && r.stderr ? '\n' : '') + (r.stderr || '');
  return (helps[key] = { code: r.status, signal: r.signal || '', timedOut,
                         error: r.error && !timedOut ? String(r.error.code || r.error.message) : '',
                         out: o.slice(0, payload.helpCap) });
}

// ---- flags the package's own code knows about ------------------------------
// A CLI framework can hide an option from --help (commander's hideHelp) and
// still accept it, so "not in --help" alone does not mean "rejected". A flag
// whose literal text appears in the package's JS is told apart from one that
// appears nowhere.
function sourceFlags(flags) {
  const left = new Set(flags);
  const found = [];
  const root = path.join(here, 'node_modules', ...payload.package.split('/'));
  const stack = [root];
  let files = 0, bytes = 0;
  while (stack.length && left.size && files < payload.maxSrc && bytes < payload.maxSrcBytes) {
    const dir = stack.pop();
    let entries = [];
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { continue; }
    for (const d of entries) {
      const p = path.join(dir, d.name);
      if (d.isDirectory()) { if (d.name !== 'node_modules') stack.push(p); continue; }
      if (!/\.[mc]?js$/.test(d.name) || d.name.endsWith('.min.js') || files >= payload.maxSrc) continue;
      let t = '';
      try { t = fs.readFileSync(p, 'utf8'); } catch (e) { continue; }
      files++; bytes += t.length;
      for (const f of [...left]) {
        if (new RegExp('(?<![\\w-])' + esc(f) + '(?![\\w-])').test(t)) { left.delete(f); found.push(f); }
      }
    }
  }
  return found;
}

async function main() {
  for (const sn of payload.snippets) {
    const res = [];
    for (const u of sn.uses) {
      const m = await load(u.specifier);
      res.push(m.state === 'ok' ? checkUse(m.ns, u) : { state: 'module' });
    }
    out.uses[sn.id] = res;
  }
  for (const [spec, m] of modules) out.modules[spec] = { state: m.state, message: m.message || '', via: m.via || '' };

  for (const c of payload.chains) {
    for (const st of c.steps) {
      const r = runHelp(st.key, c.bin, st.args);
      if (r.skipped || r.timedOut || r.error || !r.out.trim()) break;
    }
  }
  out.helps = helps;
  // every flag, not only those absent from all help: `--prompt` is listed by
  // `ai select` and hidden on `run`, and the judge decides which help applies
  if ((payload.flags || []).length) out.sourceFlags = sourceFlags(payload.flags);
}

try { await main(); } catch (e) { note('probe failed: ' + describe(e)); }
emit();
realExit(0);
"""


def parse_probe(stdout: str) -> dict | None:
    """The probe document out of stdout, which a package may have littered.

    The last fenced block wins; a package that prints the marker itself is
    welcome to, it will simply not be last.
    """
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

def unscoped(package: str) -> str:
    return package.rsplit("/", 1)[-1]


def bins_of(pkg_json: dict, package: str) -> dict[str, str]:
    """name -> script path, from a package.json `bin` field (a string is one
    bin named after the unscoped package)."""
    raw = pkg_json.get("bin")
    if isinstance(raw, str):
        return {unscoped(str(pkg_json.get("name") or package)): raw}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {}


def node_major_minor(version: str) -> tuple[int, int]:
    m = re.match(r"v?(\d+)\.(\d+)", version or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def strips_types(version: str) -> bool:
    """Whether `node file.mts` runs TypeScript with no flag."""
    major, minor = node_major_minor(version)
    return major >= 24 or (major == 23 and minor >= 6) or (major == 22 and minor >= 18)


def help_key(bin_: str, args: list[str] | tuple[str, ...]) -> str:
    return "\x1f".join([bin_, *args])


def value_like(word: str) -> bool:
    """A word that is a path, file, URL or value rather than a subcommand."""
    return bool(re.search(r"[/.=:]", word)) or word.isdigit()


@dataclass
class CliCheck:
    inv: Invocation
    bin: str                    # the installed bin it resolved to; "" when none
    reason: str = ""            # why it is not checkable / why there is no bin


@dataclass
class Plan:
    snippet: Snippet
    uses: list[Use] = field(default_factory=list)
    clis: list[CliCheck] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def resolve_bin(inv: Invocation, package: str, bins: dict[str, str]) -> CliCheck | None:
    """Which of this package's bins an invocation runs, or None when it is not
    this package's business (some other tool that happens to share a line)."""
    names = {package, unscoped(package)}
    if inv.bin in bins:
        return CliCheck(inv, inv.bin)
    if inv.bin in names:
        # npm exec / npx run the only bin whatever it is called
        if len(bins) == 1:
            return CliCheck(inv, next(iter(bins)))
        return CliCheck(inv, "", "installs no bin of that name")
    if inv.package == package:
        return CliCheck(inv, "", f"`{inv.bin}` is not a bin of {package}; not judged")
    return None


def plan_snippet(s: Snippet, package: str, bins: dict[str, str]) -> Plan:
    plan = Plan(s)
    if js.is_js(s.lang):
        seen: set[tuple[str, str]] = set()
        for u in js.uses(s.code):
            if js.package_of(u.specifier) == package and (u.specifier, u.name) not in seen:
                seen.add((u.specifier, u.name))
                plan.uses.append(u)
    elif s.lang.strip().lower() in js.SHELL_LANGS:
        names = {package, *bins}
        for inv in js.invocations(s.code, names):
            c = resolve_bin(inv, package, bins)
            if c is not None:
                plan.clis.append(c)
    return plan


def chain_args(inv: Invocation) -> list[str]:
    """The subcommand words worth asking the CLI about."""
    out: list[str] = []
    for w in inv.args[:MAX_DEPTH]:
        if value_like(w):
            break
        out.append(w)
    return out


def build_payload(plans: list[Plan], package: str, cfg: Config) -> dict:
    """Everything the probe needs. Pure, so it is testable without a sandbox."""
    chains: dict[str, dict] = {}
    snippets = []
    for p in plans:
        if p.uses:
            snippets.append({"id": p.snippet.id, "uses": [
                {"specifier": u.specifier, "name": u.name, "line": u.line, "typeOnly": u.type_only}
                for u in p.uses]})
        for c in p.clis:
            if not c.bin:
                continue
            words = chain_args(c.inv)
            steps = [{"key": help_key(c.bin, words[:i]), "args": words[:i]}
                     for i in range(len(words) + 1)]
            chains.setdefault(help_key(c.bin, words), {"bin": c.bin, "steps": steps})
    budget = max(30, int(cfg.probe_timeout * 0.8))
    flags = sorted({f for p in plans for c in p.clis if c.bin for f in c.inv.flags})
    return {"package": package, "snippets": snippets, "chains": list(chains.values()),
            "flags": flags, "maxSrc": 600, "maxSrcBytes": 24_000_000,
            "importMs": IMPORT_MS, "helpMs": HELP_MS, "helpCap": HELP_CAP,
            "maxDecl": 400, "maxDeclBytes": 4_000_000, "deadlineMs": budget * 1000}


# ------------------------------------------------------------------- judging
# Everything below is pure: canned probe output in, verdict out.

UNKNOWN_COMMAND = re.compile(
    r"(unknown (?:sub)?command|unknown argument|unrecogni[sz]ed (?:sub)?command|invalid command|"
    r"no such command|command[ :]+['\"`]?[\w:. -]*?['\"`]? ?(?:was )?not found|command not found|"
    r"did you mean|not a (?:valid |known )?command|could not find command|"
    r"is not a [\w-]+ command)", re.I)
HELP_LIKE = re.compile(r"(\busage\b|\boptions?\b|\bcommands?\b|--help|(?:^|\s)-h[,\s])", re.I)
LOGIN_WALL = re.compile(
    r"(please (?:log ?in|sign ?in|authenticate)|not (?:logged|signed) in|log ?in required|"
    r"authentication required|api[_ -]?key|unauthori[sz]ed|run [`'\"]?\S+ (?:login|auth))", re.I)
QUOTED = re.compile(r"""['"`]([^'"`\s]+)['"`]""")
COMMAND_HEADING = re.compile(r"^\s*(?:commands?|available commands|subcommands)\s*:?\s*$", re.I)
ARGUMENT_HEADING = re.compile(r"^\s*(?:arguments?|args)\s*:?\s*$", re.I)
USAGE_LINE = re.compile(r"^\s*(?:usage:?\s+|\$\s+)(\S.*)$", re.I)
# what a usage line may say after the bin without the command taking positionals
GENERIC_USAGE = re.compile(r"^[\[<]?(?:options?|flags?|command|commands|subcommand)[\]>]?(?:\.\.\.)?$", re.I)
UNIVERSAL_FLAGS = frozenset({"--help", "--version"})


def usable(h: dict | None) -> bool:
    """Whether a captured `--help` is text the CLI meant as help."""
    if not h or h.get("skipped") or h.get("timedOut") or h.get("error"):
        return False
    out = str(h.get("out") or "")
    if not out.strip():
        return False
    code = h.get("code")
    if code not in (0, None) and not HELP_LIKE.search(out):
        return False
    wall = code not in (0, None) and LOGIN_WALL.search(out) and not re.search(r"\busage:", out, re.I)
    return not wall


def why_unusable(h: dict | None) -> str:
    if not h or h.get("skipped"):
        return "the probe ran out of time before asking"
    if h.get("timedOut"):
        return "timed out"
    if h.get("error"):
        return f"could not be run ({h['error']})"
    out = str(h.get("out") or "")
    if not out.strip():
        return "printed nothing"
    if LOGIN_WALL.search(out):
        return "wants a login or configuration"
    return f"exited {h.get('code')} without help text"


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in str(text).strip().splitlines())


def unknown_command_line(out: str, word: str) -> str:
    """The line in which the CLI says it does not know `word` as a command,
    or "" when it does not say so about this word.

    A message that quotes some other token ('foo' while we asked about 'bar')
    is not about our word and is not taken as evidence.
    """
    for line in str(out).splitlines():
        if not UNKNOWN_COMMAND.search(line):
            continue
        quoted = QUOTED.findall(line)
        if word.lower() in line.lower() or not quoted or all(q.lower() == word.lower() for q in quoted):
            return line.strip()
    return ""


def flag_present(flag: str, text: str) -> bool:
    """Whether `--flag` appears in help text; `--no-x` is satisfied by `--x`
    or `--[no-]x`, and `--x` by a documented `--no-x`."""
    def has(f: str) -> bool:
        return bool(re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", text))
    name = flag[2:]
    if has(flag):
        return True
    if name.startswith("no-"):
        base = name[3:]
        return has(f"--{base}") or "--[no-]" + base in text
    return has(f"--no-{name}") or "--[no-]" + name in text


def command_names(text: str, bin_: str) -> set[str]:
    """The command names a help text lists in its command table, aliases
    included (`run|test`). Empty when it has no recognisable table."""
    names: set[str] = set()
    in_table, indent = False, -1
    for line in str(text).splitlines():
        if COMMAND_HEADING.match(line):
            in_table, indent = True, -1
            continue
        if not in_table:
            continue
        if not line.strip():
            continue
        width = len(line) - len(line.lstrip())
        if width == 0:
            in_table = False             # the next section
            continue
        if indent < 0:
            indent = width
        if width != indent:
            continue                     # a wrapped description
        words = line.split()
        while words and words[0] == bin_:
            words = words[1:]            # yargs prints `tool build`
        if words:
            names.update(n for n in words[0].split("|") if n)
    return names


def takes_positionals(text: str) -> bool:
    """Whether a help text says the command takes free-form arguments, in
    which case an unlisted word may be a value, not a missing command."""
    lines = str(text).splitlines()
    if any(ARGUMENT_HEADING.match(ln) for ln in lines):
        return True
    for ln in lines:
        m = USAGE_LINE.match(ln)
        if m:
            # `Usage: momentic run|test [options] [tests...]`: after the command
            # path, any bracketed word that is not options/command is a positional
            return any(re.fullmatch(r"[\[<].*[\]>]|.*\.\.\.", w) and not GENERIC_USAGE.match(w)
                       for w in m.group(1).split()[1:])
    return False


@dataclass
class Walk:
    resolved: list[str] = field(default_factory=list)
    root_ok: bool = True              # whether the root `--help` was usable at all
    help: dict | None = None          # help of the deepest command that resolved
    reliable: bool = True             # whether `help` can be trusted for flag checks
    missing_word: str = ""
    missing_line: str = ""
    inferred: bool = False            # from the command table, not an error the CLI printed
    note: str = ""


def walk(bin_: str, words: list[str], helps: dict) -> Walk:
    """Left to right, ask the CLI about each word and let it answer."""
    root = helps.get(help_key(bin_, []))
    w = Walk(help=root)
    if not usable(root):
        w.reliable = w.root_ok = False
        w.note = f"`{bin_} --help` {why_unusable(root)}"
        return w
    parent = root
    for i, word in enumerate(words):
        h = helps.get(help_key(bin_, words[:i + 1]))
        if h is None or h.get("skipped") or h.get("timedOut") or h.get("error") \
                or not str(h.get("out") or "").strip():
            w.reliable = False
            w.note = f"`{bin_} {' '.join(words[:i + 1])} --help` {why_unusable(h)}"
            return w
        said = unknown_command_line(str(h.get("out")), word)
        if said:
            w.missing_word, w.missing_line = word, said
            return w
        if not usable(h):
            w.reliable = False
            w.note = f"`{bin_} {' '.join(words[:i + 1])} --help` {why_unusable(h)}"
            return w
        if _norm(str(h.get("out"))) == _norm(str(parent.get("out") if parent else "")):
            # The CLI swallowed the word (commander prints the parent's help for
            # `tool nonsense --help`). That is a positional - unless the parent's
            # own command table is complete and takes no free-form arguments, in
            # which case the CLI has told us, in its own words, what it knows.
            ptext = str(parent.get("out") if parent else "")
            listed = command_names(ptext, bin_)
            if len(listed) >= 2 and word.lower() != "help" and word not in listed \
                    and not takes_positionals(ptext):
                path = " ".join([bin_, *w.resolved])
                w.missing_word, w.inferred = word, True
                w.missing_line = (f"'{word}' is not among the commands `{path} --help` lists "
                                  f"({', '.join(sorted(listed))[:200]})")
            return w                  # swallowed as a positional argument
        w.resolved.append(word)
        w.help = parent = h
    return w


def _excerpt(text: str, limit: int = DETAIL_CAP) -> str:
    lines = [ln.rstrip() for ln in str(text).strip().splitlines() if ln.strip()]
    return "\n".join(lines)[:limit]


def judge_cli(c: CliCheck, helps: dict, package: str, bins: dict[str, str],
              in_source: frozenset[str] | set[str] = frozenset()) -> tuple[int, list[dict], list[str]]:
    """(checks that held, findings, notes) for one invocation. `in_source` are
    the flags the package's own code names: one of those missing from `--help`
    is hidden or undocumented, not wrong."""
    inv = c.inv
    if not c.bin:
        if c.reason.startswith("installs no bin"):
            have = ", ".join(sorted(bins)) or "none"
            return 0, [{"check": "bin", "subject": inv.bin, "line": inv.line,
                        "message": f"{package} installs no bin named '{inv.bin}' (bins: {have})",
                        "detail": inv.text[:DETAIL_CAP]}], []
        return 0, [], [f"line {inv.line}: {c.reason}"]

    w = walk(c.bin, chain_args(inv), helps)
    if not w.root_ok:
        return 0, [], [f"line {inv.line}: {w.note}; commands and flags not judged"]
    held = 1       # the bin exists and answers --help
    if w.missing_word:
        subject = " ".join([c.bin, *w.resolved, w.missing_word])
        return held, [{"check": "command", "subject": subject, "line": inv.line,
                       "message": (w.missing_line if w.inferred else
                                   f"the CLI does not know the command '{w.missing_word}': {w.missing_line}"),
                       "detail": _excerpt(w.help.get("out", "")) if w.help else ""}], []
    held += len(w.resolved)
    notes: list[str] = []
    if w.note:
        notes.append(f"line {inv.line}: {w.note}; flags not judged")
    findings: list[dict] = []
    root = helps.get(help_key(c.bin, []))
    texts = [str(h.get("out") or "") for h in (w.help, root) if h and usable(h)]
    cmd = " ".join([c.bin, *w.resolved])
    for flag in inv.flags:
        if flag in UNIVERSAL_FLAGS:
            continue
        if not w.reliable:
            continue
        if any(flag_present(flag, t) for t in texts):
            held += 1
        elif flag in in_source:
            notes.append(f"line {inv.line}: {flag} is not in `{cmd} --help` but the package's own "
                         "code accepts it (a hidden flag); not judged")
        else:
            findings.append({
                "check": "flag", "subject": f"{cmd} {flag}", "line": inv.line,
                "message": f"{flag} does not appear in `{cmd} --help`",
                "detail": _excerpt(w.help.get("out", "") if w.help else "")})
    return held, findings, notes


def judge_uses(plan: Plan, doc: dict) -> tuple[int, list[dict], list[str]]:
    """(checks that held, findings, notes) for a snippet's imports."""
    held, findings, notes = 0, [], []
    modules = doc.get("modules") or {}
    recs = (doc.get("uses") or {}).get(plan.snippet.id)
    if not plan.uses:
        return 0, [], []
    if recs is None:
        return 0, [], ["probe returned no record for the imports"]
    said: set[str] = set()
    for u, rec in zip(plan.uses, recs, strict=False):
        mod = modules.get(u.specifier) or {}
        state = mod.get("state", "error")
        if state in ("missing", "not_exported"):
            if u.specifier not in said:
                said.add(u.specifier)
                findings.append({
                    "check": "module", "subject": u.specifier, "line": u.line,
                    "message": mod.get("message") or f"cannot load '{u.specifier}'",
                    "detail": ""})
            continue
        if state != "ok":
            note = f"{u.specifier}: {mod.get('message') or state}; imports from it not judged"
            if note not in notes:
                notes.append(note)
            continue
        if u.specifier not in said:
            said.add(u.specifier)
            held += 1
        st = rec.get("state")
        if st in ("ok", "types"):
            if u.name != "*":
                held += 1
        elif st == "missing":
            where = ("neither at runtime nor in its type declarations" if rec.get("declarations")
                     else "at runtime")
            findings.append({
                "check": "export", "subject": f"{u.specifier}.{u.name}", "line": u.line,
                "message": f"'{u.name}' is not exported by '{u.specifier}' ({where})",
                "detail": ""})
        else:
            notes.append(f"{u.specifier}.{u.name}: {rec.get('why') or 'could not be checked'}")
    return held, findings, notes


def _render(findings: list[dict], notes: list[str], probe: dict) -> str:
    keep = findings[:MAX_FINDINGS]
    for f in keep:
        f["detail"] = str(f.get("detail") or "")[:DETAIL_CAP]
    return json.dumps({
        "probe": probe, "npm": keep,
        "failed": [f"{f['subject']}: {f['message']}" for f in keep],
        "notes": [n[:240] for n in notes[:8]]}, indent=1)


def judge(plan: Plan, doc: dict, package: str, bins: dict[str, str]) -> tuple[Status, str]:
    """(status, Result.stderr) for one structural snippet on one version."""
    helps = doc.get("helps") or {}
    held, findings, notes = judge_uses(plan, doc)
    for c in plan.clis:
        h, f, n = judge_cli(c, helps, package, bins, set(doc.get("sourceFlags") or []))
        held += h
        findings += f
        notes += n
    notes = plan.notes + notes + list(doc.get("notes") or [])
    probe = {"lane": "structural", "node": doc.get("node", ""), "bins": sorted(bins)}
    status: Status
    if findings:
        status = "fail"
    elif held:
        status = "pass"
    else:
        status = "unverified"
        if not notes:
            notes = ["nothing in this snippet could be checked against the package"]
    return status, _render(findings, notes, probe)


# --------------------------------------------------------------- execute lane

MODULE_MISSING = re.compile(r"Cannot find (?:package|module) '([^']+)'")
NOT_EXPORTED = re.compile(r"Package subpath '([^']+)' is not defined by \"exports\" in (\S+)"
                          r"|No \"exports\" main defined in (\S+)")
NO_EXPORT = re.compile(r"The requested module '([^']+)' does not provide an export named '([^']+)'")
REFERENCE = re.compile(r"ReferenceError: ([\w$]+) is not defined")
TS_UNSUPPORTED = re.compile(r"(ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX|ERR_INVALID_TYPESCRIPT_SYNTAX|"
                            r"ERR_UNSUPPORTED_NODE_MODULES_TYPE_STRIPPING)")
ERROR_LINE = re.compile(r"^(?:[\w$]*Error|Error \[[\w]+\])[^\n]*:\s.+$", re.M)
SNIPPET_LINE = re.compile(r"snippet-\d+\.[mc]?[jt]s:(\d+)")
KILLED = (124, 137)


def error_line(text: str) -> str:
    m = ERROR_LINE.search(text or "")
    if m:
        return m.group(0).strip()[:300]
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1][:300] if lines else "non-zero exit, no output captured"


def strip_prompts(code: str) -> str:
    return "\n".join(re.sub(r"^\s*[$>]\s+", "", ln) for ln in code.splitlines())


def classify_run(package: str, text: str, ts: bool = False) -> tuple[Status, dict | None, str]:
    """(status, finding, note) for a JS snippet that exited non-zero.

    A finding only when the error is about this package. Everything that is
    about the sandbox, the snippet being an excerpt, or some other package is
    `unverified`.
    """
    text = text or ""
    if ts and (TS_UNSUPPORTED.search(text) or "SyntaxError" in text or NO_EXPORT.search(text)):
        return "unverified", None, ("TypeScript the Node type stripper cannot run as written "
                                    "(it does not elide type-only imports); not judged")
    nx = NO_EXPORT.search(text)      # the ESM linker reports this as a SyntaxError
    if "SyntaxError" in text and not nx:
        return "unverified", None, "not a complete program (SyntaxError)"
    ref = REFERENCE.search(text)
    if ref:
        return "unverified", None, f"an excerpt that relies on context: {ref.group(1)} is not defined"
    mm = MODULE_MISSING.search(text)
    if mm:
        name = mm.group(1)
        mine = js.package_of(name) == package or f"node_modules/{package}/" in name
        if not mine:
            return "unverified", None, f"needs '{name}', which is not this package; not judged"
    ne = NOT_EXPORTED.search(text)
    if ne and f"node_modules/{package}/" not in (ne.group(2) or ne.group(3) or ""):
        return "unverified", None, "a subpath of another package is not exported; not judged"
    if nx and js.package_of(nx.group(1)) != package:
        return "unverified", None, f"'{nx.group(1)}' is not this package; not judged"
    if CREDENTIAL_ERROR.search(text) or CREDENTIAL_RUN.search(text):
        return "unverified", None, "needs credentials the sandbox does not have"
    if NETWORK_ERROR.search(text) or NETWORK_RUN.search(text):
        return "unverified", None, "reached for the network or the filesystem and was refused"
    m = SNIPPET_LINE.search(text)
    return "fail", {
        "check": "runtime", "subject": package, "line": int(m.group(1)) if m else 0,
        "message": error_line(text), "detail": _excerpt(text)}, ""


# ------------------------------------------------------------------- sandbox

@dataclass
class Prepared:
    ok: bool
    blocked_by: str = ""
    note: str = ""
    node: str = ""
    bins: dict[str, str] = field(default_factory=dict)


def install_spec(package: str, version: str) -> str:
    return package if version.startswith("HEAD") else f"{package}@{version}"


def transient(tail: str) -> bool:
    return bool(TRANSIENT_INSTALL.search(tail) or TRANSIENT_NPM.search(tail))


async def _new_sandbox(daytona, cfg: Config):
    """The default snapshot has Node 24; an image is only for an override."""
    image = getattr(cfg, "node_image", "") or IMAGE
    if not image:
        return await runner._new_sandbox(daytona, cfg)
    from daytona import CreateSandboxFromImageParams, Resources
    params = CreateSandboxFromImageParams(
        image=image, env_vars=dict(cfg.sandbox_env),
        resources=Resources(cpu=1, memory=2, disk=5), ephemeral=True)
    last: Exception | None = None
    for attempt in range(1, runner.CREATE_ATTEMPTS + 1):
        try:
            return await daytona.create(params, timeout=900, on_snapshot_create_logs=lambda s: None)
        except Exception as e:
            if QUOTA_ERROR.search(f"{type(e).__name__}: {e}"):
                raise
            last = e
            if attempt < runner.CREATE_ATTEMPTS:
                await asyncio.sleep(2 * attempt)
    raise last if last else RuntimeError("sandbox creation failed")


async def _upload(sandbox, files: dict[str, str]) -> None:
    from daytona import FileUpload
    await sandbox.fs.upload_files([
        FileUpload(source=text.encode(), destination=f"{ROOT}/{path}")
        for path, text in files.items()])


async def _prepare(sandbox, package: str, version: str, cfg: Config) -> Prepared:
    """A fresh work dir with `package` installed; what Node and which bins."""
    await sandbox.process.exec(f"mkdir -p {ROOT}", timeout=30)
    r = await sandbox.process.exec("npm init -y >/dev/null 2>&1; node --version", cwd=ROOT, timeout=60)
    node = (r.result or "").strip().splitlines()[-1] if (r.result or "").strip() else ""
    if node_major_minor(node)[0] < MIN_NODE:
        return Prepared(False, "sandbox", f"Node {node or 'not found'} in the sandbox; need >= {MIN_NODE}")

    cmd = ("npm install --no-audit --no-fund --loglevel=error --save-exact "
           + shlex.quote(install_spec(package, version)))
    tail = ""
    for attempt in range(1, INSTALL_ATTEMPTS + 1):
        r = await sandbox.process.exec(cmd, cwd=ROOT, timeout=cfg.install_timeout)
        if r.exit_code == 0:
            tail = ""
            break
        tail = (r.result or "")[-1500:]
        if not transient(tail):
            break              # ETARGET / E404: the version is not on the registry
        if attempt < INSTALL_ATTEMPTS:
            await asyncio.sleep(2 * attempt)
    if tail:
        return Prepared(False, "install", tail, node)

    r = await sandbox.process.exec(
        f"cat node_modules/{shlex.quote(package)}/package.json", cwd=ROOT, timeout=30)
    try:
        bins = bins_of(json.loads(r.result or "{}"), package)
    except json.JSONDecodeError:
        bins = {}
    return Prepared(True, node=node, bins=bins)


async def _probe(sandbox, plans: list[Plan], package: str, cfg: Config) -> tuple[dict | None, str]:
    payload = build_payload(plans, package, cfg)
    await _upload(sandbox, {"probe.mjs": PROBE_JS, "payload.json": json.dumps(payload)})
    r = await sandbox.process.exec("node probe.mjs payload.json", cwd=ROOT, timeout=cfg.probe_timeout)
    return parse_probe(r.result or ""), (r.result or "")[-800:]


def _blocked(snippets: list[Snippet], version: str, by: str, note: str) -> list[Result]:
    return [Result(id=s.id, page=s.page, version=version, status="blocked", blocked_by=by,
                   stderr=note, tier=s.tier) for s in snippets]


async def _drop(daytona, sandbox) -> None:
    if sandbox is not None:
        try:
            await daytona.delete(sandbox)
        except Exception:
            pass


def _structural_results(plans: list[Plan], doc: dict | None, package: str, bins: dict[str, str],
                        version: str, secrets: list[str], tail: str) -> list[Result]:
    out: list[Result] = []
    for p in plans:
        s = p.snippet
        if doc is None:
            out.append(Result(id=s.id, page=s.page, version=version, status="blocked",
                              blocked_by="probe", stderr=redact(tail or "probe returned no document", secrets),
                              tier=s.tier))
            continue
        status, body = judge(p, doc, package, bins)
        out.append(Result(id=s.id, page=s.page, version=version, status=status,
                          stderr=redact(body, secrets), tier=s.tier))
    return out


async def run_structural(daytona, snippets: list[Snippet], package: str, version: str,
                         cfg: Config, sem: asyncio.Semaphore, log=print) -> list[Result]:
    """Every structural snippet for one version: one sandbox, one probe."""
    if not snippets:
        return []
    secrets = cfg.secrets()
    sandbox = None
    try:
        async with sem:
            t0 = time.perf_counter()
            sandbox = await _new_sandbox(daytona, cfg)
            t_create = time.perf_counter() - t0

            t0 = time.perf_counter()
            prep = await _prepare(sandbox, package, version, cfg)
            t_install = time.perf_counter() - t0
            if not prep.ok:
                log(f"    {version}: {prep.blocked_by} failed")
                return _blocked(snippets, version, prep.blocked_by, redact(prep.note, secrets))

            plans = [plan_snippet(s, package, prep.bins) for s in snippets]
            t0 = time.perf_counter()
            doc, tail = await _probe(sandbox, plans, package, cfg)
            t_probe = time.perf_counter() - t0
            log(f"    {version}: create {t_create:.1f}s · install {t_install:.1f}s "
                f"· probe {t_probe:.1f}s ({len(snippets)} snippets, node {prep.node})")
            return _structural_results(plans, doc, package, prep.bins, version, secrets, tail)
    except Exception as e:
        return _failed(e, snippets, version, secrets, log)
    finally:
        await _drop(daytona, sandbox)


def _failed(e: Exception, snippets: list[Snippet], version: str, secrets: list[str], log) -> list[Result]:
    msg = f"{type(e).__name__}: {e}"
    if QUOTA_ERROR.search(msg):
        log(f"    {version}: sandbox quota exceeded - lower --concurrency")
    else:
        log(f"    {version}: sandbox error - {type(e).__name__}")
    return _blocked(snippets, version, "sandbox", redact(msg[:800], secrets))


def _ok(probe: dict, notes: list[str] | None = None) -> str:
    return json.dumps({"probe": probe, "npm": [], "failed": [], "notes": notes or []}, indent=1)


async def _run_one(sandbox, s: Snippet, k: int, ts_ok: bool, package: str, cfg: Config,
                   secrets: list[str], probe: dict) -> tuple[Status, str]:
    """Run one execute-tier snippet. (status, Result.stderr)"""
    lang = s.lang.strip().lower()
    if lang in js.SHELL_LANGS:
        r = await sandbox.process.exec(strip_prompts(s.code), cwd=ROOT,
                                       timeout=max(cfg.install_timeout, cfg.snippet_timeout))
        body = redact((r.result or "")[-2000:], secrets)
        if r.exit_code == 0:
            return "pass", _ok(probe)
        if transient(body) or NETWORK_RUN.search(body) or NETWORK_ERROR.search(body):
            return "unverified", _ok(probe, [f"the install hit the network, not the docs: {error_line(body)}"])
        return "fail", _render([{"check": "runtime", "subject": s.code.strip().splitlines()[0][:80],
                                 "line": 1, "message": error_line(body), "detail": _excerpt(body)}],
                               [], probe)

    ts = js.is_ts(lang)
    ext = ("mts" if js.is_esm(s.code) else "cts") if ts else ("mjs" if js.is_esm(s.code) else "cjs")
    name = f"snippet-{k}.{ext}"
    await _upload(sandbox, {name: s.code})
    if not ts:
        c = await sandbox.process.exec(f"node --check {name}", cwd=ROOT, timeout=30)
        if c.exit_code != 0:
            return "unverified", _ok(probe, ["not a complete program (docs showed an excerpt): "
                                             + redact(error_line(c.result or ""), secrets)])
    r = await sandbox.process.exec(
        f"CI=1 NO_COLOR=1 timeout -k 5 {cfg.snippet_timeout} node {name}", cwd=ROOT,
        timeout=cfg.snippet_timeout + 20)
    body = redact((r.result or "")[-2500:], secrets)
    if r.exit_code == 0:
        return "pass", _ok(probe)
    if r.exit_code in KILLED:
        return "unverified", _ok(probe, [f"still running after {cfg.snippet_timeout}s (a server or watcher?)"])
    status, finding, note = classify_run(package, body, ts)
    if status == "fail" and finding:
        return "fail", _render([finding], [], probe)
    return status, _ok(probe, [note])


def needs_probe(s: Snippet, ts_ok: bool) -> bool:
    """Execute-tier snippets that cannot be run here and are checked instead."""
    lang = s.lang.strip().lower()
    if lang in ("jsx", "tsx"):
        return True
    return js.is_ts(lang) and not ts_ok


async def run_chain(daytona, chain: list[Snippet], package: str, version: str, cfg: Config,
                    sem: asyncio.Semaphore, log=print) -> list[Result]:
    """One sandbox per chain: an install here is what the next snippet runs against."""
    secrets = cfg.secrets()
    sandbox = None
    try:
        async with sem:
            sandbox = await _new_sandbox(daytona, cfg)
            prep = await _prepare(sandbox, package, version, cfg)
            if not prep.ok:
                log(f"    {version}: {prep.blocked_by} failed")
                return _blocked(chain, version, prep.blocked_by, redact(prep.note, secrets))

            ts_ok = strips_types(prep.node)
            probe = {"lane": "execute", "node": prep.node, "bins": sorted(prep.bins)}
            # JSX, and TS on a Node that cannot strip types: checked, never run
            checked: dict[str, Result] = {}
            fallback = [s for s in chain if needs_probe(s, ts_ok)]
            if fallback:
                plans = [plan_snippet(s, package, prep.bins) for s in fallback]
                doc, tail = await _probe(sandbox, plans, package, cfg)
                for r in _structural_results(plans, doc, package, prep.bins, version, secrets, tail):
                    checked[r.id] = r

            results: list[Result] = []
            blocked_by: str | None = None
            for k, s in enumerate(chain):
                if blocked_by:
                    results.append(Result(id=s.id, page=s.page, version=version, status="blocked",
                                          blocked_by=blocked_by, tier=s.tier))
                    continue
                if s.id in checked:
                    results.append(checked[s.id])      # structural: blocks nothing
                    continue
                lang = s.lang.strip().lower()
                if lang not in js.SHELL_LANGS and not js.is_js(lang):
                    results.append(Result(id=s.id, page=s.page, version=version, status="unverified",
                                          stderr=_ok(probe, [f"no way to run '{s.lang}' snippets"]),
                                          tier=s.tier))
                    continue
                status, body = await _run_one(sandbox, s, k, ts_ok, package, cfg, secrets, probe)
                results.append(Result(id=s.id, page=s.page, version=version, status=status,
                                      stderr=redact(body, secrets), tier=s.tier))
                if status == "fail":
                    blocked_by = s.id          # an execute snippet really does establish state
            return results
    except Exception as e:
        return _failed(e, chain, version, secrets, log)
    finally:
        await _drop(daytona, sandbox)


async def run_npm_matrix(snippets: list[Snippet], package: str, versions: list[str],
                         cfg: Config, log=print) -> list[Result]:
    """The npm counterpart of `runner.run_matrix`: one Result per (snippet, version)."""
    from daytona import AsyncDaytona

    from ..extract.tiers import chains

    structural = [s for s in snippets if s.tier == "structural"]
    executable = [s for s in snippets if s.tier == "execute"]
    skipped = [s for s in snippets if s.tier == "unverifiable"]

    if not cfg.can_execute:
        log("  no DAYTONA_API_KEY - skipping execution, marking unverified")
        return [Result(id=s.id, page=s.page, version=v, status="unverified", tier=s.tier)
                for s in snippets for v in versions]

    exec_chains = chains(executable) if executable else []
    concurrency = max(1, min(cfg.concurrency, MAX_CONCURRENCY))
    lanes = len(versions) if structural else 0
    log(f"  {lanes + len(exec_chains) * len(versions)} sandboxes: {lanes} structural "
        f"({len(structural)} snippets batched per version)"
        + (f" + {len(exec_chains)} execute chains x {len(versions)}" if exec_chains else "")
        + f", concurrency {concurrency}")

    out: list[Result] = []
    async with AsyncDaytona(runner._cfg(cfg)) as daytona:       # one client for the run
        sem = asyncio.Semaphore(concurrency)
        tasks = [run_structural(daytona, structural, package, v, cfg, sem, log) for v in versions]
        tasks += [run_chain(daytona, c, package, v, cfg, sem, log)
                  for v in versions for c in exec_chains]
        got = await asyncio.gather(*tasks, return_exceptions=True)
        for batch in got:
            if isinstance(batch, BaseException):
                log(f"    task failed - {type(batch).__name__}")
                continue
            out.extend(batch)
        await runner.sweep(daytona, log)

    out.extend(Result(id=s.id, page=s.page, version=v, status="unverified", tier=s.tier)
               for s in skipped for v in versions)
    return out
