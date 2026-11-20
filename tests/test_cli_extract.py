"""Which package owns a command line, and what that does to a snippet's tier.

The measured case: Relate's docs run the standalone `relate` binary in 330
snippets. `@relate/relate` shares the name but declares no `bin`, and counting
those snippets as its own left every one `structural` and unverified.
"""
from __future__ import annotations

from docrot.extract import fences, symbols, tiers
from docrot.extract.commands import MIN_COMMAND_LINES, Ownership, command_lines
from docrot.extract.pipeline import attribute, extract, match_languages
from docrot.models import PackageRef, Page, Snippet

PYPI = PackageRef(package="relate-db", import_name="relate", ecosystem="pypi")
NPM = PackageRef(package="@relate/relate", import_name="@relate/relate", ecosystem="npm")
CLI = PackageRef(package="relate", import_name="relate", ecosystem="cli")

DOCS = """\
# Relate

```bash
relate query "MATCH (n) RETURN n"
relate clone acme/graph
relate serve --port 8080
```

```bash
npx @relate/relate init
```

```python
import relate
relate.connect()
```

```js
import { Relate } from "@relate/relate";
```

```bash
pip install relate-db
```

```bash
git status
```

Run `relate query` to ask. relate relate relate.
"""


def _extract(packages, cfg, bins=None, text=DOCS):
    page = Page(url="https://a.io/docs", path="/docs", text=text)
    return extract([page], packages, cfg, log=lambda m: None, bins=bins)


def _by_code(ex, start):
    return next(s for s in ex.snippets if s.code.startswith(start))


# --------------------------------------------------------- the measured overstatement

def test_commands_of_a_cli_target_are_structural_and_attributed_to_it_not_to_the_npm_package(cfg):
    ex = _extract([PYPI, NPM, CLI], cfg, bins={"@relate/relate": ()})
    q = _by_code(ex, "relate query")
    assert (q.package, q.tier, q.reason) == ("relate", "structural", "")
    assert "cli:relate" in q.symbols and "cli:relate query" in q.symbols


def test_without_a_cli_target_those_commands_are_unverifiable_not_structural(cfg):
    ex = _extract([PYPI, NPM], cfg, bins={"@relate/relate": ()})
    q = _by_code(ex, "relate query")
    assert (q.tier, q.reason) == ("unverifiable", "no-package-use")
    assert not any(s.startswith("cli:") for s in q.symbols)
    assert q.package != "@relate/relate" or q.tier == "unverifiable"


def test_an_npm_package_with_a_matching_declared_bin_owns_the_command(cfg):
    ex = _extract([PYPI, NPM, CLI], cfg, bins={"@relate/relate": ("relate",)})
    q = _by_code(ex, "relate query")
    assert (q.package, q.tier) == ("@relate/relate", "structural")


def test_a_registry_only_npm_package_keeps_owning_the_command_named_like_it(cfg):
    plain = PackageRef(package="relate", import_name="relate", ecosystem="npm")
    ex = _extract([PYPI, plain], cfg)                   # no bins entry: package.json was never read
    q = _by_code(ex, "relate query")
    assert (q.package, q.tier) == ("relate", "structural")


def test_an_explicit_cli_target_beats_the_name_guess_for_a_package_nobody_read(cfg):
    plain = PackageRef(package="relate", import_name="relate", ecosystem="npm")
    own = Ownership(["relate"], None, ["relate"])
    assert {r.kind for r in own.runs("relate query")} == {"cli"}
    assert {r.kind for r in Ownership(["relate"], {"relate": ("relate",)}, ["relate"]).runs("relate query")} == {"npm"}
    assert plain.ecosystem == "npm"


def test_npx_always_belongs_to_the_npm_package(cfg):
    ex = _extract([PYPI, NPM, CLI], cfg, bins={"@relate/relate": ()})
    s = _by_code(ex, "npx @relate/relate")
    assert (s.package, s.tier) == ("@relate/relate", "structural")


def test_npx_of_somebody_elses_package_is_nobodys_command(cfg):
    own = Ownership([], None, ["relate"])
    assert own.runs("npx -p other relate query") == []
    assert own.runs("npx relate query") == []


# ------------------------------------------------------------- other snippets are left alone

