"""S3b - the LLM agreement test, unblocked.

S3 froze a rule-based tier assignment for all 131 snippets and parked the
comparison for want of a key. This runs it.

Three questions, three calls:

  A  corpus payload   the shape llm.py sends today: snippets + 120K chars of
                      docs excerpt (~30K tokens)
  B  fences payload   the shape the v2 plan recommends: snippets only, no
                      excerpt (~7.4K tokens). Does dropping 75% of the input
                      change the judgement?
  B' repeat of B      identical bytes, to test whether the byte-identical
                      system prompt actually earns a cache read - §5.1 of the
                      spec asserts it does and nobody has checked

Decision criteria, set before the run in 2026_09_12_spikes.md:

  agreement > 90%  -> cut the LLM from the critical path, keep the rules
  agreement < 90%  -> keep it only if the disagreements adjudicate in the
                      model's favour

Usage:  python spikes/s3b_llm_agreement.py
"""
from __future__ import annotations

import collections
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RUN = ROOT / "data" / "runs" / "latest"
OUT = ROOT / "spikes" / "out"
BASELINE = OUT / "s3_rule_baseline.json"

from docrot import config as cfgmod          # noqa: E402
from docrot.extract.llm import SYSTEM_PROMPT  # noqa: E402
from docrot.models import LLMJudgement        # noqa: E402


def build_payload(snippets: list[dict], pages_text: str, variant: str) -> dict:
    """`corpus` is what the code sends today; `fences` is the plan's proposal."""
    body = {
        "snippets": [
            {"id": s["id"], "page": s["page"], "lang": s["lang"],
             "code": s["code"][:1200]}
            for s in snippets
        ],
    }
    if variant == "corpus":
        body["docs_excerpt"] = pages_text[:120_000]
    return body


def call(cfg, payload: dict, label: str) -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=cfg.extraction_api_key,
                    base_url=cfg.extraction_base_url)
    primary, *fallbacks = cfg.extraction_models
    extra_body: dict = {"provider": {"require_parameters": True}}
    if fallbacks:
        extra_body["models"] = [primary, *fallbacks]

    rec: dict = {"label": label, "primary": primary, "error": None,
                 "validated_first_try": None}
    t0 = time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=primary,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "judgement", "strict": True,
                                "schema": LLMJudgement.model_json_schema()},
            },
            extra_headers={"X-OpenRouter-Title": "docrot"},
            extra_body=extra_body,
        )
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        rec["latency"] = round(time.perf_counter() - t0, 1)
        return rec

    rec["latency"] = round(time.perf_counter() - t0, 1)
    rec["served_by"] = resp.model
    rec["fell_back"] = resp.model != primary

    u = getattr(resp, "usage", None)
    if u is not None:
        det = getattr(u, "prompt_tokens_details", None)
        rec["usage"] = {
            "prompt_tokens": getattr(u, "prompt_tokens", None),
            "completion_tokens": getattr(u, "completion_tokens", None),
            "cost": getattr(u, "cost", None),
            "cached_tokens": getattr(det, "cached_tokens", None) if det else None,
            "cache_write_tokens": (getattr(det, "cache_write_tokens", None)
                                   if det else None),
        }

    raw = resp.choices[0].message.content
    rec["raw_chars"] = len(raw or "")
    try:
        judged = LLMJudgement.model_validate_json(raw)
        rec["validated_first_try"] = True
        rec["tiers"] = {j.id: j.tier for j in judged.snippets}
        rec["requires"] = {j.id: j.requires for j in judged.snippets
                           if j.requires}
        rec["claims"] = len(judged.claims)
        rec["claim_sample"] = [
            {"text": c.text[:120], "symbols": c.symbols}
            for c in judged.claims[:5]
        ]
    except Exception as e:
        rec["validated_first_try"] = False
        rec["validation_error"] = f"{type(e).__name__}: {str(e)[:300]}"
        rec["raw_head"] = (raw or "")[:400]
    return rec


