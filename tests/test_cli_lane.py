"""CLI lane (standalone binaries): the help-judging rules per framework, the
version-pin check, the plans, and the whole lane run for real against fake CLIs
in a local stand-in for the sandbox. No Daytona, no network.

Every judging test encodes a false "your docs are wrong" the lane must not
report, or a real one it must - in the CLI's own words.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from docrot.acquire.binaries import Asset, CliRelease
from docrot.config import Config
from docrot.models import Snippet
from docrot.verify import cli, clihelp, npm
from docrot.verify.clihelp import CliCheck
from tests.fakes import run_async


def _snip(code: str, sid: str = "s1", lang: str = "bash", tier: str = "structural") -> Snippet:
    return Snippet(id=sid, page="p", lang=lang, code=textwrap.dedent(code).strip(), tier=tier)  # type: ignore[arg-type]


def _h(out: str, code: int = 0) -> dict:
    return {"code": code, "out": out}


# ----------------------------------------------------------- canned help, per framework
CLAP_ROOT = """\
Usage: tool [OPTIONS] <COMMAND>

Commands:
  run    Run the thing
  build  Build the thing
  help   Print this message or the help of the given subcommand(s)

Options:
      --verbose  Chatty
  -h, --help     Print help
  -V, --version  Print version
"""
CLAP_RUN = """\
Usage: tool run [OPTIONS]

Options:
      --ci    CI mode
      --fast  Skip checks
  -h, --help  Print help
"""
CLAP_UNKNOWN_SUB = ("error: unrecognized subcommand 'deploy'\n\n  tip: a similar subcommand exists: 'build'\n\n"
                    "Usage: tool [OPTIONS] <COMMAND>\n\nFor more information, try '--help'.\n")
CLAP_UNKNOWN_FLAG = ("error: unexpected argument '--bogus' found\n\n  tip: a similar argument exists: '--ci'\n\n"
                     "Usage: tool run [OPTIONS]\n\nFor more information, try '--help'.\n")
COBRA_ROOT = """\
A tool.

Usage:
  tool [command]

Available Commands:
  run         Run it
  build       Build it

Flags:
  -h, --help      help for tool
      --verbose   chatty
"""
COBRA_UNKNOWN_SUB = 'Error: unknown command "deploy" for "tool"\nRun \'tool --help\' for usage.\n'
COBRA_UNKNOWN_FLAG = "Error: unknown flag: --bogus\nUsage:\n  tool run [flags]\n"
ARGPARSE_ROOT = """\
usage: tool [-h] {run,build} ...

positional arguments:
  {run,build}
    run        run it
    build      build it

options:
  -h, --help   show this help message and exit
"""
ARGPARSE_UNKNOWN_SUB = ("usage: tool [-h] {run,build} ...\ntool: error: argument command: "
                        "invalid choice: 'deploy' (choose from 'run', 'build')\n")
ARGPARSE_UNKNOWN_FLAG = "usage: tool run [-h] [--ci]\ntool run: error: unrecognized arguments: --bogus\n"
CLICK_UNKNOWN_SUB = "Usage: tool [OPTIONS] COMMAND [ARGS]...\nTry 'tool --help' for help.\n\nError: No such command 'deploy'.\n"
CLICK_UNKNOWN_FLAG = "Usage: tool run [OPTIONS]\nTry 'tool run --help' for help.\n\nError: No such option: --bogus\n"
HAND_UNKNOWN_FLAG = "Error: unknown flag: --bogus\nUsage: tool [OPTIONS] [--database PATH] [QUERY]\n"
HAND_UNEXPECTED = "Error: unexpected argument to extension: bogus\nUsage: tool extension init <name>\n"


@pytest.mark.parametrize("out", [CLAP_UNKNOWN_SUB, COBRA_UNKNOWN_SUB, ARGPARSE_UNKNOWN_SUB, CLICK_UNKNOWN_SUB,
                                 HAND_UNEXPECTED.replace("bogus", "deploy"),
                                 "error: unexpected argument 'deploy' found\n"])
def test_every_framework_says_in_its_own_words_that_a_command_is_unknown(out):
    assert clihelp.unknown_command_line(out, "deploy")


def test_a_complaint_about_some_other_token_is_not_about_our_word():
    assert clihelp.unknown_command_line(CLAP_UNKNOWN_SUB, "run") == ""
    assert clihelp.unknown_command_line(COBRA_UNKNOWN_SUB, "build") == ""
    assert clihelp.unknown_command_line(CLAP_ROOT, "deploy") == ""


@pytest.mark.parametrize("out", [CLAP_UNKNOWN_FLAG, COBRA_UNKNOWN_FLAG, ARGPARSE_UNKNOWN_FLAG,
                                 CLICK_UNKNOWN_FLAG, HAND_UNKNOWN_FLAG,
                                 "flag provided but not defined: -bogus\n".replace("-bogus", "--bogus"),
                                 "error: Found argument '--bogus' which wasn't expected\n",
                                 "unrecognized option '--bogus'\n"])
def test_every_framework_says_in_its_own_words_that_a_flag_is_unknown(out):
    assert "--bogus" in clihelp.unknown_flag_line(out, "--bogus")
    assert clihelp.unknown_flag_line(out, "--ci") == ""              # not about --ci
    assert clihelp.unknown_flag_line(out, "--bog") == ""             # a prefix is another flag


def test_a_usage_block_that_merely_lists_the_flag_is_not_a_rejection():
    assert clihelp.unknown_flag_line(CLAP_RUN, "--ci") == ""
    assert clihelp.unknown_flag_line("Options:\n  --ci  CI mode\n", "--ci") == ""


def _walk(root: str, words: list[str], **subs: str) -> clihelp.Walk:
    helps = {clihelp.help_key("tool", []): _h(root)}
    for path, text in subs.items():
        helps[clihelp.help_key("tool", path.split("_"))] = _h(text, 2 if "error" in text.lower() else 0)
    return clihelp.walk("tool", words, helps)


def test_clap_names_the_unknown_subcommand_and_the_walk_stops_there():
    w = _walk(CLAP_ROOT, ["deploy"], deploy=CLAP_UNKNOWN_SUB)
    assert w.missing_word == "deploy" and "unrecognized subcommand 'deploy'" in w.missing_line and not w.inferred
    w = _walk(CLAP_ROOT, ["run"], run=CLAP_RUN)
    assert w.resolved == ["run"] and not w.missing_word and "--ci" in str(w.help)


def test_cobra_argparse_and_click_unknown_commands_are_findings_too():
    for root, bad in ((COBRA_ROOT, COBRA_UNKNOWN_SUB), (ARGPARSE_ROOT, ARGPARSE_UNKNOWN_SUB),
                      (CLAP_ROOT, CLICK_UNKNOWN_SUB)):
        w = _walk(root, ["deploy"], deploy=bad)
        assert w.missing_word == "deploy", bad


# ---------------------------------------------- unknown command versus a positional
HAND_ROOT = """\
Usage: hand [OPTIONS] [--database PATH] [QUERY]
       hand <COMMAND> [ARGS]

