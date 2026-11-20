"""Standalone-CLI acquisition: which tags are the tool's, which release asset is
the binary for a sandbox, and the install script that carries it out. No
network and no sandbox: canned GitHub API payloads, and the generated script
run locally against `file://` assets.

The fixtures are real: Relate's v1.2.0 asset list (a raw binary among jars,
wheels, npm tarballs and an FFI library) and a few well-known shapes.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest

from docrot.acquire import binaries as b
from docrot.acquire.binaries import Asset, Recipe

RELATE_ASSETS = [
    "install.ps1", "install.sh", "relate-1.2.0-linux-aarch_64.jar", "relate-1.2.0-linux-x86_64.jar",
    "relate-1.2.0-osx-aarch_64.jar", "relate-1.2.0-osx-x86_64.jar", "relate-1.2.0-windows-x86_64.jar",
    "relate-1.2.0.jar", "relate-darwin-amd64", "relate-darwin-arm64",
    "relate-ffi-aarch64-apple-darwin.tar.gz", "relate-ffi-aarch64-unknown-linux-gnu.tar.gz",
    "relate-ffi-x86_64-apple-darwin.tar.gz", "relate-ffi-x86_64-pc-windows-msvc.tar.gz",
    "relate-ffi-x86_64-unknown-linux-gnu.tar.gz", "relate-linux-amd64", "relate-linux-arm64",
    "relate-relate-1.2.0.tgz", "relate-relate-darwin-arm64-1.2.0.tgz", "relate-relate-darwin-x64-1.2.0.tgz",
    "relate-relate-linux-arm64-1.2.0.tgz", "relate-relate-linux-x64-1.2.0.tgz", "relate-relate-wasm-1.2.0.tgz",
    "relate-relate-win32-x64-1.2.0.tgz", "relate-windows-amd64.exe", "RelateFFI.xcframework.zip",
    "relate_db-1.2.0-cp39-abi3-macosx_10_12_x86_64.whl", "relate_db-1.2.0-cp39-abi3-macosx_11_0_arm64.whl",
    "relate_db-1.2.0-cp39-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
    "relate_db-1.2.0-cp39-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
    "relate_db-1.2.0-cp39-abi3-win_amd64.whl", "relate_db-1.2.0.tar.gz", "releases.json",
    "SHA256SUMS", "SHA256SUMS.sig", "THIRDPARTY-NOTICES.md",
]


def _assets(names: list[str], tag: str = "v1.2.0") -> list[Asset]:
    return [Asset(n, f"https://github.com/o/r/releases/download/{tag}/{n}") for n in names]


def _pick(names: list[str], tool: tuple[str, ...] = (), arch: str = "x86_64") -> str:
    got = b.pick_asset(_assets(names), "linux", arch, tool)
    return got.name if got else ""


# ------------------------------------------------------------------ tags
def test_a_tag_is_the_tools_when_it_is_a_bare_version_or_behind_its_own_name():
    assert b.tag_version("v1.2.0") == "1.2.0"
    assert b.tag_version("1.2.0") == "1.2.0"
    assert b.tag_version("v1.2") == "1.2"
    assert b.tag_version("cli-v2.40.0") == "2.40.0"
    assert b.tag_version("release-1.0.0") == "1.0.0"
    assert b.tag_version("relate-v1.2.0", ("relate",)) == "1.2.0"
    assert b.tag_version("v1.0.0-rc.1") == "1.0.0-rc.1"      # pre-release filtering is the caller's


def test_a_release_of_something_else_in_the_repo_is_not_a_version_of_the_tool():
    # Relate publishes extensions into the same repo; 0.14.0 is the extension's version
    assert b.tag_version("relate-extensions-git-to-graph-0.14.0", ("relate",)) == ""
    assert b.tag_version("relate-extensions-ideas-0.1.0", ("relate",)) == ""
    assert b.tag_version("nightly") == "" and b.tag_version("latest") == ""
    assert b.tag_version("docs-v1.0.0", ("relate",)) == ""


def _release(tag: str, names: list[str], **kw) -> dict:
    return {"tag_name": tag, "draft": False, "prerelease": False, "published_at": kw.get("at", "2026-11-20T09:20:24Z"),
            "assets": [{"name": n, "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{n}",
                        "size": 10} for n in names], **{k: v for k, v in kw.items() if k != "at"}}


def test_parse_releases_keeps_stable_tool_releases_oldest_first_with_their_assets():
    payload = [
        _release("v1.2.0", RELATE_ASSETS, at="2026-11-20T09:20:24Z"),
        _release("relate-extensions-git-to-graph-0.14.0", ["git-to-graph-0.14.0-linux-amd64.tar.gz"]),
        _release("v1.1.0", RELATE_ASSETS, at="2026-11-17T15:11:40Z"),
        _release("v1.3.0-rc.1", RELATE_ASSETS, prerelease=True),
        _release("v1.0.9", [], draft=True),
        _release("v0.9.0", ["relate-linux-amd64"], at="2026-10-01T00:00:00Z"),
    ]
    got = b.parse_releases(payload, ("relate",))
    assert [(r.label, r.tag, r.released_at) for r in got] == [
        ("0.9.0", "v0.9.0", "2026-10-01"), ("1.1.0", "v1.1.0", "2026-11-17"), ("1.2.0", "v1.2.0", "2026-11-20")]
    assert got[-1].assets[0].name == "install.ps1" and got[-1].assets[0].url.endswith("/v1.2.0/install.ps1")


def test_a_pre_release_by_label_is_dropped_even_when_github_does_not_flag_it():
    got = b.parse_releases([_release("v2.0.0-beta.3", []), _release("v1.9.0", [])])
    assert [r.label for r in got] == ["1.9.0"]


def test_cli_releases_are_release_rows_from_the_github_api(monkeypatch):
    seen = []

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    def get(url, **kw):
        seen.append((url, kw["headers"]["User-Agent"]))
        return Resp([_release("v1.1.0", ["x"], at="2026-11-17T00:00:00Z"), _release("v1.2.0", ["x"])])

    monkeypatch.setattr(httpx, "get", get)
    rows = b.cli_releases("https://github.com/relatedb/relate.git", "relate")
    assert [(r.label, r.released_at) for r in rows] == [("1.1.0", "2026-11-17"), ("1.2.0", "2026-11-20")]
    assert seen[0][0].startswith("https://api.github.com/repos/relatedb/relate/releases?per_page=100")
    assert "docrot" in seen[0][1] and "@" not in seen[0][1]            # a generic client name


def test_a_repo_with_tags_but_no_releases_still_has_versions(monkeypatch):
    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    def get(url, **kw):
        return Resp([] if "/releases" in url else [{"name": "v0.2.0"}, {"name": "v0.1.0"}, {"name": "junk"}])

    monkeypatch.setattr(httpx, "get", get)
    idx = b.release_index("https://github.com/o/tool")
    assert [(r.label, r.tag, r.assets) for r in idx] == [("0.1.0", "v0.1.0", []), ("0.2.0", "v0.2.0", [])]


def test_no_github_repo_and_a_dead_network_are_no_releases(monkeypatch):
    assert b.cli_releases("https://gitlab.com/o/r") == []

    def boom(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    assert b.cli_releases("https://github.com/o/r") == []


def test_release_pages_link_the_tag():
    assert b.release_page("https://github.com/o/r.git", "v1.2.0") == "https://github.com/o/r/releases/tag/v1.2.0"
    assert b.release_page("https://github.com/o/r") == "https://github.com/o/r/releases"
    assert b.release_page("https://example.com/x", "v1") == "https://example.com/x"


# --------------------------------------------------------------- asset choice
def test_relate_picks_its_raw_binary_over_the_npm_tarball_and_the_ffi_library():
    assert _pick(RELATE_ASSETS, ("relate",)) == "relate-linux-amd64"
    assert _pick(RELATE_ASSETS, ("relate",), "aarch64") == "relate-linux-arm64"
    ranked = [a.name for a in b.rank_assets(_assets(RELATE_ASSETS), "linux", "x86_64", ("relate",))]
    # the lookalikes follow, in order: they say linux and a CPU but are other products
    assert ranked[:3] == ["relate-linux-amd64", "relate-relate-linux-x64-1.2.0.tgz",
                          "relate-ffi-x86_64-unknown-linux-gnu.tar.gz"]
    assert not any(n.endswith((".jar", ".whl", ".exe", ".sh", ".ps1", ".sig", ".md")) or "darwin" in n
                   for n in ranked)
    assert "SHA256SUMS" not in ranked and "releases.json" not in ranked and "relate_db-1.2.0.tar.gz" not in ranked


def test_ripgrep_style_triples_prefer_musl_then_gnu():
    names = ["ripgrep-14.1.0-aarch64-unknown-linux-gnu.tar.gz",
             "ripgrep-14.1.0-x86_64-unknown-linux-gnu.tar.gz",
             "ripgrep-14.1.0-x86_64-unknown-linux-musl.tar.gz",
             "ripgrep-14.1.0-x86_64-apple-darwin.tar.gz",
             "ripgrep-14.1.0-x86_64-pc-windows-msvc.zip",
             "ripgrep-14.1.0-armv7-unknown-linux-gnueabihf.tar.gz",
             "ripgrep-14.1.0-i686-unknown-linux-gnu.tar.gz",
             "ripgrep_14.1.0-1_amd64.deb", "ripgrep-14.1.0.tar.gz.sha256"]
    assert _pick(names, ("rg", "ripgrep")) == "ripgrep-14.1.0-x86_64-unknown-linux-musl.tar.gz"
    assert _pick([n for n in names if "musl" not in n], ("rg", "ripgrep")) == \
        "ripgrep-14.1.0-x86_64-unknown-linux-gnu.tar.gz"
    assert _pick(names, ("rg", "ripgrep"), "aarch64") == "ripgrep-14.1.0-aarch64-unknown-linux-gnu.tar.gz"


def test_gh_style_underscored_names():
    names = ["gh_2.40.0_linux_amd64.tar.gz", "gh_2.40.0_linux_arm64.tar.gz", "gh_2.40.0_linux_386.tar.gz",
             "gh_2.40.0_linux_amd64.deb", "gh_2.40.0_linux_amd64.rpm", "gh_2.40.0_macOS_amd64.zip",
             "gh_2.40.0_windows_amd64.msi", "gh_2.40.0_checksums.txt"]
    assert _pick(names, ("gh",)) == "gh_2.40.0_linux_amd64.tar.gz"
    assert _pick(names, ("gh",), "aarch64") == "gh_2.40.0_linux_arm64.tar.gz"


def test_a_raw_binary_a_zip_and_a_package_are_all_candidates_in_that_order_of_preference():
    assert _pick(["tool-linux-amd64", "tool-darwin-amd64", "tool-windows-amd64.exe"], ("tool",)) == "tool-linux-amd64"
    assert _pick(["tool-1.0-linux-x86_64.zip", "tool-1.0-macos.zip"], ("tool",)) == "tool-1.0-linux-x86_64.zip"
    assert _pick(["tool_1.0_amd64.deb", "tool-1.0-1.x86_64.rpm", "tool.sha256", "tool.sig"], ("tool",)) \
        == "tool_1.0_amd64.deb"                                           # a package only when nothing else is
    assert _pick(["tool-linux-amd64.tar.gz", "tool-linux-amd64.tar.xz"], ("tool",)) == "tool-linux-amd64.tar.gz"


def test_nothing_for_this_machine_is_none_not_the_wrong_one():
    assert _pick(["tool-darwin-arm64", "tool-windows-amd64.exe", "tool-linux-arm64"], ("tool",)) == ""
    assert _pick(["SHA256SUMS", "tool.sha256", "tool-1.0.tar.gz", "tool-src.tar.gz"], ("tool",)) == ""
    assert _pick([], ("tool",)) == ""


def test_asset_kinds_and_architectures():
    assert b.asset_kind("t-linux-amd64") == "raw" and b.asset_kind("t.tar.gz") == "archive"
    assert b.asset_kind("t.deb") == "package" and b.asset_kind("t.sha256") == "skip"
    assert b.asset_kind("t.whl") == "skip" and b.asset_kind("t.exe") == "skip"
    assert [b.arch_of(n) for n in ("a-x86_64", "a-amd64", "a_x64", "a-aarch64", "a-arm64", "a-aarch_64",
                                   "a-386", "a-armv7", "a-linux")] == \
        ["x86_64", "x86_64", "x86_64", "aarch64", "aarch64", "aarch64", "i386", "arm", ""]
    assert b.want_arch("amd64") == "x86_64" and b.want_arch("arm64") == "aarch64"


# -------------------------------------------------------------------- recipes
def test_recipes_are_assets_best_first_then_the_callers_command():
    rel = b.CliRelease("1.1.0", "v1.1.0", assets=_assets(RELATE_ASSETS, "v1.1.0"))
    got = b.recipes_for(rel, "1.1.0", ("relate",), "linux", "x86_64", "curl -fsSL https://x/i.sh | sh -s -- {version}")
    assert [r.kind for r in got] == ["asset", "asset", "asset", "command"]
    assert got[0].asset == "relate-linux-amd64" and got[0].url.endswith("/v1.1.0/relate-linux-amd64")
    assert got[-1].command == "curl -fsSL https://x/i.sh | sh -s -- 1.1.0"


def test_no_command_is_ever_derived_without_the_caller():
    assert b.recipes_for(None, "1.0.0", ("tool",), "linux", "x86_64") == []
    rel = b.CliRelease("1.0.0", "v1.0.0", assets=_assets(["tool-darwin-arm64"]))
    assert b.recipes_for(rel, "1.0.0", ("tool",), "linux", "x86_64") == []
    # only a template the caller passed is run; with no release the command alone remains
    got = b.recipes_for(None, "1.0.0", ("tool",), "linux", "x86_64", "get-tool {version}")
    assert [r.kind for r in got] == ["command"]


def test_a_version_cannot_become_a_second_command():
    assert b.render_template("get {version}", "1.0.0; rm -rf ~") == "get '1.0.0; rm -rf ~'"
    assert b.render_template("get {version} {tag}", "1.0.0", "v1.0.0") == "get 1.0.0 v1.0.0"


# ----------------------------------------------------------- the install script
@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def _run_script(recipe: Recipe, home: Path, name: str = "tool") -> subprocess.CompletedProcess:
    script = home.parent / "install.sh"
    script.write_text(b.install_script(recipe, name, str(home / "bin")))
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(home)}, cwd=home.parent)


needs_curl = pytest.mark.skipif(shutil.which("curl") is None or shutil.which("bash") is None,
                                reason="curl or bash missing")


def _asset(tmp_path: Path, name: str, data: bytes) -> Recipe:
    d = tmp_path / "served"
    d.mkdir(exist_ok=True)
    (d / name).write_bytes(data)
    return Recipe("asset", name, url=(d / name).as_uri(), asset=name)


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for path, data in members.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o755 if "bin" in path or path.endswith("tool") else 0o644
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


SCRIPT = b"#!/bin/sh\necho tool 1.2.3\n"


@needs_curl
def test_a_raw_binary_download_becomes_the_executable(tmp_path, home):
    r = _run_script(_asset(tmp_path, "tool-linux-amd64", SCRIPT), home)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([str(home / "bin" / "tool")], capture_output=True, text=True)
    assert out.stdout.strip() == "tool 1.2.3"


@needs_curl
def test_an_archive_is_searched_for_the_executable(tmp_path, home):
    data = _tar_gz({"tool-1.2.3-x86_64/README.md": b"hi", "tool-1.2.3-x86_64/tool": SCRIPT,
                    "tool-1.2.3-x86_64/completions/tool.bash": b"#"})
    r = _run_script(_asset(tmp_path, "tool-1.2.3-x86_64.tar.gz", data), home)
    assert r.returncode == 0, r.stderr
    assert subprocess.run([str(home / "bin" / "tool")], capture_output=True, text=True).stdout.strip() == "tool 1.2.3"


@needs_curl
def test_a_zip_is_unpacked(tmp_path, home):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("tool", SCRIPT)
    r = _run_script(_asset(tmp_path, "tool-1.0-linux-x86_64.zip", buf.getvalue()), home)
    assert r.returncode == 0, r.stderr
    assert (home / "bin" / "tool").exists() and os.access(home / "bin" / "tool", os.X_OK)


@needs_curl
def test_a_download_that_fails_installs_nothing(tmp_path, home):
    bad = Recipe("asset", "tool-linux-amd64", url=(tmp_path / "nowhere").as_uri(), asset="tool-linux-amd64")
    r = _run_script(bad, home)
    assert r.returncode != 0 and not (home / "bin" / "tool").exists()


@needs_curl
def test_an_archive_with_no_executable_is_an_error_not_a_silent_success(tmp_path, home):
    # the only file is not executable and is not the tool
    data = _tar_gz({"docs/readme.txt": b"nothing"})
    r = _run_script(_asset(tmp_path, "tool-1.0-linux.tar.gz", data), home)
    assert r.returncode == 3 and "no executable" in r.stderr


@needs_curl
def test_the_callers_command_may_install_anywhere_the_usual_places_are_searched(tmp_path, home):
    cmd = "mkdir -p $HOME/.local/bin && printf '#!/bin/sh\\necho tool 9\\n' > $HOME/.local/bin/tool && " \
          "chmod +x $HOME/.local/bin/tool"
    r = _run_script(Recipe("command", "the install command", command=cmd), home)
    assert r.returncode == 0, r.stderr
    assert subprocess.run([str(home / "bin" / "tool")], capture_output=True, text=True).stdout.strip() == "tool 9"
    # and one that leaves nothing behind fails, whatever its own exit status
    r = _run_script(Recipe("command", "noop", command="true"), home, "ghost")
    assert r.returncode == 3 and "left no ghost" in r.stderr


@needs_curl
def test_a_failing_stage_of_a_piped_install_fails_the_recipe(tmp_path, home):
    r = _run_script(Recipe("command", "piped", command="false | cat"), home)
    assert r.returncode != 0
