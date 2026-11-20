"""The npm lane's extraction side: which snippets reach it, as what, for which package."""
from __future__ import annotations

import pytest

from docrot.extract import fences, symbols, tiers
from docrot.extract.pipeline import (
    _root,
    attribute,
    extract,
    match_languages,
    not_checked_line,
    prune_declares,
)
from docrot.models import REASONS, PackageRef, Page, Snippet

NPM = PackageRef(package="momentic", import_name="momentic", ecosystem="npm")
PYPI = PackageRef(package="alpha", import_name="alpha", ecosystem="pypi")


def _snip(lang, code, id_="s"):
    return Snippet(id=id_, page="p", lang=lang, code=code)


def _run(lang, code, npm=("momentic",), creds=frozenset(), assume=False):
    s = _snip(lang, code)
    symbols.annotate([s], list(npm), npm_packages=list(npm))
    tiers.assign([s], set(creds), list(npm), assume, npm_packages=list(npm))
    return s


# ------------------------------------------------------------------ symbols

def test_named_imports_become_specifier_dot_name():
    s = _run("js", "import { run } from 'momentic'\nimport fs from 'fs'\nimport x from 'lodash'\nrun()")
    assert s.symbols == ["momentic.run"]


def test_a_namespace_import_is_the_bare_specifier_plus_its_members():
    s = _run("javascript", "import * as s from 'momentic/server'\ns.start()")
    assert s.symbols == ["momentic/server", "momentic/server.start"]


def test_default_and_type_only_imports_are_symbols_too():
    s = _run("ts", "import m from 'momentic'\nimport type { Config } from 'momentic'\nm.go()")
    assert s.symbols == ["momentic.Config", "momentic.default", "momentic.default.go"]


def test_scoped_package_symbols_keep_the_scope():
    s = _run("js", "const { init } = require('@momentic/wizard')", npm=("@momentic/wizard",))
    assert s.symbols == ["@momentic/wizard.init"]


def test_js_has_no_symbols_without_npm_packages():
    s = _snip("js", "import { run } from 'momentic'")
    symbols.annotate([s], ["alpha"])
    assert s.symbols == []


def test_shell_gets_install_and_cli_tokens_beside_pip_and_env():
    s = _run("bash", "pip install alpha==1\nexport MOMENTIC_API_KEY=x\n"
                     "npm install -g momentic@2.1.0 left-pad\nnpx momentic run flow.yaml --ci\n"
                     "momentic ./flows")
    assert s.symbols == ["cli:momentic", "cli:momentic run", "env:MOMENTIC_API_KEY",
                         "npm:left-pad", "npm:momentic@2.1.0", "pip:alpha==1"]


def test_a_path_or_url_is_not_a_subcommand():
    s = _run("sh", "$ npx momentic https://x.io/a\n$ npx momentic Flow.yaml")
    assert s.symbols == ["cli:momentic"]


def test_shell_without_npm_packages_is_unchanged():
    s = _snip("bash", "npm install momentic\npip install alpha")
    symbols.annotate([s], ["alpha"])
    assert s.symbols == ["pip:alpha"]


# -------------------------------------------------------------------- tiers

def test_a_js_snippet_using_the_package_is_structural_by_default():
    assert _run("js", "import { run } from 'momentic'\nawait run('a')").tier == "structural"


def test_the_blanket_rule_lifts_with_assume_credentialed_or_held_creds():
    code = "import { run } from 'momentic'\nawait run('a')"
    assert _run("js", code, assume=True).tier == "execute"
    assert _run("js", code, creds={"OTHER_API_KEY"}).tier == "execute"


@pytest.mark.parametrize("extra", [
    "fetch('https://x.io')", "axios.get('u')", "http.request(o)", "new WebSocket(u)",
    "app.listen(3000)", "const k = process.env.MOMENTIC_API_KEY", "const key = 'your-key'",
    "document.title = 'x'", "window.open()", "localStorage.setItem('a', 'b')",
    "navigator.userAgent", "const el = <App />", "const el = <div>hi</div>",
])
def test_nothing_we_lack_is_ever_promoted(extra):
    code = f"import {{ run }} from 'momentic'\n{extra}\nrun()"
    assert _run("js", code, assume=True).tier == "structural"