Commands:
  query          Run a statement session
  clone          Copy a database into a new store and exit
  info           Print store metadata
  extension      Scaffold, install, check, list, or remove command extensions
  skill          Install, inspect, or remove the embedded skill bundle

Options:
  -h, --help    Show this help and exit
  -d, --database PATH  Database directory
"""
HAND_EXT = """\
Usage: hand extension init <name> [--lang sh|py|ts|rust]
       hand extension install <name> [--from DIR]
       hand extension list [--available]
       hand extension remove <name>

extension manages the extensions.

Actions:
  init <name>     Scaffold a package directory
  install <name>  Install a package
"""
HAND_SKILL = "Usage: hand skill install|remove|list|check [--agent NAME] [--force]\n\nskill manages the bundle.\n"


def _hand(words: list[str]) -> clihelp.Walk:
    helps = {clihelp.help_key("hand", []): _h(HAND_ROOT)}
    for i in range(1, len(words) + 1):
        parent = words[:i - 1]
        text = {"extension": HAND_EXT, "skill": HAND_SKILL}.get(words[0], HAND_ROOT)
        helps[clihelp.help_key("hand", words[:i])] = _h(text if i >= 1 and words[0] in ("extension", "skill")
                                                         else HAND_ROOT)
        del parent
    return clihelp.walk("hand", words, helps)


def test_a_bare_word_where_the_cli_takes_a_free_form_query_is_not_a_missing_command():
    w = _hand(["whatever"])
    assert not w.missing_word                  # `hand whatever` is a legal [QUERY]


def test_a_word_one_slip_from_a_command_is_called_a_typo_even_where_a_query_fits():
    w = _hand(["querry"])
    assert w.missing_word == "querry" and w.inferred and "did you mean 'query'?" in w.missing_line
    assert not _hand(["info"]).missing_word


def test_actions_spelled_in_the_usage_lines_decide_where_the_help_prints_the_same_text():
    w = _hand(["extension", "instal"])
    assert w.resolved == ["extension"] and w.missing_word == "instal" and w.inferred
    assert "check" not in w.missing_line and "did you mean 'install'?" in w.missing_line
    w = _hand(["extension", "install"])
    assert w.resolved == ["extension", "install"] and not w.missing_word
    w = _hand(["skill", "bogus"])
    assert w.missing_word == "bogus" and "install, list, remove" in w.missing_line.replace("check, ", "")
    assert not _hand(["skill", "check"]).missing_word


def test_usage_commands_reads_alternatives_and_gives_up_where_a_value_fits():
    assert clihelp.usage_commands(HAND_EXT, ["hand", "extension"]) == {"init", "install", "list", "remove"}
    assert clihelp.usage_commands(HAND_SKILL, ["hand", "skill"]) == {"install", "remove", "list", "check"}
    assert clihelp.usage_commands(HAND_ROOT, ["hand"]) == set()                   # [OPTIONS] / [QUERY]
    assert clihelp.usage_commands("Usage: hand run <name> [flags]\n", ["hand", "run"]) == set()
    assert clihelp.usage_commands("Usage: hand extension init <name>\n       hand extension <thing>\n",
                                  ["hand", "extension"]) == set()


def test_placeholders_in_documented_commands_are_values_not_subcommands():
    for word in ("[STATEMENT]", "<name>", "SOURCE", "on|off|status", "./db", "7474", "{x}", "$HOME"):
        assert clihelp.value_like(word), word
    for word in ("query", "git-to-graph", "install"):
        assert not clihelp.value_like(word), word


def test_commands_that_define_their_own_flags_are_recognised():
    assert clihelp.open_flags("Usage: hand run <name> [flags]\n\nRun it.\n")
    assert clihelp.open_flags("Usage: hand run <name> [-d PATH] [--<flag> VALUE ...]\n")
    assert not clihelp.open_flags(CLAP_RUN) and not clihelp.open_flags(HAND_ROOT)


# --------------------------------------------------------------------- versions
@pytest.mark.parametrize("wanted,out,state", [
    ("1.1.0", "Relate v1.1.0\nLicense: Apache 2.0 (binary)\n", "ok"),
    ("v1.1.0", "relate 1.1.0 (rustc 1.75.0)", "ok"),
    ("1.2", "tool 1.2.0", "ok"),                                       # 1.2 == 1.2.0
    ("1.1.0", "Relate v1.2.0\nLicense: Apache 2.0 (binary)\n", "mismatch"),     # a pin that was ignored
    ("2.40.0", "gh version 2.39.1 (2023-12-13)\nhttps://github.com/cli/cli/releases/tag/v2.39.1", "mismatch"),
    ("1.1.0", "Relate\nLicense: Apache 2.0\n", "unknown"),             # `2.0` is a license, not a version
    ("1.1.0", "", "unknown"),
    ("1.0.0-rc.1", "tool 1.0.0-rc.1", "ok"),
])
def test_the_binary_must_report_the_version_that_was_asked_for(wanted, out, state):
    got, note = cli.check_version(wanted, out)
    assert got == state
    if state == "mismatch":
        assert "not the requested" in note and "cannot be checked" in note


# ----------------------------------------------------------------------- plans
def test_plan_keeps_this_tools_invocations_under_its_names_and_not_the_npm_package():
    s = _snip("""
        relate query --database ./team.rl < team.cypher
        npx relate query --json
        rl info --database ./x
        git status
        echo hi | relate telemetry status
    """)
    plan = cli.plan_snippet(s, "relate", ("rl",))
    assert [(c.bin, c.inv.args, c.inv.flags) for c in plan.clis] == [
        ("relate", ("query",), ("--database",)), ("relate", ("info",), ("--database",)),
        ("relate", ("telemetry", "status"), ())]
    assert cli.plan_snippet(_snip("relate query", lang="python"), "relate").clis == []


def test_every_distinct_help_is_asked_once_however_many_snippets_repeat_it():
    plans = [cli.plan_snippet(_snip(f"relate query --database ./d{i}.rl\nrelate extension install greet",
                                    sid=f"s{i}"), "relate") for i in range(40)]
    reqs = cli.help_requests(plans, "relate")
    assert sorted(r["argv"] for r in reqs) == sorted([
        ["relate", "--help"], ["relate", "query", "--help"], ["relate", "extension", "--help"],
        ["relate", "extension", "install", "--help"], ["relate", "extension", "install", "greet", "--help"]])
    assert len({r["key"] for r in reqs}) == len(reqs)


def test_help_fallbacks_are_only_asked_where_dash_dash_help_gave_nothing():
    plans = [cli.plan_snippet(_snip("tool run --ci"), "tool")]
    helps = {clihelp.help_key("tool", []): _h("nope", 2), clihelp.help_key("tool", ["run"]): _h("x")}
    fb = cli.fallback_requests(plans, "tool", helps)
    assert [r["argv"] for r in fb] == [["tool", "-h"]]                 # only the root; `run` hangs off it
    helps = {clihelp.help_key("tool", []): _h(CLAP_ROOT), clihelp.help_key("tool", ["run"]): _h("nope", 2)}
    assert [r["argv"] for r in cli.fallback_requests(plans, "tool", helps)] == \
        [["tool", "run", "-h"], ["tool", "help", "run"]]
    merged = cli.merge_fallbacks(helps, {clihelp.help_key("tool", ["run"]) + "\x1f-h": _h(CLAP_RUN)})
    assert merged[clihelp.help_key("tool", ["run"])]["via"] == "-h"
    assert cli.merge_fallbacks(helps, {clihelp.help_key("tool", ["run"]) + "\x1f-h": _h("", 1)}) == helps


# ------------------------------------------------- hidden versus unknown flags
def _doc(own: str, ask_out: str | None, strings: dict | None = None, cmd_words=("run",)) -> dict:
    helps = {clihelp.help_key("tool", []): _h(CLAP_ROOT), clihelp.help_key("tool", list(cmd_words)): _h(own)}
    asks = {} if ask_out is None else {cli.ask_key("tool " + " ".join(cmd_words), "--x"): _h(ask_out, 2)}
    return {"helps": helps, "asks": asks, "strings": strings or {}, "notes": []}


def _says(doc: dict, flag: str = "--x") -> dict | None:
    return cli.make_flag_says(doc, "tool")("tool run", flag)


def test_a_flag_the_cli_rejects_is_a_finding_in_the_cli_own_words():
    s = _says(_doc(CLAP_RUN, CLAP_UNKNOWN_FLAG.replace("--bogus", "--x")))
    assert s and s["state"] == "rejected" and "unexpected argument '--x' found" in s["line"]


def test_a_flag_the_cli_accepts_silently_is_hidden_not_wrong():
    # clap `hide = true`: `tool run --x --help` just prints help, and the binary names the flag
    s = _says(_doc(CLAP_RUN, CLAP_RUN, {"--x": True}))
    assert s and s["state"] == "unknown" and "binary contains the flag" in s["why"]
    # one that answers about the flag itself (a missing value) has plainly accepted it
    s = _says(_doc(CLAP_RUN, "error: a value is required for '--x <X>' but none was supplied\n"))
    assert s and s["state"] == "accepted"


def test_help_that_wins_over_a_bad_flag_is_settled_by_the_binary_itself():
    s = _says(_doc(CLAP_RUN, CLAP_RUN, {"--x": False}))
    assert s and s["state"] == "absent"
    s = _says(_doc(CLAP_RUN, CLAP_RUN, {}))                           # binary could not be searched
    assert s and s["state"] == "unknown"


def test_an_unanswered_ask_and_an_extension_defined_flag_are_never_findings():
    assert (_says(_doc(CLAP_RUN, None)) or {})["state"] == "unknown"
    own = "Usage: tool run <name> [flags]\n\nRun an installed extension.\n"
    s = _says(_doc(own, CLAP_UNKNOWN_FLAG.replace("--bogus", "--x")))
    assert s and s["state"] == "unknown" and "extension defines them" in s["why"]


def _judge(code: str, doc: dict, name: str = "tool") -> tuple[str, dict]:
    plan = cli.plan_snippet(_snip(code), name)
    status, body = cli.judge(plan, doc, name, {"lane": "cli", "tool": name})
    return status, json.loads(body)


def test_judging_a_snippet_uses_the_help_the_asks_and_the_binary():
    doc = _doc(CLAP_RUN, CLAP_UNKNOWN_FLAG.replace("--bogus", "--x"))
    status, b = _judge("tool run --ci --fast", doc)
    assert status == "pass" and b["cli"] == []
    status, b = _judge("tool run --ci --x", doc)
    assert status == "fail" and b["cli"][0]["check"] == "flag" and b["cli"][0]["subject"] == "tool run --x"
    assert "unexpected argument '--x' found" in b["cli"][0]["message"]
    assert b["failed"][0].startswith("tool run --x:") and b["probe"]["lane"] == "cli"

    helps = {clihelp.help_key("tool", []): _h(CLAP_ROOT), clihelp.help_key("tool", ["deploy"]): _h(CLAP_UNKNOWN_SUB, 2)}
    status, b = _judge("tool deploy --fast", {"helps": helps})
    assert status == "fail" and b["cli"][0]["check"] == "command" and b["cli"][0]["subject"] == "tool deploy"
    assert "unrecognized subcommand 'deploy'" in b["cli"][0]["message"]


def test_a_cli_that_would_not_answer_is_unverified_with_the_reason():
    helps = {clihelp.help_key("tool", []): {"timedOut": True}}
    status, b = _judge("tool run --ci", {"helps": helps})
    assert status == "unverified" and "timed out" in b["notes"][0] and b["cli"] == []
    status, b = _judge("echo nothing here", {"helps": {}})
    assert status == "unverified" and "nothing in this snippet" in b["notes"][0]


def test_the_npm_lane_still_judges_through_the_shared_rules():
    # the move changed no name npm.py offers and no verdict it gives
    for name in ("walk", "judge_cli", "flag_present", "command_names", "takes_positionals", "usable",
                 "unknown_command_line", "help_key", "chain_args", "value_like", "CliCheck"):
        assert getattr(npm, name) is getattr(clihelp, name)
    inv = npm.js.invocations("cli deploy --ci", {"cli"})[0]
    helps = {clihelp.help_key("cli", []): _h(CLAP_ROOT), clihelp.help_key("cli", ["deploy"]): _h(CLAP_UNKNOWN_SUB, 2)}
    held, findings, _ = npm.judge_cli(CliCheck(inv, "cli"), helps, "pkg", {"cli": "x"})
    assert held == 1 and findings[0]["check"] == "command"


# ------------------------- what a page says on purpose (Relate's docs, measured)
RELATE_ROOT = """\
Usage: relate [OPTIONS] [--database PATH] [QUERY]
       relate <COMMAND> [ARGS]

