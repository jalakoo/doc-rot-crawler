from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from docrot.store import ScanStatus
from tests.fakes import make_outcome

pytestmark = pytest.mark.e2e

DOCS = ["https://docs.a.io", "https://guides.a.io"]
REPOS = ["https://github.com/a/alpha", "https://github.com/a/beta"]


def seed(dashboard, name="Seeded", docs=DOCS[:1], repos=REPOS[:1], runs=1):
    rec = dashboard.store.create(docs, repos, name=name)
    for _ in range(runs):
        out = make_outcome(docs, repos)
        dashboard.store.save_run(rec.id, out.report, [r.model_dump() for r in out.results],
                                 out.extraction.model_dump_json())
    dashboard.store.set_status(rec.id, ScanStatus(state="done"))
    return rec


RELATE = "https://github.com/relatedb/relate"
FOUND = {"repo": RELATE, "commit": "abc123def456", "branch": "main", "other": ["rust", "go"],
         "packages": [
             {"name": "@relate/relate", "import_name": "@relate/relate", "ecosystem": "npm",
              "path": "clients/javascript", "spec": "npm:@relate/relate"},
             {"name": "relate-db", "import_name": "relate", "ecosystem": "pypi",
              "path": "surfaces/relate-python", "spec": "pypi:relate-db"},
             {"name": "Relate.Net", "import_name": "Relate.Net", "ecosystem": "nuget",
              "path": "clients/dotnet/src/Relate.Net", "spec": "nuget:Relate.Net"}]}

# a repo whose GitHub releases carry the `relate` binary lists it beside its packages
FOUND_CLI = {**FOUND, "packages": [*FOUND["packages"],
             {"name": "relate", "import_name": "relate", "ecosystem": "cli", "path": "",
              "spec": "cli:relate", "latest": "1.2.0"}]}

COVERAGE = {"tiers": {"execute": 4, "structural": 10, "unverifiable": 172},
            "unverifiable": {"no-package": 120, "not-code": 35, "no-checker": 17},
            "no_checker": {"rust": 9, "go": 8},
            "labels": {"no-package": "use no scanned package", "not-code": "not code",
                       "no-checker": "no checker"},
            "pages": {}}


def open_form_with_repo(page: Page, dashboard, repo: str = RELATE):
    page.goto(dashboard.url)
    page.locator(".toolbar [data-new]").click()
    page.get_by_role("textbox", name="Docs URL 1", exact=True).fill(DOCS[0])
    page.get_by_role("textbox", name="Repository 1", exact=True).fill(repo)
    return page.locator("#dlg")


def card(page: Page, name: str):
    return page.locator("article.card").filter(has=page.get_by_role("link", name=name, exact=True))


def test_empty_dashboard_invites_a_first_scan(page: Page, dashboard):
    page.goto(dashboard.url)
    expect(page.locator("#empty")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("New scan")).first).to_be_visible()


