"""Settings saved from the dashboard, kept in the operating system's keychain.

Everything the dashboard saves lands in one keychain item (service `docrot`,
account `settings`): a JSON object of `ENV_NAME -> value`. `config.load` lays
it over the environment, so a value saved here wins over the same name in
`.env`, and removing it falls back to `.env` again.

The keychain is macOS Keychain, Windows Credential Locker or the Secret Service
on Linux, whichever `keyring` finds. Nothing is ever written to a file, and a
secret's value never leaves this module towards the browser: `describe` reports
only whether one is set and where it came from.
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

SERVICE = "docrot"
ITEM = "settings"
SANDBOX_PREFIX = "SANDBOX_ENV_"
SANDBOX_KEY = re.compile(r"^SANDBOX_ENV_[A-Z][A-Z0-9_]{0,63}$")
MAX_VALUE = 4096


class Unavailable(Exception):
    """This machine has no keychain docrot can use."""


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    secret: bool = False
    kind: str = "text"          # "text" or "int"
    default: str = ""           # what applies when neither keychain nor .env sets it
    hint: str = ""


GROUPS = [
    {"id": "sandbox", "title": "Sandbox execution",
     "note": "Daytona runs the snippets. Without a key, pages are reported unverified."},
    {"id": "llm", "title": "Extraction model",
     "note": "Optional. Without a key, tiering falls back to rules and prose claims are skipped."},
    {"id": "graph", "title": "Graph store",
     "note": "Neo4j. Optional - only `docrot blast` needs it."},
    {"id": "tuning", "title": "Run tuning", "note": "Leave blank to keep the default."},
]

FIELDS = [
    Field("DAYTONA_API_KEY", "Daytona API key", "sandbox", secret=True),
    Field("DAYTONA_API_URL", "Daytona API URL", "sandbox",
          hint="Only for a self-hosted or non-default Daytona region."),
    Field("DAYTONA_TARGET", "Daytona target", "sandbox"),

    Field("EXTRACTION_API_KEY", "API key", "llm", secret=True,
          hint="An OpenRouter key, or a key for whatever the base URL points at."),
    Field("EXTRACTION_BASE_URL", "Base URL", "llm", default="https://openrouter.ai/api/v1"),
    Field("EXTRACTION_MODELS", "Models", "llm",
          default="anthropic/claude-opus-5,anthropic/claude-sonnet-5",
          hint="Comma-separated vendor/model ids. The first is used; the rest are fallbacks."),

    Field("NEO4J_URI", "URI", "graph", default="bolt://localhost:7687",
          hint="Use neo4j+s:// for Aura."),
    Field("NEO4J_USERNAME", "Username", "graph", default="neo4j"),
    Field("NEO4J_PASSWORD", "Password", "graph", secret=True),
    Field("NEO4J_DATABASE", "Database", "graph", default="neo4j"),

    Field("DOCROT_MAX_PAGES", "Max pages per site", "tuning", kind="int", default="60"),
    Field("DOCROT_CONCURRENCY", "Concurrent sandboxes", "tuning", kind="int", default="8"),
    Field("DOCROT_SNIPPET_TIMEOUT", "Snippet timeout (s)", "tuning", kind="int", default="45"),
    Field("DOCROT_INSTALL_TIMEOUT", "Install timeout (s)", "tuning", kind="int", default="180"),
    Field("DOCROT_BUILD_TIMEOUT", ".NET build timeout (s)", "tuning", kind="int", default="900"),
    Field("DOCROT_DOTNET_IMAGE", ".NET sandbox image", "tuning",
          default="mcr.microsoft.com/dotnet/sdk:10.0"),
    Field("DOCROT_NODE_IMAGE", "npm sandbox image", "tuning",
          hint="Leave blank to use Daytona's default snapshot (Node 24)."),
    Field("DOCROT_USER_AGENT", "User agent", "tuning",
          default="docrot/0.1 (+https://github.com/your-org/docrot)"),
]
BY_KEY = {f.key: f for f in FIELDS}


# ------------------------------------------------------------------ backends

class Backend(Protocol):
    name: str

    def read(self) -> str | None: ...
    def write(self, text: str) -> None: ...
    def clear(self) -> None: ...


class KeyringBackend:
    """The OS keychain, through `keyring`."""

    def __init__(self) -> None:
        try:
            import keyring
            from keyring.backends import fail
        except ImportError as e:
            raise Unavailable("the `keyring` package is not installed - "
                              "run: uv pip install -e .") from e
        ring = keyring.get_keyring()
        if isinstance(ring, fail.Keyring):
            raise Unavailable("no system keychain was found on this machine")
        self._keyring = keyring
        self.name = getattr(ring, "name", "") or type(ring).__name__

    def _call(self, fn, *args):
        try:
            return fn(SERVICE, ITEM, *args)
        except Exception as e:      # locked, denied, or no session bus
            raise Unavailable(f"the system keychain refused: {e}") from e

    def read(self) -> str | None:
        return self._call(self._keyring.get_password)

    def write(self, text: str) -> None:
        self._call(self._keyring.set_password, text)

    def clear(self) -> None:
        if self.read() is not None:
            self._call(self._keyring.delete_password)


class MemoryBackend:
    """For tests: the same contract, held in a variable."""
    name = "memory"

    def __init__(self) -> None:
        self.text: str | None = None

    def read(self) -> str | None:
        return self.text

    def write(self, text: str) -> None:
        self.text = text

    def clear(self) -> None:
        self.text = None


_lock = threading.Lock()
_backend: Backend | None = None


def use(backend: Backend | None) -> None:
    """Swap the backend (tests). `None` goes back to the OS keychain."""
    global _backend
    _backend = backend


def backend() -> Backend:
    global _backend
    if _backend is None:
        _backend = KeyringBackend()
    return _backend


# ------------------------------------------------------------- read / write

def _read() -> dict[str, str]:
    raw = backend().read()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v and _known(k)}


def overrides() -> dict[str, str]:
    """What the dashboard saved. Empty when there is no keychain: a missing
    keychain must not stop a scan that `.env` alone can configure."""
    try:
        return _read()
    except Unavailable:
        return {}


def _known(key: str) -> bool:
    return key in BY_KEY or bool(SANDBOX_KEY.match(key))


def _check(key: str, value: str) -> str | None:
    """The reason `value` cannot be saved under `key`, or None."""
    field = BY_KEY.get(key)
    label = field.label if field else key
    if len(value) > MAX_VALUE:
        return f"{label}: too long."
    if any(ch in value for ch in "\r\n\0"):
        return f"{label}: must be a single line."
    if field and field.kind == "int" and not (value.isascii() and value.isdigit() and int(value) > 0):
        return f"{label}: must be a whole number above zero."
    return None


def save(changes: Mapping[str, str | None]) -> None:
    """Merge `changes` into the keychain. A value of None or "" removes that
    override. Raises ValueError(list of messages) and saves nothing if any
    entry is bad."""
    errors, clean = [], {}
    for key, value in changes.items():
        if not _known(key):
            errors.append(f"{key}: not a setting. Sandbox credentials are named "
                          "SANDBOX_ENV_ plus capitals, digits and underscores.")
            continue
        value = (value or "").strip()
        problem = _check(key, value) if value else None
        if problem:
            errors.append(problem)
        clean[key] = value
    if errors:
        raise ValueError(errors)
    with _lock:
        current = _read()
        for key, value in clean.items():
            if value:
                current[key] = value
            else:
                current.pop(key, None)
        if current:
            backend().write(json.dumps(current, sort_keys=True))
        else:
            backend().clear()


def describe(env: Mapping[str, str]) -> dict:
    """The settings page's data. `env` is the environment without overrides
    (the shell plus `.env`). Secret values are never included."""
    try:
        saved, available, name, problem = _read(), True, backend().name, ""
    except Unavailable as e:
        saved, available, name, problem = {}, False, "", str(e)

    def source(key: str) -> str:
        return "keychain" if key in saved else "env" if env.get(key) else "default"

    fields = []
    for f in FIELDS:
        src = source(f.key)
        effective = saved.get(f.key) or env.get(f.key) or f.default
        fields.append({
            "key": f.key, "label": f.label, "group": f.group, "secret": f.secret,
            "kind": f.kind, "hint": f.hint, "source": src, "set": bool(effective),
            # what is typed into the box, and what applies if the box is empty
            "value": "" if f.secret else saved.get(f.key, ""),
            "fallback": "" if f.secret else (env.get(f.key) or f.default),
        })
    names = sorted({k for k in saved if SANDBOX_KEY.match(k)}
                   | {k for k, v in env.items() if v and SANDBOX_KEY.match(k)})
    sandbox = [{"key": k, "name": k[len(SANDBOX_PREFIX):], "source": source(k)} for k in names]
    return {"available": available, "backend": name, "problem": problem,
            "groups": GROUPS, "fields": fields, "sandbox": sandbox}