def test_jsx_fences_and_type_only_use_stay_structural():
    code = "import { run } from 'momentic'\nrun()"
    assert _run("tsx", code, assume=True).tier == "structural"
    assert _run("ts", "import type { A } from 'momentic'\nlet a: A", assume=True).tier == "structural"


def test_markup_inside_a_string_or_a_ts_generic_is_not_jsx():
    code = "import { run } from 'momentic'\nconst a: Map<string, number> = new Map()\nconst h = '</x'\nrun(a, h)"
    assert _run("ts", code, assume=True).tier == "execute"


def test_a_credential_we_hold_promotes():
    code = "import { run } from 'momentic'\nrun(process.env.MOMENTIC_API_KEY)"
    assert _run("js", code, creds={"MOMENTIC_API_KEY"}).tier == "execute"


def test_js_of_an_unrelated_library_is_unverifiable():
    assert _run("js", "import _ from 'lodash'\n_.map([])").tier == "unverifiable"
    assert _run("js", "console.log(1)").tier == "unverifiable"


def test_js_is_unverifiable_in_a_scan_without_npm_packages():
    s = _snip("js", "import { run } from 'momentic'")
    tiers.assign([s], set(), ["alpha"])
    assert s.tier == "unverifiable"


def test_a_pure_install_of_our_package_runs():
    assert _run("bash", "npm install momentic").tier == "execute"
    assert _run("bash", "npm i momentic@1.2 @momentic/wizard", npm=("momentic", "@momentic/wizard")).tier \
        == "execute"


def test_an_install_of_other_packages_tells_us_nothing():
    assert _run("bash", "npm install left-pad").tier == "unverifiable"
    assert _run("bash", "npm install momentic left-pad").tier == "unverifiable"


def test_a_cli_invocation_is_structural_never_run():
    assert _run("bash", "npx momentic run x.yaml --ci").tier == "structural"
    assert _run("bash", "npx momentic run x.yaml --ci", assume=True).tier == "structural"


def test_pip_install_keeps_todays_rule_in_an_npm_scan():
    assert _run("bash", "pip install alpha").tier == "execute"
    assert _run("bash", "echo hi").tier == "unverifiable"


def test_prose_languages_stay_unverifiable():
    assert _run("yaml", "name: momentic\nsteps: []").tier == "unverifiable"


# ------------------------------------------------------------------- touches

def test_touches_reads_npm_names_whole():
    s = Snippet(id="1", page="p", lang="python", code="", symbols=["socket.io.Server"])
    assert tiers._touches(s, "socket.io")        # split on the first dot it was `socket`
    sub = Snippet(id="2", page="p", lang="js", code="", symbols=["momentic/server.start"])
    assert tiers._touches(sub, "momentic")
    assert not tiers._touches(sub, "mom")


# --------------------------------------------------------------- attribution

def test_root_accepts_subpaths_and_never_splits_a_dotted_name():
    imports = {"momentic": "momentic", "socket": "socket", "socket.io": "socket.io"}
    npm = set(imports)
    assert _root("momentic/server.start", imports, npm) == "momentic"
    assert _root("socket.io", imports, npm) == "socket.io"
    assert _root("socket.io.Server", imports, npm) == "socket.io"
    assert _root("socket.io", {"socket": "socket"}, {"socket"}) == ""
    assert _root("momentic.run", {"momentic": "momentic"}) == "momentic"
    assert _root("momentic/server", {"momentic": "momentic"}) == ""     # not an npm name: as before


def test_a_js_snippet_belongs_to_the_first_package_it_imports():
    pkgs = [PYPI, NPM, PackageRef(package="socket.io", import_name="socket.io", ecosystem="npm")]
    a = _snip("js", "import io from 'socket.io'\nimport m from 'momentic'")
    b = _snip("js", "import { x } from 'momentic/server'")
    c = _snip("js", "import { x } from 'lodash'")
    attribute([a, b, c], pkgs)
    assert [s.package for s in (a, b, c)] == ["socket.io", "momentic", "alpha"]