def test_new_scan_with_several_sources_runs_to_a_report(page: Page, dashboard):
    page.goto(dashboard.url)
    page.locator(".toolbar [data-new]").click()
    dialog = page.locator("#dlg")
    expect(dialog).to_be_visible()

    # nothing entered
    dialog.get_by_role("button", name="Start scan").click()
    expect(page.locator("#formErr")).to_contain_text("at least one docs URL or repository")

    dialog.get_by_label("Name", exact=True).fill("Alpha stack")
    page.get_by_role("textbox", name="Docs URL 1", exact=True).fill(DOCS[0])
    dialog.get_by_role("button", name="+ Add another docs URL").click()
    page.get_by_role("textbox", name="Docs URL 2", exact=True).fill("not a url")
    page.get_by_role("textbox", name="Repository 1", exact=True).fill(REPOS[0])
    dialog.get_by_role("button", name="+ Add another repo").click()
    page.get_by_role("textbox", name="Repository 2", exact=True).fill(REPOS[1])
    dialog.get_by_role("button", name="Start scan").click()
    expect(page.locator("#formErr")).to_have_text("Docs URL 2 isn't an http(s) address.")
    expect(page.get_by_role("textbox", name="Docs URL 2", exact=True)).to_have_attribute("aria-invalid", "true")

    page.get_by_role("textbox", name="Docs URL 2", exact=True).fill(DOCS[1])
    dialog.get_by_role("button", name="Start scan").click()
    expect(dialog).to_be_hidden()

    c = card(page, "Alpha stack")
    expect(c.locator(".state")).to_have_text(re.compile("Queued|Scanning"))
    expect(c.locator(".srcs-list li")).to_have_count(4)
    expect(c.locator(".state")).to_have_text("Rot found", timeout=15_000)
    expect(c.locator(".ribbon.mini")).to_contain_text("Findings")

    c.get_by_role("link", name="Alpha stack").click()
    expect(page).to_have_url(re.compile(r"#/scan/alpha-stack-"))
    expect(page.locator("#sources li")).to_have_count(4)
    expect(page.locator("#tlBody .tl-head")).to_have_count(2)       # one per package
    expect(page.locator("#map .tile").first).to_be_visible()
    expect(page.locator("#sub")).to_contain_text("alpha")
    expect(page.locator("#sub")).to_contain_text("beta")


def test_new_scan_sends_packages_aligned_with_repos(page: Page, dashboard):
    page.goto(dashboard.url)
    page.locator(".toolbar [data-new]").click()
    dialog = page.locator("#dlg")
    page.get_by_role("textbox", name="Docs URL 1", exact=True).fill(DOCS[0])
    page.get_by_role("textbox", name="Repository 1", exact=True).fill(REPOS[0])
    dialog.get_by_role("button", name="+ Add another repo").click()
    page.get_by_role("textbox", name="Repository 2", exact=True).fill(REPOS[1])
    page.get_by_role("textbox", name="Package for repository 2", exact=True).fill("pkg-b")
    dialog.get_by_role("button", name="+ Add a package without a repo").click()
    page.get_by_role("textbox", name="Package without a repo 1", exact=True).fill("pkg-c")
    dialog.get_by_role("button", name="Start scan").click()
    expect(dialog).to_be_hidden()
    assert [r.packages for r in dashboard.store.all()] == [["", "pkg-b", "pkg-c"]]


def test_detail_view_navigation(page: Page, dashboard):
    seed(dashboard)
    page.goto(dashboard.url)
    card(page, "Seeded").get_by_role("link", name="Seeded").click()

    expect(page.locator("#findTitle")).to_have_text(re.compile("SYMBOL"))
    page.locator('#map .tile[data-st="unlit"]').first.click()
    expect(page.locator("#findTitle")).to_have_text("PROSE ONLY — NOTHING TO CHECK")
    page.locator('#map .tile[data-st="drift"]').first.click()
    expect(page.locator("#findTitle")).to_have_text("STALE VERSION PIN")

    page.locator("#sym").fill("alpha.run")
    page.locator("#check").click()
    expect(page.locator("#impactOut")).to_contain_text("breaks 2 pages")
    expect(page.locator("#map .tile.lit")).to_have_count(2)
    page.locator("#sym").fill("nothing_here")
    page.locator("#sym").press("Enter")
    expect(page.locator("#impactOut")).to_contain_text("Safe to rename")

    page.get_by_role("link", name="◀ All scans").click()
    expect(page.locator("#viewList")).to_be_visible()
    expect(card(page, "Seeded").get_by_role("link", name="Seeded")).to_be_focused()


def test_rename_persists_across_reloads(page: Page, dashboard):
    rec = seed(dashboard)
    page.goto(dashboard.url)
    card(page, "Seeded").get_by_role("button", name="Rename Seeded").click()
    field = page.get_by_label(re.compile("Scan name"))
    field.fill("Renamed scan")
    field.press("Enter")
    expect(card(page, "Renamed scan")).to_be_visible()
    page.wait_for_function(
        "fetch('/api/scans').then(r => r.json()).then(l => l[0].name === 'Renamed scan')")
    assert dashboard.store.get(rec.id).name == "Renamed scan"

    page.reload()
    expect(card(page, "Renamed scan")).to_be_visible()

    # Escape cancels
    card(page, "Renamed scan").get_by_role("button", name="Rename Renamed scan").click()
    page.get_by_label(re.compile("Scan name")).fill("Nope")
    page.get_by_label(re.compile("Scan name")).press("Escape")
    expect(card(page, "Renamed scan")).to_be_visible()


