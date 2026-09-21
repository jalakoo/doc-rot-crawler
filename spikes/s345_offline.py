"""S3 / S4 / S5 - the three offline spikes.

S3  LLM extraction     - is the corpus the size the spec assumes, and does the
                         model earn a place in the critical path at all?
S4  claim -> symbol    - does lexical matching reach useful precision/recall,
                         or is a hosted embedding endpoint actually required?
S5  `execute` tier     - what fraction of snippets could ever reach the tier
                         the spec calls "the strongest evidence"?

S5 needs a second corpus to be worth anything: Perseus is credential-heavy by
nature, so a census over it alone would prove only that Perseus is
credential-heavy. Streamlit is named in 2026_09_08_repos.md as the
zero-credential target, and publishes llms.txt, so it is the control.

Usage:  python spikes/s345_offline.py
"""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RUN = ROOT / "data" / "runs" / "latest"
REPO = ROOT / "data" / "cache" / "repo-perseus-client"
OUT = ROOT / "spikes" / "out"

from docrot.extract import fences, tiers            # noqa: E402
from docrot.models import Page                      # noqa: E402


# ===========================================================================
# S3 - corpus sizing and the rule baseline
# ===========================================================================
def s3_llm_extraction() -> dict:
    extraction = json.loads((RUN / "extraction.json").read_text())
    pages = extraction["pages"]
    snippets = extraction["snippets"]

    corpus = "\n\n".join(f"# {p.get('title') or p['path']}\n{p.get('text','')}"
                         for p in pages)
    chars = len(corpus)
    # ~3.6 chars/token is a reasonable estimate for English + code; the exact
    # tokenizer varies by provider and does not change the conclusion here.
    est_tokens = round(chars / 3.6)

    from docrot.models import Extraction
    schema = json.dumps(Extraction.model_json_schema())

    # What actually has to reach the model: the spec says fences come from a
    # parser and code symbols from `ast`, so the model only needs the fence
    # bodies plus surrounding prose for judgement - not the whole corpus.
    fences_only = "\n\n".join(
        f'[{s["id"]}] ({s["lang"]})\n{s["code"]}' for s in snippets)
    fences_tokens = round(len(fences_only) / 3.6)

    baseline = {s["id"]: s["tier"] for s in snippets}
    (OUT / "s3_rule_baseline.json").write_text(json.dumps(baseline, indent=2))

    return {
        "pages": len(pages),
        "snippets": len(snippets),
        "corpus_chars": chars,
        "corpus_tokens_est": est_tokens,
        "fences_only_tokens_est": fences_tokens,
        "fences_pct_of_corpus": round(fences_tokens / max(est_tokens, 1), 3),
        "schema_bytes": len(schema),
        "pct_of_200k_window": round(est_tokens / 200_000, 3),
        "pct_of_1m_window": round(est_tokens / 1_000_000, 3),
        "rule_baseline_tiers": dict(collections.Counter(baseline.values())),
        "llm_call_status": "BLOCKED - no EXTRACTION_API_KEY in environment",
        "baseline_written": str(OUT / "s3_rule_baseline.json"),
    }


# ===========================================================================
# S4 - claim -> symbol matching without embeddings
# ===========================================================================
IDENT = re.compile(r"`([A-Za-z_][\w.]*)`|\b([a-z_]+_[a-z_]+)\b|\b([A-Z][a-z]+[A-Z]\w*)\b")
SENT = re.compile(r"(?<=[.!?])\s+")


def head_symbol_names() -> set[str]:
    import ast
    names: set[str] = set()
    for py in (REPO / "perseus_client").rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    names.add(node.name)
    return names


def extract_claims(pages: list[dict]) -> list[dict]:
    """Prose sentences that mention something identifier-shaped."""
    claims = []
    for p in pages:
        text = p.get("text", "")
        # drop fenced code so the claims are genuinely prose
        prose = re.sub(r"```.*?```", " ", text, flags=re.S)
        for i, sent in enumerate(SENT.split(prose)):
            sent = " ".join(sent.split())
            if not (30 <= len(sent) <= 400):
                continue
            hits = {g for m in IDENT.finditer(sent) for g in m.groups() if g}
            if hits:
                claims.append({"id": f"{p['path'][-30:]}-c{i}", "page": p["url"],
                               "text": sent, "candidates": sorted(hits)})
    return claims


def lexical_match(claim: dict, symbols: set[str]) -> list[str]:
    """The spec's stated fallback: identifier-shaped tokens matched against the
    symbol table. No embeddings, no service."""
    out = []
    for cand in claim["candidates"]:
        tail = cand.split(".")[-1]
        if tail in symbols:
            out.append(tail)
    return sorted(set(out))


