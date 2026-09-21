/* Doc Rot Crawler - pure helpers shared by the dashboard (app.js) and its unit
   tests (tests/js). Nothing here touches the DOM or the network. */
(function (root, factory) {
  "use strict";
  var lib = factory();
  if (typeof module === "object" && module.exports) module.exports = lib;
  else root.DocrotLib = lib;
})(this, function () {
  "use strict";

  var PHASES = ["acquiring", "registry", "extracting", "verifying", "graph", "report"];
  var STATE_LBL = { rot: "Rot found", clean: "Clean", running: "Scanning", queued: "Queued",
                    failed: "Failed", new: "Not run", unverified: "Unverified" };
  var MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  var PAGE_ORDER = ["fail", "drift", "block", "pass", "unlit"];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function isHttp(u) { return /^https?:\/\//.test(u || ""); }
  function hostOf(u) { try { return new URL(u).host; } catch { return u; } }

  /* github.com/org/repo -> org/repo; a docs URL -> host/path; else as given */
  function shortSrc(u) {
    try {
      var x = new URL(u), p = x.pathname.replace(/\/+$/, "").replace(/\.git$/, "");
      if (!/^https?:$/.test(x.protocol)) throw new Error("not http");
      return x.host === "github.com" || x.host === "gitlab.com" ? p.slice(1) : x.host + p;
    } catch {
      return String(u).replace(/^git@[^:]+:/, "").replace(/\.git$/, "");
    }
  }
  function repoName(u) { return shortSrc(u).split("/").pop(); }

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function fmtAbs(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return "";
    return d.getDate() + " " + MON[d.getMonth()] + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function fmtAgo(iso, nowMs) {
    var t = new Date(iso).getTime();
    if (isNaN(t)) return "";
    var s = Math.max(0, ((nowMs == null ? Date.now() : nowMs) - t) / 1000);
    if (s < 45) return "just now";
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    var d = Math.floor(s / 86400);
    return d + (d === 1 ? " day ago" : " days ago");
  }
  function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
  function andList(items) {
    if (items.length < 2) return items.join("");
    return items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
  }

  /* Same rules as docrot/store.py - the server re-checks, this is for instant feedback. */
  function isDocsUrl(v) {
    try {
      var u = new URL(v);
      return /^https?:$/.test(u.protocol) && u.hostname.indexOf(".") > 0;
    } catch { return false; }
  }
  function isRepo(v) {
    if (/^git@[\w.-]+:[\w.-]+\/[\w.-]+$/.test(v)) return true;
    if (/^(https?|ssh):\/\//.test(v)) return isDocsUrl(v.replace(/^ssh:/, "https:"));
    return /^(\/|\.{1,2}\/|~\/)/.test(v);
  }

  /* {docs, repos, errors} from raw field values: trimmed, de-duplicated, checked. */
  function checkSources(docsRaw, reposRaw) {
    var docs = [], repos = [], errors = [], invalid = { docs: [], repos: [] };
    function walk(list, ok, out, bad, noun, why) {
      list.forEach(function (raw, i) {
        var v = String(raw || "").trim();
        if (!v) return;
        if (!ok(v)) { errors.push(noun + " " + (i + 1) + " " + why); bad.push(i); }
        else if (out.indexOf(v) < 0) out.push(v);
      });
    }
    walk(docsRaw, isDocsUrl, docs, invalid.docs, "Docs URL", "isn't an http(s) address.");
    walk(reposRaw, isRepo, repos, invalid.repos, "Repository", "isn't a git URL or a local path (/…, ./…, ~/…).");
    if (!errors.length && !docs.length && !repos.length) {
      errors.push("Add at least one docs URL or repository — docrot needs something to read.");
    }
    return { docs: docs, repos: repos, errors: errors, invalid: invalid };
  }

  function defaultName(docs, repos) {
    return docs.length ? shortSrc(docs[0]) : repos.length ? repoName(repos[0]) : "";
  }

  function lastRun(scan) { return scan.runs && scan.runs.length ? scan.runs[scan.runs.length - 1] : null; }

  /* The card's headline state. A failed attempt wins over an older good run,
     and a run that verified nothing is never called clean: zero findings
     because every install was blocked is not the same as zero rot. */
  function cardState(scan) {
    var st = scan.status && scan.status.state;
    if (st === "running" || st === "queued" || st === "failed") return st;
    var last = lastRun(scan);
    if (!last) return "new";
    if (last.stats && last.stats.findings) return "rot";
    var ps = last.page_states || {};
    if (!ps.pass && ps.block) return "unverified";
    return "clean";
  }

  /* Findings change between the last two runs: null for a first run. */
  function findingsDelta(scan) {
    var runs = scan.runs || [];
    if (runs.length < 2) return null;
    return (runs[runs.length - 1].stats.findings || 0) - (runs[runs.length - 2].stats.findings || 0);
  }

  function activityAt(scan) {
    var st = scan.status || {}, last = lastRun(scan);
    var at = st.state === "running" ? st.started_at
           : st.state === "queued" ? st.queued_at
           : st.state === "failed" ? st.finished_at
           : last ? last.at : scan.created_at;
    var t = Date.parse(at || scan.created_at || 0);
    return isNaN(t) ? 0 : t;
  }

  /* Running first, then the queue in the order it will run, then everything
     else by most recent activity. */
  function sortScans(scans) {
    function rank(s) {
      var st = s.status && s.status.state;
      return st === "running" ? 0 : st === "queued" ? 1 : 2;
    }
    return scans.slice().sort(function (a, b) {
      var r = rank(a) - rank(b);
      if (r) return r;
      return rank(a) === 1 ? activityAt(a) - activityAt(b) : activityAt(b) - activityAt(a);
    });
  }

  function fleetTotals(scans) {
    var t = { scans: scans.length, live: 0, pages: 0, drifted: 0, findings: 0, lastAt: "" };
    scans.forEach(function (s) {
      var st = s.status && s.status.state, last = lastRun(s);
      if (st === "running" || st === "queued") t.live++;
      if (last) {
        t.pages += last.stats.pages || 0;
        t.drifted += last.stats.drifted || 0;
        t.findings += last.stats.findings || 0;
        if (!t.lastAt || last.at > t.lastAt) t.lastAt = last.at;
      }
    });
    return t;
  }

  /* One entry per page state, in severity order, for the card's page bar. */
  function pageBarCells(pageStates) {
    var out = [];
    PAGE_ORDER.forEach(function (k) {
      for (var i = 0; i < ((pageStates || {})[k] || 0); i++) out.push(k);
    });
    return out;
  }

  /* Finding descriptions are the one place the report supplies markup: the
     builder writes <b> around names it has already escaped. Everything else is
     escaped here, so a crafted report or imported file cannot inject an
     element - and with it, an inline event handler. */
  function richText(text) {
    return esc(text)
      .replace(/&lt;(\/?)(b|code)&gt;/g, "<$1$2>");
  }

  /* A report from before multi-source support gets the fields app.js expects. */
  function normalizeReport(rep) {
    if (!rep.docs_urls) rep.docs_urls = rep.docs_url ? [rep.docs_url] : [];
    if (!rep.repo_urls) rep.repo_urls = rep.repo_url ? [rep.repo_url] : [];
    if (!rep.packages || !rep.packages.length) {
      rep.packages = [{ package: rep.package, import_name: rep.import_name,
                        repo_url: rep.repo_url, versions: rep.versions || [] }];
    }
    if (!rep.sources) {
      var repoPages = 0, total = 0;
      (rep.sections || []).forEach(function (s) {
        s.pages.forEach(function () { total++; if (s.name === "Repository docs") repoPages++; });
      });
      rep.sources = rep.docs_urls.map(function (u) { return { kind: "docs", url: u, pages: total - repoPages }; })
        .concat(rep.repo_urls.map(function (u) { return { kind: "repo", url: u, pages: repoPages }; }));
    }
    return rep;
  }

  /* Timeline rows grouped by package, each group with its own version header. */
  function timelineGroups(rep) {
    var pkgs = rep.packages;
    return pkgs.map(function (p, i) {
      return {
        name: p.package, versions: p.versions || [],
        rows: (rep.timeline || []).filter(function (r) { return (r.pkg || 0) === i; })
      };
    }).filter(function (g) { return g.rows.length || pkgs.length === 1; });
  }

  function apiErrors(detail) {
    if (!detail) return ["Something went wrong talking to the docrot server."];
    if (typeof detail === "string") return [detail];
    return detail.map(function (d) {
      return typeof d === "string" ? d
        : (d.msg || "invalid") + (d.loc ? " (" + d.loc.slice(1).join(".") + ")" : "");
    });
  }

  return {
    PHASES: PHASES, STATE_LBL: STATE_LBL, esc: esc, isHttp: isHttp, hostOf: hostOf,
    shortSrc: shortSrc, repoName: repoName, fmtAbs: fmtAbs, fmtAgo: fmtAgo, plural: plural,
    andList: andList, isDocsUrl: isDocsUrl, isRepo: isRepo, checkSources: checkSources,
    defaultName: defaultName, lastRun: lastRun, cardState: cardState,
    findingsDelta: findingsDelta, activityAt: activityAt, sortScans: sortScans,
    fleetTotals: fleetTotals, pageBarCells: pageBarCells, normalizeReport: normalizeReport,
    timelineGroups: timelineGroups, apiErrors: apiErrors, richText: richText
  };
});
