"""JavaScript / TypeScript parsing: masking, module uses, shell commands, installs,
module kind, and the symbol table of a package's source."""
from __future__ import annotations

import textwrap

import pytest

from docrot.extract import javascript as js
from docrot.extract.javascript import Invocation, Use, invocations, uses


def _src(code: str) -> str:
    return textwrap.dedent(code).strip("\n")


def _pairs(code: str) -> list[tuple[str, str]]:
    return [(u.specifier, u.name) for u in uses(code)]


# --------------------------------------------------------------------- langs
def test_language_sets_are_case_insensitive_and_ts_is_a_subset():
    assert js.is_js("JavaScript") and js.is_js("mjs") and js.is_js("TSX")
    assert js.is_ts("TypeScript") and js.is_ts("cts")
    assert not js.is_ts("js") and not js.is_js("python") and not js.is_js("bash")
    assert js.TS_LANGS < js.JS_LANGS
    assert {"bash", "console", "zsh"} <= js.SHELL_LANGS


# ------------------------------------------------------------------- masking
MASK_FIXTURES = [
    "const a = 'x'; // c\n/* multi\nline */ const b = \"y\";\n",
    "const re = /['\"]/g; const s = `a ${ \"}\" } b ${`n${1}`}`;\nrequire('x');",
    "const el = <div className=\"x\">Don't</div>;\nconst y = require('after');",
    "const a = 1 / 2 / 3; const r = x.replace(/\\//g, '/');",
    "let unterminated = 'oops\nnext line",
    "`never closed ${ x",
    "/* never closed",
    "",
    "a\r\nb\r\n'c'\r\n",
]


@pytest.mark.parametrize("code", MASK_FIXTURES)
def test_mask_keeps_length_and_newline_positions(code):
    m = js.mask(code)
    assert len(m) == len(code)
    assert [i for i, c in enumerate(m) if c == "\n"] == [i for i, c in enumerate(code) if c == "\n"]


def test_mask_blanks_comments_and_literal_contents_but_keeps_delimiters():
    m = js.mask("a('x') // require('y')\n/* import z */ b(`t`)")
    assert "require" not in m and "import" not in m and "x" not in m
    assert m.startswith("a(' ')") and "b(` `)" in m


def test_regex_literal_holding_quotes_does_not_open_a_string():
    code = "const re = /['\"]/g;\nconst m = require('real');"
    assert _pairs(code) == [("real", "*")]


def test_division_is_not_mistaken_for_a_regex():
    code = "const a = b / c; const d = e / f;\nconst x = require('real'); // 'tail"
    assert _pairs(code) == [("real", "*")]


def test_regex_after_return_and_open_paren():
    code = "function f(s) { return /require\\('no'\\)/.test(s); }\nx.match(/'/);\nrequire('yes');"
    assert _pairs(code) == [("yes", "*")]


def test_template_literal_with_nested_strings_and_braces():
    code = "const s = `a ${ \"}\" + `inner ${ '`' }` } require('no')`;\nrequire('yes');"
    assert _pairs(code) == [("yes", "*")]


def test_jsx_does_not_break_masking_of_what_follows():
    code = _src("""
        import { Button } from 'ui';
        export const A = () => (
          <div className="x" data-a='b'>
            <p>Don't panic</p>
            <br />
            text
          </div>
        );
        const m = require('after');
    """)
    assert ("ui", "Button") in _pairs(code) and ("after", "*") in _pairs(code)


def test_commented_and_quoted_require_are_not_reported():
    code = "// const x = require('other');\nconst s = \"require('other')\";\nconst t = 'import y from \"other\"';"
    assert uses(code) == []


# ----------------------------------------------------------------- package_of
@pytest.mark.parametrize(("spec", "pkg"), [
    ("momentic", "momentic"),
    ("momentic/server", "momentic"),
    ("@scope/pkg/sub/x.js", "@scope/pkg"),
    ("@scope/pkg", "@scope/pkg"),
    ("lodash.merge", "lodash.merge"),
    ("./x", ""), ("../x", ""), ("/abs/x", ""), (".", ""), ("#internal/x", ""),
    ("node:fs", ""), ("fs", ""), ("fs/promises", ""), ("path", ""), ("child_process", ""), ("http", ""),
    ("https://cdn.x/y.js", ""), ("data:text/javascript,1", ""),
    ("@scope", ""), ("@", ""), ("", ""), ("  ", ""), ("has space", ""),
])
def test_package_of(spec, pkg):
    assert js.package_of(spec) == pkg


