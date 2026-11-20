from __future__ import annotations

import json
import os
import socket
import subprocess
import sys

import pytest

from docrot.store import (
    KEEP_RUNS,
    ScanOptions,
    ScanStatus,
    Store,
    clean_packages,
    clean_sources,
    default_name,
    is_docs_url,
    is_repo,
)
from tests.fakes import make_outcome


@pytest.mark.parametrize("value,ok", [
    ("https://docs.example.com", True),
    ("http://docs.example.com/latest/", True),
    ("ftp://docs.example.com", False),
    ("https://localhost", False),
    ("not a url", False),
])
def test_docs_url_rules(value, ok):
    assert is_docs_url(value) is ok


@pytest.mark.parametrize("value,ok", [
    ("https://github.com/org/repo", True),
    ("git@github.com:org/repo.git", True),
    ("ssh://git@github.com/org/repo", True),
    ("/abs/path", True), ("./rel", True), ("~/code/repo", True),
    ("pydantic-core", False), ("github.com/org/repo", False),
])
def test_repo_rules(value, ok):
    assert is_repo(value) is ok


def test_clean_sources_trims_dedupes_and_reports_every_problem():
    docs, repos = clean_sources([" https://a.io ", "https://a.io", ""], ["./r"])
    assert docs == ["https://a.io"] and repos == ["./r"]
    with pytest.raises(ValueError) as e:
        clean_sources(["https://a.io", "nope"], ["bad"])
    assert e.value.args[0] == [
        "Docs URL 2 isn't an http(s) address.",
        "Repository 1 isn't a git URL or a local path (/…, ./…, ~/…).",
    ]
    with pytest.raises(ValueError, match="at least one"):
        clean_sources(["  "], [])


def test_default_name():
    assert default_name(["https://docs.a.io/latest/"], []) == "docs.a.io/latest"
    assert default_name([], ["git@github.com:org/thing.git"]) == "thing"


def test_create_persists_and_reloads(store, tmp_path):
    rec = store.create(["https://docs.a.io"], ["https://github.com/a/b", "https://github.com/a/c"],
                       name="  Alpha   docs ", options=ScanOptions(versions=2))
    assert rec.name == "Alpha docs" and rec.id.startswith("alpha-docs-")
    again = Store(tmp_path / "data").get(rec.id)
    assert again == rec
    assert again.options.versions == 2


def test_rename_validates(store):
    rec = store.create(["https://docs.a.io"], [])
    assert store.update(rec.id, name="New name").name == "New name"
    with pytest.raises(ValueError):
        store.update(rec.id, name="   ")
    with pytest.raises(ValueError):
        store.update(rec.id, name="x" * 61)
    with pytest.raises(KeyError):
        store.update("missing-000000", name="x")


def test_ids_cannot_escape_the_store(store):
    assert store.get("../etc") is None
    with pytest.raises(KeyError):
        store.status("../../x")


def test_find_by_sources_ignores_order(store):
    rec = store.create(["https://b.io", "https://a.io"], ["./r"])
    assert store.find_by_sources(["https://a.io", "https://b.io"], ["./r"]).id == rec.id
    assert store.find_by_sources(["https://a.io"], ["./r"]) is None


def test_save_run_and_report(store):
    rec = store.create(["https://docs.a.io"], ["https://github.com/a/alpha"])
    out = make_outcome(rec.docs, rec.repos)
    run = store.save_run(rec.id, out.report, [r.model_dump() for r in out.results],
                         out.extraction.model_dump_json())
    assert run.stats["findings"] == out.report["stats"]["findings"]
    assert sum(run.page_states.values()) == out.report["stats"]["pages"]
    assert store.report(rec.id)["stats"] == out.report["stats"]
    assert store.report(rec.id, run.id) is not None
    with pytest.raises(KeyError):
        store.report(rec.id, "../../secrets")


def test_run_ids_are_unique_and_history_is_capped(store):
    rec = store.create(["https://docs.a.io"], [])
    report = {"generated_at": "2026-09-15T10:00:00", "stats": {}, "sections": []}
    for _ in range(KEEP_RUNS + 2):
        store.save_run(rec.id, report, [], "{}")
    runs = store.runs(rec.id)
    assert len(runs) == KEEP_RUNS
    assert len({r.id for r in runs}) == KEEP_RUNS