Commands:
  query          Run a statement session (oneshot, stdin, or shell)
  clone          Copy a database into a new store and exit
  telemetry      Manage anonymous usage metrics (on, off, status)

Options:
  -h, --help    Show this help and exit
  --read-only[=FRESH]  Open an existing database without writing
"""
RELATE_CLONE = "Usage: relate clone [--wal-dir DIR] SOURCE DEST\n\nclone copies a database.\n"
RELATE_TELEMETRY = "Usage: relate telemetry on|off|status\n\ntelemetry manages anonymous usage metrics.\n"
# a hand-written parser answers an unknown word in the CLI's own words, and names the command it has elsewhere
RELATE_GATED = ("Error: unknown command: agent-feedback. the person's feedback surface is 'relate feedback'; "
                "the agent surface is 'relate agent-feedback', and the grant is managed by a person at a terminal. "
                "Run 'relate agent-feedback --help' for more information.\n")


def _relate_doc(**extra) -> dict:
    helps = {clihelp.help_key("relate", []): _h(RELATE_ROOT),
             clihelp.help_key("relate", ["clone"]): _h(RELATE_CLONE),
             clihelp.help_key("relate", ["telemetry"]): _h(RELATE_TELEMETRY),
             clihelp.help_key("relate", ["agent-feedback"]): _h(RELATE_GATED, 2), **extra}
    asks = {cli.ask_key("relate", "--readonly"): _h("Error: unknown flag: --readonly\n", 2),
            cli.ask_key("relate clone", "--wal-dri"): _h(RELATE_CLONE)}
    return {"helps": helps, "asks": asks, "strings": {"--wal-dri": False}, "notes": []}


def _judge_shown(code: str, shown: str = "", doc: dict | None = None) -> tuple[str, dict]:
    s = _snip(code)
    s.shows = shown
    status, body = cli.judge(cli.plan_snippet(s, "relate"), doc or _relate_doc(), "relate", {"lane": "cli"})
    return status, json.loads(body)


def test_a_flag_the_page_shows_being_rejected_is_the_documented_error_not_rot():
    code = 'relate --readonly "RETURN 1 AS ok"\necho "exit=$?"'
    status, b = _judge_shown(code)
    assert status == "fail" and b["cli"][0]["subject"] == "relate --readonly"       # unexplained, it is a finding
    shown = "Error: unknown flag: --readonly\nDid you mean '--read-only'?\nexit=2"
    status, b = _judge_shown(code, shown)
    assert status != "fail" and b["cli"] == []
    assert "shows --readonly being rejected on purpose" in b["notes"][0]


def test_a_command_the_page_shows_failing_is_not_a_missing_command():
    helps = {clihelp.help_key("relate", ["telemetry", "maybe"]): _h(RELATE_TELEMETRY)}
    code = "relate telemetry maybe\necho exit=$?"
    shown = "Error: unexpected argument to telemetry: maybe\nUsage: relate telemetry on|off|status\nexit=2"
    status, b = _judge_shown(code, "", _relate_doc(**helps))
    assert status == "fail" and b["cli"][0]["check"] == "command"
    status, b = _judge_shown(code, shown, _relate_doc(**helps))
    assert status == "pass" and b["cli"] == [] and "failing on purpose" in b["notes"][0]


def test_the_shown_error_must_name_the_very_token_not_just_look_like_an_error():
    code = 'relate --readonly "RETURN 1"'
    assert _judge_shown(code, "Error: something broke\nexit=1")[0] == "fail"
    assert _judge_shown(code, "relate --readonly is great\nand mentioned without an error")[0] == "fail"
    assert _judge_shown(code, "Error: unknown flag: --read-only-ish")[0] == "fail"       # a longer name is another flag
    assert clihelp.shown_error("Error: unknown flag: --a\n", "") is False
    assert clihelp.shown_error("", "--a") is False


def test_a_command_the_cli_itself_names_as_its_own_is_gated_by_mode_not_removed():
    status, b = _judge_shown("relate agent-feedback --expected x --observed y")
    assert status == "pass" or status == "unverified"
    assert b["cli"] == [] and "names `relate agent-feedback` as a command it has" in " ".join(b["notes"])
    # a help hint that merely echoes the word is not the CLI naming a command
    echo = "Error: unknown command: nope. Run 'relate nope --help' for more information.\n"
    helps = {clihelp.help_key("relate", ["nope"]): _h(echo, 2)}
    status, b = _judge_shown("relate nope", "", _relate_doc(**helps))
    assert status == "fail" and b["cli"][0]["subject"] == "relate nope"


@pytest.mark.parametrize("line", [
    "RELATE_AGENT_MODE=1 relate clone a b",
    "env RELATE_TELEMETRY=0 relate telemetry status",
    "$ XDG_CONFIG_HOME=/x relate telemetry off",
    "cd x && RELATE_AGENT_MODE=1 relate clone a b",
])
def test_a_command_run_under_its_own_environment_is_not_judged_without_it(line):
    status, b = _judge_shown(line + " --bogus-flag")
    assert status == "unverified" and b["cli"] == [] and "can change the commands and flags" in b["notes"][0]


def test_exporting_the_tools_own_variables_taints_the_later_commands_too():
    status, b = _judge_shown("export RELATE_AGENT_MODE=1\nrelate agent-feedback --expected x")
    assert status == "unverified" and b["cli"] == [] and "sets RELATE_*" in b["notes"][0]
    status, b = _judge_shown("export FOO=1\nrelate clone --wal-dri a b")
    assert status == "fail"                                   # an unrelated variable changes nothing


def test_a_plain_command_after_an_unrelated_one_is_still_judged():
    inv = cli.js.invocations("FOO=1 ls && relate clone a", {"relate"})[0]
    assert clihelp.env_prefixed(inv) == ""
    assert clihelp.env_prefixed(cli.js.invocations("A=1 B=2 relate clone a", {"relate"})[0]) == "A=1 B=2"


def test_the_fences_after_a_command_are_what_it_is_said_to_print():
    from docrot.extract.fences import parse
    from docrot.models import Page
    text = ("Mistyped:\n\n```bash verify\nrelate --readonly x\n```\n\n```text\nError: unknown flag: --readonly\n```\n\n"
            "```text\nexit=2\n```\n\nA paragraph.\n\n```text\nnot attached\n```\n\n"
            "```json\n{}\n```\n\n```text\nnot attached either\n```\n")
    snips = parse([Page(url="u", path="/p", text=text)])
    assert snips[0].shows == "Error: unknown flag: --readonly\nexit=2"
    assert all(s.shows == "" for s in snips[1:])


# ------------------------------------------------------------------ no key, no sandbox
def test_without_a_daytona_key_everything_is_unverified_and_says_so():
    logs: list[str] = []
    snips = [_snip("relate info", sid="a"), _snip("relate info", sid="b", tier="execute")]
    got = run_async(cli.run_cli_matrix(snips, "relate", ["1.0.0", "1.1.0"], Config(), logs.append,
                                       repo_url="https://github.com/relatedb/relate"))
    assert len(got) == 4 and {r.status for r in got} == {"unverified"}
    assert {(r.id, r.version) for r in got} == {(i, v) for i in "ab" for v in ("1.0.0", "1.1.0")}
    assert logs == ["  no DAYTONA_API_KEY - skipping execution, marking unverified"]


def test_the_release_for_a_version_is_found_whatever_the_v_prefix():
    idx = [CliRelease("1.1.0", "v1.1.0"), CliRelease("1.2.0", "v1.2.0")]
    assert cli._release_for(idx, "v1.2.0") is idx[1] and cli._release_for(idx, "1.1") is idx[0]
    assert cli._release_for(idx, "9.9.9") is None and cli._release_for([], "1.0.0") is None


# --------------------------------------------- the lane, run for real against fake CLIs
FAKE_CLI = r'''#!/usr/bin/env python3
import sys
STYLE = "__STYLE__"
VERSION = "__VERSION__"
args = sys.argv[1:]
ROOT = """Usage: tool [OPTIONS] <COMMAND>