# ----------------------------------------------------------------------- uses
@pytest.mark.parametrize(("code", "expected"), [
    ("import {a, b as c} from 'p'", [("p", "a"), ("p", "b")]),
    ("import x from 'p'", [("p", "default")]),
    ("import x, {a} from 'p'", [("p", "default"), ("p", "a")]),
    ("import * as ns from 'p'", [("p", "*")]),
    ("import 'p'", [("p", "*")]),
    ("import \"p\"", [("p", "*")]),
    ("import {a} from `p`", [("p", "a")]),
    ("const {a, b: c} = require('p')", [("p", "a"), ("p", "b")]),
    ("const { a = 1, ...rest } = require('p')", [("p", "a")]),
    ("const p = require('p')", [("p", "*")]),
    ("require('p')", [("p", "*")]),
    ("require  (  'p'  )", [("p", "*")]),
    ("const {a} = await import('p')", [("p", "a")]),
    ("const m = await import('p')", [("p", "*")]),
    ("await import('p')", [("p", "*")]),
    ("export {a} from 'p'", [("p", "a")]),
    ("export {a as b, type C} from 'p'", [("p", "a"), ("p", "C")]),
    ("export * from 'p'", [("p", "*")]),
    ("export * as ns from 'p'", [("p", "*")]),
    ("import x = require('p')", [("p", "*")]),
    ("import {\n  a, // first\n  /* mid */ b,\n  c,\n} from 'p'", [("p", "a"), ("p", "b"), ("p", "c")]),
    ("import {a} from 'p'\nimport {a} from 'p'", [("p", "a")]),
    ("import {default as d} from 'p'", [("p", "default")]),
    ("import type from 'p'", [("p", "default")]),
    ("import {type} from 'p'", [("p", "type")]),
])
def test_uses_names(code, expected):
    assert _pairs(code) == expected


@pytest.mark.parametrize("code", [
    "require(name)", "require('a' + b)", "require(`p${x}`)", "const x = require()",
    "obj.require('p')", "foo.import('p')", "import(name)", "import {", "import x from", "import {a} from",
])
def test_uses_skips_what_is_not_a_literal_module_or_is_malformed(code):
    assert uses(code) == []


def test_uses_covers_every_package_and_callers_filter_with_package_of():
    code = "import fs from 'fs'; import x from './local'; import m from '@scope/pkg/sub'"
    kept = [u.specifier for u in uses(code) if js.package_of(u.specifier)]
    assert kept == ["@scope/pkg/sub"]


def test_uses_records_one_based_lines_and_keeps_the_first_of_duplicates():
    code = "\n\nimport {a} from 'p';\nconst {a: z} = require('p');\n"
    [u] = uses(code)
    assert u == Use("p", "a", 3)


def test_type_only_imports_are_flagged_and_inline_type_modifier_works():
    code = _src("""
        import type { Config } from 'momentic'
        import { defineConfig, type Options } from "momentic"
        import type * as T from 'types'
        import type Def from 'defs'
        export type { E } from 'e'
    """)
    got = {(u.specifier, u.name): u.type_only for u in uses(code)}
    assert got == {
        ("momentic", "Config"): True, ("momentic", "defineConfig"): False, ("momentic", "Options"): True,
        ("types", "*"): True, ("defs", "default"): True, ("e", "E"): True,
    }


def test_runtime_use_beats_a_type_only_use_of_the_same_name():
    code = "import type { A } from 'p'\nimport { A } from 'p'"
    [u] = uses(code)
    assert (u.name, u.type_only, u.line) == ("A", False, 1)


def test_namespace_member_reads_become_uses_and_keep_the_star_entry():
    code = _src("""
        import * as ns from 'p'
        ns.foo(1)
        ns.foo.bar
        ns?.baz
        ns . spaced
        other.nope()
    """)
    assert _pairs(code) == [("p", "*"), ("p", "foo"), ("p", "baz"), ("p", "spaced")]