def test_failed_scan_then_rerun(page: Page, dashboard):
    rec = seed(dashboard, name="Flaky")
    dashboard.fail_next = "no pages acquired"
    dashboard.worker.enqueue(rec.id)
    dashboard.worker.join()

    page.goto(f"{dashboard.url}/#/scan/{rec.id}")
    expect(page.locator("#noticeTitle")).to_have_text("SCAN FAILED — NOTHING WAS VERIFIED")
    expect(page.locator("#noticeDesc")).to_contain_text("no pages acquired")
    expect(page.locator("#reportBody")).to_be_visible()                 # last good run

    page.get_by_role("button", name="Re-run").click()
    expect(page.locator("#noticeTitle")).to_have_text(re.compile("SCANNING|QUEUED"))
    expect(page.locator("#crumbRun")).to_contain_text("run 2 of 2", timeout=15_000)
    expect(page.locator("#notice")).to_be_hidden()


def test_phone_width_has_no_horizontal_scroll(page: Page, dashboard):
    rec = seed(dashboard, docs=DOCS, repos=REPOS)
    page.set_viewport_size({"width": 400, "height": 800})
    for path, ready in (("/", "article.card"), (f"/#/scan/{rec.id}", "#map .tile")):
        page.goto(dashboard.url + path)
        page.wait_for_selector(ready)
        width = page.evaluate("document.documentElement.scrollWidth")
        assert width <= 400, f"{path} scrolls sideways ({width}px)"


def test_export_jsonl_from_the_detail_view(page: Page, dashboard, tmp_path):
    import json
    rec = seed(dashboard)
    page.goto(f"{dashboard.url}/#/scan/{rec.id}")
    link = page.get_by_role("link", name="Export JSONL")
    expect(link).to_be_visible()
    with page.expect_download() as info:
        link.click()
    download = info.value
    assert download.suggested_filename.startswith(rec.id) and download.suggested_filename.endswith(".jsonl")
    path = tmp_path / download.suggested_filename
    download.save_as(path)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[0]["scan"]["id"] == rec.id and len(lines) > 1


def test_export_link_hidden_before_the_first_run(page: Page, dashboard):
    rec = dashboard.store.create(DOCS[:1], [], name="Fresh")
    page.goto(f"{dashboard.url}/#/scan/{rec.id}")
    expect(page.locator("#noticeTitle")).to_have_text("NO RUNS YET")
    expect(page.get_by_role("link", name="Export JSONL")).to_be_hidden()


def test_import_an_exported_run(page: Page, dashboard, tmp_path):
    rec = seed(dashboard, name="Origin")
    export = tmp_path / "origin.jsonl"
    export.write_text(dashboard.store.export_text(rec.id))
    # point the original scan elsewhere, so the file arrives as a new scan
    dashboard.store.update(rec.id, docs=["https://moved.example.com"])

    page.goto(dashboard.url)
    page.get_by_role("button", name="Import", exact=True).click()
    dialog = page.locator("#importDlg")
    expect(dialog).to_be_visible()

    dialog.get_by_role("button", name="Import", exact=True).click()      # nothing chosen
    expect(page.locator("#importErr")).to_contain_text("Choose a .jsonl or .sarif file")

    page.locator("#fFile").set_input_files(export)
    page.locator("#fImportName").fill("From a colleague")
    dialog.get_by_role("button", name="Import", exact=True).click()

    expect(dialog).to_be_hidden()
    expect(page).to_have_url(re.compile(r"#/scan/from-a-colleague-"))
    expect(page.locator("#crumbName")).to_contain_text("From a colleague")
    expect(page.locator("#log")).to_contain_text("imported from origin.jsonl")
    expect(page.locator("#log")).to_contain_text("nothing was re-verified")
    expect(page.locator("#findTitle")).to_have_text(re.compile("SYMBOL|STALE"))
    # only pages with findings are in an export, so the prose-only page is absent
    expect(page.locator("#map .tile")).to_have_count(2)

    page.get_by_role("link", name="◀ All scans").click()
    expect(card(page, "From a colleague").locator(".state")).to_have_text("Rot found")


