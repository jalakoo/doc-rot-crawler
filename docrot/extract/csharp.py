"""C# snippets: shape, symbols, and the wrapper that makes them compile.

A C# docs snippet is almost never a program. It is a `Program.cs` excerpt
leaning on a `builder` it never declares, a class body with its usings elided,
or a lone method lifted out of a controller. None of that compiles as written,
and the error it gives is about the excerpt, not the documentation.

So every snippet is rewritten into a file of its own before it is compiled:

    <the snippet's using directives>
    namespace DocRot.Snippets.S0042 {
        <its types and namespaces, as written>
        public partial class __Members { <its fields, properties, methods> }
        public static class __Run {
            public static async Task __Main(<ambient builder, services, app ...>) {
                <its statements>
            }
        }
    }

Each piece keeps its original line numbers in a map, so a compiler error at
file line 31 is reported against the docs line it came from. The compiler
still sees every call the docs make on the package - which is the only thing
this is for.

Parsing is lexical, not Roslyn: the host has no .NET, and the shapes that
matter (depth-0 items, directives, declarations) do not need a parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CSHARP_LANGS = {"csharp", "cs", "c#"}

# Names docs lean on without declaring, and the type a reader would have.
AMBIENT = [
    ("string[]", "args"),
    ("Microsoft.AspNetCore.Builder.WebApplicationBuilder", "builder"),
    ("Microsoft.Extensions.DependencyInjection.IServiceCollection", "services"),
    ("Microsoft.AspNetCore.Builder.WebApplication", "app"),
    ("Microsoft.Extensions.Configuration.IConfiguration", "configuration"),
    ("Microsoft.Extensions.Configuration.IConfiguration", "Configuration"),
    ("Microsoft.AspNetCore.Hosting.IWebHostEnvironment", "environment"),
    ("Microsoft.AspNetCore.Hosting.IWebHostEnvironment", "env"),
    ("Microsoft.AspNetCore.Http.HttpContext", "context"),
    ("Microsoft.AspNetCore.Http.HttpContext", "httpContext"),
    ("Microsoft.AspNetCore.Http.HttpContext", "HttpContext"),
    ("System.IServiceProvider", "serviceProvider"),
    ("System.Threading.CancellationToken", "cancellationToken"),
    ("System.Threading.CancellationToken", "ct"),
]

TYPE_KW = re.compile(
    r"^(?:(?:public|internal|private|protected|sealed|static|abstract|partial|"
    r"file|readonly|unsafe|new|ref)\s+)*"
    r"(?:class|interface|enum|struct|record|delegate)\b")
NAMESPACE = re.compile(r"^namespace\s+[\w.]+\s*(;|\{)")
USING_DIRECTIVE = re.compile(
    r"^(?:global\s+)?using\s+(?:static\s+)?(?:[A-Za-z_]\w*\s*=\s*)?[A-Za-z_][\w.]*"
    r"(?:<[^;]*>)?\s*;$")
MODIFIER = re.compile(
    r"^(?:public|private|protected|internal|override|virtual|abstract|static|"
    r"readonly|const|event|extern|required|sealed)\b")
# `Task<IActionResult> Login(LoginModel m) {` - a method with its modifiers cut
METHOD_SIG = re.compile(
    r"^(?:async\s+)?[A-Za-z_][\w<>\[\],.?\s]*?\s+[A-Za-z_]\w*\s*(?:<[^>()]*>)?\s*"
    r"\([^;{}]*\)\s*(?:where\b[^{]*)?(?:\{|=>)", re.S)
ATTRIBUTE = re.compile(r"^\[[^\]]*\]\s*")
DECLARED = re.compile(r"\b(?:class|interface|enum|struct|record)\s+(\w+)")
QUALIFIED = re.compile(r"\b([A-Z]\w*(?:\.[A-Z]\w*)+)")
ELISION = re.compile(r"(?<![.?])\.\.\.(?!\.)|…")


def is_csharp(lang: str) -> bool:
    return lang.lower() in CSHARP_LANGS


# --------------------------------------------------------------------- lexing

def mask(code: str) -> str:
    """The code with comments, strings and char literals blanked to spaces.

    Same length and same newlines as the input, so offsets and line numbers
    found in the mask index straight into the original.
    """
    out = list(code)
    i, n = 0, len(code)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c, nxt = code[i], code[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = code.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif c == "/" and nxt == "*":
            j = code.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c == '"' or (c in "$@" and nxt in '"$@'):
            j = _string_end(code, i)
            blank(i, j)
            i = j
        elif c == "'":
            m = re.match(r"'(?:\\.|[^'\\\n]){1,8}'", code[i:])
            if m:
                blank(i, i + m.end())
                i += m.end()
            else:
                i += 1
        else:
            i += 1
    return "".join(out)


def _string_end(code: str, i: int) -> int:
    """Index just past the string literal starting at `i`."""
    n = len(code)
    j = i
    verbatim = interp = False
    while j < n and code[j] in "$@":
        verbatim |= code[j] == "@"
        interp |= code[j] == "$"
        j += 1
    quotes = 0
    while j + quotes < n and code[j + quotes] == '"':
        quotes += 1
    if quotes >= 3:                         # raw string literal
        end = code.find('"' * quotes, j + quotes)
        return n if end < 0 else end + quotes
    j += 1
    depth = 0
    while j < n:
        c = code[j]
        if interp and c == "{":
            if code[j + 1:j + 2] == "{":
                j += 2
                continue
            depth += 1
        elif interp and c == "}" and depth:
            depth -= 1
        elif depth == 0:
            if verbatim and c == '"':
                if code[j + 1:j + 2] == '"':
                    j += 2
                    continue
                return j + 1
            if not verbatim and c == "\\":
                j += 2
                continue
            if c == '"':
                return j + 1
            if c == "\n" and not verbatim:
                return j                     # unterminated: stop at the line end
        j += 1
    return n


# ------------------------------------------------------------------ splitting

@dataclass
class Item:
    kind: str           # using | namespace | type | member | statement | directive
    text: str
    line: int           # 1-based line in the snippet where the item starts


def items(code: str) -> list[Item]:
    """The snippet's top-level items, in order.

    An item ends at a `;` or a closing `}` at depth zero - unless the `}` is
    followed by what continues the same construct (`else`, `catch`, an
    initializer's `;`).
    """
    m = mask(code)
    out: list[Item] = []
    n = len(code)
    start = 0
    paren = brace = 0
    i = 0

    def emit(end: int) -> None:
        nonlocal start
        raw = code[start:end]
        lead = len(raw) - len(raw.lstrip())
        text = raw.strip()
        if text:
            s = start + lead
            out.append(Item(_kind(m[s:end].strip()), text, code.count("\n", 0, s) + 1))
        start = end

    while i < n:
        c = m[i]
        if c == "#" and paren == 0 and brace == 0 and m[start:i].strip() == "":
            j = code.find("\n", i)
            j = n if j < 0 else j
            emit(i)
            out.append(Item("directive", code[i:j].strip(), code.count("\n", 0, i) + 1))
            start = i = j
            continue
        if c in "([":
            paren += 1
        elif c in ")]":
            paren = max(0, paren - 1)
        elif c == "{":
            brace += 1
        elif c == "}":
            brace = max(0, brace - 1)
            if brace == 0 and paren == 0:
                rest = m[i + 1:].lstrip()
                head = m[start:i].lstrip()
                cont = rest.startswith((";", ")", ",", ".", "?", ":")) or \
                    re.match(r"(else|catch|finally)\b", rest) or \
                    (head.startswith("do") and rest.startswith("while"))
                if not cont:
                    emit(i + 1)
        elif c == ";" and paren == 0 and brace == 0:
            emit(i + 1)
        i += 1
    emit(n)
    return out


def _kind(masked: str) -> str:
    body = masked
    while True:
        a = ATTRIBUTE.match(body)
        if not a:
            break
        body = body[a.end():]
    if USING_DIRECTIVE.match(masked):
        return "using"
    if NAMESPACE.match(body):
        return "namespace"
    if TYPE_KW.match(body):
        return "type"
    if MODIFIER.match(body):
        return "member"
    if body.rstrip().endswith("}") and METHOD_SIG.match(body) and \
            not re.match(r"(if|for|foreach|while|switch|using|lock|return|await|var|new)\b",
                         body):
        return "member"
    return "statement"


# ------------------------------------------------------------------ analysis

def classify(code: str) -> tuple[str, list[dict]]:
    """(kind, declared types) - the same contract as the Python classifier.

    `declaration` is a block of types and members only; anything with a
    statement is a `program`, runnable once wrapped.
    """
    its = items(code)
    decls = [{"name": n, "params": [], "returns": "", "kind": "class", "decorated": False}
             for n in declared_types(code)]
    body = [it for it in its if it.kind not in ("using", "directive")]
    if not body:
        return "fragment", decls
    if all(it.kind in ("type", "namespace", "member") for it in body):
        return "declaration", decls
    return "program", decls


def declared_types(code: str) -> list[str]:
    return sorted(set(DECLARED.findall(mask(code))))


def namespaces_used(code: str) -> list[str]:
    """What a snippet claims to use: its `using` namespaces, and any qualified
    name written inline. The package it exercises is decided from these."""
    out: set[str] = set()
    for it in items(code):
        if it.kind == "using":
            m = re.search(r"([A-Za-z_][\w.]*)\s*(?:<[^;]*>)?\s*;$", it.text)
            if m:
                out.add(m.group(1))
    for q in QUALIFIED.findall(mask(code)):
        out.add(q)
    return sorted(out)


def has_elision(code: str) -> bool:
    """`...` standing in for code the docs left out. An argument list with a
    hole in it cannot be judged on its argument count."""
    return bool(ELISION.search(mask(code)))


# ------------------------------------------------------------------- wrapping

@dataclass
class Wrapped:
    source: str
    # file line (1-based) -> snippet line (1-based); header lines are absent
    lines: dict[int, int] = field(default_factory=dict)


def wrap(code: str, ns: str) -> Wrapped:
    """The snippet as a compilable file in namespace `ns`."""
    code = _neutralise(code)
    its = items(code)
    out: list[tuple[str, int | None]] = []

    def put(text: str, line: int | None, indent: str = "") -> None:
        for k, ln in enumerate(text.split("\n")):
            out.append((indent + ln if ln else ln, None if line is None else line + k))

    usings = [it for it in its if it.kind == "using"]
    types = [it for it in its if it.kind in ("type", "namespace")]
    members = [it for it in its if it.kind == "member"]
    stmts = [it for it in its if it.kind == "statement"]

    for it in usings:
        put(it.text, it.line)
    put(f"namespace {ns}", None)
    put("{", None)
    for it in types:
        put(it.text, it.line, "    ")
    if members:
        put("public partial class __Members", None, "    ")
        put("{", None, "    ")
        for it in members:
            put(it.text, it.line, "        ")
        put("}", None, "    ")
    if stmts:
        body = mask("\n".join(it.text for it in stmts))
        params = [f"{t} {n}" for t, n in AMBIENT if not _declares(body, n)]
        put("public static class __Run", None, "    ")
        put("{", None, "    ")
        put("public static async System.Threading.Tasks.Task __Main("
            + ", ".join(params) + ")", None, "        ")
        put("{", None, "        ")
        for it in stmts:
            put(it.text, it.line, "            ")
        put("}", None, "        ")
        put("}", None, "    ")
    put("}", None)

    lines = {i + 1: ln for i, (_, ln) in enumerate(out) if ln is not None}
    return Wrapped("\n".join(t for t, _ in out) + "\n", lines)


def _declares(masked: str, name: str) -> bool:
    """Whether the statements declare `name` themselves - `var builder = ...`
    would collide with an ambient parameter of the same name."""
    n = re.escape(name)
    return bool(re.search(
        rf"(?:\bvar|\b[A-Za-z_][\w.]*(?:<[^;=()]*>)?(?:\[\])?\??)\s+{n}\s*(?:=|;|\bin\b)",
        masked)) or bool(re.search(rf"\bout\s+(?:var|[\w.<>]+)\s+{n}\b", masked))


def _neutralise(code: str) -> str:
    """Remove what the wrapper would break: file-scoped namespaces, which
    cannot nest in ours, and region markers, which must pair within a file.
    Blanked rather than deleted, so line numbers hold."""
    m = mask(code)
    lines, mlines = code.split("\n"), m.split("\n")
    for k, ml in enumerate(mlines):
        s = ml.strip()
        if re.match(r"^namespace\s+[\w.]+\s*;$", s) or \
                re.match(r"^#\s*(region|endregion|pragma)\b", s):
            lines[k] = ""
    return "\n".join(lines)


# ------------------------------------------------------------ HEAD symbols

PUBLIC_TYPE = re.compile(
    r"^\s*public\s+(?:(?:static|sealed|abstract|partial|readonly|unsafe|new|ref)\s+)*"
    r"(?:class|interface|enum|struct|record(?:\s+(?:struct|class))?)\s+(\w+)", re.M)
PUBLIC_MEMBER = re.compile(
    r"^\s*public\s+(?:(?:static|virtual|override|abstract|async|sealed|new|required|"
    r"readonly|extern|unsafe)\s+)*[\w<>\[\],.?() ]+?\s+(\w+)\s*(?:<[^>(]*>)?\s*[({=;]", re.M)
PUBLIC_EXTENSION = re.compile(
    r"^\s*public\s+static\s+[\w<>\[\],.? ]+?\s+(\w+)\s*(?:<[^>(]*>)?\s*\(\s*this\s+"
    r"([\w.]+)", re.M)
NAMESPACE_DECL = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
# C# 14: `extension(IIdentityServerBuilder builder) { public ... AddX() }`
EXTENSION_BLOCK = re.compile(r"^\s*extension\s*(?:<[^>]*>)?\s*\(\s*([\w.]+)", re.M)


def surface(files: list[str]) -> dict:
    """A package's public API from its source at HEAD, in the shape the
    harness reflects from a release: namespaces, top-level types, each type's
    members, extension method names.

    Regex over masked source, with each member credited to the innermost type
    whose braces enclose it; partial classes merge by name. It over-collects
    a little - which only widens what a compiler error may be judged against,
    never what is reported.
    """
    ns: set[str] = set()
    types: set[str] = set()
    members: dict[str, set[str]] = {}
    ext: dict[str, set[str]] = {}
    for text in files:
        m = mask(text)
        ns.update(NAMESPACE_DECL.findall(m))
        spans = []                                # (body start, body end, type name)
        for t in PUBLIC_TYPE.finditer(m):
            body = _body(m, t.end())
            if body:
                spans.append((body[0], body[1], t.group(1)))
        blocks = [(*span, _simple(e.group(1))) for e in EXTENSION_BLOCK.finditer(m)
                  if (span := _body(m, e.end()))]
        every = {name for _, _, name in spans} | set(PUBLIC_TYPE.findall(m))
        for t in PUBLIC_TYPE.finditer(m):
            if not any(a < t.start() < b for a, b, _ in spans):
                types.add(t.group(1))
        for mm in PUBLIC_MEMBER.finditer(m):
            name = mm.group(1)
            inside = [(b - a, owner) for a, b, owner in spans if a < mm.start() < b]
            if inside and name not in every:
                members.setdefault(min(inside)[1], set()).add(name)
                for a, b, receiver in blocks:
                    if a < mm.start() < b:
                        ext.setdefault(receiver, set()).add(name)
        for name, receiver in PUBLIC_EXTENSION.findall(m):
            ext.setdefault(_simple(receiver), set()).add(name)
    return {"namespaces": sorted(ns), "types": sorted(types),
            "members": {t: sorted(v) for t, v in members.items()},
            "extensions": {t: sorted(v) for t, v in ext.items()}}


def _simple(type_name: str) -> str:
    return type_name.rsplit(".", 1)[-1]


def _body(masked: str, pos: int) -> tuple[int, int] | None:
    """(open, close) offsets of the brace block a declaration at `pos` opens,
    or None when a `;` outside parentheses ends it first
    (`public record R(string A);`)."""
    depth = paren = 0
    start = -1
    for i in range(pos, len(masked)):
        c = masked[i]
        if c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif c == ";" and paren == 0 and start < 0:
            return None
        elif c == "{":
            if start < 0:
                start = i
            depth += 1
        elif c == "}" and start >= 0:
            depth -= 1
            if depth == 0:
                return start, i
    return (start, len(masked)) if start >= 0 else None
