"""JavaScript / TypeScript snippets: what they import, what commands they run,
what a package exports.

Python has no parser for JS, and the host has no Node toolchain to borrow one
from, so this is lexical - the same bet csharp.py makes. The one thing a regex
cannot do alone is tell code from text, so everything is run over `mask(code)`:
the code with comments and the *contents* of strings, templates and regex
literals blanked. A `require('other')` inside a comment or a string then simply
is not there to match. The quote characters themselves survive the mask, which
is how module specifiers (themselves string literals) are read back out of the
original text at the offsets the masked match found.

Traps this module is built around:

- `/` is division or a regex depending on what came before it. The mask tracks
  whether the previous token ends a value; `</div>` and `/>` in JSX are never
  regexes. A regex literal holding a quote (`/['"]/`) would otherwise open a
  string that swallows the rest of the file.
- Single- and double-quoted strings end at a newline even if unterminated, so a
  stray apostrophe in JSX text (`<p>Don't</p>`) costs one line, not the file.
- Template literals nest: `${ "}" }` inside a template is a string inside an
  expression inside a string. The mask recurses to find the real end.
- Docs show shell sessions: `$ ` prompts, `\\` continuations, `&&` chains,
  heredocs, and `console` blocks that mix commands with their output. Command
  parsing splits on operators outside quotes and drops what is not a command.
- Everything errs towards reporting less. A use or an invocation that is not
  recognised costs a missed check; one that is invented costs a false alarm
  against a docs page that is fine.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

JS_LANGS = frozenset({"js", "javascript", "jsx", "mjs", "cjs", "node",
                      "ts", "typescript", "tsx", "mts", "cts"})
TS_LANGS = frozenset({"ts", "typescript", "tsx", "mts", "cts"})
SHELL_LANGS = frozenset({"bash", "sh", "shell", "console", "zsh"})

# Bare specifiers Node resolves itself. `events`, `buffer`, `util` also exist
# on npm, but a snippet saying `import fs from 'fs'` is not naming a package
# whose release could have removed anything.
NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net", "os",
    "path", "perf_hooks", "process", "punycode", "querystring", "readline",
    "repl", "stream", "string_decoder", "sys", "timers", "tls", "trace_events",
    "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
})

_ID = r"[A-Za-z_$][\w$]*"
_Q = r"['\"`]"
_PKG_NAME = re.compile(r"^(?:@[A-Za-z0-9][\w.~-]*/)?[A-Za-z0-9][\w.~-]*$")
_ARCHIVE = (".tgz", ".tar.gz", ".tar", ".zip")


def is_js(lang: str) -> bool:
    return lang.strip().lower() in JS_LANGS


def is_ts(lang: str) -> bool:
    return lang.strip().lower() in TS_LANGS


# --------------------------------------------------------------------- lexing

# After these a `/` starts a regex even though they look like identifiers.
_REGEX_AFTER = frozenset({"return", "typeof", "instanceof", "in", "of", "new", "delete",
                          "void", "throw", "case", "do", "else", "yield", "await"})
_WORD = re.compile(_ID)
_NUMBER = re.compile(r"\d[\w.]*")


def mask(code: str) -> str:
    """The code with comments and the contents of string, template and regex
    literals blanked to spaces.

    Same length and same newlines as the input, so offsets and line numbers
    found in the mask index straight into the original. Quote and slash
    delimiters are kept, so `from '   '` is still recognisably a string.
    """
    out = list(code)
    n = len(code)
    i = 0
    after_value = False       # the previous token ends a value, so `/` divides
    last_word = ""

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c, nxt = code[i], code[i + 1:i + 2]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and nxt == "/":
            j = code.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif c == "/" and nxt == "*":
            j = code.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c in "'\"":
            end, closed = _string_end(code, i, c)
            blank(i + 1, end)
            i = end + 1 if closed else end
            after_value, last_word = True, ""
        elif c == "`":
            end = _template_end(code, i)
            blank(i + 1, end)
            i = end + 1
            after_value, last_word = True, ""
        elif c == "/" and _starts_regex(code, i, after_value, last_word):
            end = _regex_end(code, i)
            blank(i + 1, end)
            i = end + 1
            while i < n and (code[i].isalnum() or code[i] == "_"):    # flags
                i += 1
            after_value, last_word = True, ""
        elif (w := _WORD.match(code, i)):
            last_word = w.group()
            after_value = last_word not in _REGEX_AFTER
            i = w.end()
        elif (d := _NUMBER.match(code, i)):
            after_value, last_word = True, ""
            i = d.end()
        else:
            after_value, last_word = c in ")]}", ""
            i += 1
    return "".join(out)


def _starts_regex(code: str, i: int, after_value: bool, last_word: str) -> bool:
    if code[i - 1:i] == "<" or code[i + 1:i + 2] == ">":      # JSX `</div>`, `<br />`
        return False
    if after_value and last_word not in _REGEX_AFTER:
        return False
    return _regex_end(code, i) >= 0


def _string_end(code: str, i: int, quote: str) -> tuple[int, bool]:
    """(index of the closing quote or of the line end, whether it closed)."""
    n = len(code)
    j = i + 1
    while j < n:
        c = code[j]
        if c == "\\":
            j += 2
        elif c == quote:
            return j, True
        elif c == "\n":
            return j, False
        else:
            j += 1
    return n, False


def _template_end(code: str, i: int) -> int:
    """Index of the backtick closing the template opened at `i` (or `len`)."""
    n = len(code)
    j = i + 1
    while j < n:
        c = code[j]
        if c == "\\":
            j += 2
        elif c == "`":
            return j
        elif c == "$" and code[j + 1:j + 2] == "{":
            j = _expr_end(code, j + 2) + 1
        else:
            j += 1
    return n


def _expr_end(code: str, j: int) -> int:
    """Index of the `}` closing a `${` whose body starts at `j`."""
    n = len(code)
    depth = 1
    while j < n:
        c = code[j]
        if c in "'\"":
            j = _string_end(code, j, c)[0] + 1
            continue
        if c == "`":
            j = _template_end(code, j) + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n


def _regex_end(code: str, i: int) -> int:
    """Index of the `/` closing the regex opened at `i`, or -1 (not a regex)."""
    n = len(code)
    j = i + 1
    in_class = False
    while j < n:
        c = code[j]
        if c == "\n":
            return -1
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            return j if j > i + 1 else -1          # `//` is a comment, never empty
        j += 1
    return -1


def _line_of(code: str, offset: int) -> int:
    return code.count("\n", 0, offset) + 1


def _match_close(m: str, open_idx: int) -> int:
    """Index of the bracket closing the one at `open_idx` in masked text, or -1."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: list[str] = []
    for k in range(open_idx, len(m)):
        c = m[k]
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if not stack or stack.pop() != c:
                return -1
            if not stack:
                return k
    return -1


