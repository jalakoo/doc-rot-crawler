"""Versions and install recipes for STANDALONE command-line tools.

A standalone tool is a binary that is not an npm, PyPI or NuGet package:
`relate` (Rust), `gh`, `rg`, `kubectl`. Nothing in a registry lists its
versions, so the source is the repo's GitHub releases, and the way to get a
specific one is the release asset for the sandbox's OS and CPU.

What Relate showed (measured 2026-11, not assumed):

  * `curl -sSfL https://relatedb.ai/install.sh | bash` has no version flag and
    no pin variable: it reads releases.json, takes `latest.tag`, downloads
    `relate-<os>-<arch>` from GitHub's release download route and verifies it
    against a signed SHA256SUMS. It can only ever install the latest release,
    so a script like that cannot build a version matrix. The asset it fetches
    is a plain public release asset, though - `relate-linux-amd64` on every
    `vX.Y.Z` tag - and fetching THAT is what pins a version.
  * The releases list is shared with other things the repo publishes: SDK jars,
    wheels, npm tarballs, an FFI library, and whole releases for extensions
    (`relate-extensions-git-to-graph-0.14.0`) that are not the tool. A tag is
    only a release of the tool when it is a bare version, or a version behind
    the tool's own name or a generic word (`v1.2.0`, `cli-v1.2.0`,
    `release-1.2.0`); `relate-extensions-...-0.14.0` is someone else's version
    line and would be read as version 0.14.0 of the tool.
  * Asset names are the whole problem: `relate-linux-amd64` is the binary,
    `relate-relate-linux-x64-1.2.0.tgz` is an npm tarball and
    `relate-ffi-x86_64-unknown-linux-gnu.tar.gz` a C library, all of which say
    "linux" and a CPU. `pick_asset` scores what is left over once the tool's
    name, version, OS, CPU and libc are explained: an unexplained word (`ffi`,
    `npm`, `jar`) is a different product.

Recipes are small data (`Recipe`), tried in order, so choosing one is testable
without a sandbox; `install_script` turns a recipe into the shell the sandbox
runs. Out of scope on purpose: `cargo install` / `go install` (minutes per
version) and anything that derives an install script from docs text - an
install command is only ever run when the caller supplied it, and only inside
the ephemeral sandbox.
"""
from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from ..models import Release
from .registry import _key, is_prerelease

API = "https://api.github.com"
USER_AGENT = "docrot"
PAGES = 3                    # x100 releases: far more than a scan needs
BIN_DIR = "$HOME/docrot-bin"

# ----------------------------------------------------------------- repo + tags

_SLUG = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?].*)?$")


def github_slug(repo_url: str) -> tuple[str, str] | None:
    m = _SLUG.search(repo_url.strip())
    return (m.group(1), m.group(2)) if m else None


def release_page(repo_url: str, tag: str = "") -> str:
    """The repo's release page for a tag (or the list when there is no tag)."""
    slug = github_slug(repo_url)
    if not slug:
        return repo_url
    base = f"https://github.com/{slug[0]}/{slug[1]}/releases"
    return f"{base}/tag/{quote(tag)}" if tag else base


# A tag is a release of THE TOOL when it is a bare version, or a version behind
# the tool's own name or a generic word. `relate-extensions-x-0.14.0` is not.
_TAG = re.compile(r"^(?P<prefix>.*?)[-_/ ]?v?(?P<ver>\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?)$|"
                  r"^v?(?P<bare>\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?)$")
GENERIC_PREFIXES = frozenset({"v", "release", "releases", "version", "ver", "rel", "cli", "bin", "app"})


def tag_version(tag: str, names: tuple[str, ...] = ()) -> str:
    """The version a tag names, or "" when it is not a release of this tool."""
    t = tag.strip()
    m = _TAG.match(t)
    if not m:
        return ""
    if m.group("bare"):
        return str(m.group("bare"))
    prefix = (m.group("prefix") or "").strip("-_/ ").lower()
    if not prefix or prefix in GENERIC_PREFIXES or prefix in {n.lower() for n in names if n}:
        return str(m.group("ver"))
    return ""


# ---------------------------------------------------------------------- assets

@dataclass(frozen=True)
class Asset:
    name: str
    url: str = ""
    size: int = 0


@dataclass
class CliRelease:
    label: str                   # the version, as Release.label
    tag: str                     # the git tag it lives under; the asset URLs hang off it
    released_at: str = ""
    assets: list[Asset] = field(default_factory=list)
    prerelease: bool = False