def test_require_binding_member_reads_and_default_import_members():
    code = _src("""
        const p = require('p')
        p.run()
        import d from 'q'
        d.go(); d.go()
    """)
    assert ("p", "run") in _pairs(code) and ("q", "default.go") in _pairs(code)
    assert [n for s, n in _pairs(code) if s == "q"] == ["default", "default.go"]


def test_member_reads_inside_comments_and_strings_are_ignored():
    code = "import * as ns from 'p'\n// ns.hidden\nconst s = 'ns.alsoHidden'"
    assert _pairs(code) == [("p", "*")]


@pytest.mark.parametrize("rebind", [
    "function f(ns) { return ns.foo }",
    "const f = (ns) => ns.foo",
    "const f = ns => ns.foo",
    "{ let ns = other(); ns.foo }",
    "const { ns } = other(); ns.foo",
])
def test_members_of_a_rebound_name_are_not_reported(rebind):
    code = f"import * as ns from 'p'\n{rebind}"
    assert _pairs(code) == [("p", "*")]


def test_results_of_calling_or_constructing_a_binding_are_not_members():
    code = "const Client = require('p')\nconst c = new Client()\nc.foo()\nClient().bar\nclient.baz"
    assert _pairs(code) == [("p", "*")]


def test_require_of_a_member_or_a_call_result_is_not_a_namespace_binding():
    code = "const x = require('p').default\nx.other()\nconst y = require('q')(opts)\ny.z"
    assert _pairs(code) == [("p", "*"), ("p", "default"), ("q", "*")]


def test_a_name_bound_twice_gets_no_member_uses():
    code = "import * as ns from 'a'\nimport * as ns from 'b'\nns.foo"
    assert _pairs(code) == [("a", "*"), ("b", "*")]


def test_unbound_require_followed_by_member_reports_the_member():
    assert _pairs("require('p').start()") == [("p", "*"), ("p", "start")]


def test_dynamic_import_with_attributes_and_multiline_require():
    assert _pairs("await import('p', { with: { type: 'json' } })") == [("p", "*")]
    assert _pairs("const {\n a,\n b\n} = require(\n 'p'\n)") == [("p", "a"), ("p", "b")]


def test_uses_never_raises_on_garbage():
    for junk in ["import {{{{", "}}}} from 'x", "`${", "/*", "require('", "import * as from 'p'", "\x00\x01"]:
        assert isinstance(uses(junk), list)


# ----------------------------------------------------------------- invocations
def _inv(code: str, names=("momentic",)):
    return invocations(code, names)


def test_realistic_npx_line_with_version_flags_and_values():
    got = _inv("npx momentic@latest run tests/login.test.yaml --ci --base-url=https://x.io")
    assert got == [Invocation(
        bin="momentic", package="momentic", args=("run", "tests/login.test.yaml"),
        flags=("--ci", "--base-url"), line=1,
        text="npx momentic@latest run tests/login.test.yaml --ci --base-url=https://x.io")]


def test_flag_with_a_separate_value_is_recorded_once_without_the_value():
    [i] = _inv("momentic run --base-url https://x.io --ci --base-url=y")
    assert i.flags == ("--base-url", "--ci") and i.args == ("run",) and i.package == ""


@pytest.mark.parametrize("line", [
    "$ momentic run", "> momentic run", "sudo momentic run", "CI=1 momentic run",
    "CI=1 FOO='a b' momentic run", "sudo env CI=1 momentic run", "./node_modules/.bin/momentic run",
])
def test_prefixes_are_stripped(line):
    [i] = _inv(line)
    assert (i.bin, i.args) == ("momentic", ("run",))


def test_invocation_text_drops_the_prompt():
    [i] = _inv("$ momentic run")
    assert i.text == "momentic run"


def test_install_then_run_on_one_line_gives_one_invocation_of_init():
    code = "npm install -g momentic && momentic init"
    [i] = _inv(code)
    assert (i.args, i.line) == (("init",), 1)
    assert js.installs(code) == [("momentic", "")]