def _split_top(m: str, start: int, end: int) -> list[tuple[int, int]]:
    """Spans of m[start:end] split at depth-0 commas."""
    spans = []
    depth = 0
    last = start
    for k in range(start, end):
        c = m[k]
        if c in "{([":
            depth += 1
        elif c in "})]":
            depth -= 1
        elif c == "," and depth == 0:
            spans.append((last, k))
            last = k + 1
    spans.append((last, end))
    return spans


# ------------------------------------------------------------------- specifiers

def package_of(specifier: str) -> str:
    """The registry package a module specifier names, or '' when it names none."""
    s = specifier.strip()
    if not s or s[0] in "./#~" or ":" in s or "\\" in s or any(ch.isspace() for ch in s):
        return ""
    parts = s.split("/")
    if s.startswith("@"):
        if len(parts) < 2 or not parts[1]:
            return ""
        pkg = f"{parts[0]}/{parts[1]}"
    else:
        pkg = parts[0]
        if pkg in NODE_BUILTINS:
            return ""
    return pkg if _PKG_NAME.match(pkg) else ""


# ------------------------------------------------------------------------ uses

@dataclass(frozen=True)
class Use:
    """One thing a snippet takes from a module."""
    specifier: str
    name: str
    line: int
    type_only: bool = False


@dataclass
class _Binding:
    local: str
    specifier: str
    prefix: str            # '' for a whole-module binding, 'default.' for a default import
    type_only: bool
    decl: int              # offset of the declaring `const`, -1 for an import statement


_IMPORT_FROM = re.compile(
    rf"\bimport\s*(?P<type>type\b(?!\s*(?:from\b|,)))?\s*"
    rf"(?P<clause>(?:\{{[^{{}}]*\}}|\*\s*as\s+{_ID}|{_ID})(?:\s*,\s*(?:\{{[^{{}}]*\}}|\*\s*as\s+{_ID}))?)"
    rf"\s*from\s*(?P<q>{_Q})")
_EXPORT_FROM = re.compile(
    rf"\bexport\s*(?P<type>type\b)?\s*(?P<clause>\{{[^{{}}]*\}}|\*(?:\s*as\s+{_ID})?)\s*from\s*(?P<q>{_Q})")
_IMPORT_BARE = re.compile(rf"\bimport\s*(?P<q>{_Q})")
_LOAD = re.compile(rf"(?<![\w$.])(?P<call>require|import)\s*\(\s*(?P<q>{_Q})")
_DECL_LOAD = re.compile(
    rf"(?<![\w$.])(?P<kw>const|let|var|import)\s+(?P<lhs>\{{[^{{}}]*\}}|{_ID})\s*(?::[^=;{{}}]+)?="
    rf"\s*(?:await\s+)?(?P<call>require|import)\s*\(\s*(?P<q>{_Q})")