def test_importing_a_file_that_is_not_an_export(page: Page, dashboard, tmp_path):
    junk = tmp_path / "notes.jsonl"
    junk.write_text("just some notes\n")
    page.goto(dashboard.url)
    page.get_by_role("button", name="Import", exact=True).click()
    page.locator("#fFile").set_input_files(junk)
    page.locator("#importDlg").get_by_role("button", name="Import", exact=True).click()
    expect(page.locator("#importErr")).to_contain_text("not valid JSON")
    expect(page.locator("#importDlg")).to_be_visible()


def test_an_imported_file_cannot_run_script_in_the_dashboard(page: Page, dashboard):
    """A .jsonl or .sarif file may come from anywhere. Its text is rendered as
    markup, so unescaped it would run in the dashboard's own origin - which can
    drive the API and spend sandbox and model credit."""
    payload = '<img src=x onerror="window.__pwned = 1">'
    records = [
        {"type": "scan", "schema": "docrot/1", "scan": {"id": "x", "name": "Imported"},
         "run": "20260916T120000", "generated_at": "2026-09-16T12:00:00", "elapsed_s": 1,
         "sources": [], "packages": [], "options": {}, "stats": {}},
        {"type": "finding", "id": "x:s1:runtime", "kind": "runtime", "label": "snippet fails",
         "status": "fail", "severity": "error", "title": "Snippet fails",
         "summary": f"Harmless looking. {payload}",
         "doc": {"url": "https://evil.test/p", "title": "Page", "section": "S", "source": "site",
                 "origin": "", "path": "/p", "line": 1, "snippet_id": "s1", "snippet": "x",
                 "lang": "python"},
         "code": {"package": "p", "import_name": "p", "repo": "", "commit": "", "url": "",
                  "where": "", "actual": "boom"},
         "versions": {"tested": ["1.0"], "failing": ["1.0"], "passing": [], "blocked": [],
                      "unverified": []},
         "symbols": [], "tier": "structural", "blocked_by": None, "days_behind": 0,
         "evidence": "", "fix_hint": ""},
    ]
    text = "\n".join(json.dumps(r) for r in records)
    rec, _ = dashboard.store.import_run(text, filename="evil.jsonl")

    page.goto(f"{dashboard.url}/#/scan/{rec.id}")
    page.wait_for_selector("#findDesc")
    page.wait_for_timeout(300)
    assert page.evaluate("window.__pwned") is None            # nothing executed
    assert "<img" not in page.evaluate("document.getElementById('findDesc').innerHTML")
    expect(page.locator("#findDesc")).to_contain_text(payload)  # shown as the text it is