@pytest.mark.parametrize("line", [
    "npx -y momentic run", "npx --yes momentic run", "npx -p momentic momentic run",
    "npx --package=momentic momentic run", "pnpm dlx momentic run", "yarn dlx momentic run",
    "bunx momentic run", "npm exec -- momentic run", "npm exec momentic run", "bun x momentic run",
    "npx --no-install momentic@3.1.0 run",
])
def test_runners(line):
    [i] = _inv(line)
    assert (i.bin, i.package, i.args) == ("momentic", "momentic", ("run",))


def test_pnpm_exec_runs_a_local_bin_so_it_names_no_package():
    [i] = _inv("pnpm exec momentic run")
    assert (i.bin, i.package, i.args) == ("momentic", "", ("run",))


def test_yarn_name_is_too_ambiguous_to_count():
    assert _inv("yarn momentic run") == []


def test_scoped_package_runs_as_its_unscoped_bin():
    [i] = invocations("npx @momentic/wizard@latest init", ["@momentic/wizard"])
    assert (i.bin, i.package, i.args) == ("wizard", "@momentic/wizard", ("init",))
    [j] = invocations("wizard init", ["@momentic/wizard"])
    assert (j.bin, j.package) == ("wizard", "")


def test_npx_of_an_unscoped_name_does_not_match_a_scoped_package_with_that_tail():
    assert invocations("npx wizard init", ["@momentic/wizard"]) == []


def test_names_can_be_bin_names_and_unrelated_commands_are_ignored():
    code = "ls -la\nnpm run momentic\ngit commit -m momentic\nnpx other run\necho momentic"
    assert _inv(code) == []


def test_line_continuations_are_joined_and_report_the_first_line():
    code = "echo hi\nnpx momentic run \\\n  tests/a.yaml \\\n  --ci"
    [i] = _inv(code)
    assert (i.line, i.args, i.flags) == (2, ("run", "tests/a.yaml"), ("--ci",))


def test_several_commands_per_line_are_judged_separately():
    code = "cd app && momentic run --a ; npx momentic init || momentic doctor | tee out.log --nope"
    got = _inv(code)
    assert [(i.args, i.flags) for i in got] == [(("run",), ("--a",)), (("init",), ()), (("doctor",), ())]


def test_commands_after_a_pipe_are_found_too():
    [i] = _inv("cat spec.yaml | npx momentic run --stdin")
    assert (i.args, i.flags) == (("run",), ("--stdin",))


def test_comment_lines_and_trailing_comments_are_ignored():
    code = "# momentic run\nmomentic run # --ignored\n  # momentic init"
    [i] = _inv(code)
    assert (i.args, i.flags) == (("run",), ())


def test_quoted_word_ends_args_but_flags_after_it_are_collected():
    [i] = _inv("momentic run tests/a.yaml 'my test' later --ci")
    assert i.args == ("run", "tests/a.yaml") and i.flags == ("--ci",)
    [j] = _inv('momentic "run" --ci')
    assert j.args == () and j.flags == ("--ci",)


def test_short_flags_are_never_reported_and_end_the_subcommand_run():
    [i] = _inv("momentic run -v 3 --ci")
    assert i.args == ("run",) and i.flags == ("--ci",)


def test_a_quoted_operator_does_not_split_the_command():
    [i] = _inv("momentic run --name 'a && b; c' --ci")
    assert i.flags == ("--name", "--ci")


def test_redirections_are_not_arguments():
    [i] = _inv("momentic run --ci > out.log 2>&1")
    assert i.args == ("run",) and i.flags == ("--ci",)


def test_double_dash_ends_the_flags():
    [i] = _inv("momentic run --ci -- --pass-through")
    assert i.flags == ("--ci",)


def test_heredoc_bodies_are_not_commands():
    code = "cat > run.sh <<'EOF'\nmomentic run\nEOF\nmomentic init"
    got = _inv(code)
    assert [i.args for i in got] == [("init",)]


def test_console_output_lines_are_not_commands_when_other_lines_have_prompts():
    code = "$ momentic init\nmomentic run finished in 3s\nmomentic: 2 passed\n$ momentic run --ci"
    assert [i.args for i in _inv(code)] == [("init",), ("run",)]