def compare(tiers: dict[str, str], baseline: dict[str, str]) -> dict:
    """Agreement against the frozen rule assignment."""
    shared = [k for k in baseline if k in tiers]
    agree = [k for k in shared if tiers[k] == baseline[k]]
    confusion = collections.Counter(
        (baseline[k], tiers[k]) for k in shared if tiers[k] != baseline[k])
    return {
        "baseline_snippets": len(baseline),
        "model_snippets": len(tiers),
        "missing_from_model": sorted(set(baseline) - set(tiers))[:10],
        "hallucinated_ids": sorted(set(tiers) - set(baseline))[:10],
        "compared": len(shared),
        "agree": len(agree),
        "agreement": round(len(agree) / max(len(shared), 1), 3),
        "model_tier_counts": dict(collections.Counter(tiers.values())),
        "disagreements": {f"rule={a} -> model={b}": n
                          for (a, b), n in confusion.most_common()},
    }


def main() -> None:
    cfg = cfgmod.load()
    if not cfg.can_llm:
        print("no extraction key configured - nothing to run")
        return

    extraction = json.loads((RUN / "extraction.json").read_text())
    snippets = extraction["snippets"]
    pages_text = "\n\n".join(
        f"# {p.get('title') or p['path']}\n{p.get('text','')}"
        for p in extraction["pages"])
    baseline = json.loads(BASELINE.read_text())

    print(f"corpus: {len(extraction['pages'])} pages, {len(snippets)} snippets")
    print(f"models: {cfg.extraction_models}\n")

    report: dict = {"models": cfg.extraction_models, "runs": []}

    for label, variant in (("A-corpus", "corpus"),
                           ("B-fences", "fences"),
                           ("B'-fences-repeat", "fences")):
        payload = build_payload(snippets, pages_text, variant)
        print(f"{label}: payload {len(json.dumps(payload)):,} chars ...",
              flush=True)
        rec = call(cfg, payload, label)
        if rec.get("error"):
            print(f"   ERROR {rec['error']}", flush=True)
        else:
            u = rec.get("usage", {})
            print(f"   {rec['latency']}s · served_by {rec['served_by']} · "
                  f"in {u.get('prompt_tokens')} out {u.get('completion_tokens')} · "
                  f"cached {u.get('cached_tokens')} · ${u.get('cost')}", flush=True)
            if rec.get("tiers"):
                rec["comparison"] = compare(rec["tiers"], baseline)
                c = rec["comparison"]
                print(f"   agreement {c['agreement']:.1%} "
                      f"({c['agree']}/{c['compared']}) · claims {rec['claims']}",
                      flush=True)
            else:
                print(f"   SCHEMA INVALID: {rec.get('validation_error')}",
                      flush=True)
        report["runs"].append(rec)

    # --- disagreement detail, for adjudication ----------------------------
    best = next((r for r in report["runs"]
                 if r.get("tiers") and r["label"] == "B-fences"), None)
    if best:
        by_id = {s["id"]: s for s in snippets}
        detail = []
        for sid, rule_tier in baseline.items():
            model_tier = best["tiers"].get(sid)
            if model_tier and model_tier != rule_tier:
                s = by_id.get(sid, {})
                detail.append({
                    "id": sid, "rule": rule_tier, "model": model_tier,
                    "kind": s.get("kind"), "lang": s.get("lang"),
                    "code": (s.get("code") or "")[:200],
                })
        report["disagreement_detail"] = detail
        (OUT / "s3b_disagreements.json").write_text(json.dumps(detail, indent=2))
        print(f"\n{len(detail)} disagreements written to "
              f"{OUT / 's3b_disagreements.json'}")

    # --- caching check ----------------------------------------------------
    runs = {r["label"]: r for r in report["runs"]}
    first, repeat = runs.get("B-fences"), runs.get("B'-fences-repeat")
    if first and repeat and first.get("usage") and repeat.get("usage"):
        report["caching"] = {
            "first_cached_tokens": first["usage"].get("cached_tokens"),
            "repeat_cached_tokens": repeat["usage"].get("cached_tokens"),
            "first_cost": first["usage"].get("cost"),
            "repeat_cost": repeat["usage"].get("cost"),
        }

    total = sum((r.get("usage") or {}).get("cost") or 0 for r in report["runs"])
    report["total_cost"] = round(total, 4)

    (OUT / "s3b_llm_agreement.json").write_text(json.dumps(report, indent=2))
    print(f"\ntotal spend this spike: ${total:.4f}")
    print(f"wrote {OUT / 's3b_llm_agreement.json'}")


if __name__ == "__main__":
    main()