def parse_release(raw: dict, names: tuple[str, ...] = ()) -> CliRelease | None:
    """One GitHub releases-API object as a CliRelease; None for a draft, a
    pre-release, or a release that is not the tool's (see tag_version)."""
    if not isinstance(raw, dict) or raw.get("draft") or raw.get("prerelease"):
        return None
    tag = str(raw.get("tag_name") or "")
    label = tag_version(tag, names)
    if not label or is_prerelease(label):
        return None
    assets = [Asset(str(a.get("name") or ""), str(a.get("browser_download_url") or ""), int(a.get("size") or 0))
              for a in raw.get("assets") or [] if isinstance(a, dict) and a.get("name")]
    return CliRelease(label=label, tag=tag, released_at=str(raw.get("published_at") or "")[:10], assets=assets)


def parse_releases(payload: list, names: tuple[str, ...] = ()) -> list[CliRelease]:
    seen: dict[str, CliRelease] = {}
    for raw in payload or []:
        r = parse_release(raw, names)
        if r and r.label not in seen:
            seen[r.label] = r
    return sorted(seen.values(), key=lambda r: _key(r.label))


_TOKEN = ""                  # set from Settings by `use_token`; the environment is the fallback


def use_token(token: str) -> None:
    """The GitHub token Settings holds (the keychain is not in `os.environ`).
    Empty clears it. It only raises the API's rate limit for release lookups."""
    global _TOKEN
    _TOKEN = (token or "").strip()


def _headers() -> dict[str, str]:
    h = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = _TOKEN or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:                    # 60 requests an hour without one, shared by every scan on an address
        h["Authorization"] = f"Bearer {token}"
    return h


# why-not texts that mean "GitHub could not be asked" rather than "the repo has none"
UNREACHABLE = "could not reach GitHub"
RATE_LIMITED = "GitHub's API rate limit is used up (set GITHUB_TOKEN in Settings or .env to raise it)"


def _github(url: str) -> tuple[list | dict | None, str]:
    """(payload, "") or (None, why it failed) - the why is a sentence for a log.

    An exhausted rate limit is told apart from everything else because it is the
    one failure the user can fix (set GITHUB_TOKEN) and the one a busy day hits:
    60 requests an hour is shared by every scan and detect on an address."""
    try:
        r = httpx.get(url, headers=_headers(), timeout=30.0, follow_redirects=True)
        r.raise_for_status()
        return r.json(), ""
    except httpx.HTTPStatusError as e:
        code, resp = e.response.status_code, e.response
        if code == 429 or (code == 403 and (resp.headers.get("x-ratelimit-remaining") == "0"
                                            or "rate limit" in resp.text[:300].lower())):
            return None, RATE_LIMITED
        if code == 404:
            return None, "GitHub has no public repository there"
        return None, f"{UNREACHABLE} (HTTP {code})"
    except httpx.HTTPError as e:
        return None, f"{UNREACHABLE} ({type(e).__name__})"
    except ValueError:
        return None, f"{UNREACHABLE} (not JSON)"


def _get_json(url: str) -> list | dict | None:
    return _github(url)[0]


def _index(repo_url: str, names: tuple[str, ...] = ()) -> tuple[list[CliRelease], str]:
    """(releases, why not) - see `release_index`; the reason is "" when the
    repo simply has none."""
    slug = github_slug(repo_url)
    if not slug:
        return [], "not a GitHub repository"
    owner, repo = slug
    names = (*names, repo)
    payload: list = []
    problem = ""
    for page in range(1, PAGES + 1):
        got, problem = _github(f"{API}/repos/{owner}/{repo}/releases?per_page=100&page={page}")
        if not isinstance(got, list):
            break
        payload += got
        if len(got) < 100:
            break
    if problem and not payload:
        return [], problem
    found = parse_releases(payload, names)
    if found:
        return found, ""
    tags, problem = _github(f"{API}/repos/{owner}/{repo}/tags?per_page=100")
    out: dict[str, CliRelease] = {}
    for t in tags if isinstance(tags, list) else []:
        tag = str(t.get("name") or "") if isinstance(t, dict) else ""
        label = tag_version(tag, names)
        if label and not is_prerelease(label):
            out.setdefault(label, CliRelease(label=label, tag=tag))
    return sorted(out.values(), key=lambda r: _key(r.label)), ("" if out else problem)


def release_index(repo_url: str, names: tuple[str, ...] = ()) -> list[CliRelease]:
    """Every stable release of the tool, oldest first, with its assets.

    Releases first; when a repo only pushes tags there are no assets to pick
    from, but the versions are still the repo's own and an explicit install
    command can use them."""
    return _index(repo_url, names)[0]