def test_unprompted_lines_are_commands_when_no_line_has_a_prompt():
    assert [i.args for i in _inv("momentic init\nmomentic run")] == [("init",), ("run",)]


def test_invocations_never_raise_on_garbage():
    for junk in ["'", '"unterminated', "&&&&", "| |", "\\", "npx", "npx -p", "$ ", "sudo", "<<"]:
        assert isinstance(_inv(junk), list)


# --------------------------------------------------------------------- installs
@pytest.mark.parametrize(("line", "expected"), [
    ("npm install momentic", [("momentic", "")]),
    ("npm i momentic@3.74.0", [("momentic", "3.74.0")]),
    ("npm add @scope/pkg@^1", [("@scope/pkg", "^1")]),
    ("npm install pkg@latest", [("pkg", "latest")]),
    ("npm install -g momentic", [("momentic", "")]),
    ("npm install --save-dev a b@2", [("a", ""), ("b", "2")]),
    ("npm install --registry https://r.io a", [("a", "")]),
    ("yarn add a", [("a", "")]),
    ("yarn global add a", [("a", "")]),
    ("pnpm add -D a", [("a", "")]),
    ("pnpm install a", [("a", "")]),
    ("bun add a", [("a", "")]),
    ("bun install a", [("a", "")]),
    ("sudo npm install -g a", [("a", "")]),
    ("$ npm install 'a@>=1.0'", [("a", ">=1.0")]),
    ("npm install", []),
    ("npm install ./local", []),
    ("npm install ../lib/pkg", []),
    ("npm install /abs/pkg", []),
    ("npm install pkg.tgz", []),
    ("npm install https://x.io/p.tgz", []),
    ("npm install git+https://github.com/o/r.git", []),
    ("npm install github:o/r", []),
    ("npm install o/r", []),
    ("npm install file:../p", []),
    ("npm install <package-name>", []),
    ("npm install $PKG", []),
    ("npm run build", []),
    ("npm ci", []),
    ("yarn add", []),
    ("pip install a", []),
])
def test_installs(line, expected):
    assert js.installs(line) == expected


def test_installs_across_lines_and_chains_without_duplicates():
    code = "npm i a\nyarn add b && pnpm add a c@1\n# npm i hidden"
    assert js.installs(code) == [("a", ""), ("b", ""), ("c", "1")]


@pytest.mark.parametrize(("code", "pure"), [
    ("npm install momentic", True),
    ("npm i -g momentic@3.74.0", True),
    ("$ npm install a b\n$ npm add -D c", True),
    ("# deps\nnpm install a\n\nnpm install b", True),
    ("npm install a \\\n  b", True),
    ("npm install a && npm install b", False),
    ("npm install a; echo done", False),
    ("npm install a | tee log", False),
    ("npm install a > log", False),
    ("cd app\nnpm install a", False),
    ("npm install ./local", False),
    ("npm install", False),
    ("npm install a <name>", False),
    ("yarn add a", False),
    ("pnpm add a", False),
    ("sudo npm install a", False),
    ("npm run build", False),
    ("$ npm install a\nadded 1 package in 2s", False),
    ("", False),
    ("# only a comment", False),
])
def test_pure_install(code, pure):
    assert js.pure_install(code) is pure


# ------------------------------------------------------------------------ is_esm
@pytest.mark.parametrize(("code", "esm"), [
    ("import x from 'p'", True),
    ("import {a} from 'p'", True),
    ("import 'p'", True),
    ("import * as ns from 'p'", True),
    ("import type {A} from 'p'", True),
    ("export const a = 1", True),
    ("export default function () {}", True),
    ("export {a}", True),
    ("export * from 'p'", True),
    ("console.log(import.meta.url)", True),
    ("const m = await import('p')\nm.run()", True),
    ("const {a} = require('p')", False),
    ("module.exports = {a: 1}", False),
    ("exports.a = 1", False),
    ("import x = require('p')", False),
    ("const m = await import('p')\nconst r = require('q')", False),
    ("const f = async () => await import('p')", False),
    ("async function f() { await g() }", False),
    ("const m = import('p')", False),
    ("// import x from 'p'\nconst a = 1", False),
    ("const s = 'import x from \"p\"'", False),
    ("console.log(1)", False),
    ("", False),
])
def test_is_esm(code, esm):
    assert js.is_esm(code) is esm