def uses(code: str) -> list[Use]:
    """Every module the snippet loads and what it takes from each. Never raises."""
    found: list[Use] = []
    try:
        _collect_uses(code, mask(code), found)
    except Exception:
        pass
    return _dedupe(found)


def _dedupe(found: list[Use]) -> list[Use]:
    """One Use per (specifier, name): first line wins; runtime beats type-only."""
    seen: dict[tuple[str, str], Use] = {}
    for u in sorted(found, key=lambda u: u.line):
        key = (u.specifier, u.name)
        prev = seen.get(key)
        if prev is None:
            seen[key] = u
        elif prev.type_only and not u.type_only:
            seen[key] = Use(u.specifier, u.name, prev.line, False)
    return sorted(seen.values(), key=lambda u: u.line)


def _spec_at(code: str, m: str, after_quote: int, quote: str) -> tuple[str, int] | None:
    """(specifier, index of its closing quote) for a string whose contents start
    at `after_quote`. None if it never closes on its line or interpolates."""
    end = m.find(quote, after_quote)
    if end < 0 or "\n" in m[after_quote:end]:
        return None
    spec = code[after_quote:end]
    if quote == "`" and "${" in spec:
        return None
    return spec, end


def _named_items(body: str, *, exported_alias: bool = False) -> list[tuple[str, bool]]:
    """(name, type_only) for each item of a `{ a, b as c, type d }` list.

    `exported_alias` picks the name on the right of `as` (what an `export {}`
    publishes) instead of the left (what an `import {}` takes).
    """
    out = []
    for item in body.split(","):
        toks = item.split()
        type_only = False
        # `{ type X }` is a modifier; `{ type }` and `{ type as t }` import a name `type`.
        if len(toks) >= 2 and toks[0] == "type" and not (len(toks) == 3 and toks[1] == "as"):
            type_only, toks = True, toks[1:]
        if not toks:
            continue
        name = toks[-1] if exported_alias and len(toks) >= 3 and toks[-2] == "as" else toks[0]
        if re.fullmatch(_ID, name):
            out.append((name, type_only))
    return out


def _collect_uses(code: str, m: str, found: list[Use]) -> None:
    bindings: list[_Binding] = []
    consumed: set[int] = set()          # offsets of specifiers already accounted for

    def add(spec: str, name: str, offset: int, type_only: bool = False) -> None:
        found.append(Use(spec, name, _line_of(code, offset), type_only))

    for mt in _IMPORT_FROM.finditer(m):
        got = _spec_at(code, m, mt.end(), mt.group("q"))
        if not got:
            continue
        spec = got[0]
        consumed.add(mt.end())
        type_all = bool(mt.group("type"))
        clause = mt.group("clause")
        brace = re.search(r"\{([^{}]*)\}", clause)
        rest = clause[:brace.start()] + clause[brace.end():] if brace else clause
        ns = re.search(rf"\*\s*as\s+({_ID})", rest)
        if ns:
            add(spec, "*", mt.start(), type_all)
            bindings.append(_Binding(ns.group(1), spec, "", type_all, -1))
            rest = rest[:ns.start()] + rest[ns.end():]
        default = re.match(rf"\s*({_ID})", rest)
        if default:
            add(spec, "default", mt.start(), type_all)
            bindings.append(_Binding(default.group(1), spec, "default.", type_all, -1))
        if brace:
            for name, t in _named_items(brace.group(1)):
                add(spec, name, mt.start(), type_all or t)

    for mt in _EXPORT_FROM.finditer(m):
        got = _spec_at(code, m, mt.end(), mt.group("q"))
        if not got:
            continue
        consumed.add(mt.end())
        type_all = bool(mt.group("type"))
        clause = mt.group("clause")
        if clause.startswith("*"):
            add(got[0], "*", mt.start(), type_all)
        else:
            for name, t in _named_items(clause[1:-1]):
                add(got[0], name, mt.start(), type_all or t)

    for mt in _IMPORT_BARE.finditer(m):
        got = _spec_at(code, m, mt.end(), mt.group("q"))
        if got and mt.end() not in consumed:
            consumed.add(mt.end())
            add(got[0], "*", mt.start())

    for mt in _DECL_LOAD.finditer(m):
        if mt.group("kw") == "import" and mt.group("call") != "require":
            continue
        got = _spec_at(code, m, mt.end(), mt.group("q"))
        close = _call_close(m, got[1] + 1) if got else None
        if not got or close is None:
            continue
        consumed.add(mt.end())
        spec = got[0]
        if re.match(r"\s*[.(\[]", m[close:]):
            # `require('p').default`, `require('p')(opts)`: the binding is not
            # the module, so it names nothing about the module's exports.
            add(spec, "*", mt.start())
            tail = re.match(rf"\s*\??\.\s*({_ID})", m[close:])
            if tail:
                add(spec, tail.group(1), mt.start())
            continue
        lhs = mt.group("lhs")
        if lhs.startswith("{"):
            names = _destructured(lhs[1:-1])
            for name in names:
                add(spec, name, mt.start())
            if not names:
                add(spec, "*", mt.start())
        else:
            add(spec, "*", mt.start())
            bindings.append(_Binding(lhs, spec, "", False, mt.start()))

    for mt in _LOAD.finditer(m):
        if mt.end() in consumed:
            continue
        got = _spec_at(code, m, mt.end(), mt.group("q"))
        close = _call_close(m, got[1] + 1) if got else None
        if not got or close is None:        # `require('a' + b)`, `require(name)`
            continue
        add(got[0], "*", mt.start())
        tail = re.match(rf"\s*\??\.\s*({_ID})", m[close:])
        if tail:
            add(got[0], tail.group(1), mt.start())

    _member_uses(code, m, bindings, found)