def write_status(store, scan_id, **fields):
    """A status exactly as written, bypassing set_status's owner stamp - the way
    a status from an older docrot, another host or a dead process looks on disk."""
    store._write(store._dir(scan_id) / "status.json", ScanStatus(**fields).model_dump())


@pytest.fixture
def foreign():
    """A real process that is not us, killed on teardown."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield proc
    proc.kill()
    proc.wait()


def test_recover_fails_scans_with_no_recorded_owner(store):
    # changed from test_recover_marks_orphaned_scans_failed: set_status now stamps
    # an owner, so the old-format status this test is about is written raw
    a = store.create(["https://a.io"], [])
    b = store.create(["https://b.io"], [])
    write_status(store, a.id, state="running")
    store.set_status(b.id, ScanStatus(state="done"))
    assert store.recover() == [a.id]
    assert store.status(a.id).state == "failed"
    assert "Interrupted" in store.status(a.id).error
    assert store.status(b.id).state == "done"


def test_recover_leaves_a_scan_owned_by_a_live_other_process_alone_until_it_dies(store, foreign):
    rec = store.create(["https://a.io"], [])
    store.set_status(rec.id, ScanStatus(state="running", pid=foreign.pid, host=socket.gethostname()))
    assert store.recover() == []
    assert store.status(rec.id).state == "running" and store.is_active(rec.id)
    foreign.kill()
    foreign.wait()
    assert not store.is_active(rec.id)
    assert store.recover() == [rec.id]
    assert store.status(rec.id).state == "failed"


def test_recover_never_fails_the_callers_own_scan(store):
    rec = store.create(["https://a.io"], [])
    store.set_status(rec.id, ScanStatus(state="running"))
    st = store.status(rec.id)
    assert (st.pid, st.host) == (os.getpid(), socket.gethostname())
    assert store.recover() == [] and store.status(rec.id).state == "running"


def test_recover_leaves_another_hosts_scan_alone(store):
    rec = store.create(["https://a.io"], [])
    dead_pid = 2 ** 22 + 12345            # no such process here, but this is not here
    store.set_status(rec.id, ScanStatus(state="queued", pid=dead_pid, host="some-other-host"))
    assert store.recover() == [] and store.status(rec.id).state == "queued"


def test_queued_and_running_statuses_keep_their_owner_through_updates(store):
    rec = store.create(["https://a.io"], [])
    st = ScanStatus(state="queued")
    store.set_status(rec.id, st)
    st.state = "running"
    st.log.append(["t-sys", "x"])
    store.set_status(rec.id, st)
    assert store.status(rec.id).pid == os.getpid()
    store.set_status(rec.id, ScanStatus(state="done"))
    assert store.status(rec.id).pid is None          # nothing to own once finished


def test_a_status_file_from_before_owners_still_loads(store):
    rec = store.create(["https://a.io"], [])
    path = store._dir(rec.id) / "status.json"
    path.write_text(json.dumps({"state": "done", "phase": 5, "queued_at": "", "started_at": "",
                                "finished_at": "t", "error": "", "log": []}))
    st = store.status(rec.id)
    assert st.state == "done" and st.pid is None and st.host == ""


def _legacy_run(d, generated_at, findings):
    d.mkdir(parents=True)
    (d / "results.json").write_text("[]")
    (d / "extraction.json").write_text("{}")
    (d / "report.json").write_text(json.dumps({
        "generated_at": generated_at, "docs_url": "https://docs.a.io",
        "repo_url": "https://github.com/a/alpha", "package": "alpha",
        "stats": {"findings": findings}, "sections": []}))


def test_import_legacy_pairs_each_run_with_its_own_report(store, tmp_path):
    runs = tmp_path / "runs"
    # the old archiver copied the previous run's report into each archive
    _legacy_run(runs / "20260912T100500", "2026-09-12T10:00:00", 1)
    _legacy_run(runs / "20260912T101000", "2026-09-12T10:05:00", 2)
    _legacy_run(runs / "latest", "2026-09-12T10:10:00", 3)
    rec = store.import_legacy(runs)
    assert rec.docs == ["https://docs.a.io"] and rec.name == "alpha"
    got = [(r.id, r.stats["findings"]) for r in store.runs(rec.id)]
    assert got == [("20260912T100500", 2), ("20260912T101000", 3)]
    assert store.import_legacy(runs) is None           # only once


def test_packages_stay_aligned_with_repos(store):
    assert clean_packages(["", " b ", "", "c", " ", ""]) == ["", "b", "", "c"]
    assert clean_packages(None) == [] and clean_packages(["", " "]) == []
    rec = store.create(["https://docs.a.io"], ["./a", "./b"], packages=["", "pkg-b", "pkg-c", ""])
    assert store.get(rec.id).packages == ["", "pkg-b", "pkg-c"]


def test_docs_plus_a_bare_package_is_a_valid_scan(store):
    rec = store.create(["https://docs.a.io"], [], packages=["only-pkg"])
    got = store.get(rec.id)
    assert got.repos == [] and got.packages == ["only-pkg"]


def test_packages_follow_their_repos_through_de_duplication(store):
    from docrot.store import align_packages
    repos = ["./a", "./a", "./b", ""]
    # the duplicate's package is used only when the first had none
    assert align_packages(repos, ["pa", "other", "pb", "x", "loose"]) == ["pa", "pb", "loose"]
    assert align_packages(["./a", "./a", "./b"], ["", "pa", "pb"]) == ["pa", "pb"]
    rec = store.create(["https://docs.a.io"], ["./a", " ./a ", "./b"], packages=["", "pa", "pb"])
    assert store.get(rec.id).repos == ["./a", "./b"]
    assert store.get(rec.id).packages == ["pa", "pb"]


def test_package_prefixes_name_the_registry_and_are_normalised():
    from docrot.store import split_package
    assert clean_packages([" pypi:relate-db ", "npm: @relate/relate", "nuget:Relate.Net"]) == \
        ["pypi:relate-db", "npm:@relate/relate", "nuget:Relate.Net"]
    assert split_package("npm:@relate/relate") == ("npm", "@relate/relate")
    # a scoped or dotted name with no prefix is still just a name
    assert split_package("@relate/relate") == ("", "@relate/relate")
    assert split_package("Relate.Net") == ("", "Relate.Net")
    assert clean_packages(["@relate/relate", "Relate.Net"]) == ["@relate/relate", "Relate.Net"]


def test_an_unknown_or_empty_prefix_is_rejected_with_every_problem_listed():
    with pytest.raises(ValueError) as e:
        clean_packages(["ok", "cargo:serde", "PyPI:foo", "npm:", "gem:rails"])
    problems = e.value.args[0]
    assert len(problems) == 4
    assert "Package 2 (cargo:serde) has an unknown ecosystem prefix 'cargo:'" in problems[0]
    assert "use pypi:, npm: or nuget:" in problems[0]
    assert "PyPI:foo" in problems[1]            # only the lowercase spellings count
    assert "Package 4" in problems[2] and "names no package" in problems[2]
    assert "gem:rails" in problems[3]


def test_prefixed_packages_stay_aligned_with_their_repos(store):
    from docrot.store import align_packages
    repos = ["./a", "./a", "./b"]
    assert align_packages(repos, ["", "npm:pa", "pypi:pb", "nuget:loose"]) == \
        ["npm:pa", "pypi:pb", "nuget:loose"]
    rec = store.create(["https://docs.a.io"], ["./a", "./b"], packages=["", " nuget: Pb "])
    assert store.get(rec.id).packages == ["", "nuget:Pb"]


def test_a_bad_prefix_stops_a_scan_being_created_or_aligned(store):
    from docrot.store import align_packages
    with pytest.raises(ValueError, match="unknown ecosystem prefix"):
        store.create(["https://docs.a.io"], ["./a"], packages=["cargo:x"])
    with pytest.raises(ValueError):
        align_packages(["./a"], ["rubygems:x"])
    assert store.all() == []


def test_auto_is_an_ecosystem_and_old_records_keep_theirs(store):
    from pydantic import ValidationError

    assert ScanOptions().ecosystem == "pypi"             # what a record written before `auto` means
    assert ScanOptions(ecosystem="auto").ecosystem == "auto"
    with pytest.raises(ValidationError):
        ScanOptions(ecosystem="cargo")
    rec = store.create(["https://docs.a.io"], [], packages=["momentic"],
                       options=ScanOptions(ecosystem="npm"))
    path = store.scans_dir / rec.id / "scan.json"
    data = json.loads(path.read_text())
    data["options"].pop("assume_credentialed")           # an older file, missing newer fields
    path.write_text(json.dumps(data))
    got = store.get(rec.id)
    assert got.options.ecosystem == "npm" and got.packages == ["momentic"]