# ---------------------------------------------------------------------- exports_of
def test_exports_of_esm_declarations():
    src = _src("""
        export function f() {}
        export async function g() {}
        export function* gen() {}
        export class C {}
        export abstract class A {}
        export const a = 1, b = () => 2, c: number = 3;
        export let d, e
        export var { x, y: z, w = 1 } = obj;
        export interface I {}
        export type T = string
        export enum E { A }
        export const enum CE { A }
        export namespace N.M {}
        export declare function dec(): void
        export declare const dc: number
        const hidden = 1
        function alsoHidden() {}
    """)
    assert js.exports_of(src) == {
        "f", "g", "gen", "C", "A", "a", "b", "c", "d", "e", "x", "z", "w", "I", "T", "E", "CE", "N",
        "dec", "dc",
    }


def test_exports_of_lists_defaults_and_reexports():
    src = _src("""
        const a = 1, b = 2
        export { a, b as c, type T, d as default }
        export type { U } from './u'
        export * as ns from './ns'
        export * from './all'
        export default class {}
    """)
    assert js.exports_of(src) == {"a", "c", "T", "default", "U", "ns"}


def test_export_default_named_function_exports_only_default():
    assert js.exports_of("export default function foo() {}") == {"default"}


def test_exports_of_commonjs_forms():
    src = _src("""
        exports.a = 1
        module.exports.b = function () {}
        exports.same == 3
        Object.defineProperty(exports, "__esModule", { value: true });
        Object.defineProperty(exports, "viaDefine", { enumerable: true });
        exports.default = f
        module.exports = {
          c,
          d: 1,
          e() {},
          async f() {},
          *gen() {},
          get g() { return 1 },
          ...spread,
          [computed]: 1,
          'quoted-key': 2,
          get: 3,
        }
    """)
    assert js.exports_of(src) == {"a", "b", "viaDefine", "default", "c", "d", "e", "f", "gen", "g",
                                  "quoted-key", "get"}


def test_module_exports_assigned_a_non_object_adds_nothing_by_itself():
    assert js.exports_of("module.exports = function run() {}") == set()


def test_exported_class_public_methods_are_listed_as_class_dot_method():
    src = _src("""
        export class Client {
          private secret = 1
          static create() { return new Client() }
          constructor(x) {}
          run(a, b) {}
          async stop(): Promise<void> {}
          *stream() {}
          get size() { return 1 }
          _internal() {}
          #hidden() {}
          private priv() {}
          protected prot() {}
          public pub<T>(x: T): T { return x }
          field = compute(1)
          handler = () => { inner() }
          after() {}
          overload(a: string): void
          overload(a: number): void
          overload(a: any) { if (a) { nested() } }
        }
        class NotExported { nope() {} }
    """)
    got = js.exports_of(src)
    assert {"Client", "Client.create", "Client.run", "Client.stop", "Client.stream", "Client.size",
            "Client.pub", "Client.after", "Client.overload"} <= got
    leaked = {n for n in got if "." in n} - {
        "Client.create", "Client.run", "Client.stop", "Client.stream", "Client.size", "Client.pub",
        "Client.after", "Client.overload"}
    assert leaked == set()
    assert not {"Client.constructor", "Client.nested", "Client.compute", "Client.inner", "NotExported.nope"} & got


def test_class_exported_through_a_list_gets_its_methods_too():
    src = "class K { go() {} }\nexport { K }"
    assert js.exports_of(src) == {"K", "K.go"}


def test_exports_in_comments_and_strings_do_not_count():
    src = "// export const hidden = 1\nconst s = 'export const alsoHidden = 1'\n/* exports.no = 1 */\nexport const real = 1"
    assert js.exports_of(src) == {"real"}


def test_exports_of_never_raises():
    for junk in ["export", "export {", "export class", "export class X {", "module.exports = {", "export const",
                 "\x00", "export const a = (", "export default"]:
        assert isinstance(js.exports_of(junk), set)
