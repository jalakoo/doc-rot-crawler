"""Regression tests for the extraction and probe rules.

Each of these encodes a false positive a spike measured and fixed (S2, S3,
step 7). They were only ever checked by re-running the spikes against Daytona;
these pin them down offline.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from docrot.acquire.site import _to_text
from docrot.extract import claims, completeness, fences, symbols, tiers
from docrot.models import Page, Snippet
from docrot.verify.probe import MARKER, _kwargs_used, batched_script, build_targets, parse_batched


# ------------------------------------------------------------------ tiers
@pytest.mark.parametrize("code,ok", [
    ("pip install perseus-client", True),
    ("$ pip install 'perseus-client[all]==1.0'", True),
    ("uv pip install streamlit --upgrade", True),
    ("pip install -r requirements.txt", False),
    ("pip install .", False),
    ("pip install -e ./local", False),
    ("pip install x && docker run y", False),
    ("cp template.env .env", False),
    ("# just a comment", False),
])
def test_only_pure_registry_installs_execute(code, ok):
    assert tiers._pure_install(code) is ok


def _tier(code, lang="python", creds=(), assume=False, imports=("alpha",)):
    s = Snippet(id="x", page="p", lang=lang, code=code)
    symbols.annotate([s], list(imports))
    tiers.assign([s], set(creds), list(imports), assume)
    return s.tier


def test_tier_rules():
    assert _tier("{}", lang="json") == "unverifiable"
    assert _tier("def delete_file(file_id: str) -> None:") == "structural"      # declaration
    assert _tier("client.run()") == "unverifiable"                               # fragment, no symbols
    assert _tier("import alpha\nalpha.go(api_key='sk-abcdef')") == "structural"  # placeholder
    code = "import os, alpha\nalpha.go(os.environ['ALPHA_API_KEY'])"
    assert _tier(code) == "structural"
    assert _tier(code, creds={"ALPHA_API_KEY"}) == "execute"
    assert _tier("import alpha\nalpha.go()") == "structural"                     # touches the package
    assert _tier("import alpha\nalpha.go()", assume=True) == "execute"
    assert _tier("print(sum([1, 2]))") == "execute"


def test_chains_group_by_page_in_document_order():
    a2 = Snippet(id="a2", page="A", lang="python", code="", line=20, tier="execute")
    a1 = Snippet(id="a1", page="A", lang="python", code="", line=10, tier="execute")
    b1 = Snippet(id="b1", page="B", lang="python", code="", line=1, tier="unverifiable")
    assert [[s.id for s in c] for c in tiers.chains([a2, a1, b1])] == [["a1", "a2"]]


# ----------------------------------------------------------- completeness
def test_classify_program_declaration_fragment():
    assert completeness.classify("import os\nprint(os.getcwd())")[0] == "program"
    kind, decls = completeness.classify("def delete_file(file_id: str) -> None:")
    assert kind == "declaration" and decls[0]["name"] == "delete_file"
    assert completeness.classify("result = client.fetch(x)")[0] == "fragment"


def test_pydantic_model_is_a_declaration_not_a_fragment():
    code = "class Entity(BaseModel):\n    name: str = Field(default='x')\n    kind: Optional[str] = None"
    kind, decls = completeness.classify(code)
    assert kind == "declaration" and decls[0]["params"]


# ------------------------------------------------------------------ probe
def test_kwargs_are_attributed_to_the_right_call():
    used = _kwargs_used("st.pydeck_chart(pdk.Deck(map_style='x', layers=[]), use_container_width=True)")
    assert used == {"Deck": ["map_style", "layers"], "pydeck_chart": ["use_container_width"]}


def test_build_targets_skips_tutorial_helpers():
    tutorial = Snippet(id="t", page="p", lang="python", kind="program",
                       code="import alpha\n@alpha.cache\ndef load_data(n):\n    return n",
                       symbols=["alpha.cache"],
                       declares=[{"name": "load_data", "params": ["n"], "decorated": True}])
    reference = Snippet(id="r", page="p", lang="python", kind="declaration",
                        code="def go(x, y): ...",
                        declares=[{"name": "go", "params": ["x", "y"], "decorated": False}])
    t, r = build_targets([tutorial, reference], "alpha")
    assert t["claims_import"] and t["declares"] == []
    assert not r["claims_import"] and [d["name"] for d in r["declares"]] == ["go"]
    assert r["symbols"] == ["alpha"]


def test_parse_batched_ignores_install_noise():
    out = "Collecting alpha\n{not json}\n" + MARKER + '\n{"s1": {"failed": []}}'
    assert parse_batched(out) == {"s1": {"failed": []}}
    with pytest.raises(ValueError):
        parse_batched("no marker here")


def test_batched_probe_runs_against_a_real_package(tmp_path):
    """The generated probe script, executed locally against a tiny package."""
    pkg = tmp_path / "alpha"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        import enum
        def go(x, *, timeout=30): ...
        class Color(enum.Enum):
            RED = 1
        class Client:
            def run(self, fast=False): ...
    """))
    snippets = [
        Snippet(id="ok", page="p", lang="python", code="import alpha\nalpha.go(1, timeout=5)",
                symbols=["alpha.go"]),
        Snippet(id="kwarg", page="p", lang="python", code="import alpha\nalpha.go(1, retries=2)",
                symbols=["alpha.go"]),
        Snippet(id="gone", page="p", lang="python", code="import alpha\nalpha.stop()",
                symbols=["alpha.stop"]),
        Snippet(id="method", page="p", lang="python", code="import alpha\nalpha.Client().run(fast=True)",
                symbols=["alpha.Client", "alpha.Client.run"]),
    ]
    script = batched_script(build_targets(snippets, "alpha"), "alpha")
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          cwd=tmp_path, env={"PYTHONPATH": str(tmp_path)}, timeout=60)
    payload = parse_batched(proc.stdout)
    assert payload["ok"]["failed"] == []
    assert payload["method"]["failed"] == []
    assert any("retries" in f for f in payload["kwarg"]["failed"])
    assert any("stop" in f for f in payload["gone"]["failed"])
    json.dumps(payload)                                        # round-trips