def test_shell_is_attributed_by_npm_or_cli_tokens():
    wizard = PackageRef(package="@momentic/wizard", import_name="@momentic/wizard", ecosystem="npm")
    pkgs = [PYPI, NPM, wizard]
    install = _snip("bash", "")
    install.symbols = ["npm:@momentic/wizard@1.0.0"]
    cli = _snip("bash", "")
    cli.symbols = ["cli:wizard", "cli:wizard init"]
    bare = _snip("bash", "")
    bare.symbols = ["cli:momentic", "cli:momentic run"]
    attribute([install, cli, bare], pkgs)
    assert [s.package for s in (install, cli, bare)] == ["@momentic/wizard", "@momentic/wizard", "momentic"]


# ----------------------------------------------------------------- languages

def test_languages_must_match_the_attributed_ecosystem():
    pkgs = [PYPI, NPM]
    cases = [
        ("python", "import alpha", "execute", "alpha", "execute"),
        ("python", "print(1)", "structural", "momentic", "unverifiable"),
        ("js", "", "structural", "alpha", "unverifiable"),
        ("js", "", "structural", "momentic", "structural"),
        ("csharp", "", "structural", "momentic", "unverifiable"),
    ]
    snippets = []
    for lang, code, tier, package, _ in cases:
        s = _snip(lang, code)
        s.tier, s.package = tier, package
        snippets.append(s)
    match_languages(snippets, pkgs)
    assert [s.tier for s in snippets] == [c[4] for c in cases]


def test_shell_attributed_to_npm_must_install_or_run_it():
    pip_only = _snip("bash", "pip install alpha")
    pip_only.tier, pip_only.package, pip_only.symbols = "execute", "momentic", ["pip:alpha"]
    ok = _snip("bash", "npm i momentic")
    ok.tier, ok.package, ok.symbols = "execute", "momentic", ["npm:momentic"]
    npm_for_pypi = _snip("bash", "npm i momentic")
    npm_for_pypi.tier, npm_for_pypi.package, npm_for_pypi.symbols = "execute", "alpha", ["npm:momentic"]
    pip_ok = _snip("bash", "pip install alpha")
    pip_ok.tier, pip_ok.package, pip_ok.symbols = "execute", "alpha", ["pip:alpha"]
    match_languages([pip_only, ok, npm_for_pypi, pip_ok], [PYPI, NPM])
    assert [s.tier for s in (pip_only, ok, npm_for_pypi, pip_ok)] == \
        ["unverifiable", "execute", "unverifiable", "execute"]
    assert [s.reason for s in (pip_only, ok, npm_for_pypi, pip_ok)] == \
        ["no-package-use", "", "other-language", ""]


# ------------------------------------------------------------------- sniffing

def test_unlabelled_js_is_sniffed_without_stealing_python_or_bash():
    sniff = fences._sniff
    assert sniff("import { run } from 'momentic'\nrun()") == "javascript"
    assert sniff("const m = require('momentic')") == "javascript"
    assert sniff("import os\nfrom a import b") == "python"
    assert sniff("def f():\n    return 1") == "python"
    assert sniff("npx momentic run") == "bash"
    assert sniff("pip install x") == "bash"
    assert sniff("x = 1") == "text"


# ------------------------------------------------------------------ end to end

def _page(url, text):
    return Page(url=url, path=url.rsplit("/", 1)[-1], title=url, source="site", text=text)


DOCS = '''# Momentic

```js
import { run } from 'momentic'
await run('flow.yaml')
```

```ts
import * as server from 'momentic/server'
server.start()
```

```ts
import type { MomenticConfig } from 'momentic'
const c: MomenticConfig = {}
```

```js
import _ from 'lodash'
_.chunk([1], 1)
```

```bash
npx momentic run x.yaml --ci
```

```bash
npm install momentic
```

```bash
npm install left-pad
```

```bash
pip install alpha
```

```yaml
steps:
  - run: x
```

```python
import alpha
alpha.go()
```
'''