def s4_claims() -> dict:
    extraction = json.loads((RUN / "extraction.json").read_text())
    pages = extraction["pages"]
    symbols = head_symbol_names()

    claims = extract_claims(pages)
    matched = [c for c in claims if lexical_match(c, symbols)]

    # Ground truth for precision: a match is correct when the matched symbol
    # name actually appears in the claim text as a word. That is checkable
    # mechanically, so the sample does not depend on my judgement.
    tp = fp = 0
    for c in matched:
        for sym in lexical_match(c, symbols):
            if re.search(rf"\b{re.escape(sym)}\b", c["text"]):
                tp += 1
            else:
                fp += 1

    # Recall proxy: claims that name a real HEAD symbol but got no match.
    missed = []
    for c in claims:
        if lexical_match(c, symbols):
            continue
        named = [s for s in symbols
                 if len(s) > 4 and re.search(rf"\b{re.escape(s)}\b", c["text"])]
        if named:
            missed.append({"text": c["text"][:120], "should_match": named[:3]})

    total_should = len(matched) + len(missed)
    sample = [{"text": c["text"][:140], "matched": lexical_match(c, symbols)}
              for c in matched[:30]]
    (OUT / "s4_claim_sample.json").write_text(json.dumps(sample, indent=2))

    return {
        "head_symbols": len(symbols),
        "claims_extracted": len(claims),
        "claims_matched": len(matched),
        "match_rate": round(len(matched) / max(len(claims), 1), 3),
        "links_tp": tp, "links_fp": fp,
        "precision": round(tp / max(tp + fp, 1), 3),
        "claims_missed_but_should_match": len(missed),
        "recall": round(len(matched) / max(total_should, 1), 3),
        "missed_examples": missed[:5],
        "sample_written": str(OUT / "s4_claim_sample.json"),
    }


# ===========================================================================
# S5 - execute-tier census across two corpora
# ===========================================================================
def fetch_streamlit(max_pages: int = 60) -> list[Page]:
    """Streamlit publishes llms.txt - the top rung of the acquisition ladder."""
    pages: list[Page] = []
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": "docrot-spike/0.1 (+doc rot detector)"}) as c:
        for url in ("https://docs.streamlit.io/llms-full.txt",
                    "https://docs.streamlit.io/llms.txt"):
            try:
                r = c.get(url)
            except Exception as e:
                print(f"  {url}: {type(e).__name__}")
                continue
            if r.status_code != 200 or len(r.text) < 2000:
                print(f"  {url}: HTTP {r.status_code}, {len(r.text)}b")
                continue
            print(f"  {url}: HTTP 200, {len(r.text)}b")
            # llms-full.txt inlines every page; split on top-level headings
            parts = re.split(r"\n(?=#\s+\S)", r.text)
            for i, part in enumerate(parts[:max_pages]):
                title = part.splitlines()[0].lstrip("# ").strip() if part.strip() else ""
                pages.append(Page(url=f"{url}#{i}", path=f"streamlit-{i}",
                                  title=title, text=part))
            break
    return pages


def census(snippets, import_name: str, label: str) -> dict:
    """How many snippets could ever reach `execute`?

    Three credential postures, because the answer depends entirely on what you
    hold: none, the package's own key, and every key the snippets name.
    """
    out = {"corpus": label, "snippets": len(snippets)}

    for posture, creds in (("no_creds", set()),
                           ("all_named_creds",
                            {c for s in snippets
                             for c in tiers.CREDS.findall(s.code)})):
        for s in snippets:
            s.tier = "structural"
        tiers.assign(snippets, creds, import_name)
        counts = collections.Counter(s.tier for s in snippets)
        out[posture] = {
            "counts": dict(counts),
            "execute_pct": round(counts["execute"] / max(len(snippets), 1), 3),
            "creds_held": len(creds),
        }

    out["pure_install_snippets"] = sum(
        1 for s in snippets if s.lang in ("bash", "sh", "shell", "console")
        and tiers._pure_install(s.code))
    out["langs"] = dict(collections.Counter(s.lang for s in snippets))
    return out


def s5_execute_census() -> dict:
    extraction = json.loads((RUN / "extraction.json").read_text())
    from docrot.models import Snippet
    perseus = [Snippet(**s) for s in extraction["snippets"]]
    res = {"perseus": census(perseus, "perseus_client", "perseus (cached)")}

    print("S5: acquiring control corpus (Streamlit, zero credentials)...")
    sl_pages = fetch_streamlit()
    if sl_pages:
        sl = fences.parse(sl_pages)
        res["streamlit"] = census(sl, "streamlit", "streamlit (llms.txt)")
    else:
        res["streamlit"] = {"error": "acquisition failed"}
    return res


# ===========================================================================
def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {}

    print("=" * 70); print("S3 - LLM extraction"); print("=" * 70)
    report["s3"] = s3_llm_extraction()
    print(json.dumps(report["s3"], indent=2))

    print("\n" + "=" * 70); print("S4 - claim -> symbol matching"); print("=" * 70)
    report["s4"] = s4_claims()
    print(json.dumps(report["s4"], indent=2))

    print("\n" + "=" * 70); print("S5 - execute-tier census"); print("=" * 70)
    report["s5"] = s5_execute_census()
    print(json.dumps(report["s5"], indent=2))

    (OUT / "s345_offline.json").write_text(json.dumps(report, indent=2))
    print(f"\nwrote {OUT / 's345_offline.json'}")


if __name__ == "__main__":
    main()