# ------------------------------------------------------------------- lookup

@dataclass
class CliLookup:
    """What a repo's GitHub releases say about one command."""
    name: str
    releases: list[CliRelease] = field(default_factory=list)
    installable: bool = False        # a recent release has a Linux binary for `name`
    why: str = ""                    # when it is not usable, in words for a log line
    failed: bool = False             # GitHub could not be asked (rate limit, network): not "has none"

    def rows(self) -> list[Release]:
        return [Release(label=r.label, released_at=r.released_at) for r in self.releases]


RECENT = 5                           # releases whose assets decide "installable"
CACHE_SECONDS = 600
_cache: dict[tuple[str, str], tuple[float, CliLookup]] = {}


def forget() -> None:
    """Drop what `lookup` remembers (tests; a long-lived server never needs it)."""
    _cache.clear()


def lookup(repo_url: str, name: str) -> CliLookup:
    """Does `repo_url` publish a Linux binary of `name`, and which versions?

    One GitHub request per repo and command (a few more for a repo with
    hundreds of releases), remembered for ten minutes: detect, the scan's
    automatic choice, its version list and the sandbox lane all ask the same
    question, and GitHub allows 60 unauthenticated requests an hour. Only an
    answer is remembered - a rate limit is retried, not cached into a lie."""
    key = (repo_url.strip().rstrip("/").lower(), name.lower())
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    releases, problem = _index(repo_url, (name,))
    slug = github_slug(repo_url)
    names = (name, slug[1]) if slug else (name,)
    usable = [r for r in releases[-RECENT:] if pick_asset(r.assets, "linux", "x86_64", names)]
    out = CliLookup(name, releases, installable=bool(usable), why=problem)
    out.failed = problem.startswith(UNREACHABLE) or problem == RATE_LIMITED
    if not problem and not releases:
        out.why = f"{repo_url} has no GitHub releases of {name}"
    elif releases and not usable:
        out.why = (f"none of the newest {min(RECENT, len(releases))} releases of {name} carries a "
                   "Linux binary docrot can install")
    if not out.failed:
        _cache[key] = (time.monotonic(), out)
    return out


def cli_releases(repo_url: str, name: str = "") -> list[Release]:
    """The tool's stable releases as `Release` rows, oldest first - the CLI
    counterpart of `registry.releases`. Feed it to `pick_versions`.

    `name` is the command (it is how `cli-v1.2.0`-style tags are recognised);
    an empty list means the repo is not on GitHub, has no releases, or the
    network said no."""
    return [Release(label=r.label, released_at=r.released_at) for r in release_index(repo_url, (name,))]


# ------------------------------------------------------------- asset selection

# Things an asset name may say that are not a different product.
_OTHER_OS = re.compile(r"(?<![a-z])(?:darwin|macos|osx|apple|windows|win32|win64|win|msvc|freebsd|netbsd|"
                       r"openbsd|dragonfly|android|illumos|solaris|ios|wasm|wasi|exe)(?![a-z])", re.I)
_ARCHES: dict[str, str] = {
    "x86_64": r"x86[_-]64|amd64|x64|x86_64h",
    "aarch64": r"aarch[_-]?64|arm64|armv8",
    "i386": r"i[3-6]86|x86(?![_-]?64)|ia32|386",
    "arm": r"armv[5-7]\w*|armhf|armel|arm32|\barm\b",
    "other": r"s390x|ppc64le|ppc64|powerpc|riscv64|mips\w*|loongarch64|sparc\w*",
}
_ARCH_RE = {k: re.compile(rf"(?<![a-z0-9]){v}(?![a-z0-9])") for k, v in _ARCHES.items()}
_JUNK_EXT = re.compile(r"\.(sha\d*|sha\d+sum|sig|asc|pem|crt|pub|minisig|sbom|json|jsonl|txt|md|ps1|sh|yml|yaml|"
                       r"msi|pkg|dmg|exe|apk|nupkg|whl|jar|gem|snap|appimage|flatpak|zst|"
                       r"intoto|att|sigstore|bundle|spdx|cdx)$", re.I)
_PACKAGE_EXT = (".deb", ".rpm")
_ARCHIVE_EXT = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".zip")
# a word that makes it not the binary even when it says linux and a CPU
_NOT_THE_TOOL = re.compile(r"(?<![a-z])(src|source|sources|checksums?|sbom|provenance|docs?|man|completions?|"
                           r"debug|symbols|dbg|headers|include|xcframework|notices?|license)(?![a-z])", re.I)
