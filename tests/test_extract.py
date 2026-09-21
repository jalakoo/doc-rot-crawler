from __future__ import annotations

from docrot.extract import fences, symbols, tiers
from docrot.extract.pipeline import attribute
from docrot.models import PackageRef, Page, Snippet


def _page(url, path, text, source="site"):
    return Page(url=url, path=path, title=path, source=source, text=text)


FENCE = "```python\nimport alpha\nalpha.go()\n```\n\n```python\nimport alpha\n```\n"


def test_snippet_ids_are_unique_across_sites():
    pages = [_page("https://a.io/quickstart", "/quickstart", FENCE),
             _page("https://b.io/quickstart", "/quickstart", FENCE),
             _page("https://c.io/quickstart", "/quickstart", FENCE)]
    ids = [s.id for s in fences.parse(pages)]
    assert len(ids) == len(set(ids)) == 6
    # the first page keeps the historical id, so old runs still diff
    assert ids[:2] == ["quickstart-01", "quickstart-02"]
    assert ids[2].startswith("b-io-quickstart")


def test_single_site_ids_unchanged():
    ids = [s.id for s in fences.parse([_page("https://a.io/x/guide", "/x/guide", FENCE)])]
    assert ids == ["x-guide-01", "x-guide-02"]


def test_symbols_filter_to_any_target_package():
    s = Snippet(id="1", page="p", lang="python",
                code="import alpha\nimport beta_core\nimport os\nalpha.go()\nbeta_core.x.y()\nos.path.join('a')")
    symbols.annotate([s], ["alpha", "beta_core"])
    assert s.symbols == ["alpha", "alpha.go", "beta_core", "beta_core.x", "beta_core.x.y"]
    symbols.annotate([s], "alpha")
    assert s.symbols == ["alpha", "alpha.go"]


def test_credential_rule_applies_to_every_target():
    s = Snippet(id="1", page="p", lang="python", code="import beta_core\nbeta_core.run()",
                symbols=["beta_core.run"])
    tiers.assign([s], set(), ["alpha", "beta_core"])
    assert s.tier == "structural"
    tiers.assign([s], set(), ["alpha", "beta_core"], assume_credentialed=True)
    assert s.tier == "execute"


def test_attribute_snippets_to_packages():
    pkgs = [PackageRef(package="alpha", import_name="alpha"),
            PackageRef(package="beta-core", import_name="beta_core")]
    by_symbol = Snippet(id="a", page="p", lang="python", code="", symbols=["beta_core.x"])
    by_import = Snippet(id="b", page="p", lang="python", code="from beta_core import x")
    by_pip = Snippet(id="c", page="p", lang="bash", code="pip install beta_core==1.0",
                     symbols=["pip:beta_core==1.0"])
    unowned = Snippet(id="d", page="p", lang="python", code="print('hi')")
    attribute([by_symbol, by_import, by_pip, unowned], pkgs)
    assert [s.package for s in (by_symbol, by_import, by_pip, unowned)] == \
        ["beta-core", "beta-core", "beta-core", "alpha"]


def test_model_claims_are_tied_to_a_real_page_and_real_symbols():
    """The model gets one corpus excerpt, so its `page` is a guess - usually a
    title. Ingested as given, those became Page nodes keyed by title."""
    from docrot.extract.pipeline import ground
    from docrot.models import Claim

    pages = [_page("https://a.io/guide", "/guide",
                   "Call close_async when you are finished; it shuts the client down cleanly."),
             _page("https://a.io/other", "/other", "Nothing relevant here.")]
    claims = [
        # the model paraphrases rather than quotes - measured at 0 of 97 verbatim
        Claim(id="c1", page="Configuration",
              text="close_async shuts the client down cleanly when you are finished.",
              symbols=["close_async", "invented_symbol"]),
        Claim(id="c2", page="Guide",
              text="Ontologies are uploaded asynchronously and polled until ready."),
    ]
    kept = ground(claims, pages, {"close_async"})

    assert len(kept) == 1                                  # the unplaceable claim is dropped
    assert kept[0].page == "https://a.io/guide"            # by its words, not the model's guess
    assert kept[0].symbols == ["close_async"]              # invented symbols removed


def test_a_quoted_claim_is_placed_exactly():
    from docrot.extract.pipeline import ground
    from docrot.models import Claim

    pages = [_page("https://a.io/a", "/a", "Prose about nothing much."),
             _page("https://a.io/b", "/b", "build_graph accepts a list of file paths.")]
    [kept] = ground([Claim(id="c", page="?", text="build_graph accepts a list of file paths.")],
                    pages, set())
    assert kept.page == "https://a.io/b"


def test_grounding_without_a_symbol_table_keeps_the_model_symbols():
    from docrot.extract.pipeline import ground
    from docrot.models import Claim

    pages = [_page("https://a.io/g", "/g", "Some prose about build_graph and files.")]
    [kept] = ground([Claim(id="c", page="?", text="Some prose about build_graph and files.",
                           symbols=["build_graph"])], pages, set())
    assert kept.symbols == ["build_graph"] and kept.page == "https://a.io/g"