def _by_lang(snips):
    return {(s.lang, s.code.splitlines()[0]): s for s in snips}


def test_an_npm_scan_end_to_end(cfg):
    ex = extract([_page("https://d.io/a", DOCS)], [NPM], cfg, log=lambda *_: None)
    got = _by_lang(ex.snippets)
    rows = {k: (s.tier, s.package, s.symbols) for k, s in got.items()}
    assert rows[("js", "import { run } from 'momentic'")] == ("structural", "momentic", ["momentic.run"])
    assert rows[("ts", "import * as server from 'momentic/server'")] == \
        ("structural", "momentic", ["momentic/server", "momentic/server.start"])
    assert rows[("ts", "import type { MomenticConfig } from 'momentic'")] == \
        ("structural", "momentic", ["momentic.MomenticConfig"])
    assert rows[("js", "import _ from 'lodash'")] == ("unverifiable", "momentic", [])
    assert rows[("bash", "npx momentic run x.yaml --ci")] == \
        ("structural", "momentic", ["cli:momentic", "cli:momentic run"])
    assert rows[("bash", "npm install momentic")] == ("execute", "momentic", ["npm:momentic"])
    assert rows[("bash", "npm install left-pad")] == ("unverifiable", "momentic", ["npm:left-pad"])
    assert rows[("bash", "pip install alpha")][0] == "unverifiable"      # no pip lane in an npm scan
    assert rows[("yaml", "steps:")][0] == "unverifiable"
    assert rows[("python", "import alpha")][0] == "unverifiable"


def test_a_mixed_scan_keeps_every_snippet_in_its_own_lane(cfg):
    ex = extract([_page("https://d.io/a", DOCS)], [PYPI, NPM], cfg, log=lambda *_: None)
    rows = {k: (s.tier, s.package) for k, s in _by_lang(ex.snippets).items()}
    assert rows[("python", "import alpha")] == ("structural", "alpha")
    assert rows[("bash", "pip install alpha")] == ("execute", "alpha")
    assert rows[("js", "import { run } from 'momentic'")] == ("structural", "momentic")
    assert rows[("bash", "npm install momentic")] == ("execute", "momentic")
    assert rows[("bash", "npx momentic run x.yaml --ci")] == ("structural", "momentic")
    assert rows[("js", "import _ from 'lodash'")][0] == "unverifiable"
    assert rows[("bash", "npm install left-pad")][0] == "unverifiable"
    for s in ex.snippets:
        if s.tier != "unverifiable":
            lane = {"alpha": {"python", "bash"}, "momentic": {"js", "ts", "bash"}}[s.package]
            assert s.lang in lane, s


def test_a_python_only_scan_is_unchanged_by_the_npm_lane(cfg):
    ex = extract([_page("https://d.io/a", DOCS)], [PYPI], cfg, log=lambda *_: None)
    rows = {k: (s.tier, s.package, s.symbols) for k, s in _by_lang(ex.snippets).items()}
    assert rows[("js", "import { run } from 'momentic'")] == ("unverifiable", "alpha", [])
    assert rows[("bash", "npm install momentic")] == ("unverifiable", "alpha", [])
    assert rows[("bash", "npx momentic run x.yaml --ci")] == ("unverifiable", "alpha", [])
    assert rows[("bash", "pip install alpha")] == ("execute", "alpha", ["pip:alpha"])
    assert rows[("python", "import alpha")] == ("structural", "alpha", ["alpha", "alpha.go"])


# -------------------------------------------------------------------- reasons

# one fence per way of going unchecked, plus two that are checked
CORPUS = '''# Mixed

```rust
fn main() {}
```

```swift
let x = 1
```

```cypher
MATCH (n) RETURN n
```

```json
{"a": 1}
```

```python
import alpha
alpha.go()
```

```python
print("hi")
```

```js
import { run } from 'momentic'
run()
```

```js
import _ from 'lodash'
_.chunk([1], 1)
```

```bash
npm i momentic
```

```bash
git clone https://example.com/x.git
```

```bash
pip install alpha
```
'''