def test_the_cli_target_does_not_take_the_python_packages_import(cfg):
    ex = _extract([PYPI, NPM, CLI], cfg, bins={"@relate/relate": ()})
    py = _by_code(ex, "import relate")
    assert py.package == "relate-db"
    assert py.tier != "unverifiable"
    assert "relate" in py.symbols or "relate.connect" in py.symbols


def test_a_pip_install_still_belongs_to_the_python_package(cfg):
    ex = _extract([PYPI, NPM, CLI], cfg, bins={"@relate/relate": ()})
    s = _by_code(ex, "pip install")
    assert s.package == "relate-db"


def test_shell_that_runs_nothing_of_ours_is_unverifiable_for_a_cli_scan_too(cfg):
    ex = _extract([PYPI, CLI], cfg)
    s = _by_code(ex, "git status")
    assert (s.tier, s.reason) == ("unverifiable", "no-package-use")


# -------------------------------------------------------------- languages for a CLI target

def test_a_python_or_js_snippet_attributed_to_the_cli_is_unverifiable_in_another_language(cfg):
    ex = _extract([CLI], cfg)                           # the CLI is the primary: fallback attribution
    py = _by_code(ex, "import relate")
    js = _by_code(ex, "import { Relate }")
    assert (py.package, py.tier, py.reason) == ("relate", "unverifiable", "other-language")
    assert (js.package, js.tier, js.reason) == ("relate", "unverifiable", "other-language")


def test_a_shell_block_that_does_not_run_the_cli_is_no_package_use_for_a_cli_primary(cfg):
    ex = _extract([CLI], cfg)
    s = _by_code(ex, "git status")
    assert (s.tier, s.reason) == ("unverifiable", "no-package-use")


def test_only_shell_is_a_cli_languages(cfg):
    from docrot.extract.pipeline import LANGUAGE
    assert LANGUAGE["cli"] == "shell"
    s = Snippet(id="s", page="p", lang="python", code="print(1)", tier="structural", package="relate")
    match_languages([s], [CLI])
    assert s.tier == "unverifiable" and s.reason == "other-language"


# --------------------------------------------------------------------- the primary

def test_the_cli_is_never_the_primary_when_a_package_target_exists(cfg):
    ex = _extract([PYPI, NPM, CLI], cfg, bins={"@relate/relate": ()})
    assert ex.package == "relate-db" and ex.ecosystem == "pypi"
    assert [p.ecosystem for p in ex.packages] == ["pypi", "npm", "cli"]
    unnamed = _by_code(ex, "git status")
    assert unnamed.package == "relate-db"               # fallback attribution is the primary


def test_a_snippet_running_the_command_with_a_package_primary_stays_attributed_by_run(cfg):
    snips = [Snippet(id="a", page="p", lang="bash", code="relate query x", tier="structural")]
    symbols.annotate(snips, ["relate"], npm_packages=[], own=Ownership([], None, ["relate"]))
    attribute(snips, [PYPI, CLI])
    assert snips[0].package == "relate"


# ------------------------------------------------------------- the pre-scan of the docs

def _page(text):
    return Page(url="u", path="/p", text=text)


def test_command_lines_counts_shell_fences_only_not_prose():
    prose = "relate query is great. Run `relate clone x` and relate serve.\n"
    assert command_lines([_page(prose)], "relate") == 0
    fenced = "```bash\n$ relate query a\n$ relate clone b\noutput line\n```\n```text\nrelate query not shell\n```\n"
    assert command_lines([_page(fenced)], "relate") == 2


def test_command_lines_ignores_npx_runs_of_a_package_with_that_name():
    assert command_lines([_page("```bash\nnpx relate init\nnpx relate query\n```\n")], "relate") == 0


def test_the_bar_is_three_command_lines():
    assert MIN_COMMAND_LINES == 3


def test_fences_family_of_console_is_shell():
    assert fences.family("console") == "shell"


def test_tiers_without_any_target_command_are_unchanged():
    s = Snippet(id="s", page="p", lang="bash", code="relate query x")
    tiers.assign([s], set(), ["relate"], npm_packages=[])
    assert (s.tier, s.reason) == ("unverifiable", "no-package-use")
