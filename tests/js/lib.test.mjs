// Unit tests for docrot/ui/lib.js. Run: npm test  (node --test, no dependencies)
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const L = createRequire(import.meta.url)("../../docrot/ui/lib.js");

const scan = (over = {}) => ({
  id: "s", name: "S", docs: ["https://docs.a.io"], repos: [], created_at: "2026-09-01T00:00:00",
  status: { state: "done", phase: 5, log: [], queued_at: "", started_at: "", finished_at: "", error: "" },
  runs: [], ...over,
});
const run = (at, findings, extra = {}) => ({ id: at, at, elapsed: 1, stats: { findings, pages: 10, drifted: findings, snippets: 3, versions: 3 }, page_states: {}, ...extra });

test("esc escapes markup and quotes", () => {
  assert.equal(L.esc(`<a href="x">'&'</a>`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
  assert.equal(L.esc(null), "");
});

test("shortSrc", () => {
  assert.equal(L.shortSrc("https://github.com/org/repo.git"), "org/repo");
  assert.equal(L.shortSrc("https://docs.a.io/latest/"), "docs.a.io/latest");
  assert.equal(L.shortSrc("git@github.com:org/repo.git"), "org/repo");
  assert.equal(L.shortSrc("./local/path"), "./local/path");
  assert.equal(L.repoName("https://github.com/org/pydantic-core"), "pydantic-core");
});

test("fmtAgo buckets", () => {
  const now = Date.parse("2026-09-15T12:00:00");
  assert.equal(L.fmtAgo("2026-09-15T11:59:40", now), "just now");
  assert.equal(L.fmtAgo("2026-09-15T11:50:00", now), "10 min ago");
  assert.equal(L.fmtAgo("2026-09-15T07:00:00", now), "5h ago");
  assert.equal(L.fmtAgo("2026-09-14T11:00:00", now), "1 day ago");
  assert.equal(L.fmtAgo("2026-09-12T12:00:00", now), "3 days ago");
  assert.equal(L.fmtAgo("garbage", now), "");
});

test("source rules match the server's", () => {
  for (const ok of ["https://docs.a.io", "http://a.io/x"]) assert.ok(L.isDocsUrl(ok), ok);
  for (const bad of ["ftp://a.io", "https://localhost", "a.io", ""]) assert.ok(!L.isDocsUrl(bad), bad);
  for (const ok of ["https://github.com/o/r", "git@github.com:o/r.git", "ssh://git@github.com/o/r", "/abs", "./rel", "~/r"]) assert.ok(L.isRepo(ok), ok);
  for (const bad of ["pydantic-core", "github.com/o/r"]) assert.ok(!L.isRepo(bad), bad);
});

test("checkSources trims, de-duplicates and indexes problems", () => {
  const r = L.checkSources([" https://a.io ", "", "https://a.io", "nope"], ["bad", "./ok"]);
  assert.deepEqual(r.docs, ["https://a.io"]);
  assert.deepEqual(r.repos, ["./ok"]);
  assert.deepEqual(r.invalid, { docs: [3], repos: [0] });
  assert.deepEqual(r.errors, ["Docs URL 4 isn't an http(s) address.",
                              "Repository 1 isn't a git URL or a local path (/…, ./…, ~/…)."]);
  assert.match(L.checkSources(["  "], []).errors[0], /at least one/);
});

test("alignPackages keeps positions and appends repo-less packages", () => {
  // blank middle package keeps its slot
  assert.deepEqual(L.alignPackages(["r1", "r2", "r3"], ["a", "", "c"], []), ["a", "", "c"]);
  // trailing blanks are trimmed, and nothing at all gives an empty list
  assert.deepEqual(L.alignPackages(["r1", "r2"], ["a", " "], []), ["a"]);
  assert.deepEqual(L.alignPackages(["r1", "r2"], ["", ""], ["  "]), []);
  // repo-less packages follow the aligned ones, even past a blank slot
  assert.deepEqual(L.alignPackages(["r1", "r2"], ["", "b"], ["c"]), ["", "b", "c"]);
  assert.deepEqual(L.alignPackages(["r1", "r2"], ["", ""], ["c"]), ["", "", "c"]);
  // a package on a blank repo row moves to the tail instead of shifting later rows
  assert.deepEqual(L.alignPackages(["r1", "", "r3"], ["a", "orphan", "c"], ["d"]), ["a", "c", "orphan", "d"]);
  // a duplicate repo is dropped as checkSources does, so slots still match its repos
  assert.deepEqual(L.alignPackages(["r1", "r1", "r2"], ["a", "x", "b"], []), ["a", "b", "x"]);
  // no repos: just the bare packages, de-duplicated
  assert.deepEqual(L.alignPackages(["", ""], ["", ""], ["p", "p", " q "]), ["p", "q"]);
});

test("alignPackages puts a repo's first ticked package in its slot and the rest in the tail", () => {
  const picks = [["npm:@relate/relate", "nuget:Relate.Net"], null];
  assert.deepEqual(L.alignPackages(["r1", "r2"], ["", ""], [], picks), ["npm:@relate/relate", "", "nuget:Relate.Net"]);
  // a repo with nothing unticked sends nothing: the scan chooses for itself
  assert.deepEqual(L.alignPackages(["r1"], [""], [], [null]), []);
  // one pick fits in the slot, so nothing is left for the tail
  assert.deepEqual(L.alignPackages(["r1", "r2"], ["", "b"], [], [["pypi:relate-db"], null]), ["pypi:relate-db", "b"]);
  // a package typed beside the repo keeps the slot; the picks follow it
  assert.deepEqual(L.alignPackages(["r1"], ["mine"], [], [["npm:a", "npm:b"]]), ["mine", "npm:a", "npm:b"]);
  // a pick the user also typed, with or without its prefix, counts once
  assert.deepEqual(L.alignPackages(["r1"], ["relate-db"], [], [["pypi:relate-db", "npm:x"]]), ["relate-db", "npm:x"]);
  assert.deepEqual(L.alignPackages(["r1"], [""], ["npm:x"], [["npm:x", "npm:y"]]), ["npm:x", "npm:y"]);
  // the same bare name in two ecosystems is two packages
  assert.deepEqual(L.alignPackages(["r1"], [""], [], [["pypi:x", "npm:x"]]), ["pypi:x", "npm:x"]);
  // picks on a blank or duplicate repo row are ignored, as its typed package moves to the tail
  assert.deepEqual(L.alignPackages(["r1", "r1"], ["", "z"], [], [["npm:a"], ["npm:b"]]), ["npm:a", "z"]);
  assert.deepEqual(L.alignPackages(["", "r2"], ["", ""], [], [["npm:a"], ["npm:b"]]), ["npm:b"]);
  // package-without-a-repo rows still follow the picks
  assert.deepEqual(L.alignPackages(["r1", "r2"], ["", ""], ["loose"], [["npm:a", "npm:b"], null]), ["npm:a", "", "npm:b", "loose"]);
});

test("detectedPicks is null while every box is ticked", () => {
  const specs = ["pypi:a", "npm:b"];
  assert.equal(L.detectedPicks(specs, ["npm:b", "pypi:a"]), null);
  assert.deepEqual(L.detectedPicks(specs, ["npm:b"]), ["npm:b"]);
  assert.deepEqual(L.detectedPicks(specs, []), []);
});

test("detectedLine and otherNote describe what was found", () => {
  assert.deepEqual(L.detectedLine({ name: "relate-db", import_name: "relate", ecosystem: "pypi", path: "surfaces/relate-python" }),
                   { name: "relate-db", meta: "PyPI · import relate · surfaces/relate-python" });
  assert.deepEqual(L.detectedLine({ name: "@relate/relate", import_name: "@relate/relate", ecosystem: "npm", path: "" }),
                   { name: "@relate/relate", meta: "npm" });
  assert.equal(L.otherNote(["rust", "go", "java"]), "Also contains Rust, Go and Java — no checker for them yet.");
  assert.equal(L.otherNote(["swift"]), "Also contains Swift — no checker for it yet.");
  assert.equal(L.otherNote([]), "");
});

const COVERAGE = {
  tiers: { execute: 4, structural: 10, unverifiable: 172 },
  unverifiable: { "no-package": 120, "not-code": 35, "no-checker": 17 },
  no_checker: { go: 8, rust: 9 },
  labels: { "no-package": "use no scanned package", "not-code": "not code", "no-checker": "no checker" },
};

test("coverageView says what was checked and why the rest was not", () => {
  const v = L.coverageView(COVERAGE);
  assert.deepEqual([v.total, v.checked, v.unchecked], [186, 14, 172]);
  assert.deepEqual(v.reasons.map((r) => [r.label, r.n, r.detail]),
    [["use no scanned package", 120, ""], ["not code", 35, ""], ["no checker", 17, "rust 9, go 8"]]);
  assert.equal(L.coverageSentence(v),
    "186 snippets · 14 checked · 172 not checked: 120 use no scanned package · 35 not code · 17 no checker (rust 9, go 8)");
  // a reason the labels do not know shows its code; counts sort even if the report did not
  const odd = L.coverageView({ tiers: { structural: 1, unverifiable: 3 }, unverifiable: { a: 1, b: 2 } });
  assert.deepEqual(odd.reasons.map((r) => r.label), ["b", "a"]);
  assert.equal(L.coverageSentence(L.coverageView({ tiers: { execute: 2 } })), "2 snippets · 2 checked · 0 not checked");
});

test("coverageView is null without a coverage block or without snippets", () => {
  assert.equal(L.coverageView(undefined), null);
  assert.equal(L.coverageView({ tiers: { execute: 0, structural: 0, unverifiable: 0 } }), null);
});

test("cardState", () => {
  assert.equal(L.cardState(scan()), "new");
  assert.equal(L.cardState(scan({ runs: [run("2026-09-01T00:00:00", 2)] })), "rot");
  assert.equal(L.cardState(scan({ runs: [run("2026-09-01T00:00:00", 0)] })), "clean");
  assert.equal(L.cardState(scan({ runs: [run("x", 0, { page_states: { block: 40, unlit: 20 } })] })), "unverified");
  assert.equal(L.cardState(scan({ runs: [run("x", 0, { page_states: { block: 2, pass: 30 } })] })), "clean");
  for (const state of ["running", "queued", "failed"]) {
    assert.equal(L.cardState(scan({ status: { ...scan().status, state }, runs: [run("x", 0)] })), state);
  }
});

test("findingsDelta", () => {
  assert.equal(L.findingsDelta(scan({ runs: [run("a", 3)] })), null);
  assert.equal(L.findingsDelta(scan({ runs: [run("a", 3), run("b", 7)] })), 4);
  assert.equal(L.findingsDelta(scan({ runs: [run("a", 3), run("b", 1)] })), -2);
});

test("sortScans: running, then the queue in order, then most recent", () => {
  const old = scan({ id: "old", runs: [run("2026-09-01T00:00:00", 0)] });
  const live = scan({ id: "live", status: { ...scan().status, state: "running", started_at: "2026-09-15T00:00:00" } });
  const mid = scan({ id: "mid", runs: [run("2026-09-10T00:00:00", 0)] });
  const q1 = scan({ id: "q1", status: { ...scan().status, state: "queued", queued_at: "2026-09-15T00:01:00" } });
  const q2 = scan({ id: "q2", status: { ...scan().status, state: "queued", queued_at: "2026-09-15T00:02:00" } });
  assert.deepEqual(L.sortScans([old, q2, live, mid, q1]).map((s) => s.id), ["live", "q1", "q2", "mid", "old"]);
});

test("fleetTotals sums the last run of each scan", () => {
  const t = L.fleetTotals([
    scan({ runs: [run("2026-09-01T00:00:00", 9), run("2026-09-02T00:00:00", 2)] }),
    scan({ runs: [run("2026-09-05T00:00:00", 1)], status: { ...scan().status, state: "queued" } }),
    scan(),
  ]);
  assert.deepEqual(t, { scans: 3, live: 1, pages: 20, drifted: 3, findings: 3, lastAt: "2026-09-05T00:00:00" });
});

test("pageBarCells orders by severity", () => {
  assert.deepEqual(L.pageBarCells({ pass: 1, fail: 2, unlit: 1, drift: 1 }), ["fail", "fail", "drift", "pass", "unlit"]);
  assert.deepEqual(L.pageBarCells(undefined), []);
});

test("normalizeReport upgrades single-source reports", () => {
  const rep = L.normalizeReport({
    docs_url: "https://d.io", repo_url: "https://github.com/o/r", package: "r", versions: [{ label: "1" }],
    sections: [{ name: "Guide", pages: [{}, {}] }, { name: "Repository docs", pages: [{}] }],
  });
  assert.deepEqual(rep.sources, [{ kind: "docs", url: "https://d.io", pages: 2 },
                                 { kind: "repo", url: "https://github.com/o/r", pages: 1 }]);
  assert.equal(rep.packages[0].package, "r");
  assert.deepEqual(L.timelineGroups({ ...rep, timeline: [{ n: "a" }] })[0].rows, [{ n: "a" }]);
});

test("normalizeReport keeps coverage when present and tolerates its absence", () => {
  const base = { docs_urls: ["d"], repo_urls: [], packages: [{ package: "p" }], sources: [], sections: [] };
  assert.equal(L.normalizeReport({ ...base }).coverage, undefined);
  assert.equal(L.coverageView(L.normalizeReport({ ...base }).coverage), null);
  assert.deepEqual(L.normalizeReport({ ...base, coverage: COVERAGE }).coverage, COVERAGE);
  assert.equal(L.normalizeReport({ ...base, coverage: "junk" }).coverage, undefined);
});

test("timelineGroups splits rows by package and drops empty groups", () => {
  const groups = L.timelineGroups({
    packages: [{ package: "a", versions: [] }, { package: "b", versions: [] }, { package: "c", versions: [] }],
    timeline: [{ n: 1, pkg: 0 }, { n: 2, pkg: 1 }, { n: 3, pkg: 0 }],
  });
  assert.deepEqual(groups.map((g) => [g.name, g.rows.length]), [["a", 2], ["b", 1]]);
});

test("apiErrors reads both error shapes", () => {
  assert.deepEqual(L.apiErrors(["Docs URL 1 isn't an http(s) address."]), ["Docs URL 1 isn't an http(s) address."]);
  assert.deepEqual(L.apiErrors([{ loc: ["body", "options", "versions"], msg: "too small" }]), ["too small (options.versions)"]);
  assert.equal(L.apiErrors(undefined).length, 1);
});

test("richText escapes everything except the builder's <b> and <code>", () => {
  assert.equal(L.richText("<img src=x onerror=alert(1)>"), "&lt;img src=x onerror=alert(1)&gt;");
  assert.equal(L.richText("<b>close_async</b> is gone"), "<b>close_async</b> is gone");
  assert.equal(L.richText("<script>alert(1)</script>"), "&lt;script&gt;alert(1)&lt;/script&gt;");
  // an opening tag carrying attributes never survives; the stray closing tag
  // that is left behind is inert - it cannot hold a handler
  assert.equal(L.richText("<b onclick=x>no attributes</b>"), "&lt;b onclick=x&gt;no attributes</b>");
  // the only markup that survives is a bare <b>/<code> tag - never one with an
  // attribute, which is what an injected handler needs
  const tags = (text) => [...L.richText(text).matchAll(/<[^>]*>/g)].map((m) => m[0]);
  assert.deepEqual(tags('<b onerror="x">t</b>'), ["</b>"]);
  assert.deepEqual(tags("<b>t</b> <code>c</code>"), ["<b>", "</b>", "<code>", "</code>"]);
  assert.deepEqual(tags('<img src=x onerror="y">'), []);
  assert.equal(L.richText("<code>pip install x</code>"), "<code>pip install x</code>");
  assert.equal(L.richText(null), "");
});