def _reasons(packages, cfg, text=CORPUS):
    ex = extract([_page("https://d.io/a", text)], packages, cfg, log=lambda *_: None)
    return {(s.lang, s.code.splitlines()[0]): (s.tier, s.reason) for s in ex.snippets}


def test_a_language_with_no_lane_is_no_checker_and_prose_is_not_code(cfg):
    got = _reasons([NPM], cfg)
    assert got[("rust", "fn main() {}")] == ("unverifiable", "no-checker")
    assert got[("swift", "let x = 1")] == ("unverifiable", "no-checker")
    assert got[("cypher", "MATCH (n) RETURN n")] == ("unverifiable", "not-code")
    assert got[("json", '{"a": 1}')] == ("unverifiable", "not-code")


def test_an_npm_only_scan_gives_python_other_language_and_unrelated_js_no_package_use(cfg):
    got = _reasons([NPM], cfg)
    assert got[("python", "import alpha")] == ("unverifiable", "other-language")
    assert got[("js", "import _ from 'lodash'")] == ("unverifiable", "no-package-use")
    assert got[("bash", "git clone https://example.com/x.git")] == ("unverifiable", "no-package-use")
    assert got[("js", "import { run } from 'momentic'")] == ("structural", "")


def test_a_pypi_only_scan_gives_js_other_language(cfg):
    got = _reasons([PYPI], cfg)
    assert got[("js", "import { run } from 'momentic'")] == ("unverifiable", "other-language")
    assert got[("bash", "npm i momentic")] == ("unverifiable", "no-package-use")   # no npm to name
    assert got[("python", "print(\"hi\")")][1] == ""


def test_a_mixed_scan_labels_the_python_that_names_no_npm_package(cfg):
    got = _reasons([NPM, PYPI], cfg)
    # no package named: it falls to the primary, an npm one, so it is the wrong language
    assert got[("python", 'print("hi")')] == ("unverifiable", "other-language")
    assert got[("python", "import alpha")][1] == ""
    assert got[("js", "import { run } from 'momentic'")][1] == ""


def test_a_fragment_and_an_unowned_declaration_say_so(cfg):
    ex = extract([_page("https://d.io/a", "```python\nresult = widget.run()\n```\n\n"
                                          "```csharp\nvar x = Foo();\n```\n")],
                 [PYPI], cfg, log=lambda *_: None)
    assert [(s.lang, s.reason) for s in ex.snippets] == \
        [("python", "fragment"), ("csharp", "other-language")]
    decl = _snip("python", "class Model(BaseModel):\n    x: int")
    decl.tier, decl.kind, decl.declares = "structural", "declaration", [{"name": "Model"}]
    prune_declares([decl], {"alpha.Other"})
    assert (decl.tier, decl.reason) == ("unverifiable", "not-declared")


@pytest.mark.parametrize("packages", [[NPM], [PYPI], [NPM, PYPI], [PYPI, NPM]])
def test_every_unverifiable_snippet_has_a_reason_and_nothing_else_does(cfg, packages):
    ex = extract([_page("https://d.io/a", CORPUS)], packages, cfg, log=lambda *_: None)
    for s in ex.snippets:
        assert (s.tier == "unverifiable") == bool(s.reason), (s.lang, s.code, s.tier, s.reason)
        assert not s.reason or s.reason in REASONS


def test_the_log_names_what_was_not_checked_and_which_languages_lack_a_checker(cfg):
    lines = []
    extract([_page("https://d.io/a", CORPUS)], [NPM], cfg, log=lines.append)
    [gap] = [ln for ln in lines if "not checked:" in ln]
    assert gap.startswith("  not checked: ")
    assert "2 not code" in gap and "no checker (rust 1, swift 1)" in gap


def test_nothing_unverifiable_logs_no_gap_line():
    s = _snip("js", "")
    s.tier = "structural"
    assert not_checked_line([s]) == ""
