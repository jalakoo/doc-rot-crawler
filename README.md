# Doc Rot Crawler

Documentation rot detector: executes a project's docs against its real code,
across releases, and shows where they disagree.

![The dashboard: four scanned projects, then Pydantic opened - failing snippets
listed, and what the docs say beside the traceback the code actually
produced](images/dashboard.gif)

Above: 153 pages of the Pydantic docs checked against `pydantic` and
`pydantic-core`, 14 of them carrying drift — here a page whose example calls
`model_dump_json` on an object that no longer has it, raising `AttributeError`.

- [Run it](#run-it)
- [How it fits together](#how-it-fits-together)
  - [What each dependency is for](#what-each-dependency-is-for)
- [Exporting a run](#exporting-a-run)
  - [Records](#records)
  - [SARIF, for CI and code scanning](#sarif-for-ci-and-code-scanning)
  - [Importing a run](#importing-a-run)
  - [Using it](#using-it)
- [MCP server](#mcp-server)
- [Develop](#develop)
- [Security](#security)

## Run it

```bash
uv venv && uv pip install -e .
docker compose up -d          # optional: Neo4j for `docrot blast` traversals
./start.sh                    # dashboard at http://127.0.0.1:8080
./stop.sh                     # stop it, and clean up after any hung scan
```

`stop.sh` stops every dashboard started from this directory, kills git left
behind by a scan, deletes partial clones, and marks scans whose process is gone
as failed so none is left showing as running (a scan still running in another
docrot process, such as an agent's, is left alone). `--all-cache` also empties `data/cache`, which
costs the next scan a fresh clone and crawl.

Add scans from the dashboard's **New scan** dialog, or from the CLI. A scan can
pair several docs sites with several repos; every repo that declares a package is
verified against its own releases.

```bash
docrot scan --docs-url https://docs.perseus.lettria.net \
            --repo https://github.com/lettria/perseus-client
docrot scan --name "Pydantic + core" --assume-credentialed \
            --docs-url https://docs.pydantic.dev/latest \
            --repo https://github.com/pydantic/pydantic https://github.com/pydantic/pydantic-core
docrot scan --name "Duende IdentityServer" --max-pages 1000 \
            --docs-url https://docs.duendesoftware.com \
            --repo https://github.com/DuendeSoftware/products \
            --package Duende.IdentityServer Duende.BFF   # .NET: see below
docrot scans                  # what's recorded
docrot diff [--scan ID]       # regressions since the previous run; exit 1 if any (cron)
docrot blast close_async      # pages that break if a symbol is renamed
docrot export [--scan ID]     # findings as JSON Lines or SARIF - see below
docrot mcp                    # scan and read findings as MCP tools for other agents
```

Scans started from either place land in `data/scans/` and show up in both. The
dashboard runs one scan at a time: the sandbox account's CPU quota and the
end-of-run sandbox sweep both assume a single scan.

![The six phases of a scan: acquire, registry, extract, verify, graph, report -
from docs and repos in, to findings out as JSONL, SARIF, CI annotations and
agent input](images/architecture-dungeon.png)

### .NET documentation

C# snippets are compiled rather than run. A repo whose `.csproj` files declare
packages is read as a .NET repo, and each release comes from NuGet. For every
version tested, one sandbox (`mcr.microsoft.com/dotnet/sdk:10.0`, 2 CPU, 2 GiB)
installs the package, reflects over its public types and members, and compiles
every C# snippet with Roslyn. Each snippet is wrapped in its own file first:
its usings are moved to the top, its types kept as they are, its loose
statements put in a method whose parameters stand in for the `builder`,
`services` and `app` that docs never declare.

A compiler error counts as drift only when it concerns the package's own API.
The snippet, wrapper and SDK are the same in every version, so an error that
one release raises and another doesn't must come from the package itself.
`DPoPProofValidatonContext` compiles against 7.4 and not against 8.0, which
renamed it. An error every release raises is drift only if the repo defines
that name at HEAD, which means the docs describe unreleased API. Anything else
a wrapped excerpt trips over (an injected `_userManager` field the page never
showed, a type from another package) leaves the snippet `unverified`, never
failed. The finding says which way the versions split. **Behind the latest
release** is rot. **Requires a newer release** means the page is right for the
current release but not for older ones.

A .NET monorepo ships many packages. `--package` names the ones to verify,
several for one `--repo`. Without it, the scan picks the package the docs
mention most. NuGet versions are picked across major lines (8.0.8, 7.4.12,
7.3.4) rather than as three patches of one minor, because drift can only be
seen against a version old enough to compile the stale code.

Credentials come from `.env` (see `.env.sample`): `DAYTONA_API_KEY` to execute,
`EXTRACTION_API_KEY` for model-extracted claims. Without them a scan still runs,
marking what it could not check as unverified.

### npm packages and CLIs

JavaScript and TypeScript snippets are checked against the package as published.
A repo with a `package.json` is read as an npm package (a monorepo's workspaces
are searched), and each release comes from the npm registry. For every version
tested, one sandbox (Daytona's default snapshot, which carries Node 24) installs
the package and checks:

- **imports and exports.** Every module the docs import is loaded and every name
  they take from it is looked up, including subpaths and the package's shipped
  `.d.ts` files, so a type-only import is not mistaken for a missing export.
- **CLI subcommands and flags.** A shell snippet such as `npx momentic run
  --headless` is checked against the CLI's own `--help`: is `run` a subcommand,
  and does it list `--headless`.
- **runnable snippets.** A complete snippet that needs no credentials is
  syntax-checked and then run.

Documented commands are never executed, only `--help`. A snippet that needs a key,
the network, a browser or a login is left `unverified` rather than failed, as is
any CLI whose help says nothing either way. Versions are picked across major
lines rather than as the newest three: a package like momentic cuts a release
every few days, and neighbours agree on nearly everything.

A package whose repo holds only docs has no `package.json` to read. Name the
package, in the dashboard's package field beside the repo or with `--package`,
and choose npm as the ecosystem:

```bash
docrot scan --name "Momentic" --docs-url https://momentic.ai/docs \
            --repo https://github.com/momentic-ai/cli \
            --package momentic --ecosystem npm --versions 2
```

The sandbox image can be changed with `DOCROT_NODE_IMAGE` (Node 20 or newer).

Known limits: the package is installed from the npm registry, so one that is
unpublished, private or only buildable from source is not verified. A CLI that
needs a login before it prints help is `unverified`, and a snippet that depends
on files or state the page never shows is not run.

## How it fits together

A scan is one pipeline with six phases. Everything it touches outside this
process is on the right: docs sites, git remotes, the package registry, the
model that reads prose, the sandboxes that run code, and the graph.

![The scan pipeline: the dashboard and CLI enter through the API and its
queue, then six phases run top to bottom - acquire, registry, extract, verify,
graph, report - each wired to the outside system it reaches, ending in
data/scans and the exported findings](images/architecture-flowchart.png)

Each phase degrades rather than stopping: no Daytona key means snippets are
recorded `unverified` instead of being run, no OpenRouter key means lexical
claims only, and an unreachable Neo4j costs the traversal queries and nothing
else. A page nothing ran against is reported as **not verified**, never as clean.

The diagram is generated: edit `images/architecture-flowchart.html` and run
`.venv/bin/python images/render.py` to redraw it.

### What each dependency is for

| Technology | Used for | Without it |
| --- | --- | --- |
| **Daytona** (`daytona` SDK) | The sandboxes each version is installed into, then introspected or run | Every snippet is `unverified`; the scan finds nothing |
| **OpenRouter** (`openai` SDK) | Prose claims the lexical pass misses — claims only, never tiers or symbols | Lexical claims only (measured: precision 1.00, recall 0.97) |
| **PyPI / npm / NuGet** | Which releases exist and when they shipped | Falls back to `HEAD`, which only installs if the name is published |
| **.NET SDK 10 + Roslyn** (in the sandbox image) | Reflecting a NuGet package's API and compiling C# snippets against it | C# snippets are `unverified` |
| **git** ≥ 2.27 | Cloning repos: `--depth 1 --filter=blob:none` plus sparse checkout of code, docs and package metadata | No repo docs, no HEAD symbol table |
| **Neo4j 5** (`neo4j` driver, `docker-compose.yml`) | `docrot blast`, drift traversals; Symbol/Page/Snippet/Version graph | Blast radius falls back to the report's own index |
| **mcp** (official MCP Python SDK) | `docrot mcp`: the tools other AI agents call, over stdio | CLI and dashboard only |
| **FastAPI + uvicorn** | The dashboard's API and static files, bound to 127.0.0.1 | CLI only |
| **httpx** | Every HTTP fetch, with a disk cache under `data/cache` | — |
| **mistune** | Parsing code fences out of markdown, with exact line numbers | — |
| **trafilatura** | HTML to text when a docs site offers nothing better | Crawled pages lose their prose |
| **pydantic** | Models for every artefact, and the LLM's JSON schema contract | — |
| **packaging** | Comparing and sorting version labels, including pre-releases | — |
| **tenacity** | Retrying the model call | — |
| **rich** | CLI tables and colour | — |
| **Google Fonts** | The dashboard's two pixel typefaces, loaded at render | The UI falls back to a monospace stack |

Development only: **pytest** (+ `pytest-playwright`, Chromium) for unit, API and
browser tests, **node** for `node:test` and **eslint** on `docrot/ui`, plus
**ruff** and **mypy**.

## Exporting a run

A run leaves the dashboard in two formats. **JSON Lines** is the full record,
for agents and scripts; **SARIF 2.1.0** is the interchange format CI and code
scanning understand. Both come from the same records. The dashboard's own
`report.json` is shaped for rendering (HTML fragments, diff segments, a
truncated timeline) and is not meant to be parsed.

| Get it | |
| --- | --- |
| On disk | `data/scans/<scan>/runs/<run>/findings.jsonl`, written when the run is saved |
| CLI | `docrot export [--scan ID] [--run RUN] [--format jsonl\|sarif] [-o FILE] [--include-clean]` (stdout by default) |
| HTTP | `GET /api/scans/<scan>/findings.jsonl` · `…/findings.sarif` (both take `?run=` and `?include_clean=`) |
| Dashboard | **Export JSONL** and **SARIF** on a scan's page |

`--scan` defaults to the most recently run scan and `--run` to its newest run.
By default the file holds findings and blocked snippets; `--include-clean` adds
a record for every page with no drift and every prose-only page, for an agent
that needs to confirm a page is fine rather than assume it.

### Records

One JSON object per line. Line 1 has `"type": "scan"`; every later line has
`"type": "finding"`. Each line is self-contained. Nothing needs joining against
another file, so a consumer can stream, `grep`, or read only what fits.

**`scan`** - what was checked, and the totals

| Field | |
| --- | --- |
| `schema` | `"docrot/1"`. Bumped for any change that is not purely additive |
| `scan.id`, `scan.name` | the dashboard's scan |
| `run`, `generated_at`, `elapsed_s` | which run, when, how long |
| `sources[]` | `{kind: "docs"\|"repo", url, pages}` |
| `packages[]` | `{package, import_name, ecosystem, repo, branch, commit, versions_tested[]}`, one per repo with a package |
| `options` | the scan's settings (versions, max pages, credentials assumption, ...) |
| `stats` | `pages`, `drifted`, `findings`, `snippets`, `versions`, `worst_gap`; `page_states` (pages per `fail`/`drift`/`block`/`pass`/`unlit`); `results` (sandbox results per status) |

**`finding`** - one docs/code disagreement

| Field | |
| --- | --- |
| `id` | `<scan>:<snippet id>:<kind>` (`page:<path>` stands in for a page-level finding). Stable across runs of a scan, so an agent can track one finding over time |
| `kind` | `stale_pin`, `signature`, `missing_symbol`, `runtime`, `blocked`; with `--include-clean` also `clean`, `prose` |
| `label` | finer type: e.g. `kwarg renamed`, `wrong import path`, `symbol gone`, `never shipped` |
| `status` | `fail`: the docs are wrong. `blocked`: the snippet never ran, so nothing is known. `pass` / `unverifiable` for clean and prose pages |
| `severity` | `error` (signature, missing symbol, runtime) · `warning` (stale pin) · `info` (blocked) · `none` |
| `title`, `summary` | one-line name and a plain-text explanation |
| `doc` | where the docs say it: `url`, `title`, `section`, `source` (`site` or `repo`), `origin` (the docs site or repo it came from), `path`, `line`, `snippet_id`, `snippet` (the code as documented), `lang` |
| `code` | what the code does: `package`, `import_name`, `repo`, `commit`, `url` (link into the repo), `where`, `actual` (the real signature, the error, or the current release) |
| `versions` | `tested`, and which of them are `failing`, `passing`, `blocked`, `unverified` for this snippet |
| `symbols` | package symbols the snippet uses |
| `tier` | `structural` (introspected), `execute` (run in a sandbox) or `unverifiable` |
| `blocked_by` | for `blocked`, the step that failed first (e.g. `install`, or a snippet id) |
| `days_behind` | for `stale_pin`, days between the pinned and current release |
| `evidence` | raw sandbox output behind the verdict |
| `fix_hint` | what to change, generated from the finding type (no model involved) |

For repo docs (`doc.source == "repo"`), `doc.path` and `doc.line` point into the
repository at `code.commit`, so an agent can open the file and edit it. For
docs-site pages, `doc.url` and `doc.line` locate the snippet on the page.

```json
{"type": "finding", "id": "perseus-client-d49598:docs-client-file-operations-delete-file-04:missing_symbol",
 "kind": "missing_symbol", "label": "symbol gone", "status": "fail", "severity": "error",
 "title": "Symbol no longer exists",
 "summary": "perseus_client.close_async could not be resolved in 1.0.0rc16, 1.0.0rc19. It resolved in earlier releases, so this page documents code that has since been removed.",
 "doc": {"url": "https://docs.perseus.lettria.net/docs/client/file-operations/delete-file", "source": "site",
         "path": "/docs/client/file-operations/delete-file", "line": 46,
         "snippet_id": "docs-client-file-operations-delete-file-04", "snippet": "import asyncio\nimport perseus_client\n...", "lang": "python"},
 "code": {"package": "perseus-client", "repo": "https://github.com/lettria/perseus-client", "commit": "b556d1bf8ab3",
          "actual": "AttributeError: module 'perseus_client' has no attribute 'close_async'"},
 "versions": {"tested": ["1.0.0rc15", "1.0.0rc16", "1.0.0rc19"], "failing": ["1.0.0rc16", "1.0.0rc19"],
              "passing": ["1.0.0rc15"], "blocked": [], "unverified": []},
 "tier": "structural", "fix_hint": "The symbol was removed. Document its replacement, or remove the example."}
```

(Shortened: real records carry every field in the table.)

### SARIF, for CI and code scanning

`--format sarif` writes SARIF 2.1.0: GitHub code scanning, VS Code and most CI
tools render it as annotations on the file and line.

- Findings in docs committed to a repo point at `path` and `line` relative to
  the repository root (`%SRCROOT%`); findings on a docs site are located by URL.
- One rule per finding kind (`docrot/missing_symbol`, `docrot/stale_pin`, ...),
  with `error` for signature, missing-symbol and runtime findings, `warning` for
  stale pins and `note` for blocked snippets.
- `partialFingerprints` carries the stable finding id, so code scanning tracks
  one alert across runs instead of closing and reopening it.
- Each result also keeps its full JSON Lines record under `properties.docrot`,
  so a SARIF file can be imported again with nothing lost.

```yaml
# GitHub Actions: annotate a pull request with documentation rot
- run: docrot scan --docs-url "$DOCS_URL" --repo "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY"
- run: docrot export --format sarif -o docrot.sarif
- uses: github/codeql-action/upload-sarif@v3
  with: {sarif_file: docrot.sarif}
```

### Importing a run

**Import** on the dashboard takes a `.jsonl` or `.sarif` file and shows it like
a scan of your own: findings, site map, release timeline and scan log. Nothing
is re-verified, and the log says so.

- A file whose docs and repos match a scan you already have joins that scan's
  history, so a colleague's export lands beside your own runs. Anything else
  becomes a new scan, named from the file (or from the name you type).
- The same run cannot be imported twice.
- An export carries only pages with findings unless it was written with
  `--include-clean`, so an imported run shows fewer pages than the original scan.
- SARIF from another tool imports too: its results become findings, located by
  file and line, with the fields docrot did not produce left empty.

### Using it

```bash
# what is actually wrong
docrot export | jq -c 'select(.status == "fail") | {severity, title, url: .doc.url, line: .doc.line, fix_hint}'

# editable repo files that need fixing
docrot export | jq -r 'select(.status == "fail" and .doc.source == "repo") | "\(.doc.path):\(.doc.line)  \(.title)"'

# failures that are new since an earlier run (ids are stable across runs)
comm -13 <(docrot export --run OLD | jq -r 'select(.status == "fail").id' | sort) \
         <(docrot export           | jq -r 'select(.status == "fail").id' | sort)
```

For an agent, point it at the file (or the HTTP URL) and have it work through
`status == "fail"` records. Each one carries the documented snippet, the code's
actual behaviour, where to edit, and a `fix_hint`. After the docs are changed,
re-run the scan from the dashboard or `docrot scan`, and check that the ids are
gone.

## MCP server

`docrot mcp` serves docrot to other AI agents over the Model Context Protocol, so an
agent can start a scan, wait for it, and then work through the findings without anyone
copying files around. It reads and writes the same `data/scans/` store as the CLI and the
dashboard: a scan an agent starts shows up in `docrot scans` and on the dashboard.

| Tool | |
| --- | --- |
| `list_scans` | every scan: id, name, sources, state, last run and its headline counts. Start here |
| `get_scan` | one scan's state, current phase, error, last 20 log lines and run history. What an agent polls after starting a scan |
| `get_findings` | a run's findings in the [JSON Lines record shape](#records), filtered by `kinds` or `page_contains`, paged with `limit` and `offset`, with a total |
| `scan_diff` | what got worse or better since the previous run, as `docrot diff` reports it |
| `rename_impact` | which pages break if a symbol is renamed. Answered from the scan's own index, so it needs no Neo4j; the result says whether the report or the graph answered |
| `start_scan` | queue a scan of docs against repos and packages, and return at once. Runs for minutes and spends sandbox time and model credit. Reuses the scan with the same sources, as the CLI does |
| `rerun_scan` | queue another run of an existing scan |

The first five only read the store and cost nothing. `start_scan` takes `packages` the way
`--package` does: by position with `repos`, a blank keeping a repo's slot, and names past the
last repo being packages that need no repo. Results are small on purpose, since an agent pays
for every token: no full logs, and findings come a page at a time. An unknown scan id is
answered with the ids that exist, and validation messages are the dashboard's own.

Register it with Claude Code, using the absolute path to this checkout's virtualenv:

```bash
claude mcp add docrot -- /absolute/path/to/doc-rot-detector/.venv/bin/docrot mcp
```

Other clients take the same command in their config, usually under `mcpServers`:

```json
{
  "mcpServers": {
    "docrot": {
      "command": "/absolute/path/to/doc-rot-detector/.venv/bin/docrot",
      "args": ["mcp"]
    }
  }
}
```

**One scan at a time, across processes.** Every scan ends by sweeping all live sandboxes
in the account, so two scans at once delete each other's sandboxes. Inside the MCP server a
second scan waits its turn in the queue, like on the dashboard. But the store is shared, so
`start_scan` and `rerun_scan` are also refused, naming the scan, while any scan is queued or
running that this server did not start: the dashboard, `docrot scan` or another agent's
server. Every queued or running scan records the process that owns it (pid and host), so
a scan left behind by a crashed process does not block anything: when the MCP server
starts, and before each start, it marks scans whose owner has exited as failed. The same
goes for `docrot serve` at startup, `./stop.sh`, and re-running such a scan from the
dashboard. Starting the dashboard no longer fails a scan an agent or the CLI is running.
Ownership can only be checked on the machine that holds the process: a scan owned by another
host is never failed automatically, and a scan with no recorded owner (from before this was
tracked) is treated as dead. Known limit: if a dead owner's pid is reused by an unrelated
process, its scan looks alive until that process exits. When the MCP server stops, scans it
was running or had queued are marked failed.

On stdio, stdout carries only protocol frames. Everything else the server says goes to stderr.

## Settings

The dashboard's **Settings** page (`#/settings`) edits the same options as `.env` - API keys,
models, the graph store, run tuning and the `SANDBOX_ENV_*` credentials forwarded into sandboxes.
What you save there is kept in the operating system's keychain (macOS Keychain, Windows Credential
Locker, or the Secret Service on Linux) as one item, service `docrot`, account `settings`. It is
never written to a file.

A saved value overrides the same name in `.env`; removing it falls back to `.env`. The CLI reads
the keychain too, so `docrot scan` and the dashboard agree. Secrets are write-only from the page:
the server reports whether one is set and where it came from, never its value. Changes apply to
the next scan.

## Security

The dashboard binds 127.0.0.1 and has no login, because it can start sandboxes
and spend API credit. Two guards keep the rest of the web out: a write carrying
an `Origin` from anywhere else is refused (a cross-site page can POST without a
preflight), and a request whose `Host` is not a local name is refused (a
hostname pointed at 127.0.0.1). Responses carry a CSP with `script-src 'self'`.
Do not put it behind a tunnel or a reverse proxy without adding authentication.

An imported `.jsonl` or `.sarif` file is untrusted input: its text is rendered
as markup, so only `<b>` and `<code>` survive and any tag carrying an attribute
is escaped. Documentation code is only ever executed in a Daytona sandbox,
never on your machine - and so is the NuGet restore and compile of a .NET
package, whose build targets can run code of their own.

`docrot mcp` has no network listener: it speaks stdio to the one process that launched it, so
whoever can launch it can use it. Its tools can start sandboxes and spend API credit on an
agent's say-so, so register it only with agents you would let run `docrot scan`. A repo may be
a local path, exactly as on the CLI, so `start_scan` lets an agent point a scan at any directory
on this machine; the code is only read, and still executed only in a sandbox. No tool takes or
returns a credential or a setting, and none reads a file you did not name as a source. Keys stay
in `.env` and the keychain.

Credentials live in `.env` - `chmod 600` it. `SANDBOX_ENV_*` values are
forwarded into third-party sandboxes so snippets can run, and everything a
sandbox prints is redacted before it is stored. `docker-compose.yml` publishes
Neo4j on 127.0.0.1 only: its password is in the file, so do not widen that.

```bash
make audit        # pip-audit and npm audit
```

## Develop

```bash
make install     # dev deps, Playwright's Chromium, eslint
make check       # ruff + eslint, mypy, pytest (unit, API, e2e) with coverage, JS unit tests
make test-unit   # fast loop: no browser, no services
make e2e         # the dashboard in headless Chromium
make test-graph  # against the local Neo4j - wipes its graph
```

| Where | What |
| --- | --- |
| `docrot/scan.py` | the pipeline: acquire → registry → extract → verify → graph → report |
| `docrot/store.py` | scans, status and run history as JSON under `data/scans/` |
| `docrot/report/export.py`, `sarif.py`, `ingest.py` | the exports above, and reading them back in |
| `docrot/server.py`, `worker.py` | dashboard API and the serial scan queue |
| `docrot/mcp_server.py` | the MCP tools for other agents (`docrot mcp`) |
| `docrot/ui/` | the dashboard - no build step; `lib.js` holds the unit-tested logic |
| `tests/fakes.py` | real reports from synthetic inputs, so no test touches the network |