# explained by the name's structure, so not "another product"
_FILLER = frozenset({"unknown", "pc", "gnu", "musl", "static", "linux", "release", "bin", "binary", "latest",
                     "universal", "generic", "glibc", "lib", "libc", "elf", "portable", "full", "standalone"})


def arch_of(name: str) -> str:
    """The CPU an asset name says it is for, "" when it says none."""
    low = name.lower()
    for key in ("x86_64", "aarch64", "arm", "i386", "other"):
        if _ARCH_RE[key].search(low):
            return key
    return ""


def want_arch(machine: str) -> str:
    """The arch key for `uname -m`."""
    m = machine.strip().lower()
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if m in ("aarch64", "arm64", "armv8l"):
        return "aarch64"
    return m


def asset_kind(name: str) -> str:
    """archive | raw | package | skip."""
    low = name.lower()
    if low.endswith(_ARCHIVE_EXT):
        return "archive"
    if low.endswith(_PACKAGE_EXT):
        return "package"
    if _JUNK_EXT.search(low):
        return "skip"
    if re.search(r"\.[a-z][a-z0-9]{0,5}$", low):         # an extension nobody here knows
        return "skip"
    return "raw"


def _strip_ext(name: str) -> str:
    low = name.lower()
    for ext in (*_ARCHIVE_EXT, *_PACKAGE_EXT):
        if low.endswith(ext):
            return low[: -len(ext)]
    return low


def _leftover(stem: str, names: tuple[str, ...]) -> list[str]:
    """Words of the asset name that nothing explains: not the tool's name, a
    version, an OS, a CPU or a libc."""
    s = stem
    for rx in _ARCH_RE.values():
        s = rx.sub(" ", s)
    s = re.sub(r"(?<![a-z0-9])v?\d+(?:[.]\d+)*(?:[-+]\w+)?(?![a-z0-9])", " ", s)
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w]
    have = {n.lower() for n in names if n}
    out: list[str] = []
    used: set[str] = set()
    for w in words:
        if w in _FILLER or w.isdigit():
            continue
        if w in have and w not in used:
            used.add(w)                 # the name once; `relate-relate-...` has a second one
            continue
        out.append(w)
    return out


def score_asset(name: str, os_: str, arch: str, names: tuple[str, ...]) -> int | None:
    """How good a candidate this asset is for the sandbox, None when it is not one."""
    low = name.lower()
    kind = asset_kind(name)
    if kind == "skip" or _NOT_THE_TOOL.search(low) or _OTHER_OS.search(re.sub(r"[^a-z0-9]+", " ", low)):
        return None
    got = arch_of(low)
    if got and got != arch:
        return None
    if os_ == "linux" and "linux" not in low:
        # `tool-amd64` says no OS: a last resort, and only when it names the CPU -
        # a bare `SHA256SUMS` or an sdist says nothing about either
        if got != arch:
            return None
        score = -40
    else:
        score = 60
    score += 40 if got == arch else -20
    if re.search(r"musl|static", low):
        score += 30                                  # runs on any distro the sandbox might be
    elif re.search(r"gnueabi|musleabi", low):
        return None
    if kind == "package":
        score -= 100                                 # last resort: unpacked with dpkg-deb / rpm2cpio
    elif low.endswith((".tar.xz", ".txz")):
        score -= 2
    elif low.endswith((".tar.bz2", ".tbz2")):
        score -= 3
    extra = _leftover(_strip_ext(name), names)
    score -= 15 * len(extra)
    if any(n and low.startswith(n.lower()) for n in names):
        score += 10
    return score


def rank_assets(assets: list[Asset], os_: str = "linux", arch: str = "x86_64",
                names: tuple[str, ...] = ()) -> list[Asset]:
    """Candidates best first; assets that are not for this OS and CPU are gone."""
    scored = [(s, a) for a in assets if (s := score_asset(a.name, os_, arch, names)) is not None]
    scored.sort(key=lambda p: (-p[0], len(p[1].name), p[1].name))
    return [a for _, a in scored]


def pick_asset(assets: list[Asset], os_: str = "linux", arch: str = "x86_64",
               names: tuple[str, ...] = ()) -> Asset | None:
    """The release asset to install for this OS and CPU, or None.

    Prefers a static (musl) build, then gnu; `.tar.gz`/`.tgz` and a raw
    binary over `.tar.xz`; and never an asset with a word that names another
    product (`-ffi`, an npm tarball, a `.jar`). Checksums, signatures and
    installers are not candidates at all, and a `.deb`/`.rpm` only when
    nothing else is."""
    ranked = rank_assets(assets, os_, arch, names)
    return ranked[0] if ranked else None


