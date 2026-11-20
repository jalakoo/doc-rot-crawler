"""npm lane: payloads, probe parsing, the judging rules, and the real Node
probe run against fixture packages. No Daytona, no network.

Every judging test encodes a false "your docs are wrong" the lane must not
report, or a real one it must.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from docrot.config import Config
from docrot.models import Snippet
from docrot.verify import npm
from docrot.verify.npm import CliCheck, Plan
from tests.fakes import run_async

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _snip(code: str, lang: str = "ts", sid: str = "s1", tier: str = "structural") -> Snippet:
    return Snippet(id=sid, page="p", lang=lang, code=textwrap.dedent(code).strip(), tier=tier)  # type: ignore[arg-type]


# --------------------------------------------------------------- plans and payload
def test_bins_of_reads_string_and_object_forms():
    assert npm.bins_of({"name": "@acme/tool", "bin": "cli.js"}, "@acme/tool") == {"tool": "cli.js"}
    assert npm.bins_of({"bin": {"a": "x.js", "b": "y.js"}}, "p") == {"a": "x.js", "b": "y.js"}
    assert npm.bins_of({}, "p") == {}


def test_plan_keeps_only_this_packages_uses_and_bins():
    s = _snip("""
        import { a } from 'momentic';
        import { b } from 'momentic/server';
        import { c } from 'other';
        import fs from 'fs';
        import { a } from 'momentic';
    """)
    plan = npm.plan_snippet(s, "momentic", {"momentic": "x"})
    assert [(u.specifier, u.name) for u in plan.uses] == [("momentic", "a"), ("momentic/server", "b")]

    sh = _snip("npx momentic run --ci\nnpx prettier --write .\nmomentic-extra go", "bash")
    plan = npm.plan_snippet(sh, "momentic", {"momentic": "x"})
    assert [(c.bin, c.inv.args, c.inv.flags) for c in plan.clis] == [("momentic", ("run",), ("--ci",))]


def test_a_package_with_one_bin_runs_it_whatever_the_docs_call_it():
    inv = npm.js.invocations("npx @acme/tool go", {"@acme/tool"})[0]
    assert npm.resolve_bin(inv, "@acme/tool", {"acme-cli": "x"}).bin == "acme-cli"      # type: ignore[union-attr]
    c = npm.resolve_bin(inv, "@acme/tool", {"a": "x", "b": "y"})
    assert c is not None and c.bin == "" and c.reason.startswith("installs no bin")
    # some other tool on the line is not this package's business
    other = npm.js.invocations("npx -p @acme/tool other-bin go", {"@acme/tool"})
    assert other and npm.resolve_bin(other[0], "@acme/tool", {"a": "x"}).bin == ""      # type: ignore[union-attr]
    assert npm.resolve_bin(npm.js.invocations("tsc --noEmit", {"tsc"})[0], "momentic", {"m": "x"}) is None


def test_payload_batches_uses_and_dedupes_help_chains():
    cfg = Config()
    plans = [npm.plan_snippet(_snip("import { a } from 'pkg'"), "pkg", {"pkg": "x"}),
             npm.plan_snippet(_snip("npx pkg run deploy --ci", "bash", "s2"), "pkg", {"pkg": "x"}),
             npm.plan_snippet(_snip("pkg run deploy --fast", "sh", "s3"), "pkg", {"pkg": "x"}),
             npm.plan_snippet(_snip("pkg init ./my-app", "sh", "s4"), "pkg", {"pkg": "x"})]
    p = npm.build_payload(plans, "pkg", cfg)
    assert p["snippets"] == [{"id": "s1", "uses": [
        {"specifier": "pkg", "name": "a", "line": 1, "typeOnly": False}]}]
    by_last = {tuple(c["steps"][-1]["args"]): c for c in p["chains"]}
    assert [s["args"] for s in by_last[("run", "deploy")]["steps"]] == [[], ["run"], ["run", "deploy"]]
    assert len(p["chains"]) == 2                    # the repeated run/deploy is asked about once
    assert ["init"] in [c["steps"][-1]["args"] for c in p["chains"]]    # the path is a value, not asked
    json.dumps(p)                                   # serialisable as it stands


def test_node_version_gates_type_stripping():
    assert npm.strips_types("v24.22.1") and npm.strips_types("v22.18.0") and npm.strips_types("v23.6.0")
    assert not npm.strips_types("v22.17.9") and not npm.strips_types("v20.11.0")
    assert not npm.strips_types("") and not npm.strips_types("garbage")


def test_install_spec_for_head_is_the_bare_package():
    assert npm.install_spec("momentic", "HEAD") == "momentic"
    assert npm.install_spec("@a/b", "1.2.3") == "@a/b@1.2.3"


def test_transient_install_failures_are_retried_but_a_missing_version_is_not():
    assert npm.transient("npm error network request failed ECONNRESET")
    assert npm.transient("npm ERR! network getaddrinfo EAI_AGAIN registry.npmjs.org")
    assert npm.transient("npm error 429 Too Many Requests")
    assert not npm.transient("npm error code ETARGET\nnpm error notarget No matching version found for x@9.9.9")


# ------------------------------------------------------------------- probe output
def test_parse_probe_ignores_what_packages_print_around_the_document():
    doc = {"node": "v24", "uses": {}, "helps": {}}
    out = ("hello from the package\n{not json}\n" f"\n{npm.MARKER}\n{json.dumps(doc)}\n{npm.MARKER}\n"
           "bye\n")
    assert npm.parse_probe(out) == doc
    # a package that prints the marker itself: the last well-formed block wins
    echoed = f"{npm.MARKER}\nnope\n{npm.MARKER}\n" + out
    assert npm.parse_probe(echoed) == doc
    assert npm.parse_probe("no document at all") is None
    assert npm.parse_probe("") is None


# --------------------------------------------------------------- judging: imports
def _doc(modules: dict, uses: dict, helps: dict | None = None, notes: list | None = None) -> dict:
    return {"node": "v24.0.0", "modules": modules, "uses": uses, "helps": helps or {}, "notes": notes or []}


def _body(stderr: str) -> dict:
    return json.loads(stderr)


def test_everything_present_passes():
    plan = npm.plan_snippet(_snip("import { run } from 'pkg'"), "pkg", {})
    status, body = npm.judge(plan, _doc({"pkg": {"state": "ok"}}, {"s1": [{"state": "ok"}]}), "pkg", {})
    assert status == "pass" and _body(body)["npm"] == []


def test_a_module_the_package_does_not_ship_is_a_finding_once():
    plan = npm.plan_snippet(_snip("import { a, b } from 'pkg/server'"), "pkg", {})
    doc = _doc({"pkg/server": {"state": "not_exported",
                               "message": "[ERR_PACKAGE_PATH_NOT_EXPORTED] Package subpath './server' is not defined"}},
               {"s1": [{"state": "module"}, {"state": "module"}]})
    status, body = npm.judge(plan, doc, "pkg", {})
    b = _body(body)
    assert status == "fail" and len(b["npm"]) == 1
    assert b["npm"][0]["check"] == "module" and b["npm"][0]["subject"] == "pkg/server"
    assert b["failed"][0].startswith("pkg/server: ")


def test_a_module_that_throws_when_loaded_is_unverified_not_failed():
    plan = npm.plan_snippet(_snip("import { a } from 'pkg'"), "pkg", {})
    doc = _doc({"pkg": {"state": "error", "message": "ReferenceError: window is not defined"}},
               {"s1": [{"state": "module"}]})
    status, body = npm.judge(plan, doc, "pkg", {})
    b = _body(body)
    assert status == "unverified" and b["npm"] == []
    assert "window is not defined" in b["notes"][0]


def test_a_missing_export_is_a_finding_and_says_where_it_looked():
    plan = npm.plan_snippet(_snip("import { gone } from 'pkg'"), "pkg", {})
    doc = _doc({"pkg": {"state": "ok"}}, {"s1": [{"state": "missing", "declarations": True}]})
    status, body = npm.judge(plan, doc, "pkg", {})
    f = _body(body)["npm"][0]
    assert status == "fail" and f["check"] == "export" and f["subject"] == "pkg.gone"
    assert "type declarations" in f["message"]


def test_an_unknowable_type_only_import_is_unverified():
    plan = npm.plan_snippet(_snip("import type { T } from 'pkg'"), "pkg", {})
    doc = _doc({"pkg": {"state": "ok"}},
               {"s1": [{"state": "unknown", "why": "type-only import and the package ships no declarations"}]})
    status, body = npm.judge(plan, doc, "pkg", {})
    assert status == "pass"        # the module itself loaded and held
    assert "no declarations" in _body(body)["notes"][0]


def test_nothing_checkable_is_unverified_with_a_reason():
    plan = npm.plan_snippet(_snip("echo hello", "bash"), "pkg", {})
    status, body = npm.judge(plan, _doc({}, {}), "pkg", {})
    assert status == "unverified" and _body(body)["notes"]


# ---------------------------------------------------------------- judging: the CLI
COMMANDER_ROOT = """Usage: tool [options] [command]