def _call_close(m: str, i: int) -> int | None:
    """Index just past the `)` closing a call whose string argument ended at
    `i`, allowing `, { with: ... }` attributes. None if it is not a plain call."""
    mt = re.match(r"\s*(\)|,)", m[i:])
    if not mt:
        return None
    if mt.group(1) == ")":
        return i + mt.end()
    depth = 1
    for k in range(i + mt.end(), len(m)):
        if m[k] in "([{":
            depth += 1
        elif m[k] in ")]}":
            depth -= 1
            if depth == 0:
                return k + 1
    return None


def _destructured(body: str) -> list[str]:
    """Exported names pulled out by `const { a, b: c, d = 1, ...rest } = ...`."""
    out = []
    for item in body.split(","):
        key = re.split(r"[:=]", item, maxsplit=1)[0].strip()
        if re.fullmatch(_ID, key):
            out.append(key)
    return out


def _member_uses(code: str, m: str, bindings: list[_Binding], found: list[Use]) -> None:
    """`ns.foo` after `import * as ns` / `const ns = require(...)` is a use of
    `foo`. That is how a removed export is caught when docs use a namespace."""
    counts: dict[str, int] = {}
    for b in bindings:
        counts[b.local] = counts.get(b.local, 0) + 1
    for b in bindings:
        if counts[b.local] > 1 or _rebound(m, b):
            continue
        member = re.compile(rf"(?<![\w$.]){re.escape(b.local)}\s*\??\.\s*({_ID})")
        for mt in member.finditer(m):
            found.append(Use(b.specifier, b.prefix + mt.group(1), _line_of(code, mt.start()), b.type_only))


def _rebound(m: str, b: _Binding) -> bool:
    """Whether the snippet declares the binding's name again (a shadowing
    parameter, a later `let`). Scope is not tracked, so any hit disables it."""
    n = re.escape(b.local)
    patterns = (
        rf"(?<![\w$.])(?:const|let|var|function\*?|class)\s+{n}(?![\w$])",
        rf"(?<![\w$.])(?:const|let|var)\s*[{{\[][^}}\]]*?(?<![\w$.]){n}(?![\w$])",
        rf"(?<![\w$.]){n}\s*=>",
        rf"\([^()]*(?<![\w$.]){n}(?![\w$])[^()]*\)\s*(?:=>|\{{)",
        rf"\bcatch\s*\(\s*{n}(?![\w$])",
    )
    for p in patterns:
        for mt in re.finditer(p, m):
            if mt.start() != b.decl:
                return True
    return False


# ------------------------------------------------------------------- shell

@dataclass(frozen=True)
class Invocation:
    """One command line that runs a package's CLI."""
    bin: str
    package: str
    args: tuple[str, ...]
    flags: tuple[str, ...]
    line: int
    text: str


@dataclass(frozen=True)
class _Tok:
    text: str
    quoted: bool      # any part of the word was quoted


_PROMPT = re.compile(r"^\s*(\$|>)\s+(.*)$")
_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_]\w*)\1(?!<)")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_REDIRECT = re.compile(r"^\d*[<>]{1,2}&?")


def _command_lines(code: str, *, keep_output: bool = False) -> list[tuple[int, str, str]]:
    """(line, text, prompt) for each logical command line of a shell block.

    Continuations are joined, comments and heredoc bodies dropped. When some
    lines carry a `$ ` prompt the block is a session transcript, so unprompted
    lines are command output and are dropped (unless `keep_output`).
    """
    raw = code.splitlines()
    out: list[tuple[int, str, str]] = []
    i = 0
    while i < len(raw):
        start = i + 1
        line = raw[i]
        while line.rstrip().endswith("\\") and not line.rstrip().endswith("\\\\") and i + 1 < len(raw):
            i += 1
            line = line.rstrip()[:-1] + " " + raw[i].strip()
        i += 1
        mt = _PROMPT.match(line)
        prompt, text = (mt.group(1), mt.group(2)) if mt else ("", line)
        text = text.strip()
        hd = _HEREDOC.search(text)
        if hd:
            while i < len(raw) and raw[i].strip() != hd.group(2):
                i += 1
            i += 1
        if text and not text.startswith("#"):
            out.append((start, text, prompt))
    if not keep_output and any(p == "$" for _, _, p in out):
        out = [row for row in out if row[2] == "$"]
    return out


