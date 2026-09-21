"""Neo4j load and queries.

Works against the local docker container or an Aura instance - the only
difference is the URI scheme (bolt:// vs neo4j+s://), which comes from the
environment. Every method degrades to a no-op / empty result when the driver
cannot connect, so a dead graph costs you the traversal queries and nothing
else.
"""
from __future__ import annotations

import json

from ..config import Config
from ..models import Extraction, Result, Target

INDEXES = [
    "CREATE INDEX symbol_name   IF NOT EXISTS FOR (s:Symbol)  ON (s.name)",
    "CREATE INDEX page_url      IF NOT EXISTS FOR (p:Page)    ON (p.url)",
    "CREATE INDEX snippet_id    IF NOT EXISTS FOR (s:Snippet) ON (s.id)",
    "CREATE INDEX version_label IF NOT EXISTS FOR (v:Version) ON (v.label)",
]

def canon(name: str) -> str:
    """One naming convention for Symbol nodes: the bare identifier.

    Three conventions had grown up side by side - EXERCISES stored the dotted
    path from `ast` (`perseus_client.close_async`), while DEFINED_AT_HEAD and
    ABOUT stored bare names (`close_async`). Pages therefore linked to a dotted
    node carrying no existence edges, while the bare node held the answer, and
    §9.5 reported every documented symbol as drift with the polarity inverted.

    Bare names are also what §9.4 takes as input (`docrot blast close_async`).
    """
    tail = (name or "").split("declared:")[-1].split(".")[-1].strip()
    # Install and env tokens ride in the same list (`pip:perseus-client[all]`,
    # `requirements.txt`, a pinned `1.0.0-rc.19`). Their tails are not symbols,
    # and they surfaced in the drift report as `txt` and `19`.
    return tail if tail.isidentifier() else ""


def _canon_syms(row: dict, import_name: str) -> dict:
    """Canonicalise a row's symbol list, dropping the bare package name -
    every snippet imports it, so it links everything to everything."""
    seen, out = set(), []
    for s in row.get("symbols") or []:
        # `pip:`/`env:` entries are install and environment tokens, not code
        # symbols - probe.py filters them the same way. A dotted path whose
        # segments are not all identifiers is a filename or a pinned version
        # (`requirements.txt` arrived as the symbol `txt`).
        if s.startswith(("pip:", "env:")):
            continue
        if not all(part.isidentifier() for part in s.split(".") if part):
            continue
        c = canon(s)
        if not c or c == import_name or c in seen:
            continue
        seen.add(c)
        out.append(c)
    row["symbols"] = out
    return row



def _import_of(package: str, targets: list[Target]) -> str:
    return next((t.import_name for t in targets if t.package == package),
                targets[0].import_name)


