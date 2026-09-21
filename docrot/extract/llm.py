"""Optional LLM stage, routed through OpenRouter.

The model is asked for ONE thing: factual prose claims about named API symbols.
It does not assign tiers - spike S3 measured that against the rule baseline and
the rules won outright (see `apply` below). Fences, code symbols and tiers all
come from parsers and rules, so the pipeline runs end to end with no key.

Two OpenRouter features carry weight here:

  `models`   an ordered preference list sent as a fallback chain. OpenRouter
             moves to the next entry on context-length errors, moderation
             flags, rate limits, and provider downtime, and bills whichever
             model actually answered.

  `provider: {require_parameters: true}`
             routes only to endpoints that implement every parameter in the
             request - including `response_format: json_schema`. Without it a
             request can land on a provider that treats the schema as a
             suggestion, which for a stage whose entire contract is schema
             validity is the one failure worth designing out.
"""
from __future__ import annotations

import json

from tenacity import retry, stop_after_attempt, wait_fixed

from ..config import Config
from ..models import Claim, LLMJudgement, Snippet

SYSTEM_PROMPT = (
    "You extract factual claims from software documentation prose. "
    "A claim is a sentence asserting something checkable about a named API "
    "symbol - what it accepts, returns, raises, or requires. "
    "You never invent code, symbols or line numbers; only name symbols that "
    "appear in the text you are given. "
    "Ignore code blocks: they are verified separately. "
    "Return an empty `snippets` array - snippet classification is not your "
    "job. Return JSON matching the supplied schema exactly."
)


def judge(snippets: list[Snippet], pages_text: str, cfg: Config, log=print
          ) -> LLMJudgement | None:
    if not cfg.can_llm:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        log("  openai sdk missing - lexical claims only")
        return None

    client = OpenAI(api_key=cfg.extraction_api_key, base_url=cfg.extraction_base_url)
    primary, *fallbacks = cfg.extraction_models
    # Claims need prose, not code - S3 measured 54 claims from the corpus
    # payload against 3 from a fences-only payload. Snippet bodies are dropped:
    # they were only ever there for the tier judgement we no longer ask for.
    payload = {"docs_excerpt": pages_text[:120_000]}

    # OpenRouter-only parameters ride in extra_body; the OpenAI SDK has no
    # named argument for either.
    extra_body: dict = {"provider": {"require_parameters": True}}
    if fallbacks:
        extra_body["models"] = [primary, *fallbacks]

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(2), reraise=False)
    def _call():
        resp = client.chat.completions.create(
            model=primary,
            messages=[
                # Byte-identical between runs, but do NOT expect a cache read:
                # S3 measured cached_tokens=0 on two identical requests, and
                # the repeat cost more. This prompt is ~200 tokens, far below
                # any provider's minimum cacheable prefix, and nothing here
                # sets a cache breakpoint.
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "judgement",
                                "strict": True,
                                "schema": LLMJudgement.model_json_schema()},
            },
            extra_headers={"X-OpenRouter-Title": "docrot"},
            extra_body=extra_body,
        )
        # whichever model answered - may be a fallback, and it is what was billed
        return resp.model, LLMJudgement.model_validate_json(
            resp.choices[0].message.content or "")

    try:
        served_by, out = _call()
        if served_by and served_by != primary:
            log(f"  llm: {primary} unavailable, served by {served_by}")
        log(f"  llm extracted {len(out.claims)} claims via {served_by or primary}")
        return out
    except Exception as e:
        # tenacity wraps the real failure in a RetryError, which on its own
        # says nothing about what went wrong - unwrap it before logging.
        cause = getattr(e, "last_attempt", None)
        if cause is not None and cause.failed:
            e = cause.exception()
        log(f"  llm stage failed ({type(e).__name__}: {str(e)[:160]}) "
            f"- lexical claims stand")
        return None


def apply(judgement: LLMJudgement | None, snippets: list[Snippet]) -> list[Claim]:
    """Claims only. The model does NOT assign tiers.

    Spike S3 measured its tier judgement against the frozen rule baseline:
    64.9% agreement (claude-sonnet-5), 36.6% (claude-opus-5). The disagreements
    are destructive in both directions - it promoted 41 shell snippets like
    `cp template.env .env` out of `unverifiable` (17 of 17 then failed in a
    clean sandbox, and the other 24 had no symbols to probe, so they would have
    become vacuous passes), and demoted real findings to `unverifiable`.
    Applied to the 15 snippets that produced genuine rot findings, the rules
    keep 15; Sonnet keeps 14; Opus keeps 13.

    So `tier` and `requires` stay with the deterministic rules, and the model
    contributes only prose claims - the one output whose deterministic
    replacement (lexical discovery in `claims.py`) is shallow by design.
    """
    if not judgement:
        return []
    return judgement.claims