_OPERATORS = ("&&", "||", ";", "|", "&")


def _segments(text: str) -> list[list[_Tok]]:
    """The command line split into simple commands on `&&`, `||`, `;`, `|`, each
    a list of words. Quotes group words and mark them; redirections are dropped."""
    segs: list[list[_Tok]] = [[]]
    word: list[str] = []
    quoted = False
    has_word = False
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal word, quoted, has_word
        if has_word:
            segs[-1].append(_Tok("".join(word), quoted))
        word, quoted, has_word = [], False, False

    while i < n:
        c = text[i]
        if c in "'\"":
            end = i + 1
            while end < n and text[end] != c:
                end += 2 if (c == '"' and text[end] == "\\") else 1
            word.append(text[i + 1:end])
            quoted, has_word = True, True
            i = end + 1
        elif c == "\\" and i + 1 < n:
            word.append(text[i + 1])
            has_word = True
            i += 2
        elif c.isspace():
            flush()
            i += 1
        elif c == "#" and not has_word:
            break
        elif (op := next((o for o in _OPERATORS if text.startswith(o, i)), None)) and not (
                op == "&" and (text[i + 1:i + 2] == ">" or text[i - 1:i] in (">", "<"))):
            flush()
            segs.append([])
            i += len(op)
        else:
            word.append(c)
            has_word = True
            i += 1
    flush()
    return [_drop_redirects(s) for s in segs if s]


def _drop_redirects(toks: list[_Tok]) -> list[_Tok]:
    out = []
    skip = False
    for t in toks:
        if skip:
            skip = False
        elif not t.quoted and _REDIRECT.match(t.text):
            skip = _REDIRECT.sub("", t.text, count=1) == ""      # `>` alone eats the next word
        else:
            out.append(t)
    return out


def _strip_prefix(toks: list[_Tok]) -> list[_Tok]:
    """Drop `sudo`, `env`, `time`, `nohup` and `VAR=x` prefixes."""
    i = 0
    while i < len(toks):
        t = toks[i]
        if _ENV_ASSIGN.match(t.text):      # `FOO='a b'` is quoted but still an assignment
            i += 1
        elif t.quoted:
            break
        elif t.text in ("sudo", "env", "time", "nohup", "exec"):
            i += 1
            while i < len(toks) and not toks[i].quoted and toks[i].text.startswith("-"):
                i += 2 if toks[i].text in ("-u", "-g") and t.text == "sudo" else 1
        else:
            break
    return toks[i:]


def _commands(code: str) -> list[tuple[int, str, list[_Tok]]]:
    out = []
    for line, text, _ in _command_lines(code):
        for seg in _segments(text):
            seg = _strip_prefix(seg)
            if seg:
                out.append((line, text, seg))
    return out


_LONG_VALUE_FLAGS = frozenset({"--registry", "--prefix", "--cache", "--userconfig", "--tag",
                               "--workspace", "--filter", "--cwd", "--dir", "--loglevel",
                               "--shell", "--save-prefix"})


def _pkg_spec(text: str) -> tuple[str, str] | None:
    """(name, version-or-'') for `pkg`, `pkg@1.2`, `@scope/pkg@^1`; None for
    anything that is not a registry package (paths, URLs, tarballs, placeholders)."""
    if text.lower().endswith(_ARCHIVE):
        return None
    mt = re.fullmatch(r"((?:@[A-Za-z0-9][\w.~-]*/)?[A-Za-z0-9][\w.~-]*)(?:@([^\s@:]+))?", text)
    return (mt.group(1), mt.group(2) or "") if mt else None


def _runner_words(rest: list[_Tok]) -> tuple[list[str], int] | None:
    """Skip runner options (`-y`, `--`, `-p pkg`); (packages from -p, index of
    the target word in `rest`), or None when there is no usable target."""
    pkgs: list[str] = []
    i = 0
    while i < len(rest):
        t = rest[i]
        if t.quoted or not t.text.startswith("-") or t.text == "-":
            return pkgs, i
        if t.text in ("-p", "--package") and i + 1 < len(rest):
            pkgs.append(rest[i + 1].text)
            i += 2
        elif t.text.startswith("--package="):
            pkgs.append(t.text.split("=", 1)[1])
            i += 1
        elif t.text in ("-c", "--call"):
            return None
        elif t.text in _LONG_VALUE_FLAGS and i + 1 < len(rest):
            i += 2
        else:
            i += 1
    return None


