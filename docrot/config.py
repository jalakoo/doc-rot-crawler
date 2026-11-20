"""Runtime configuration, loaded from the environment.

Nothing here reaches out to the network. Every optional integration exposes an
`enabled` flag so callers can degrade instead of raising.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from . import settings

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
RUNS = DATA / "runs"

SANDBOX_ENV_PREFIX = "SANDBOX_ENV_"

# Default extraction models, in preference order. OpenRouter ids are
# `vendor/model`. The first is the primary; the rest are fallbacks it tries on
# context-length errors, moderation flags, rate limits, or provider downtime.
DEFAULT_EXTRACTION_MODELS = [
    "anthropic/claude-opus-5",
    "anthropic/claude-sonnet-5",
]


def _models(env: Mapping[str, str], name: str, default: list[str]) -> list[str]:
    """Comma-separated model ids, in preference order.

    `EXTRACTION_MODELS=anthropic/claude-opus-5,openai/gpt-sol-latest` selects a
    primary plus one fallback. Empty or unset falls back to the default list.
    """
    raw = env.get(name) or env.get("EXTRACTION_MODEL", "")
    picked = [m.strip() for m in raw.split(",") if m.strip()]
    return picked or list(default)


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Config:
    # --- sandbox execution ---
    daytona_api_key: str = ""
    daytona_api_url: str = ""
    daytona_target: str = ""

    # --- graph ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "docrotlocal"  # noqa: S105 - docker-compose.yml's local password
    neo4j_database: str = "neo4j"

    # --- extraction llm (OpenRouter) ---
    # `extraction_models` is an ordered preference list. The first entry is the
    # primary; the rest are handed to OpenRouter as fallbacks, which it tries in
    # order on context-length errors, moderation flags, rate limits, or provider
    # downtime. Model ids are `vendor/model`.
    extraction_api_key: str = ""
    extraction_base_url: str = "https://openrouter.ai/api/v1"
    extraction_models: list[str] = field(
        default_factory=lambda: list(DEFAULT_EXTRACTION_MODELS))

    # --- github: raises the rate limit of release lookups; never enters a sandbox ---
    github_token: str = ""

    # --- tuning ---
    max_pages: int = 60
    concurrency: int = 8
    snippet_timeout: int = 45
    install_timeout: int = 180
    probe_timeout: int = 180    # one batched probe exec per version
    clone_timeout: int = 600    # per git step; clones are sparse and blobless
    build_timeout: int = 900    # one `dotnet build` of every C# snippet per version
    dotnet_image: str = ""      # sandbox image for NuGet packages; "" is the .NET SDK
    node_image: str = ""        # sandbox image for npm packages; "" is Daytona's default snapshot
    user_agent: str = "docrot/0.1 (+https://github.com/your-org/docrot)"

    # --- credentials forwarded into sandboxes (prefix stripped) ---
    sandbox_env: dict[str, str] = field(default_factory=dict)

    @property
    def can_execute(self) -> bool:
        return bool(self.daytona_api_key)

    @property
    def can_llm(self) -> bool:
        return bool(self.extraction_api_key and self.extraction_base_url
                    and self.extraction_models)

    @property
    def extraction_model(self) -> str:
        """The primary model. Kept as a property so report/log lines that name
        a single model keep working."""
        return self.extraction_models[0] if self.extraction_models else ""

    def secrets(self) -> list[str]:
        """Values that must never appear in captured output or on disk."""
        vals = list(self.sandbox_env.values())
        for v in (self.daytona_api_key, self.extraction_api_key,
                  self.neo4j_password, self.github_token):
            if v and len(v) > 6:
                vals.append(v)
        return vals


def base_env(env_file: str | Path | None = None) -> dict[str, str]:
    """The shell environment with `.env` laid over it - everything except what
    the dashboard's settings page saved."""
    load_dotenv(env_file or ROOT.parent / ".env", override=False)
    load_dotenv(ROOT / ".env", override=True)
    return dict(os.environ)


def load(env_file: str | Path | None = None) -> Config:
    # Settings saved from the dashboard live in the OS keychain and win over
    # `.env`. They are read on every load, so the next scan picks them up.
    env = base_env(env_file)
    env.update(settings.overrides())

    sandbox_env = {
        k[len(SANDBOX_ENV_PREFIX):]: v
        for k, v in env.items()
        if k.startswith(SANDBOX_ENV_PREFIX) and v
    }

    cfg = Config(
        daytona_api_key=env.get("DAYTONA_API_KEY", ""),
        daytona_api_url=env.get("DAYTONA_API_URL", ""),
        daytona_target=env.get("DAYTONA_TARGET", ""),
        neo4j_uri=env.get("NEO4J_URI", "bolt://localhost:7687"),
        neo4j_user=env.get("NEO4J_USERNAME", "neo4j"),
        neo4j_password=env.get("NEO4J_PASSWORD", "docrotlocal"),
        neo4j_database=env.get("NEO4J_DATABASE", "neo4j"),
        github_token=(env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip(),
        extraction_api_key=env.get("EXTRACTION_API_KEY", ""),
        extraction_base_url=env.get(
            "EXTRACTION_BASE_URL", "https://openrouter.ai/api/v1"),
        extraction_models=_models(env, "EXTRACTION_MODELS", DEFAULT_EXTRACTION_MODELS),
        max_pages=_int(env, "DOCROT_MAX_PAGES", 60),
        concurrency=_int(env, "DOCROT_CONCURRENCY", 8),
        snippet_timeout=_int(env, "DOCROT_SNIPPET_TIMEOUT", 45),
        install_timeout=_int(env, "DOCROT_INSTALL_TIMEOUT", 180),
        probe_timeout=_int(env, "DOCROT_PROBE_TIMEOUT", 180),
        clone_timeout=_int(env, "DOCROT_CLONE_TIMEOUT", 600),
        build_timeout=_int(env, "DOCROT_BUILD_TIMEOUT", 900),
        dotnet_image=env.get("DOCROT_DOTNET_IMAGE", ""),
        node_image=env.get("DOCROT_NODE_IMAGE", ""),
        user_agent=env.get("DOCROT_USER_AGENT", Config.user_agent),
        sandbox_env=sandbox_env,
    )
    CACHE.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    return cfg


def redact(text: str, secrets: list[str]) -> str:
    """Strip credential values out of captured output."""
    if not text:
        return text
    for s in secrets:
        if s:
            text = text.replace(s, "***REDACTED***")
    return text