Commands:
  run    Run the thing
  build  Build the thing

Options:
      --verbose  Chatty
  -h, --help     Print help
"""
RUN = """Usage: tool run [OPTIONS]

Options:
      --ci    CI mode
      --fast  Skip checks
  -h, --help  Print help
"""
KNOWN = {"--ci", "--fast", "--verbose", "--secret", "--help", "-h", "--version"}
if args == ["--version"]:
    print("tool " + VERSION); sys.exit(0)
if STYLE == "dashh" and "--help" in args:
    print("nope"); sys.exit(2)
helping = "--help" in args or "-h" in args
words = [a for a in args if not a.startswith("-")]
if words[:1] == ["help"]:
    words, helping = words[1:], True
sub = words[:1]
if sub and sub[0] not in ("run", "build"):
    if STYLE == "clap":
        print("error: unrecognized subcommand '%s'\n\nUsage: tool [OPTIONS] <COMMAND>" % sub[0], file=sys.stderr); sys.exit(2)
    # `hand`: swallow it as a positional
    sub = []
if STYLE == "clap":
    for a in args:
        if a.startswith("--") and a not in KNOWN:
            print("error: unexpected argument '%s' found\n\nUsage: tool run [OPTIONS]" % a, file=sys.stderr); sys.exit(2)
if helping:
    print(RUN if sub == ["run"] else ROOT); sys.exit(0)
print("ran"); sys.exit(0)
'''


class _R:
    def __init__(self, code: int, text: str):
        self.exit_code, self.result = code, text


class _Process:
    def __init__(self, home: Path):
        self.home = home

    async def exec(self, cmd: str, cwd: str | None = None, timeout: int | None = None):
        if cmd.startswith("uname -sm"):
            return _R(0, f"Linux x86_64\n{self.home}\n")
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                           env={**os.environ, "HOME": str(self.home)})
        return _R(p.returncode, p.stdout + p.stderr)


class _Fs:
    async def upload_files(self, files):
        for f in files:
            Path(f.destination).parent.mkdir(parents=True, exist_ok=True)
            Path(f.destination).write_bytes(f.source)


class FakeSandbox:
    def __init__(self, home: Path):
        self.process, self.fs = _Process(home), _Fs()


class FakeDaytona:
    def __init__(self):
        self.deleted: list[object] = []

    async def delete(self, sb):
        self.deleted.append(sb)


needs_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("curl", "bash", "python3")), reason="curl, bash and python3 needed")


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """A fake sandbox rooted in tmp_path, and a way to publish a fake CLI as a release asset."""
    root = tmp_path / "root"
    monkeypatch.setattr(cli, "ROOT", str(root))
    home = tmp_path / "home"
    home.mkdir()
    sb = FakeSandbox(home)

    async def new_sandbox(daytona, cfg):
        return sb

    monkeypatch.setattr(cli.runner, "_new_sandbox", new_sandbox)

    class Lane:
        def __init__(self):
            self.daytona = FakeDaytona()
            self.sandbox = sb
            self.home = home

        def release(self, style: str, version: str, name: str = "tool-linux-amd64") -> CliRelease:
            d = tmp_path / f"assets-{style}-{version}"
            d.mkdir(exist_ok=True)
            f = d / name
            f.write_text(FAKE_CLI.replace("__STYLE__", style).replace("__VERSION__", version))
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
            return CliRelease(version, f"v{version}", assets=[Asset(name, f.as_uri())])

        def run(self, snips: list[Snippet], version: str, rel: CliRelease | None, install: str = "",
                logs: list[str] | None = None) -> list:
            sem = asyncio.Semaphore(1)
            return run_async(cli.run_version(self.daytona, snips, "tool", version, rel, ("tool",), install,
                                             Config(), sem, (logs.append if logs is not None else print)))

    return Lane()


def _by_id(results) -> dict:
    return {r.id: r for r in results}


@needs_tools
def test_a_clap_style_cli_is_installed_probed_and_judged(lane):
    logs: list[str] = []
    snips = [_snip("tool run --ci --fast", "ok"), _snip("tool run --bogus", "badflag"),
             _snip("tool deploy", "badcmd"), _snip("tool run --secret --ci", "hidden"),
             _snip("echo unrelated", "other"), _snip("tool run", "exec", tier="execute")]
    got = _by_id(lane.run([s for s in snips if s.tier == "structural"], "1.4.0", lane.release("clap", "1.4.0"),
                          logs=logs))
    assert got["ok"].status == "pass" and got["other"].status == "unverified"
    assert got["badflag"].status == "fail" and got["badcmd"].status == "fail"
    bf = json.loads(got["badflag"].stderr)
    assert bf["cli"][0]["check"] == "flag" and "unexpected argument '--bogus' found" in bf["cli"][0]["message"]
    assert bf["probe"]["tag"] == "v1.4.0" and bf["probe"]["installed"] == "tool 1.4.0"
    assert "unrecognized subcommand 'deploy'" in json.loads(got["badcmd"].stderr)["cli"][0]["message"]
    # clap accepts the hidden flag silently; the binary mentions it (it is in this very script): not judged
    hs = json.loads(got["hidden"].stderr)
    assert got["hidden"].status == "pass" and any("--secret" in n for n in hs["notes"])
    assert lane.daytona.deleted == [lane.sandbox]                       # cleaned up
    assert "5 snippets" in logs[0] and "distinct --help" in logs[0] and "tool 1.4.0" in logs[0]


@needs_tools
def test_a_help_wins_cli_is_settled_by_searching_the_binary(lane):
    snips = [_snip("tool run --ci", "ok"), _snip("tool run --nonesuch", "nope"), _snip("tool run --secret", "hid")]
    got = _by_id(lane.run(snips, "2.0.0", lane.release("hand", "2.0.0")))
    assert got["ok"].status == "pass" and got["hid"].status == "pass"
    nope = json.loads(got["nope"].stderr)
    assert got["nope"].status == "fail" and "nowhere in the binary" in nope["cli"][0]["message"]


@needs_tools
def test_a_cli_without_dash_dash_help_is_asked_with_dash_h(lane):
    logs: list[str] = []
    got = _by_id(lane.run([_snip("tool run --ci --zzz", "a")], "1.0.0", lane.release("dashh", "1.0.0"), logs=logs))
    # `--help` prints "nope" for the root and everything below it; -h is what works, one level per round
    assert got["a"].status == "pass" and "3 fallbacks" in logs[0]
    plans = [cli.plan_snippet(_snip("tool run --ci"), "tool")]
    fb = cli.fallback_requests(plans, "tool", {clihelp.help_key("tool", []): _h("nope", 2)})
    assert fb and fb[0]["argv"] == ["tool", "-h"]


@needs_tools
def test_an_install_that_ignores_the_pin_blocks_the_version_instead_of_reporting_it(lane):
    # the asset says 1.5.0 whatever tag it was published under
    rel = lane.release("clap", "1.5.0")
    rel.label, rel.tag = "1.4.0", "v1.4.0"
    got = lane.run([_snip("tool run --ci", "a")], "1.4.0", rel)
    assert got[0].status == "blocked" and got[0].blocked_by == "install"
    assert "reports 1.5.0, not the requested 1.4.0" in got[0].stderr
    assert lane.daytona.deleted == [lane.sandbox]


@needs_tools
def test_a_binary_that_prints_no_version_is_probed_with_a_note_not_blocked(lane):
    rel = lane.release("clap", "1.0.0")
    p = Path(rel.assets[0].url.removeprefix("file://"))
    p.write_text(p.read_text().replace('print("tool " + VERSION)', 'print("tool")'))
    got = lane.run([_snip("tool run --ci", "a")], "1.0.0", rel)
    assert got[0].status == "pass"
    assert any("pin to 1.0.0 could not be confirmed" in n for n in json.loads(got[0].stderr)["notes"])


@needs_tools
def test_a_release_with_no_usable_asset_falls_back_to_the_callers_command_and_then_blocks(lane):
    snip = [_snip("tool run --ci", "a")]
    rel = CliRelease("1.0.0", "v1.0.0", assets=[Asset("tool-darwin-arm64", "file:///nowhere")])
    got = lane.run(snip, "1.0.0", rel)
    assert got[0].status == "blocked" and "no install command was supplied" in got[0].stderr
    fake = lane.release("clap", "1.0.0")
    cmd = f"mkdir -p $HOME/.local/bin && cp {fake.assets[0].url.removeprefix('file://')} $HOME/.local/bin/tool"
    got = lane.run(snip, "1.0.0", rel, install=cmd)
    assert got[0].status == "pass" and json.loads(got[0].stderr)["probe"]["recipe"].startswith("the install command")
    got = lane.run(snip, "1.0.0", None, install="exit 1")
    assert got[0].status == "blocked" and got[0].blocked_by == "install"


@needs_tools
def test_a_binary_that_cannot_start_here_tries_the_next_asset(lane):
    good = lane.release("clap", "1.0.0", "tool-linux-amd64")
    bad = Path(good.assets[0].url.removeprefix("file://")).parent / "tool-x86_64-unknown-linux-musl"
    bad.write_bytes(b"\x7fELF-not-really")
    bad.chmod(0o755)
    rel = CliRelease("1.0.0", "v1.0.0", assets=[Asset(bad.name, bad.as_uri()), good.assets[0]])
    got = lane.run([_snip("tool run --ci", "a")], "1.0.0", rel)
    assert got[0].status == "pass"
    assert json.loads(got[0].stderr)["probe"]["recipe"] == "release asset tool-linux-amd64"


@needs_tools
def test_a_secret_in_the_output_never_reaches_the_report(lane, monkeypatch):
    rel = lane.release("clap", "1.0.0")
    cfg_secrets = ["s3cr3t-token"]
    monkeypatch.setattr(Config, "secrets", lambda self: cfg_secrets)
    p = Path(rel.assets[0].url.removeprefix("file://"))
    p.write_text(p.read_text().replace('print(RUN if sub == ["run"] else ROOT)',
                                       'print((RUN if sub == ["run"] else ROOT) + "token s3cr3t-token")'))
    got = lane.run([_snip("tool run --ci --zzz", "a")], "1.0.0", rel)
    assert "s3cr3t-token" not in got[0].stderr


def test_the_probe_script_is_valid_python_and_prints_a_fenced_document():
    compile(cli.PROBE_PY, "probe.py", "exec")
    assert cli.parse_probe("noise\n" + cli.MARKER + '\n{"runs": {}}\n' + cli.MARKER + "\ntrailing") == {"runs": {}}
    assert cli.parse_probe("no document") is None
    assert cli.parse_probe(f"{cli.MARKER}\nnot json\n{cli.MARKER}") is None


def test_the_python_running_these_tests_can_run_the_probe(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(cli.PROBE_PY)
    (tmp_path / "payload.json").write_text(json.dumps({
        "requests": [{"key": "k", "argv": [sys.executable, "-c", "print('hi')"]},
                     {"key": "missing", "argv": ["/no/such/binary", "--help"]},
                     {"key": "slow", "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}],
        "helpMs": 1000, "workers": 3, "deadlineMs": 20000}))
    r = subprocess.run([sys.executable, "-I", str(probe), "payload.json"], cwd=tmp_path, capture_output=True,
                       text=True, timeout=60)
    doc = cli.parse_probe(r.stdout)
    assert doc is not None, r.stdout + r.stderr
    assert doc["runs"]["k"]["out"].strip() == "hi" and doc["runs"]["k"]["code"] == 0
    assert doc["runs"]["missing"]["error"]
    assert doc["runs"]["slow"]["timedOut"] is True and doc["runs"]["slow"]["code"] is None