def _tail(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _match(toks: list[_Tok], wanted: frozenset[str], tails: frozenset[str]) -> tuple[str, str, list[_Tok]] | None:
    """(bin, package, remaining words) when this command runs one of `wanted`."""
    cmd = toks[0].text
    sub = toks[1].text if len(toks) > 1 and not toks[1].quoted else ""
    runner: str | None = None
    rest: list[_Tok] = []
    if cmd in ("npx", "bunx"):
        runner, rest = "named", toks[1:]
    elif (cmd == "npm" and sub in ("exec", "x")) or (cmd == "pnpm" and sub == "dlx") \
            or (cmd == "yarn" and sub == "dlx") or (cmd == "bun" and sub == "x"):
        runner, rest = "named", toks[2:]
    elif cmd == "pnpm" and sub == "exec":
        runner, rest = "bare", toks[2:]
    if runner is None:
        bare = cmd.rsplit("node_modules/.bin/", 1)[-1] if "node_modules/.bin/" in cmd else cmd
        if toks[0].quoted or (bare not in wanted and bare not in tails):
            return None
        return bare, "", toks[1:]
    found = _runner_words(rest)
    if found is None or found[1] >= len(rest) or rest[found[1]].quoted:
        return None
    pkgs, at = found
    target = rest[at].text
    after = rest[at + 1:]
    if runner == "bare":
        return (target, "", after) if target in wanted or target in tails else None
    spec = _pkg_spec(target)
    if pkgs:
        named = _pkg_spec(pkgs[0])
        if named and (named[0] in wanted or target in wanted or target in tails):
            return target, named[0], after
        return None
    if spec and spec[0] in wanted:
        return _tail(spec[0]), spec[0], after
    return None


def _args_flags(rest: list[_Tok]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Leading bare words (subcommand candidates) and the long flag names."""
    args: list[str] = []
    flags: list[str] = []
    collecting = True
    for t in rest:
        if t.quoted:
            collecting = False          # a quoted word is a value, never a subcommand
            continue
        if t.text == "--":
            break                       # everything after is passed through to something else
        if t.text.startswith("--"):
            collecting = False
            flag = t.text.split("=", 1)[0]
            if re.fullmatch(r"--[A-Za-z0-9][\w-]*", flag) and flag not in flags:
                flags.append(flag)
        elif t.text.startswith("-") and len(t.text) > 1:
            collecting = False          # short flags name nothing, but end the subcommand run
        elif collecting:
            args.append(t.text)
    return tuple(args), tuple(flags)


def invocations(code: str, names: Iterable[str]) -> list[Invocation]:
    """Command lines in a shell snippet that run one of `names` (package or bin names)."""
    wanted = frozenset(names)
    tails = frozenset(_tail(n) for n in wanted if n.startswith("@"))
    out = []
    for line, text, toks in _commands(code):
        hit = _match(toks, wanted, tails)
        if hit:
            bin_, package, rest = hit
            args, flags = _args_flags(rest)
            out.append(Invocation(bin_, package, args, flags, line, text))
    return out


# ----------------------------------------------------------------- installs

_NPM_INSTALL = frozenset({"install", "i", "add"})
_OTHER_INSTALL = {"yarn": frozenset({"add"}), "pnpm": frozenset({"add", "install", "i"}),
                  "bun": frozenset({"add", "install", "i"})}


def _install_args(toks: list[_Tok]) -> list[_Tok] | None:
    """The words after the install subcommand, or None if this is not an install."""
    cmd = toks[0].text
    sub = toks[1].text if len(toks) > 1 else ""
    if cmd == "npm" and sub in _NPM_INSTALL:
        return toks[2:]
    if cmd in _OTHER_INSTALL and sub in _OTHER_INSTALL[cmd]:
        return toks[2:]
    if cmd == "yarn" and sub == "global" and len(toks) > 2 and toks[2].text == "add":
        return toks[3:]
    return None


def _package_words(words: list[_Tok]) -> list[_Tok]:
    """The non-flag words, skipping the value of flags that take one."""
    out = []
    i = 0
    while i < len(words):
        t = words[i]
        if not t.quoted and t.text.startswith("-"):
            i += 2 if t.text in _LONG_VALUE_FLAGS else 1
        else:
            out.append(t)
            i += 1
    return out


def installs(code: str) -> list[tuple[str, str]]:
    """(package, version-or-'') for each registry package on an install line."""
    out: list[tuple[str, str]] = []
    for _, _, toks in _commands(code):
        words = _install_args(toks)
        if words is None:
            continue
        for t in _package_words(words):
            spec = _pkg_spec(t.text)
            if spec and spec not in out:
                out.append(spec)
    return out


def pure_install(code: str) -> bool:
    """True when the block is nothing but `npm install <registry packages>` lines."""
    rows = _command_lines(code, keep_output=True)
    if not rows:
        return False
    for _, text, prompt in rows:
        if prompt == "" and any(p == "$" for _, _, p in rows):
            return False                            # command output in a session transcript
        if re.search(r"[<>`]|\$\(", text):
            return False
        segs = _segments(text)
        if len(segs) != 1 or segs[0][0].text != "npm" or segs[0][0].quoted:
            return False
        words = _install_args(segs[0])
        if words is None or segs[0][1].text not in _NPM_INSTALL:
            return False
        pkgs = _package_words(words)
        if not pkgs or any(_pkg_spec(t.text) is None for t in pkgs):
            return False
    return True


# --------------------------------------------------------------------- modules

_STATIC_IMPORT = re.compile(
    rf"(?:^|[;}}])\s*import\s*(?:type\s+)?(?:\{{|\*|{_ID}\s*(?:,|from\b)|{_Q})|\bimport\.meta\b", re.M)
_STATIC_EXPORT = re.compile(
    r"(?:^|[;}])\s*export\s+(?:default\b|const\b|let\b|var\b|function\b|class\b|async\b|\{|\*|type\b|"
    r"interface\b|enum\b|abstract\b|declare\b|namespace\b)", re.M)
_CJS = re.compile(r"(?<![\w$.])require\s*\(|(?<![\w$.])module\s*\.\s*exports\b|(?<![\w$.])exports\s*\.\s*[\w$]+\s*=")


def is_esm(code: str) -> bool:
    """True if the snippet uses static import/export (or top-level await with no
    CommonJS); False for CommonJS or for code that is neither."""
    m = mask(code)
    if _STATIC_IMPORT.search(m) or _STATIC_EXPORT.search(m):
        return True
    return not _CJS.search(m) and _top_level_await(m)


def _top_level_await(m: str) -> bool:
    depth = 0
    for mt in re.finditer(r"[{(\[]|[})\]]|(?<![\w$.])await\b", m):
        tok = mt.group()
        if tok in "{([":
            depth += 1
        elif tok in "})]":
            depth -= 1
        elif depth <= 0:
            line_start = m.rfind("\n", 0, mt.start()) + 1
            line_end = m.find("\n", mt.end())
            if "async" not in m[line_start:len(m) if line_end < 0 else line_end]:
                return True
    return False


# --------------------------------------------------------------------- exports

_EXPORT = r"(?<![\w$.])export\s+(?:declare\s+)?"
_R_FUNCTION = re.compile(rf"{_EXPORT}(?P<default>default\s+)?(?:async\s+)?function\b\s*\*?\s*(?P<name>{_ID})?")
_R_CLASS = re.compile(rf"{_EXPORT}(?P<default>default\s+)?(?:abstract\s+)?class\b\s*(?P<name>{_ID})?")
_R_TYPE = re.compile(rf"{_EXPORT}(?:const\s+)?(?:interface|enum|namespace|module|type)\s+(?P<name>{_ID})")
_R_VAR = re.compile(rf"{_EXPORT}(?:const|let|var)\s+(?!enum\b)")
_R_DEFAULT = re.compile(r"(?<![\w$.])export\s+default\b")
_R_LIST = re.compile(r"(?<![\w$.])export\s+(?:type\s+)?\{([^{}]*)\}")
_R_STAR_AS = re.compile(rf"(?<![\w$.])export\s*\*\s*as\s+(?P<name>{_ID})\s*from\b")
_R_EXPORTS_DOT = re.compile(rf"(?<![\w$.])(?:module\s*\.\s*)?exports\s*\.\s*(?P<name>{_ID})\s*=(?!=)")
_R_DEFINE = re.compile(rf"\bObject\s*\.\s*defineProperty\s*\(\s*(?:module\s*\.\s*)?exports\s*,\s*(?P<q>{_Q})")
_R_MODULE_OBJ = re.compile(r"(?<![\w$.])module\s*\.\s*exports\s*=\s*\{")
_R_CLASS_DECL = re.compile(rf"(?<![\w$.])class\s+(?P<name>{_ID})")
_NOT_EXPORTS = frozenset({"__esModule"})


def exports_of(source: str) -> set[str]:
    """Public names a JS/TS source file exports. Never raises."""
    try:
        return _exports_of(source, mask(source))
    except Exception:
        return set()


def _exports_of(source: str, m: str) -> set[str]:
    out: set[str] = set()

    for mt in _R_FUNCTION.finditer(m):
        if mt.group("default"):
            out.add("default")
        elif mt.group("name"):
            out.add(mt.group("name"))
    for mt in _R_CLASS.finditer(m):
        name = mt.group("name")
        if mt.group("default"):
            out.add("default")
        elif name and name not in ("extends", "implements"):
            out.add(name)
    for mt in _R_TYPE.finditer(m):
        out.add(mt.group("name"))
    for mt in _R_VAR.finditer(m):
        out.update(_declared_names(m, mt.end()))
    if _R_DEFAULT.search(m):
        out.add("default")
    for mt in _R_LIST.finditer(m):
        out.update(n for n, _ in _named_items(mt.group(1), exported_alias=True))
    for mt in _R_STAR_AS.finditer(m):
        out.add(mt.group("name"))
    for mt in _R_EXPORTS_DOT.finditer(m):
        out.add(mt.group("name"))
    for mt in _R_DEFINE.finditer(m):
        got = _spec_at(source, m, mt.end(), mt.group("q"))
        if got and got[0]:
            out.add(got[0])
    for mt in _R_MODULE_OBJ.finditer(m):
        out.update(_object_keys(source, m, mt.end() - 1))

    out -= _NOT_EXPORTS
    for mt in _R_CLASS_DECL.finditer(m):
        cls = mt.group("name")
        if cls in out:
            out.update(f"{cls}.{meth}" for meth in _class_methods(m, mt.end()))
    return out


def _declared_names(m: str, pos: int) -> set[str]:
    """Names bound by the declarators of `export const a = 1, {b, c: d} = o, e`."""
    end = _statement_end(m, pos)
    names: set[str] = set()
    for a, b in _split_top(m, pos, end):
        decl = m[a:b].strip()
        if decl.startswith("{"):
            close = _match_close(m, a + (len(m[a:b]) - len(m[a:b].lstrip())))
            if close > 0:
                for item in m[a + (len(m[a:b]) - len(m[a:b].lstrip())) + 1:close].split(","):
                    key = re.split(r"=", item.split(":")[-1], maxsplit=1)[0].strip()
                    key = key.removeprefix("...")
                    if re.fullmatch(_ID, key):
                        names.add(key)
        elif decl.startswith("["):
            continue
        else:
            mt = re.match(_ID, decl)
            if mt:
                names.add(mt.group())
    return names


def _statement_end(m: str, pos: int) -> int:
    """End of the declaration starting at `pos`: a depth-0 `;`, or a depth-0
    newline that does not continue the expression."""
    depth = 0
    last = ""
    k = pos
    while k < len(m):
        c = m[k]
        if c in "{([":
            depth += 1
        elif c in "})]":
            depth -= 1
            if depth < 0:
                return k
        elif depth == 0 and c == ";":
            return k
        elif depth == 0 and c == "\n" and last and last not in ",=+-*/&|?:<>({[":
            nxt = m[k:].lstrip()[:1]
            if nxt not in (",", ".", "?", ":", "+", "-", "*", "/", "&", "|", "="):
                return k
        if not c.isspace():
            last = c
        k += 1
    return len(m)


def _object_keys(source: str, m: str, open_idx: int) -> set[str]:
    """Keys of the object literal whose `{` is at `open_idx`."""
    close = _match_close(m, open_idx)
    if close < 0:
        return set()
    keys: set[str] = set()
    for a, b in _split_top(m, open_idx + 1, close):
        item = m[a:b]
        lead = a + len(item) - len(item.lstrip())
        item = item.strip()
        if item[:1] in ("'", '"'):
            end = m.find(item[0], lead + 1)
            if end > 0 and re.match(r"\s*:", m[end + 1:]):
                key = source[lead + 1:end]
                if key and not any(ch.isspace() for ch in key):
                    keys.add(key)
            continue
        mt = re.match(rf"(?:(?:async|get|set)\s+(?=[\w$*])|\*\s*)*({_ID})\s*(?=[,:(]|$)", item)
        if mt:
            keys.add(mt.group(1))
    return keys


_CLASS_MEMBER = re.compile(
    rf"(?<![@\w$.#])(?P<mods>(?:(?:public|private|protected|static|async|readonly|override|abstract|"
    rf"get|set|declare)\s+)*)(?:\*\s*)?(?P<name>#?{_ID})\s*(?:<[^<>()]*>\s*)?\??\s*\(")
_NOT_METHODS = frozenset({"if", "for", "while", "switch", "catch", "function", "return", "constructor"})
_CONTINUES = ",=(:+-*/&|?<>[."


def _class_methods(m: str, after_name: int) -> set[str]:
    """Public methods of the class whose name ends at `after_name`."""
    brace = m.find("{", after_name)
    semi = m.find(";", after_name)
    if brace < 0 or 0 <= semi < brace:
        return set()
    close = _match_close(m, brace)
    if close < 0:
        return set()
    body = m[brace + 1:close]
    depth_at: list[int] = []
    depth = 0
    for c in body:
        if c in "})]":
            depth -= 1
        depth_at.append(depth)
        if c in "{([":
            depth += 1
    out = set()
    for mt in _CLASS_MEMBER.finditer(body):
        name = mt.group("name")
        if depth_at[mt.start()] != 0 or name.startswith(("_", "#")) or name in _NOT_METHODS:
            continue
        if re.search(r"\b(?:private|protected)\b", mt.group("mods")):
            continue
        before = body[:mt.start()].rstrip()
        prev = before[-1:]
        gap = body[len(before):mt.start()]
        if prev and not (prev in "{};" or ("\n" in gap and prev not in _CONTINUES)):
            continue
        out.add(name)
    return out