def test_settings_are_saved_to_the_keychain_and_never_shown_back(page: Page, dashboard, keychain):
    page.goto(dashboard.url)
    page.get_by_role("link", name="Settings").click()
    expect(page.locator("#setBackend")).to_have_text("stored in memory")

    key = page.get_by_label(re.compile("^Daytona API key"))
    expect(key).to_have_attribute("type", "password")
    key.fill("dtn-secret")
    page.locator("#setForm").get_by_label(re.compile("^Max pages per site")).fill("12")
    page.get_by_role("button", name="+ Add a credential").click()
    page.get_by_label("New credential 1 name").fill("openai_api_key")
    page.get_by_label("New credential 1 value").fill("sk-secret")
    page.get_by_role("button", name="Save settings").click()

    expect(page.locator("#setSaved")).to_contain_text("Saved to the keychain")
    assert json.loads(keychain.text) == {"DAYTONA_API_KEY": "dtn-secret", "DOCROT_MAX_PAGES": "12",
                                         "SANDBOX_ENV_OPENAI_API_KEY": "sk-secret"}
    # the page is rebuilt from the server's answer, which carries no secret
    expect(page.get_by_label(re.compile("^Daytona API key"))).to_have_value("")
    expect(page.locator("#setForm").get_by_label(re.compile("^Max pages per site"))).to_have_value("12")
    assert "secret" not in page.content()

    row = page.locator('[data-row="SANDBOX_ENV_OPENAI_API_KEY"]')
    row.get_by_role("button", name="Remove").click()
    page.get_by_role("button", name="Save settings").click()
    expect(row).to_have_count(0)
    assert "SANDBOX_ENV_OPENAI_API_KEY" not in json.loads(keychain.text)

    page.get_by_role("link", name=re.compile("All scans")).click()
    expect(page.locator("#viewList")).to_be_visible()
    expect(page.locator("#viewSettings")).to_be_hidden()


# ------------------------------------------------------------- detect packages

def test_detect_then_untick_one_sends_the_ticked_packages_with_their_ecosystem(page: Page, dashboard):
    dashboard.detections[RELATE] = FOUND
    dialog = open_form_with_repo(page, dashboard)
    dialog.get_by_role("button", name="Detect packages").click()

    group = page.get_by_role("group", name="Packages found in relatedb/relate")
    expect(group.get_by_role("checkbox")).to_have_count(3)
    for box in group.get_by_role("checkbox").all():
        expect(box).to_be_checked()
    expect(group).to_contain_text("relate-db")
    expect(group).to_contain_text("PyPI · import relate · surfaces/relate-python")
    expect(group).to_contain_text("Also contains Rust and Go — no checker for them yet.")
    expect(page.locator("#detectStatus")).to_contain_text("Found 3 packages in 1 repo")

    group.get_by_role("checkbox", name="@relate/relate").uncheck()
    dialog.get_by_role("button", name="Start scan").click()
    expect(dialog).to_be_hidden()
    assert [r.packages for r in dashboard.store.all()] == [["pypi:relate-db", "nuget:Relate.Net"]]
    assert [r.options.ecosystem for r in dashboard.store.all()] == ["auto"]


def test_detect_and_leave_every_box_ticked_sends_no_packages(page: Page, dashboard):
    dashboard.detections[RELATE] = FOUND
    dialog = open_form_with_repo(page, dashboard)
    dialog.get_by_role("button", name="Detect packages").click()
    expect(page.get_by_role("checkbox")).to_have_count(3)
    dialog.get_by_role("button", name="Start scan").click()
    expect(dialog).to_be_hidden()
    assert [r.packages for r in dashboard.store.all()] == [[]]


def test_a_typed_package_and_the_ticked_ones_are_sent_together(page: Page, dashboard):
    dashboard.detections[RELATE] = FOUND
    dialog = open_form_with_repo(page, dashboard)
    dialog.get_by_role("button", name="Detect packages").click()
    page.get_by_role("checkbox", name="Relate.Net").uncheck()
    page.get_by_role("checkbox", name="relate-db").uncheck()
    page.get_by_role("textbox", name="Package for repository 1", exact=True).fill("by-hand")
    dialog.get_by_role("button", name="Start scan").click()
    expect(dialog).to_be_hidden()
    assert [r.packages for r in dashboard.store.all()] == [["by-hand", "npm:@relate/relate"]]


def test_a_detected_cli_is_listed_with_its_label_and_can_be_unticked(page: Page, dashboard):
    dashboard.detections[RELATE] = FOUND_CLI
    dialog = open_form_with_repo(page, dashboard)
    dialog.get_by_role("button", name="Detect packages").click()
    group = page.get_by_role("group", name="Packages found in relatedb/relate")
    expect(group.get_by_role("checkbox")).to_have_count(4)
    expect(group).to_contain_text("CLI · GitHub release binaries · latest 1.2.0")
    group.locator('input[value="cli:relate"]').uncheck()
    dialog.get_by_role("button", name="Start scan").click()
    expect(dialog).to_be_hidden()
    assert [r.packages for r in dashboard.store.all()] == [
        ["npm:@relate/relate", "pypi:relate-db", "nuget:Relate.Net"]]