class GraphStore:
    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self.log = log
        self.driver = None
        self.ok = False
        try:
            from neo4j import GraphDatabase
            self.driver = GraphDatabase.driver(
                cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password))
            self.driver.verify_connectivity()
            self.ok = True
        except Exception as e:
            log(f"  graph unavailable ({type(e).__name__}) - "
                f"report will render from results.json")

    def _run(self, cypher: str, **params):
        if not self.ok or self.driver is None:
            return []
        try:
            recs, _, _ = self.driver.execute_query(
                cypher, database_=self.cfg.neo4j_database, **params)
            return [r.data() for r in recs]
        except Exception as e:
            self.log(f"  query failed: {type(e).__name__}")
            return []

    def close(self):
        if self.driver:
            self.driver.close()

    # ------------------------------------------------------------------ load

    def load(self, ex: Extraction, results: list[Result], targets: list[Target],
             head_symbols: dict[str, set[str]]):
        """Replace the graph with this run. `head_symbols` is keyed by package.

        Version nodes are keyed on (package, label): two packages in one scan
        can both publish `1.0.0`, and merging on the label alone would give one
        package's verdicts to the other.
        """
        if not self.ok or not targets:
            return
        primary = targets[0].package
        for stmt in INDEXES:          # indexes before ingest, or traversals scan
            self._run(stmt)

        self._run("MATCH (n) DETACH DELETE n")

        for t in targets:
            self._run("""
            MERGE (pkg:Package {name: $pkg, ecosystem: $eco})
            WITH pkg UNWIND $versions AS v
              MERGE (ver:Version {package: $pkg, label: v.label})
                SET ver.released_at = v.released_at
              MERGE (pkg)-[:HAS_VERSION]->(ver)
            """, pkg=t.package, eco=t.ecosystem,
                 versions=[r.model_dump() for r in t.releases])

        commits = {t.repo_url: t.commit for t in targets if t.repo_url}
        self._run("""
        UNWIND $pages AS row
          MERGE (src:Source {kind: row.source, url: coalesce(row.origin, '')})
            SET src.commit_sha = $commits[coalesce(row.origin, '')]
          MERGE (p:Page {url: row.url})
            SET p.path = row.path, p.title = row.title
          MERGE (src)-[:HAS_PAGE]->(p)
        """, pages=[p.model_dump(exclude={"text"}) for p in ex.pages], commits=commits)

        self._run("""
        UNWIND $snips AS row
          MERGE (p:Page {url: row.page})
          MERGE (s:Snippet {id: row.id})
            SET s.tier = row.tier, s.lang = row.lang, s.code = row.code
          MERGE (p)-[:CONTAINS]->(s)
        WITH s, row
        UNWIND row.symbols AS sym
          MERGE (y:Symbol {name: sym})
          MERGE (s)-[:EXERCISES]->(y)
        """, snips=[_canon_syms(s.model_dump(), _import_of(s.package, targets))
                    for s in ex.snippets])

        # second pass: both endpoints must exist before the edge
        self._run("""
        UNWIND $edges AS e
          MATCH (a:Snippet {id: e.src}), (b:Snippet {id: e.dst})
          MERGE (a)-[:REQUIRES]->(b)
        """, edges=[{"src": s.id, "dst": r} for s in ex.snippets for r in s.requires])

        self._run("""
        UNWIND $claims AS row
          MERGE (p:Page {url: row.page})
          MERGE (c:Claim {id: row.id}) SET c.text = row.text
          MERGE (p)-[:STATES]->(c)
        WITH c, row
        UNWIND row.symbols AS sym
          MERGE (y:Symbol {name: sym})
          MERGE (c)-[:ABOUT]->(y)
        """, claims=[_canon_syms(c.model_dump(), ex.import_name)
                     for c in ex.claims])

        self._run("""
        UNWIND $rows AS row
          MATCH (s:Snippet {id: row.id})
          MERGE (v:Version {package: row.package, label: row.version})
          MERGE (s)-[r:VERIFIED_ON]->(v)
            SET r.status = row.status, r.stderr = row.stderr,
                r.blocked_by = row.blocked_by
        """, rows=[dict(r.model_dump(), package=r.package or primary)
                   for r in results])

        for t in targets:
            self._run("""
            MERGE (src:Source {kind: 'repo', url: $url})
            WITH src UNWIND $syms AS sym
              MERGE (y:Symbol {name: sym})
              MERGE (y)-[:DEFINED_AT_HEAD]->(src)
            """, url=t.repo_url,
                 syms=sorted({canon(s) for s in head_symbols.get(t.package, set())
                              if canon(s)}))

        self.ingest_exists_in(results, primary)
        self.infer_documents()
        self.log("  graph loaded")

    def ingest_exists_in(self, results: list[Result], primary: str = ""):
        """Symbol -[:EXISTS_IN]-> Version, from sandbox introspection.

        The fourth ingest pass of the spec's §8, and the one §9.5 depends on.
        Without it, every symbol looks absent from every release, so the drift
        query reports the entire HEAD surface as "documented but unreleased" -
        157 false rows rather than the handful that are real.

        The probe already answers this per symbol per version; it just was not
        being read back out.
        """
        rows = []
        for r in results:
            if not r.stderr:
                continue
            try:
                payload = json.loads(r.stderr)
            except (ValueError, TypeError):
                continue
            # execute-lane output is whatever the snippet printed, which can
            # itself be JSON - an array from a Pydantic example crashed ingest
            probe = payload.get("probe", {}) if isinstance(payload, dict) else {}
            if not isinstance(probe, dict):
                continue
            for sym, rec in probe.items():
                if not isinstance(rec, dict) or not rec.get("exists"):
                    continue
                # `declared:Name` is the documented-signature channel; the bare
                # dotted symbol is the resolution channel. Both prove existence.
                name = canon(sym)
                if not name:
                    continue
                rows.append({"name": name, "version": r.version,
                             "package": r.package or primary})
        if not rows:
            return
        self._run("""
        UNWIND $rows AS row
          MERGE (y:Symbol {name: row.name})
          MERGE (v:Version {package: row.package, label: row.version})
          MERGE (y)-[:EXISTS_IN]->(v)
        """, rows=rows)

    # --------------------------------------------------------------- queries

    def infer_documents(self):
        """Page -[:DOCUMENTS]-> Version, derived from which versions its
        snippets pass on. Nobody ever told us what version a page describes.
        """
        return self._run("""
        MATCH (p:Page)-[:CONTAINS]->(s:Snippet)-[v:VERIFIED_ON]->(ver:Version)
        WITH p, ver, collect(v.status) AS statuses
        WHERE none(x IN statuses WHERE x = 'fail')
        WITH p, ver ORDER BY ver.released_at DESC
        WITH p, head(collect(ver)) AS newest_clean
        MERGE (p)-[:DOCUMENTS]->(newest_clean)
        RETURN p.url AS url, newest_clean.label AS version
        """)

    def decay(self, current: str):
        return self._run("""
        MATCH (p:Page)-[:CONTAINS]->(s:Snippet)-[v:VERIFIED_ON]->(:Version {label: $cur})
        WITH p, count(s) AS total,
             sum(CASE v.status WHEN 'fail'    THEN 1 ELSE 0 END) AS failed,
             sum(CASE v.status WHEN 'blocked' THEN 1 ELSE 0 END) AS blocked
        RETURN p.url AS url, total, failed, blocked,
               toFloat(failed) / total AS decay
        ORDER BY decay DESC, failed DESC
        """, cur=current)

    def matrix(self):
        return self._run("""
        MATCH (p:Page)-[:CONTAINS]->(s:Snippet)-[v:VERIFIED_ON]->(ver:Version)
        WITH p, ver, collect(v.status) AS statuses
        WITH p, ver, none(x IN statuses WHERE x = 'fail') AS clean
        WITH p, collect({version: ver.label, clean: clean}) AS results
        RETURN p.url AS url,
               [r IN results WHERE r.clean     | r.version] AS true_for,
               [r IN results WHERE NOT r.clean | r.version] AS broken_on
        ORDER BY size(broken_on) DESC
        """)

    def blast_radius(self, symbol: str):
        return self._run("""
        MATCH (sym:Symbol {name: $symbol})<-[:EXERCISES|ABOUT*1..3]-(n)
        MATCH (p:Page)-[:CONTAINS|STATES]->(n)
        RETURN DISTINCT p.url AS url, count(n) AS touchpoints
        ORDER BY touchpoints DESC
        """, symbol=symbol)

    def drift(self):
        """Symbols where HEAD and the published releases disagree - documents
        describing removed code, or code that never shipped.
        """
        return self._run("""
        MATCH (sym:Symbol)
        // Bind the Source so the count is null-aware. `count(*)` after an
        // OPTIONAL MATCH counts ROWS, which is >= 1 even when the match fails,
        // so `count(*) > 0` is always true - every symbol looked present at
        // HEAD and the drift polarity was inverted for all of them.
        OPTIONAL MATCH (sym)-[:DEFINED_AT_HEAD]->(src:Source {kind: 'repo'})
        WITH sym, count(src) > 0 AS at_head
        OPTIONAL MATCH (sym)-[:EXISTS_IN]->(v:Version)
        WITH sym, at_head, collect(v.label) AS in_releases
        WHERE at_head <> (size(in_releases) > 0)
        // Only symbols a snippet actually exercised: those were probed in
        // every tested release, so an empty `in_releases` means genuinely
        // absent. A claim-linked symbol is never probed, so its empty list
        // means "not tested" - counting those reported the whole documented
        // surface as drift.
        MATCH (p:Page)-[:CONTAINS]->(:Snippet)-[:EXERCISES]->(sym)
        RETURN DISTINCT p.url AS url, sym.name AS symbol,
               at_head, in_releases
        """)