A tool.

Options:
  -V, --version   output the version number
  --verbose       chatty
  -h, --help      display help for command

Commands:
  run [options]   run the tests
  deploy          deploy it
"""
COMMANDER_RUN = """Usage: tool run [options]

Options:
  --ci            run in CI mode
  --no-cache      do not use the cache
  -h, --help      display help for command
"""
COMMANDER_UNKNOWN = "error: unknown command 'frobnicate'\n"

YARGS_ROOT = """tool <command>

Commands:
  tool build  Build it
  tool serve  Serve it

Options:
  --help     Show help  [boolean]
  --version  Show version number  [boolean]
  --config   Path to config  [string]
"""
YARGS_UNKNOWN = "Unknown command: frobnicate\n"

OCLIF_ROOT = """A tool

VERSION
  tool/1.0.0

USAGE
  $ tool [COMMAND]

COMMANDS
  deploy  Deploy it
  help    Display help
"""
OCLIF_UNKNOWN = " ›   Error: command frobnicate not found\n"


def _h(out: str, code: int = 0, **kw) -> dict:
    return {"out": out, "code": code, "signal": "", "timedOut": False, "error": "", **kw}


def _cli(code: str, bins: dict | None = None, package: str = "tool") -> tuple[CliCheck, dict]:
    bins = {"tool": "x"} if bins is None else bins
    plan = npm.plan_snippet(_snip(code, "bash"), package, bins)
    assert plan.clis, code
    return plan.clis[0], bins


def _judge_cli(code: str, helps: dict, **kw):
    c, bins = _cli(code, **kw)
    return npm.judge_cli(c, {npm.help_key("tool", list(k)): v for k, v in helps.items()}, "tool", bins)


def test_a_real_command_and_flags_hold():
    held, findings, notes = _judge_cli("tool run --ci --no-cache",
                                       {(): _h(COMMANDER_ROOT), ("run",): _h(COMMANDER_RUN)})
    assert findings == [] and notes == [] and held >= 3


def test_commander_unknown_command_is_a_finding():
    _held, findings, _ = _judge_cli("tool frobnicate --ci",
                                   {(): _h(COMMANDER_ROOT), ("frobnicate",): _h(COMMANDER_UNKNOWN, 1)})
    [f] = findings
    assert f["check"] == "command" and f["subject"] == "tool frobnicate"
    assert "unknown command 'frobnicate'" in f["message"]


def test_yargs_and_oclif_unknown_commands_are_findings():
    _, findings, _ = _judge_cli("tool frobnicate", {(): _h(YARGS_ROOT), ("frobnicate",): _h(YARGS_UNKNOWN, 1)})
    assert [f["check"] for f in findings] == ["command"]
    _, findings, _ = _judge_cli("tool frobnicate", {(): _h(OCLIF_ROOT), ("frobnicate",): _h(OCLIF_UNKNOWN, 2)})
    assert [f["check"] for f in findings] == ["command"]


def test_an_error_about_some_other_word_is_not_evidence():
    _, findings, _notes = _judge_cli("tool frobnicate",
                                    {(): _h(COMMANDER_ROOT),
                                     ("frobnicate",): _h("error: unknown command 'other'\n", 1)})
    assert findings == []


def test_a_word_whose_help_equals_its_parents_was_swallowed_as_a_positional():
    # a CLI that takes free-form arguments at the root: the word is a value
    root = YARGS_ROOT.replace("Options:", "Arguments:\n  files  what to build\n\nOptions:")
    _held, findings, _ = _judge_cli("tool myapp --config", {(): _h(root + "\n"), ("myapp",): _h(root)})
    assert findings == []            # not a missing command...
    _held, findings, _ = _judge_cli("tool myapp --nope", {(): _h(root), ("myapp",): _h(root)})
    assert [f["check"] for f in findings] == ["flag"]    # ...and flags are judged against the root
    # a help with no command table at all says nothing about which words are commands
    plain = "Usage: tool [options]\n\nOptions:\n  --config  path\n  -h, --help\n"
    _held, findings, _ = _judge_cli("tool myapp", {(): _h(plain), ("myapp",): _h(plain)})
    assert findings == []


def test_commander_swallows_an_unknown_word_so_the_command_table_is_the_evidence():
    """Measured on momentic: `momentic frobnicate --help` prints the root help, exit 0,
    no error. The root lists its commands and takes no free-form arguments."""
    swallowed = {(): _h(COMMANDER_ROOT), ("frobnicate",): _h(COMMANDER_ROOT)}
    _, findings, _ = _judge_cli("tool frobnicate", swallowed)
    [f] = findings
    assert f["check"] == "command" and f["subject"] == "tool frobnicate"
    assert "not among the commands" in f["message"] and "deploy" in f["message"]
    # a listed word, an alias, and `help` are not findings
    aliased = COMMANDER_ROOT.replace("run [options]  ", "run|test [options]")
    for word in ("run", "test", "help"):
        _, findings, _ = _judge_cli(f"tool {word}", {(): _h(aliased), (word,): _h(aliased)})
        assert findings == [], word
    # a root that takes positional arguments might be taking this word as one
    args_root = COMMANDER_ROOT.replace("Usage: tool [options] [command]", "Usage: tool [options] [command] [files...]")
    _, findings, _ = _judge_cli("tool frobnicate", {(): _h(args_root), ("frobnicate",): _h(args_root)})
    assert findings == []


def test_command_tables_are_read_from_commander_yargs_and_oclif_helps():
    assert npm.command_names(COMMANDER_ROOT, "tool") == {"run", "deploy"}
    assert npm.command_names(YARGS_ROOT, "tool") == {"build", "serve"}
    assert npm.command_names(OCLIF_ROOT, "tool") == {"deploy", "help"}
    assert not npm.takes_positionals(COMMANDER_ROOT) and not npm.takes_positionals(YARGS_ROOT)
    assert not npm.takes_positionals(OCLIF_ROOT)
    assert npm.takes_positionals("Usage: tool run|test [options] [tests...]\n")


def test_values_that_look_like_paths_are_never_asked_about():
    c, _bins = _cli("tool init ./my-app v1.2 8080")
    assert npm.chain_args(c.inv) == ["init"]
    _held, findings, notes = _judge_cli("tool ./my-app", {(): _h(COMMANDER_ROOT)})
    assert findings == [] and notes == []


def test_missing_flag_is_a_finding_present_and_negated_ones_are_not():
    helps = {(): _h(COMMANDER_ROOT), ("run",): _h(COMMANDER_RUN)}
    _, findings, _ = _judge_cli("tool run --parallel", helps)
    [f] = findings
    assert f["check"] == "flag" and f["subject"] == "tool run --parallel"
    assert "tool run --help" in f["message"]
    # --no-cache is documented; --cache is satisfied by --no-cache; --no-ci by --ci
    _, findings, _ = _judge_cli("tool run --cache --no-ci --no-cache", helps)
    assert findings == []


def test_a_flag_hidden_from_help_but_known_to_the_package_is_not_a_finding():
    """momentic's `run --ai-select` is commander's hideHelp(): accepted, never listed."""
    c, bins = _cli("tool run --ai-select --parallel")
    helps = {npm.help_key("tool", list(k)): v
             for k, v in {(): _h(COMMANDER_ROOT), ("run",): _h(COMMANDER_RUN)}.items()}
    held, findings, notes = npm.judge_cli(c, helps, "tool", bins, {"--ai-select"})
    assert [f["subject"] for f in findings] == ["tool run --parallel"]    # the unknown one still fails
    assert any("--ai-select" in n and "hidden" in n for n in notes)
    held_without, findings_without, _ = npm.judge_cli(c, helps, "tool", bins)
    assert {f["subject"] for f in findings_without} == {"tool run --ai-select", "tool run --parallel"}
    assert held == held_without


def test_the_payload_asks_the_probe_which_unlisted_flags_the_package_names():
    c, _ = _cli("tool run --ai-select --ci")
    plan = npm.Plan(snippet=Snippet(id="s", page="p", lang="bash", code="x", tier="structural"),
                    clis=[c])
    assert npm.build_payload([plan], "tool", Config())["flags"] == ["--ai-select", "--ci"]


def test_flag_documented_only_on_the_root_still_counts():
    _, findings, _ = _judge_cli("tool run --verbose", {(): _h(COMMANDER_ROOT), ("run",): _h(COMMANDER_RUN)})
    assert findings == []


def test_universal_flags_are_never_checked():
    _, findings, _ = _judge_cli("tool run --help --version", {(): _h("Usage: tool\n"), ("run",): _h("Usage: tool run x\n")})
    assert findings == []


def test_flag_matching_is_exact_not_a_prefix():
    assert npm.flag_present("--config", "  --config  Path")
    assert not npm.flag_present("--conf", "  --config  Path")
    assert not npm.flag_present("--config", "  --no-config-file")
    assert npm.flag_present("--no-color", "  --[no-]color  colours")


@pytest.mark.parametrize("root", [
    _h("", 1),                                                       # nothing at all
    _h("", None, timedOut=True),                                     # hung
    _h("Error: please log in first\n", 1),                           # demands login
    _h("", None, error="ENOENT"),                                    # bin not runnable
    {"skipped": True},
])
def test_unusable_root_help_makes_every_cli_check_unverified(root):
    held, findings, notes = _judge_cli("tool run --ci --nope", {(): root})
    assert held == 0 and findings == [] and notes and "not judged" in notes[0]


def test_help_that_exits_nonzero_but_prints_usage_is_still_help():
    held, findings, _ = _judge_cli("tool run --ci", {(): _h(COMMANDER_ROOT, 1), ("run",): _h(COMMANDER_RUN, 1)})
    assert findings == [] and held >= 2


def test_a_subcommand_whose_help_hangs_leaves_its_flags_unverified():
    _held, findings, notes = _judge_cli("tool run --nope",
                                       {(): _h(COMMANDER_ROOT), ("run",): _h("", None, timedOut=True)})
    assert findings == [] and "flags not judged" in notes[0]


def test_a_package_that_installs_no_such_bin_is_a_finding():
    c, bins = _cli("npx @acme/tool go", {"a": "x", "b": "y"}, package="@acme/tool")
    _held, findings, _ = npm.judge_cli(c, {}, "@acme/tool", bins)
    [f] = findings
    assert f["check"] == "bin" and "a, b" in f["message"]
    c, bins = _cli("npx foo go", {}, package="foo")
    _, findings, _ = npm.judge_cli(c, {}, "foo", bins)
    assert findings and "none" in findings[0]["message"]


def test_judge_combines_imports_and_cli_checks():
    s = _snip("npx tool run --parallel", "bash")
    plan = npm.plan_snippet(s, "tool", {"tool": "x"})
    doc = _doc({}, {}, {npm.help_key("tool", []): _h(COMMANDER_ROOT),
                        npm.help_key("tool", ["run"]): _h(COMMANDER_RUN)})
    status, body = npm.judge(plan, doc, "tool", {"tool": "x"})
    assert status == "fail" and _body(body)["npm"][0]["check"] == "flag"
    assert _body(body)["probe"]["bins"] == ["tool"]


# --------------------------------------------------------- execute lane: classify
def test_syntax_error_and_reference_error_are_excerpts_not_failures():
    assert npm.classify_run("pkg", "SyntaxError: Unexpected token '}'")[0] == "unverified"
    st, f, note = npm.classify_run("pkg", "ReferenceError: client is not defined\n    at snippet-0.mjs:3:1")
    assert st == "unverified" and f is None and "client" in note


def test_export_not_found_and_not_a_function_are_failures():
    err = ("file:///w/snippet-0.mjs:1\nimport { gone } from 'pkg';\n         ^^^^\n"
           "SyntaxError: The requested module 'pkg' does not provide an export named 'gone'\n"
           "    at ModuleJob._instantiate (node:internal/modules/esm/module_job:180:21)")
    st, f, _ = npm.classify_run("pkg", err)
    assert st == "fail" and f and "does not provide an export named 'gone'" in f["message"]
    st, f, _ = npm.classify_run("pkg", "TypeError: client.go is not a function\n    at snippet-2.cjs:7:8")
    assert st == "fail" and f and f["check"] == "runtime" and f["line"] == 7
    assert "is not a function" in f["message"]
    st, f, _ = npm.classify_run("pkg", "TypeError: pkg.Client is not a constructor\n    at snippet-0.cjs:2:1")
    assert st == "fail"


def test_module_not_found_is_a_failure_only_for_this_package():
    mine = ("node:internal/modules/esm/resolve:306\nError [ERR_MODULE_NOT_FOUND]: Cannot find module "
            "'/w/node_modules/pkg/dist/nope.js' imported from /w/snippet-0.mjs")
    assert npm.classify_run("pkg", mine)[0] == "fail"
    other = "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'dotenv' imported from /w/snippet-0.mjs"
    assert npm.classify_run("pkg", other)[0] == "unverified"
    local = "Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/w/helpers.js' imported from /w/snippet-0.mjs"
    assert npm.classify_run("pkg", local)[0] == "unverified"
    gone = ("Error [ERR_PACKAGE_PATH_NOT_EXPORTED]: Package subpath './x' is not defined by \"exports\" in "
            "/w/node_modules/pkg/package.json imported from /w/snippet-0.mjs")
    assert npm.classify_run("pkg", gone)[0] == "fail"
    theirs = gone.replace("node_modules/pkg/", "node_modules/zzz/")
    assert npm.classify_run("pkg", theirs)[0] == "unverified"


def test_a_missing_export_of_another_package_is_not_ours():
    err = "SyntaxError: The requested module 'react' does not provide an export named 'nope'"
    assert npm.classify_run("pkg", err)[0] == "unverified"
    err = "SyntaxError: The requested module 'pkg/sub' does not provide an export named 'nope'"
    st, f, _ = npm.classify_run("pkg", err)
    assert st == "fail" and f


def test_network_and_credentials_are_unverified():
    for text in ("Error: connect ECONNREFUSED 127.0.0.1:5432", "TypeError: fetch failed\n  cause: ENOTFOUND api.x.io",
                 "Error: getaddrinfo EAI_AGAIN api.x.io", "Error: 401 Unauthorized",
                 "Error: Missing API key. Set MOMENTIC_API_KEY", "Error: 403 Forbidden"):
        assert npm.classify_run("pkg", text)[0] == "unverified", text


def test_typescript_the_stripper_cannot_run_is_unverified():
    err = "SyntaxError: The requested module 'pkg' does not provide an export named 'Config'"
    st, _f, note = npm.classify_run("pkg", err, ts=True)
    assert st == "unverified" and "type" in note
    assert npm.classify_run("pkg", "TypeError [ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX]: enums", ts=True)[0] == "unverified"


def test_prompts_are_stripped_from_shell_snippets_before_they_run():
    assert npm.strip_prompts("$ npm install pkg\n> npm i other") == "npm install pkg\nnpm i other"


def test_needs_probe_is_for_jsx_and_for_ts_on_an_old_node():
    assert npm.needs_probe(_snip("<A/>", "tsx"), True) and npm.needs_probe(_snip("<A/>", "jsx"), True)
    assert npm.needs_probe(_snip("let a: number", "ts"), False)
    assert not npm.needs_probe(_snip("let a: number", "ts"), True)
    assert not npm.needs_probe(_snip("1", "js"), False)


# ----------------------------------------------------------- the no-key path
def test_without_an_api_key_everything_is_unverified_and_nothing_connects():
    logs: list[str] = []
    snips = [_snip("import a from 'pkg'", sid="a"), _snip("npm install pkg", "bash", "b", "execute"),
             _snip("x", "text", "c", "unverifiable")]
    out = run_async(npm.run_npm_matrix(snips, "pkg", ["1.0.0", "HEAD"], Config(), logs.append))
    assert logs == ["  no DAYTONA_API_KEY - skipping execution, marking unverified"]
    assert {(r.id, r.version, r.status) for r in out} == {
        (i, v, "unverified") for i in "abc" for v in ("1.0.0", "HEAD")}
    assert {r.id: r.tier for r in out} == {"a": "structural", "b": "execute", "c": "unverifiable"}


# ------------------------------------------------------- the real probe, in Node
def _pkg(root: Path, name: str, files: dict[str, str], manifest: dict | None = None) -> None:
    base = root / "node_modules" / Path(*name.split("/"))
    base.mkdir(parents=True, exist_ok=True)
    (base / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0", **(manifest or {})}))
    for rel, text in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))


CLI_JS = """\
#!/usr/bin/env node
const args = process.argv.slice(2);
const words = args.filter((a) => !a.startsWith('-'));
const root = `Usage: cli [options] [command]

Options:
  --verbose       chatty
  -h, --help      display help for command

Commands:
  run [options]   run the tests
`;
const run = `Usage: cli run [options]

Options:
  --ci            run in CI mode
  -h, --help      display help for command
`;
if (!words.length) { console.log(root); }
else if (words[0] === 'run') { console.log(run); }
else { console.error(`error: unknown command '${words[0]}'`); process.exit(1); }
"""


@pytest.fixture
def world(tmp_path: Path) -> Path:
    """A work dir with a handful of hand-built packages in node_modules."""
    _pkg(tmp_path, "esm-pkg", {
        "index.js": "export const run = () => 1;\nexport default function main() {}\n",
        "server.js": "export const serve = () => 2;\n"},
        {"type": "module", "exports": {".": "./index.js", "./server": "./server.js"}})
    _pkg(tmp_path, "cjs-pkg", {
        "index.js": "console.log('cjs-pkg says hi on import');\nmodule.exports = { run() {}, helper() {} };\n",
        "lib/util.js": "module.exports = { util() {} };\n"}, {"main": "index.js"})
    _pkg(tmp_path, "typed", {
        "index.js": "export const make = () => 1;\n",
        "index.d.ts": ("export interface Config { a: number }\nexport declare function make(): number;\n"
                       "type Hidden = string;\nexport type { Hidden };\n")},
        {"type": "module", "main": "index.js", "types": "index.d.ts"})
    _pkg(tmp_path, "thrower", {"index.js": "throw new Error('window is not defined');\n"}, {"main": "index.js"})
    _pkg(tmp_path, "exiter", {"index.js": "console.log('bye'); process.exit(3);\n"}, {"main": "index.js"})
    _pkg(tmp_path, "broken", {"index.js": "require('ghost-dependency');\n"}, {"main": "index.js"})
    _pkg(tmp_path, "clipkg", {"bin/cli.js": CLI_JS, "index.js": "module.exports = {};\n"},
         {"main": "index.js", "bin": {"cli": "bin/cli.js"}})
    bindir = tmp_path / "node_modules" / ".bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "cli"
    shim.write_text(CLI_JS)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def _probe(world: Path, package: str, *snippets: Snippet, timeout: int = 60) -> tuple[list[Plan], dict, dict]:
    manifest = json.loads((world / "node_modules" / Path(*package.split("/")) / "package.json").read_text())
    bins = npm.bins_of(manifest, package)
    plans = [npm.plan_snippet(s, package, bins) for s in snippets]
    (world / "probe.mjs").write_text(npm.PROBE_JS)
    (world / "payload.json").write_text(json.dumps(npm.build_payload(plans, package, Config())))
    r = subprocess.run(["node", "probe.mjs", "payload.json"], cwd=world, capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, "NO_COLOR": "1"}, check=False)
    doc = npm.parse_probe(r.stdout)
    assert doc is not None, r.stdout + r.stderr
    return plans, doc, bins


def _verdict(world: Path, package: str, code: str, lang: str = "ts") -> tuple[str, dict]:
    plans, doc, bins = _probe(world, package, _snip(code, lang))
    status, body = npm.judge(plans[0], doc, package, bins)
    return status, json.loads(body)


@needs_node
def test_esm_package_exports_default_and_subpaths(world):
    status, b = _verdict(world, "esm-pkg", "import main, { run } from 'esm-pkg';\nimport { serve } from 'esm-pkg/server';")
    assert status == "pass" and b["npm"] == []
    status, b = _verdict(world, "esm-pkg", "import { run, gone } from 'esm-pkg';")
    assert status == "fail"
    [f] = b["npm"]
    assert f["check"] == "export" and f["subject"] == "esm-pkg.gone" and f["line"] == 1


@needs_node
def test_a_subpath_the_exports_map_lacks_is_a_module_finding(world):
    status, b = _verdict(world, "esm-pkg", "import { x } from 'esm-pkg/internal';\nimport { y } from 'esm-pkg/internal';")
    assert status == "fail" and len(b["npm"]) == 1
    f = b["npm"][0]
    assert f["check"] == "module" and f["subject"] == "esm-pkg/internal"
    assert "ERR_PACKAGE_PATH_NOT_EXPORTED" in f["message"] and "/tmp" not in f["message"]


@needs_node
def test_cjs_package_named_imports_come_from_module_exports(world):
    status, b = _verdict(world, "cjs-pkg", "import { run, helper } from 'cjs-pkg';\nimport cjs from 'cjs-pkg';")
    assert status == "pass", b
    status, b = _verdict(world, "cjs-pkg", "const { run, nope } = require('cjs-pkg');", "js")
    assert status == "fail" and b["npm"][0]["subject"] == "cjs-pkg.nope"


@needs_node
def test_default_dot_member_is_found_on_the_default_export(world):
    status, b = _verdict(world, "cjs-pkg", "import lib from 'cjs-pkg';\nlib.run();")
    assert status == "pass", b
    status, b = _verdict(world, "cjs-pkg", "import lib from 'cjs-pkg';\nlib.run();\nlib.missing();")
    assert status == "fail" and b["npm"][0]["subject"] == "cjs-pkg.default.missing", b
    status, b = _verdict(world, "cjs-pkg", "const lib = require('cjs-pkg');\nlib.run();", "js")
    assert status == "pass", b


@needs_node
def test_package_json_is_loaded_with_require_not_import(world):
    status, b = _verdict(world, "cjs-pkg", "const { version, name } = require('cjs-pkg/package.json');", "js")
    assert status == "pass", b
    status, b = _verdict(world, "cjs-pkg", "const { nope } = require('cjs-pkg/package.json');", "js")
    assert status == "fail" and b["npm"][0]["check"] == "export"


@needs_node
def test_a_cjs_subpath_that_only_require_can_resolve_is_not_a_finding(world):
    # import() needs an extension here; the docs say require('cjs-pkg/lib/util')
    status, b = _verdict(world, "cjs-pkg", "const { util } = require('cjs-pkg/lib/util');", "js")
    assert status == "pass", b
    status, b = _verdict(world, "cjs-pkg", "const x = require('cjs-pkg/lib/nowhere');", "js")
    assert status == "fail" and b["npm"][0]["check"] == "module"


@needs_node
def test_type_only_names_are_found_in_the_shipped_declarations(world):
    status, b = _verdict(world, "typed", "import { make, type Config } from 'typed';\nimport type { Hidden } from 'typed';")
    assert status == "pass", b
    status, b = _verdict(world, "typed", "import type { Nope } from 'typed';")
    assert status == "fail" and "type declarations" in b["npm"][0]["message"]
    status, b = _verdict(world, "typed", "import { Config } from 'typed';")        # interface imported as a value
    assert status == "pass", b


@needs_node
def test_a_type_only_import_from_a_package_with_no_declarations_is_unverified(world):
    status, b = _verdict(world, "esm-pkg", "import type { Config } from 'esm-pkg';")
    assert status == "pass" and b["npm"] == [] and any("no declarations" in n for n in b["notes"])


@needs_node
def test_modules_that_throw_or_exit_on_import_are_unverified_and_do_not_end_the_probe(world):
    plans, doc, bins = _probe(
        world, "thrower", _snip("import { a } from 'thrower';", sid="t"))
    status, body = npm.judge(plans[0], doc, "thrower", bins)
    assert status == "unverified" and "window is not defined" in json.loads(body)["notes"][0]
    status, b = _verdict(world, "exiter", "import { a } from 'exiter';")
    assert status == "unverified" and "process.exit" in b["notes"][0]
    # the package's own dependency missing is not the docs' fault
    status, b = _verdict(world, "broken", "import { a } from 'broken';")
    assert status == "unverified" and b["npm"] == []


@needs_node
def test_package_noise_around_the_document_is_survived(world):
    _plans, doc, _bins = _probe(world, "cjs-pkg", _snip("import { run } from 'cjs-pkg';"))
    assert doc["modules"]["cjs-pkg"]["state"] == "ok"


@needs_node
def test_the_cli_is_asked_and_judged_for_real(world):
    status, b = _verdict(world, "clipkg", "npx cli run --ci --verbose", "bash")
    assert status == "pass", b
    status, b = _verdict(world, "clipkg", "cli run --parallel", "bash")
    assert status == "fail" and b["npm"][0]["check"] == "flag" and b["npm"][0]["subject"] == "cli run --parallel"
    status, b = _verdict(world, "clipkg", "cli deploy --ci", "bash")
    assert status == "fail"
    [f] = b["npm"]
    assert f["check"] == "command" and f["subject"] == "cli deploy"
    assert "unknown command 'deploy'" in f["message"]


@needs_node
def test_a_bin_the_package_does_not_install_is_a_finding_before_anything_runs(world):
    status, b = _verdict(world, "clipkg", "npx clipkg run", "bash")
    assert status == "pass", b       # one bin, so npx runs it whatever the docs call it
    _pkg(world, "nobin", {"index.js": "module.exports = {};\n"}, {"main": "index.js"})
    status, b = _verdict(world, "nobin", "npx nobin go", "bash")
    assert status == "fail" and b["npm"][0]["check"] == "bin"


@needs_node
def test_a_bare_command_is_not_a_missing_bin_when_the_package_installs_none(world):
    _pkg(world, "nobin", {"index.js": "module.exports = {};\n"}, {"main": "index.js"})
    status, b = _verdict(world, "nobin", "nobin go --fast", "bash")
    assert status == "unverified" and b["npm"] == []
    assert "separate program" in b["notes"][0]
    status, b = _verdict(world, "nobin", "npx nobin go", "bash")       # this one claims a bin
    assert status == "fail" and b["npm"][0]["check"] == "bin"


@needs_node
def test_a_release_with_no_entry_point_is_unverified_not_a_missing_module(world):
    _pkg(world, "reserved", {"README.md": "# reserved\n"}, {})
    status, b = _verdict(world, "reserved", "const { Database } = require('reserved');", "js")
    assert status == "unverified" and b["npm"] == []
    assert "ships no entry point" in b["notes"][0]
    _pkg(world, "typo-main", {"other.js": "module.exports = {};\n"}, {"main": "index.js"})
    status, b = _verdict(world, "typo-main", "const { a } = require('typo-main');", "js")
    assert status == "fail"                                            # it declares an entry it lacks


@needs_node
def test_the_probe_batches_many_snippets_in_one_process(world):
    plans, doc, bins = _probe(
        world, "esm-pkg",
        _snip("import { run } from 'esm-pkg'", sid="a"),
        _snip("import { nope } from 'esm-pkg'", sid="b"),
        _snip("import { serve } from 'esm-pkg/server'", sid="c"))
    assert [npm.judge(p, doc, "esm-pkg", bins)[0] for p in plans] == ["pass", "fail", "pass"]