def test_unticking_the_packages_and_keeping_the_cli_sends_only_cli(page: Page, dashboard):
    dashboard.detections[RELATE] = FOUND_CLI
    dialog = open_form_with_repo(page, dashboard)
    dialog.get_by_role("button", name="Detect packages").click()
    for spec in ("npm:@relate/relate", "pypi:relate-db", "nuget:Relate.Net"):
        page.locator(f'.found input[value="{spec}"]').uncheck()
    dialog.get_by_role("button", name="Start scan").click()
    expect(dialog).to_be_hidden()
    assert [r.packages for r in dashboard.store.all()] == [["cli:relate"]]


def test_a_cli_package_needs_a_github_repo_and_says_so_before_sending(page: Page, dashboard):
    dialog = open_form_with_repo(page, dashboard, repo="/srv/code/relate")
    page.get_by_role("textbox", name="Package for repository 1", exact=True).fill("cli:relate")
    dialog.get_by_role("button", name="Start scan").click()
    expect(page.locator("#formErr")).to_contain_text("cli:relate needs the GitHub repository")
    assert dashboard.store.all() == []
    expect(page.locator("#fEco option")).to_have_text(["Auto-detect", "PyPI", "npm", "NuGet"])


def test_unticking_everything_asks_for_a_package(page: Page, dashboard):
    dashboard.detections[RELATE] = FOUND
    dialog = open_form_with_repo(page, dashboard)
    dialog.get_by_role("button", name="Detect packages").click()
    expect(page.get_by_role("checkbox")).to_have_count(3)
    for box in page.get_by_role("checkbox").all():
        box.uncheck()
    dialog.get_by_role("button", name="Start scan").click()
    expect(page.locator("#formErr")).to_contain_text("Tick at least one package for relatedb/relate")
    assert dashboard.store.all() == []


def test_editing_the_repo_after_detecting_drops_what_was_found(page: Page, dashboard):
    dashboard.detections[RELATE] = FOUND
    dialog = open_form_with_repo(page, dashboard)
    dialog.get_by_role("button", name="Detect packages").click()
    page.get_by_role("checkbox", name="relate-db").uncheck()
    page.get_by_role("textbox", name="Repository 1", exact=True).fill(RELATE + "-fork")
    expect(page.locator(".found")).to_have_count(0)
    dialog.get_by_role("button", name="Start scan").click()
    expect(dialog).to_be_hidden()
    assert [r.packages for r in dashboard.store.all()] == [[]]       # nothing stale is sent


def test_a_repo_that_cannot_be_read_shows_its_error_beside_it(page: Page, dashboard):
    dialog = open_form_with_repo(page, dashboard, "https://github.com/a/private")
    dialog.get_by_role("button", name="Detect packages").click()
    found = page.locator(".found")
    expect(found).to_contain_text("repository not found.")
    expect(found).to_contain_text("name a package by hand")
    expect(found.get_by_role("checkbox")).to_have_count(0)
    # the scan can still be started and is not blocked by a failed look
    dialog.get_by_role("button", name="Start scan").click()
    expect(dialog).to_be_hidden()
    assert [r.packages for r in dashboard.store.all()] == [[]]


def test_detect_with_no_repo_says_to_add_one(page: Page, dashboard):
    page.goto(dashboard.url)
    page.locator(".toolbar [data-new]").click()
    page.locator("#dlg").get_by_role("button", name="Detect packages").click()
    expect(page.locator("#detectStatus")).to_have_text("Add a repository first, then look inside it.")
    assert dashboard.detected == []


def test_ecosystem_defaults_to_auto_detect(page: Page, dashboard):
    page.goto(dashboard.url)
    page.locator(".toolbar [data-new]").click()
    page.locator("summary").filter(has_text="Scan options").click()
    eco = page.get_by_label("Ecosystem")
    expect(eco).to_have_value("auto")
    expect(eco.locator("option").first).to_have_text("Auto-detect")


