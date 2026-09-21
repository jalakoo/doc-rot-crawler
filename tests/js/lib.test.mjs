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
