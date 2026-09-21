"""S4/S5 refinements - two numbers from the first pass were measuring the
wrong thing, and a spike that reports a confounded number is worse than no
spike.

S5 confound: `tiers._tier` short-circuits every snippet that touches the
target package to `structural` whenever no credentials are held. So the
"execute %" of the first pass measured that rule, not the corpus. The real
question is the *ceiling*: how many snippets could execute successfully if we
did hold whatever they need.

S4 confound: the recall proxy counted a claim as "should have matched"
whenever any HEAD symbol name appeared in it as a word - including symbols
whose names are ordinary English ("graph", "close", "settings"). Those matches
would be noise, not recall. Ground truth is tightened to identifier-shaped
names only.

Usage:  python spikes/s345b_refine.py
"""
from __future__ import annotations

import ast
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RUN = ROOT / "data" / "runs" / "latest"
REPO = ROOT / "data" / "cache" / "repo-perseus-client"
OUT = ROOT / "spikes" / "out"

from docrot.extract import completeness, fences, tiers   # noqa: E402
from docrot.models import Page, Snippet                  # noqa: E402
from spikes.s345_offline import (extract_claims, fetch_streamlit,   # noqa: E402
                                 head_symbol_names, lexical_match)


# ===========================================================================
# S5 - the true execute ceiling
# ===========================================================================
CRED_HINT = re.compile(
    r"(api[_-]?key|token|secret|password|credential|\bauth\b|os\.environ|getenv)", re.I)
PLACEHOLDER = tiers.PLACEHOLDER


def execute_ceiling(snippets: list[Snippet], import_name: str) -> dict:
    """A snippet can execute if it is a complete program, names no credential,
    carries no placeholder value, and reaches the network only through the
    install. This is the ceiling - the tier can never exceed it."""
    buckets = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)

    for s in snippets:
        if s.lang in ("bash", "sh", "shell", "console"):
            bucket = "install-only" if tiers._pure_install(s.code) else "shell-other"
        elif s.lang not in ("python", "py", "python3"):
            bucket = "not-code"
        else:
            kind, _ = completeness.classify(s.code)
            if kind != "program":
                bucket = f"not-runnable:{kind}"
            elif CRED_HINT.search(s.code):
                bucket = "needs-credential"
            elif PLACEHOLDER.search(s.code):
                bucket = "placeholder-value"
            else:
                bucket = "CEILING-executable"
        buckets[bucket] += 1
        if len(examples[bucket]) < 2:
            examples[bucket].append(s.code[:110].replace("\n", " ⏎ "))

    n = max(len(snippets), 1)
    return {
        "snippets": len(snippets),
        "buckets": dict(buckets.most_common()),
        "ceiling_count": buckets["CEILING-executable"],
        "ceiling_pct": round(buckets["CEILING-executable"] / n, 3),
        "structural_eligible_pct": round(
            (n - buckets["not-code"] - buckets["shell-other"]) / n, 3),
        "examples": {k: v for k, v in examples.items()},
    }


# ===========================================================================
# S4 - recall against identifier-shaped ground truth only
# ===========================================================================
IDENTIFIER_SHAPED = re.compile(r"(_|[a-z][A-Z])")   # snake_case or CamelCase


def s4_refined() -> dict:
    extraction = json.loads((RUN / "extraction.json").read_text())
    pages = extraction["pages"]
    all_syms = head_symbol_names()
    # only names no reader would mistake for an English word
    strict = {s for s in all_syms if IDENTIFIER_SHAPED.search(s) and len(s) > 5}

    claims = extract_claims(pages)
    matched, missed = [], []
    for c in claims:
        hits = lexical_match(c, all_syms)
        should = [s for s in strict
                  if re.search(rf"\b{re.escape(s)}\b", c["text"])]
        if hits:
            matched.append(c)
        elif should:
            missed.append({"text": c["text"][:110], "should_match": should[:3]})

    tp = fp = 0
    for c in matched:
        for sym in lexical_match(c, all_syms):
            if re.search(rf"\b{re.escape(sym)}\b", c["text"]):
                tp += 1
            else:
                fp += 1

    recall = len(matched) / max(len(matched) + len(missed), 1)
    return {
        "head_symbols_all": len(all_syms),
        "head_symbols_identifier_shaped": len(strict),
        "claims_extracted": len(claims),
        "claims_matched": len(matched),
        "claims_missed_strict": len(missed),
        "precision": round(tp / max(tp + fp, 1), 3),
        "recall_strict": round(recall, 3),
        "missed_examples": missed[:6],
    }


# ===========================================================================
def main() -> None:
    report = {}

    print("=" * 70); print("S4 refined - identifier-shaped ground truth"); print("=" * 70)
    report["s4_refined"] = s4_refined()
    print(json.dumps(report["s4_refined"], indent=2))

    print("\n" + "=" * 70); print("S5 refined - true execute ceiling"); print("=" * 70)
    extraction = json.loads((RUN / "extraction.json").read_text())
    perseus = [Snippet(**s) for s in extraction["snippets"]]
    report["perseus_ceiling"] = execute_ceiling(perseus, "perseus_client")
    print("perseus:", json.dumps(report["perseus_ceiling"], indent=2))

    print("\nacquiring Streamlit control corpus...")
    sl_pages = fetch_streamlit()
    if sl_pages:
        sl = fences.parse(sl_pages)
        report["streamlit_ceiling"] = execute_ceiling(sl, "streamlit")
        print("streamlit:", json.dumps(report["streamlit_ceiling"], indent=2))
    else:
        report["streamlit_ceiling"] = {"error": "acquisition failed"}

    (OUT / "s345b_refine.json").write_text(json.dumps(report, indent=2))
    print(f"\nwrote {OUT / 's345b_refine.json'}")


if __name__ == "__main__":
    main()