# --------------------------------------------------------------------- recipes

@dataclass(frozen=True)
class Recipe:
    kind: str                    # "asset" | "command"
    description: str
    url: str = ""                # asset: where it downloads from
    asset: str = ""              # asset: its file name
    command: str = ""            # command: the caller's template, `{version}` and `{tag}` filled in


def render_template(template: str, version: str, tag: str = "") -> str:
    """The caller's install command with `{version}` and `{tag}` filled in,
    shell-quoted so a version can never become a second command."""
    return template.replace("{version}", shlex.quote(version)).replace("{tag}", shlex.quote(tag or version))


def recipes_for(rel: CliRelease | None, version: str, names: tuple[str, ...], os_: str, arch: str,
                install: str = "", limit: int = 3) -> list[Recipe]:
    """Install recipes for one version, in the order they are tried: release
    assets for this OS and CPU, best first, then the caller's own command.

    The command template comes from the caller and nowhere else; an install
    script found in docs text is never derived and run."""
    out: list[Recipe] = []
    if rel is not None:
        for a in rank_assets(rel.assets, os_, arch, names)[:limit]:
            if a.url:
                out.append(Recipe("asset", f"release asset {a.name}", url=a.url, asset=a.name))
    if install.strip():
        out.append(Recipe("command", "the install command supplied for this CLI",
                          command=render_template(install.strip(), version, rel.tag if rel else "")))
    return out


def install_script(recipe: Recipe, name: str, bin_dir: str = BIN_DIR) -> str:
    """The shell that carries a recipe out in the sandbox: leaves an executable
    called `name` in `bin_dir`. Exit 0 only when it did.

    An archive is unpacked and searched for the executable (`name`, else
    `name-*`, else the only executable file in it); a raw download is the
    executable. Downloads follow redirects and fail on HTTP errors, so a 404
    page is never "installed"."""
    q = shlex.quote
    if recipe.kind == "command":
        # Installers put the binary wherever they like; look where they usually do
        # and bring it to bin_dir, where the probe finds it whatever the PATH.
        where = f"{bin_dir}:$HOME/.local/bin:$HOME/.cargo/bin:$HOME/go/bin:$HOME/bin:/usr/local/bin:/usr/local/go/bin"
        return (f"set -eo pipefail\nmkdir -p \"{bin_dir}\"\nexport PATH=\"{where}:$PATH\"\n{recipe.command}\n"
                f"P=$(command -v {q(name)} || true)\n"
                f"[ -n \"$P\" ] || {{ echo 'the install command ran but left no {name} on the PATH' >&2; exit 3; }}\n"
                f"[ \"$P\" = \"{bin_dir}/{name}\" ] || cp -L \"$P\" \"{bin_dir}/{name}\"\n")
    a = recipe.asset.lower()
    work = "$(mktemp -d)"
    lines = ["set -e", f"mkdir -p \"{bin_dir}\"", f"W={work}", 'cd "$W"',
             f"curl -fsSL --retry 3 --retry-delay 2 --proto '=https,file' -o asset {q(recipe.url)}"]
    if a.endswith((".tar.gz", ".tgz")):
        lines.append("tar -xzf asset")
    elif a.endswith((".tar.xz", ".txz")):
        lines.append("tar -xJf asset")
    elif a.endswith((".tar.bz2", ".tbz2")):
        lines.append("tar -xjf asset")
    elif a.endswith(".zip"):
        lines.append("python3 -m zipfile -e asset . 2>/dev/null || unzip -q asset")
    elif a.endswith(".deb"):
        lines.append("dpkg-deb -x asset .")
    elif a.endswith(".rpm"):
        lines.append("rpm2cpio asset | cpio -idm --quiet")
    else:
        lines.append(f"cp asset {q(name)}")
    lines += [
        "rm -f asset",
        f"F=$(find . -type f -name {q(name)} | head -n 1)",
        f"[ -n \"$F\" ] || F=$(find . -type f -name {q(name + '-*')} ! -name '*.md' ! -name '*.txt' | head -n 1)",
        "[ -n \"$F\" ] || F=$(find . -type f -perm -u+x | head -n 1)",
        "[ -n \"$F\" ] || { echo 'no executable found in the release asset' >&2; exit 3; }",
        f"cp \"$F\" \"{bin_dir}/{name}\"",
        f"chmod +x \"{bin_dir}/{name}\"",
    ]
    return "\n".join(lines) + "\n"
