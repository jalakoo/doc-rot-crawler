"""Is a snippet a runnable program, a declaration, or a fragment?

Reference documentation is full of blocks like

    def delete_file(file_id: str) -> None:

which are signatures on display, not code to run. Executing them yields an
IndentationError that says nothing about whether the documentation is correct.

Worse, a fragment that happens to run proves nothing either. So classify
statically, before any sandbox is involved:

  program     self-contained; every free name is imported or built in
  declaration signatures only - compare them against introspection instead
  fragment    references names it never binds; not safe to execute
"""
from __future__ import annotations

import ast
import builtins

BUILTINS = set(dir(builtins))
TYPING = {"Optional", "List", "Dict", "Any", "Union", "Tuple", "Set", "Literal",
          "Callable", "Iterable", "Sequence", "Mapping", "Self", "Type",
          # Pydantic names appear unbound in a documented model declaration,
          # which made `class Entity(BaseModel)` read as a fragment and drop to
          # `unverifiable`. Measured in S3: 3 snippets, the extraction model's
          # only correct disagreement with the rules.
          "BaseModel", "Field", "ConfigDict", "Enum", "dataclass", "datetime"}


def classify(code: str) -> tuple[str, list[dict]]:
    """Returns (kind, declared_signatures)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # a `def f(x) -> T:` line with its body elided will not parse, but
        # it is a signature on display - the most useful shape there is
        headers = _headers_from_text(code)
        return ("declaration" if headers else "fragment"), headers

    if not tree.body:
        return "fragment", []

    decls = [_sig(n) for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            bases = [ast.unparse(b) for b in n.bases]
            decls.append({"name": n.name, "params": _class_fields(n),
                          "returns": "", "kind": "class",
                          "is_enum": any("Enum" in b for b in bases),
                          "decorated": bool(n.decorator_list)})

    executable = [n for n in tree.body if not isinstance(
        n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
            ast.Import, ast.ImportFrom, ast.Expr))]

    # Only calls that would actually *run* disqualify a block from being a
    # declaration. A call inside a class body - `x: T = Field(default=...)`,
    # or a default argument - is declaration syntax, not executable code.
    # Counting those made every documented Pydantic model read as a fragment.
    calls = [n for node in executable for n in ast.walk(node)
             if isinstance(n, ast.Call)]

    if decls and not executable and not calls:
        return "declaration", decls

    if _free_names(tree):
        return "fragment", decls

    return "program", decls


def _sig(node) -> dict:
    a = node.args
    params = [p.arg for p in (*a.posonlyargs, *a.args)]
    if a.vararg:
        params.append("*" + a.vararg.arg)
    params += [p.arg for p in a.kwonlyargs]
    if a.kwarg:
        params.append("**" + a.kwarg.arg)
    return {
        "name": node.name,
        "params": params,
        "returns": ast.unparse(node.returns) if node.returns else "",
        "kind": "function",
        # Reference docs display a bare signature. A decorated definition is
        # code being written - a tutorial's own `@st.cache_data def load_data`
        # lives in the snippet, not in the package.
        "decorated": bool(node.decorator_list),
    }


def _free_names(tree: ast.AST) -> set[str]:
    """Names read at module level that nothing in the snippet ever binds."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for x in node.names:
                bound.add(x.asname or x.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for x in node.names:
                bound.add(x.asname or x.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.alias):
            bound.add(node.asname or node.name.split(".")[0])

    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    for node in ast.walk(tree):          # base classes, annotations
        if isinstance(node, ast.ClassDef):
            for b in node.bases:
                if isinstance(b, ast.Name):
                    used.add(b.id)

    return used - bound - BUILTINS - TYPING


def _class_fields(node: ast.ClassDef) -> list[str]:
    """Annotated class attributes are the documented field list.

    `class File(BaseModel): id: str; name: str` documents two fields. Reporting
    zero made the probe flag every real field as undocumented.
    """
    fields: list[str] = []
    for sub in node.body:
        if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
            fields.append(sub.target.id)
        elif isinstance(sub, ast.Assign):
            for tgt in sub.targets:
                if isinstance(tgt, ast.Name):
                    fields.append(tgt.id)
    return fields


def split_params(text: str) -> list[str]:
    """Split a parameter list on commas at bracket depth 0.

    Splitting on every comma turns `metadata: Optional[Dict[str, Any]] = None`
    into a parameter called `Any]]` - six of the false positives S2 measured.
    """
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [p.strip() for p in out if p.strip()]


def _headers_from_text(code: str) -> list[dict]:
    """Signatures from a block that will not parse - a bare `def f(x): ` line
    with its body elided is the single commonest shape in reference docs.

    Walks to the matching close paren rather than trusting `[^)]*`, which stops
    at the first `)` and so truncates any annotation containing one.
    """
    import re
    out = []
    for m in re.finditer(r"^[ \t]*(?:async\s+)?def\s+(\w+)\s*\(", code, re.M):
        prior = code[:m.start()].rstrip().rsplit("\n", 1)[-1].lstrip()
        decorated = prior.startswith("@")
        i, depth = m.end() - 1, 0
        while i < len(code):
            if code[i] in "([{":
                depth += 1
            elif code[i] in ")]}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        inner = code[m.end():i]
        rm = re.match(r"\s*->\s*([^:\n]+):", code[i + 1:i + 200])
        params = [raw.split(":")[0].split("=")[0].strip()
                  for raw in split_params(inner)
                  if raw not in ("self", "cls")]
        out.append({"name": m.group(1), "params": [p for p in params if p],
                    "returns": (rm.group(1).strip() if rm else ""),
                    "kind": "function", "decorated": decorated})
    return out