# ------------------------------------------------------------ claims, html
def test_claim_linking_is_lexical_and_exact():
    syms = {"build_graph", "save_to_neo4j", "Client"}
    assert claims.link("Call `perseus.build_graph` with paths.", syms) == ["build_graph"]
    assert claims.link("Use save_to_neo4j to persist it.", syms) == ["save_to_neo4j"]
    assert claims.link("Nothing relevant to see here at all.", syms) == []
    page = Page(url="u", path="/p", text="The build_graph function accepts a list of file paths. ```x```")
    [c] = claims.extract([page], syms)
    assert c.symbols == ["build_graph"]


def test_html_to_text_never_drops_a_fence():
    html = ("<html><title>Guide</title><body><article><p>Install it.</p>"
            "<pre><code>import alpha\nalpha.go()</code></pre></article></body></html>")
    text = _to_text(html)
    assert text.startswith("# Guide")
    assert "```python\nimport alpha\nalpha.go()\n```" in text
    [snip] = fences.parse([Page(url="u", path="/guide", text=text)])
    assert snip.lang == "python"


# ------------------------------------------------------------------- pins
def test_pins_come_only_from_install_shaped_text():
    from docrot.acquire.pins import find_pins
    text = """
pip install perseus-client==1.0.0-rc.19
uv add "streamlit[charts]==1.49.0"
requests==2.32.3
dependencies = ["pydantic==2.13.5"]
assert m.x == 1
if count == 10 and ratio == 1.0:
    total == 123
"""
    assert find_pins(text) == ["1.0.0-rc.19", "1.49.0", "2.13.5", "2.32.3"]


