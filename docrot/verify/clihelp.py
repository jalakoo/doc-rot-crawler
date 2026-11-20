"""Judging a CLI by its own help: the pure half, shared by the npm and the binary lanes.

A documented command line is checked against what the CLI says about itself and
nothing else. `<tool> <words...> --help` is asked once per distinct prefix (the
lane batches and caches them); everything below is canned text in, verdict out,
so it is testable without a sandbox, a Node, or a binary.

The rules, each one a false "your docs are wrong" that cost something to learn:

  * A word is a subcommand only if the CLI treats it as one, and it is missing
    only if the CLI says so in its own words. Frameworks say it differently:
    commander/yargs `unknown command`, clap `unrecognized subcommand 'x'`
    (and `unexpected argument 'x' found` for a word with nowhere to go), cobra
    `unknown command "x" for "tool"`, argparse `invalid choice: 'x'`, click
    `No such command 'x'`, a hand-written parser `unexpected argument to
    clone: x`. A message that quotes some other token is not about our word.
  * A CLI that swallows an unknown word as a positional prints the parent's
    help again, byte for byte. That is not "the command exists" and not "it
    does not": the parent's own command table decides, when it has one.
  * A flag absent from `--help` is not necessarily wrong: frameworks hide
    options (commander `hideHelp`, clap `hide = true`) and still accept them.
    The npm lane greps the package's code; the binary lane asks the CLI
    (`<cmd> --flag --help`: help is a safe request, and clap and cobra reject
    an unknown flag before they print it) and, when the CLI answers only with
    help, searches the binary itself. `judge_cli` takes that evidence as
    `flag_says` and never decides it.
  * Help conventions differ. `--help` is asked first; `-h` and `help <words>`
    are only used when it printed nothing a human would call help.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..extract.javascript import Invocation

MAX_DEPTH = 4           # subcommand words walked per invocation
HELP_CAP = 60_000       # chars of help kept per command
DETAIL_CAP = 300


def help_key(bin_: str, args: list[str] | tuple[str, ...]) -> str:
    return "\x1f".join([bin_, *args])


def value_like(word: str) -> bool:
    """A word that is a path, file, URL, value or placeholder rather than a
    subcommand: `./db`, `7474`, `[STATEMENT]`, `<name>`, `on|off`, `SOURCE`."""
    return (bool(re.search(r"[/.=:\[\]<>{}|$]", word)) or word.isdigit()
            or (len(word) > 1 and word.isupper()))


@dataclass
class CliCheck:
    inv: Invocation
    bin: str                    # the installed bin it resolved to; "" when none
    reason: str = ""            # why it is not checkable / why there is no bin


def chain_args(inv: Invocation) -> list[str]:
    """The subcommand words worth asking the CLI about."""
    out: list[str] = []
    for w in inv.args[:MAX_DEPTH]:
        if value_like(w):
            break
        out.append(w)
    return out


# ------------------------------------------------------------------- judging
# Everything below is pure: canned probe output in, verdict out.

UNKNOWN_COMMAND = re.compile(
    r"(unknown (?:sub)?command|unknown argument|unrecogni[sz]ed (?:sub)?command|invalid command|"
    r"no such command|command[ :]+['\"`]?[\w:. -]*?['\"`]? ?(?:was )?not found|command not found|"
    r"did you mean|not a (?:valid |known )?command|could not find command|"
    r"is not a [\w-]+ command|"
    r"invalid choice|unknown help topic|"                      # argparse; cobra's `help nonsense`
    r"unexpected argument ['\"`]?[\w:.-]+['\"`]? found|"       # clap: a word with nowhere to go
    r"unexpected argument to [\w-]+:)", re.I)                  # a hand-written parser
# What a CLI says about an option it does not have. Each is about ONE token, so
# the judge still demands that the line names the flag we asked about.
UNKNOWN_FLAG = re.compile(
    r"(unexpected argument|unknown (?:long |short )?(?:flag|option)|unknown shorthand flag|"
    r"no such option|unrecogni[sz]ed (?:option|argument|arguments|flag)|invalid option|"
    r"illegal option|flag provided but not defined|found argument|"
    r"(?:option|flag)[^\n]{0,40}(?:is |was )?not (?:recogni[sz]ed|a valid|known|supported))", re.I)
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


def unknown_flag_line(out: str, flag: str) -> str:
    """The line in which the CLI says it does not accept `flag`, or "".

    Only a line that names this very flag counts: `unexpected argument '--other'`
    is about something else, and a usage block that merely lists the flag is not
    a complaint."""
    pat = re.compile(rf"(?<![\w-]){re.escape(flag)}(?![\w-])")
    for line in str(out).splitlines():
        if UNKNOWN_FLAG.search(line) and pat.search(line):
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


_ACTION = re.compile(r"[a-z][\w-]*(?:\|[a-z][\w-]*)*")
OPEN_FLAGS = re.compile(r"--<[^>]+>|\[flags\]|\[--flags?\b|\[--\*\]", re.I)


def _usage_block(text: str) -> list[str]:
    """The first `Usage:` line and the indented lines that continue it."""
    lines: list[str] = []
    for ln in str(text).splitlines():
        if USAGE_LINE.match(ln) or (lines and ln.strip() and ln[0].isspace()):
            lines.append(ln)
        elif lines:
            break
    return lines


def usage_commands(text: str, path: list[str]) -> set[str]:
    """The subcommand words the usage lines of `path`'s help spell out:

        Usage: relate extension init <name> [--lang L]
               relate extension install <name> ...      -> {init, install, ...}
        Usage: relate skill install|remove|list|check   -> {install, remove, ...}

    Help with no command table (a hand-written `Actions:` list, or none) still
    names its actions in its synopsis. Empty when ANY usage line has something
    else where the action goes (`[STATEMENT]`, `<name>`): there the CLI takes
    a free-form word, and a word that is not listed may simply be a value."""
    found: set[str] = set()
    for ln in _usage_block(text):
        toks = re.sub(r"^\s*usage:?\s*", "", ln, flags=re.I).split()
        if toks[:len(path)] != path or len(toks) <= len(path):
            continue
        nxt = toks[len(path)]
        if not _ACTION.fullmatch(nxt):
            return set()
        found.update(nxt.split("|"))
    return found


def open_flags(text: str) -> bool:
    """Whether the usage says the command takes flags of its own making
    (`relate run <name> [--<flag> VALUE ...]`: an extension defines them), so
    a flag missing from this help is not evidence of anything."""
    return any(OPEN_FLAGS.search(ln) for ln in _usage_block(text))


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def near_miss(word: str, names: set[str]) -> str:
    """The listed command `word` is a typo of (one edit for a short word, two
    for a long one), or "". For a CLI that takes free-form positionals, a bare
    word that is not a command could be a value - unless it is one slip away
    from a command, which no value is."""
    if len(word) < 4:
        return ""
    limit = 1 if len(word) <= 6 else 2
    best = min(names, key=lambda n: _distance(word.lower(), n.lower()), default="")
    return best if best and 0 < _distance(word.lower(), best.lower()) <= limit else ""


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
            # own command table or synopsis is complete and takes no free-form
            # arguments, in which case the CLI has told us, in its own words,
            # what it knows.
            ptext = str(parent.get("out") if parent else "")
            path = [bin_, *w.resolved]
            spelled = usage_commands(ptext, path)
            listed = command_names(ptext, bin_) | spelled
            if word in listed and word.lower() != "help":
                w.resolved.append(word)       # named by the help itself; it has no help of its own
                continue
            if len(listed) >= 2 and word.lower() != "help":
                guess = near_miss(word, listed)
                if spelled or not takes_positionals(ptext) or guess:
                    w.missing_word, w.inferred = word, True
                    w.missing_line = (f"'{word}' is not among the commands `{' '.join(path)} --help` lists "
                                      f"({', '.join(sorted(listed))[:200]})"
                                      + (f"; did you mean '{guess}'?" if guess else ""))
            return w                  # swallowed as a positional argument
        w.resolved.append(word)
        w.help = parent = h
    return w


def _excerpt(text: str, limit: int = DETAIL_CAP) -> str:
    lines = [ln.rstrip() for ln in str(text).strip().splitlines() if ln.strip()]
    return "\n".join(lines)[:limit]


# ------------------------------------------------ what a page says on purpose
# Three ways a documented command is "wrong" in the binary's eyes and still not
# rot. Each was a false finding on Relate's docs (measured: 9 of the 9 failures
# on the newest release were one of these), and each prefers saying nothing.

ENV_WORD = re.compile(r"^[A-Za-z_]\w*=")
OPERATORS = frozenset({"&&", "||", ";", "|", "&"})
ERROR_LINE = re.compile(r"(error|unknown|unexpected|unrecogni[sz]ed|invalid|no such|not found|not a valid|"
                        r"syntax|usage|did you mean|requires)", re.I)
HELP_HINT = re.compile(r"""['"`][^'"`]*(?:--help|\s-h)['"`]""")


def env_prefixed(inv: Invocation) -> str:
    """The `NAME=value` words in front of the command on its own line, "" when
    there are none. `RELATE_AGENT_MODE=1 relate agent-feedback ...` is a command
    of another mode: the CLI offers different commands and flags under it, and a
    probe made without the variable cannot say what the docs' reader sees."""
    words: list[str] = []
    for w in str(inv.text).split():
        if w in OPERATORS:
            words = []
        elif w == inv.bin or w.endswith("/" + inv.bin):
            break
        else:
            words.append(w)
    else:
        return ""
    return " ".join(w for w in words if ENV_WORD.match(w))


def names_the_command(line: str, bin_: str, word: str) -> bool:
    """Does the CLI's own complaint about `word` name `<bin> <word>` as a command
    it has? `unknown command: agent-feedback ... the agent surface is 'relate
    agent-feedback'` is a command that exists in another mode, not one that was
    removed. A `Run 'relate agent-feedback --help'` hint only echoes the input."""
    text = HELP_HINT.sub(" ", line)
    return bool(re.search(rf"(?<![\w-]){re.escape(bin_)}\s+{re.escape(word)}(?![\w-])", text))


def shown_error(shown: str, token: str) -> bool:
    """Does the page print an error that names `token` right after the command?
    A reference page that shows `relate --bogus` failing with `Error: unknown
    flag: --bogus` documents the error; the flag being unknown is the point."""
    if not shown or not token:
        return False
    pat = re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w-])")
    return any(ERROR_LINE.search(ln) and pat.search(ln) for ln in shown.splitlines())


# What a lane can tell about a flag that `--help` does not list: (command, flag)
# -> {"state": accepted | rejected | absent | unknown, "why": ..., "line": ...}
# or None when it has no evidence. Accepted = a hidden flag; rejected = the CLI
# said so itself; absent = help silent and the flag is nowhere in the binary.
FlagSays = Callable[[str, str], "dict | None"]


def judge_cli(c: CliCheck, helps: dict, owner: str, bins: dict[str, str],
              in_source: frozenset[str] | set[str] = frozenset(),
              flag_says: FlagSays | None = None,
              shown: str = "") -> tuple[int, list[dict], list[str]]:
    """(checks that held, findings, notes) for one invocation.

    `owner` names what the CLI belongs to in a message (a package, a binary).
    `in_source` are the flags the package's own code names (npm lane): one of
    those missing from `--help` is hidden or undocumented, not wrong.
    `flag_says` is the binary lane's evidence about such a flag, asked of the
    CLI itself. `shown` is what the page prints after the command: a command
    it shows failing is not reported for failing that way.
    """
    inv = c.inv
    if c.bin and (env := env_prefixed(inv)):
        return 0, [], [f"line {inv.line}: the command sets {env}, which can change the commands and flags "
                       "the CLI offers; not judged"]
    if not c.bin:
        if c.reason.startswith("installs no bin"):
            have = ", ".join(sorted(bins)) or "none"
            return 0, [{"check": "bin", "subject": inv.bin, "line": inv.line,
                        "message": f"{owner} installs no bin named '{inv.bin}' (bins: {have})",
                        "detail": inv.text[:DETAIL_CAP]}], []
        return 0, [], [f"line {inv.line}: {c.reason}"]

    w = walk(c.bin, chain_args(inv), helps)
    if not w.root_ok:
        return 0, [], [f"line {inv.line}: {w.note}; commands and flags not judged"]
    held = 1       # the bin exists and answers --help
    if w.missing_word:
        subject = " ".join([c.bin, *w.resolved, w.missing_word])
        if names_the_command(w.missing_line, c.bin, " ".join([*w.resolved, w.missing_word])):
            return held, [], [f"line {inv.line}: the CLI itself names `{subject}` as a command it has, "
                              "only not in this mode; not judged"]
        if shown_error(shown, w.missing_word):
            return held, [], [f"line {inv.line}: the page shows `{subject}` failing on purpose; not judged"]
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
    detail = _excerpt(w.help.get("out", "") if w.help else "")
    for flag in inv.flags:
        if flag in UNIVERSAL_FLAGS:
            continue
        if not w.reliable:
            continue
        if any(flag_present(flag, t) for t in texts):
            held += 1
            continue
        if shown_error(shown, flag):
            notes.append(f"line {inv.line}: the page shows {flag} being rejected on purpose; not judged")
            continue
        says = flag_says(cmd, flag) if flag_says else None
        if flag in in_source:
            says = {"state": "accepted", "why": "the package's own code accepts it"}
        state = str((says or {}).get("state") or "")
        if state == "accepted":
            notes.append(f"line {inv.line}: {flag} is not in `{cmd} --help` but "
                         f"{(says or {}).get('why') or 'the CLI accepts it'} (a hidden flag); not judged")
        elif state == "unknown":
            notes.append(f"line {inv.line}: {flag} is not in `{cmd} --help`; "
                         f"{(says or {}).get('why') or 'the CLI would not say whether it accepts it'}; not judged")
        elif state == "rejected":
            findings.append({
                "check": "flag", "subject": f"{cmd} {flag}", "line": inv.line,
                "message": f"{flag} is not accepted by `{cmd}`: {(says or {}).get('line', '')}".rstrip(": "),
                "detail": detail})
        elif state == "absent":
            findings.append({
                "check": "flag", "subject": f"{cmd} {flag}", "line": inv.line,
                "message": f"{flag} does not appear in `{cmd} --help` and nowhere in the binary",
                "detail": detail})
        else:
            findings.append({
                "check": "flag", "subject": f"{cmd} {flag}", "line": inv.line,
                "message": f"{flag} does not appear in `{cmd} --help`", "detail": detail})
    return held, findings, notes