def test_a_no_package_failure_offers_detect_with_the_sources_filled_in(page: Page, dashboard):
    dashboard.detections[RELATE] = FOUND
    rec = dashboard.store.create(DOCS[:1], [RELATE], name="Relate")
    dashboard.fail_next = "no package to verify - name one"
    dashboard.worker.enqueue(rec.id)
    dashboard.worker.join()

    page.goto(f"{dashboard.url}/#/scan/{rec.id}")
    page.get_by_role("button", name="Detect packages and start a new scan").click()
    expect(page.locator("#dlg")).to_be_visible()
    expect(page.get_by_role("textbox", name="Repository 1", exact=True)).to_have_value(RELATE)
    expect(page.get_by_role("textbox", name="Docs URL 1", exact=True)).to_have_value(DOCS[0])
    expect(page.get_by_role("checkbox")).to_have_count(3)


def test_other_failures_do_not_offer_it(page: Page, dashboard):
    rec = dashboard.store.create(DOCS[:1], REPOS[:1], name="Flaky")
    dashboard.fail_next = "no pages acquired"
    dashboard.worker.enqueue(rec.id)
    dashboard.worker.join()
    page.goto(f"{dashboard.url}/#/scan/{rec.id}")
    expect(page.locator("#noticeDesc")).to_contain_text("no pages acquired")
    expect(page.get_by_role("button", name="Detect packages and start a new scan")).to_have_count(0)


def test_detect_form_has_no_horizontal_scroll_on_a_phone(page: Page, dashboard):
    dashboard.detections[RELATE] = FOUND
    page.set_viewport_size({"width": 400, "height": 800})
    dialog = open_form_with_repo(page, dashboard)
    dialog.get_by_role("button", name="Detect packages").click()
    expect(page.get_by_role("checkbox")).to_have_count(3)
    assert page.evaluate("document.documentElement.scrollWidth") <= 400
    assert page.evaluate("document.getElementById('dlg').scrollWidth <= document.getElementById('dlg').clientWidth")


# ------------------------------------------------------------------ coverage

def seed_with_coverage(dashboard, coverage, name="Covered"):
    docs, repos = DOCS[:1], REPOS[:1]
    rec = dashboard.store.create(docs, repos, name=name)
    out = make_outcome(docs, repos)
    if coverage is None:
        out.report.pop("coverage", None)
    else:
        out.report["coverage"] = coverage
    dashboard.store.save_run(rec.id, out.report, [r.model_dump() for r in out.results],
                             out.extraction.model_dump_json())
    dashboard.store.set_status(rec.id, ScanStatus(state="done"))
    return rec


def test_the_detail_view_shows_what_was_not_checked_and_why(page: Page, dashboard):
    rec = seed_with_coverage(dashboard, COVERAGE)
    page.goto(f"{dashboard.url}/#/scan/{rec.id}")
    panel = page.locator("#coverage")
    expect(panel).to_be_visible()
    expect(page.locator("#covSum").locator("span")).to_have_text(["186 snippets", "14 checked", "172 not checked"])
    items = page.locator("#covList li")
    expect(items).to_have_count(4)
    expect(items.nth(0)).to_contain_text("14checked")
    expect(items.nth(1)).to_contain_text("120use no scanned package")
    expect(items.nth(3)).to_contain_text("17no checker")
    expect(items.nth(3)).to_contain_text("rust 9, go 8")
    expect(page.locator("#covBar")).to_have_attribute("aria-label", re.compile("186 snippets · 14 checked · 172 not checked: 120 use"))


def test_a_report_without_coverage_hides_the_panel(page: Page, dashboard):
    rec = seed_with_coverage(dashboard, None, name="Old")
    page.goto(f"{dashboard.url}/#/scan/{rec.id}")
    expect(page.locator("#map .tile").first).to_be_visible()
    expect(page.locator("#coverage")).to_be_hidden()