def test_graph_ingest_ignores_non_probe_json():
    from docrot.graph.store import GraphStore
    from docrot.models import Result

    class Recording(GraphStore):
        def __init__(self):
            self.ok, self.rows = True, None

        def _run(self, cypher, **params):
            self.rows = params.get("rows")
            return []

    g = Recording()
    g.ingest_exists_in([
        Result(id="a", page="p", version="1", status="pass", stderr='[{"printed": "by a snippet"}]'),
        Result(id="b", page="p", version="1", status="pass", stderr='{"probe": ["not", "a", "dict"]}'),
        Result(id="c", page="p", version="1", status="pass",
               stderr='{"probe": {"alpha.go": {"exists": true}}, "failed": []}'),
    ], primary="alpha")
    assert g.rows == [{"name": "go", "version": "1", "package": "alpha"}]


def test_diff_detail_tolerates_non_dict_output():
    from docrot.report.diff import _entry
    row = _entry(("p", "s", "1"), {"status": "pass"}, {"status": "fail", "stderr": "[1, 2]"})
    assert row["detail"] == ""


def test_symbol_roots_must_be_a_target_package():
    s = Snippet(id="x", page="p", lang="python",
                code="from pydantic_extra_types.coordinate import Latitude\nimport pydantic\npydantic.x()")
    symbols.annotate([s], ["pydantic"])
    assert s.symbols == ["pydantic", "pydantic.x"]


def test_declarations_unknown_at_head_are_pruned():
    from docrot.extract.pipeline import prune_declares
    example = Snippet(id="e", page="p", lang="python", kind="declaration", tier="structural",
                      code="class Model(BaseModel):\n    x: int",
                      declares=[{"name": "Model", "params": ["x"]}])
    reference = Snippet(id="r", page="p", lang="python", kind="declaration", tier="structural",
                        code="def int_schema(schema) -> dict",
                        declares=[{"name": "int_schema", "params": ["schema"]}])
    prune_declares([example, reference], {"GenerateJsonSchema", "GenerateJsonSchema.int_schema"})
    assert example.declares == [] and example.tier == "unverifiable"
    assert [d["name"] for d in reference.declares] == ["int_schema"] and reference.tier == "structural"
    untouched = Snippet(id="u", page="p", lang="python", kind="declaration", tier="structural",
                        code="", declares=[{"name": "Model", "params": []}])
    prune_declares([untouched], set())
    assert untouched.declares


def test_probe_ignores_variadics_and_finds_lazy_submodules(tmp_path):
    pkg = tmp_path / "lazy"
    (pkg / "json_schema").mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "def __getattr__(name):\n    import importlib\n    return importlib.import_module('lazy.' + name)\n"
        "class Base:\n    def __init__(self, *args, **kwargs): ...\n")
    (pkg / "json_schema" / "__init__.py").write_text(
        "class Generator:\n    def int_schema(self, schema): ...\n")
    reference = Snippet(id="ref", page="p", lang="python", kind="declaration",
                        code="def int_schema(schema) -> dict",
                        declares=[{"name": "int_schema", "params": ["schema"], "decorated": False}])
    init = Snippet(id="init", page="p", lang="python", kind="declaration",
                   code="def Base(**data)", declares=[{"name": "Base", "params": ["*", "**data"],
                                                        "decorated": False}])
    script = batched_script(build_targets([reference, init], "lazy"), "lazy")
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          cwd=tmp_path, env={"PYTHONPATH": str(tmp_path)}, timeout=60)
    payload = parse_batched(proc.stdout)
    assert payload["ref"]["failed"] == [], payload["ref"]
    assert payload["init"]["failed"] == [], payload["init"]


def test_page_titles_drop_markdown_link_syntax():
    from docrot.acquire.site import _title
    assert _title("# [Conversion Table](https://pydantic.dev/x)\nbody", "x") == "Conversion Table"
    assert _title("no heading", "fallback") == "fallback"
