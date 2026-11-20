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

  /* The positional `packages` list that goes with checkSources' repos:
     packages[i] names the package for repos[i] (blank keeps the slot), then
     packages with no repo follow. A package typed on a blank or duplicate repo
     row can't keep its slot, so it moves to the repo-less tail. Trailing blanks
     are dropped; repeated names are kept once.

     `picks[i]` is what "Detect packages" left ticked for repos[i]: null when
     detection did not run or every box stayed ticked (the scan then chooses
     for itself), else the prefixed specs ("npm:@relate/relate") to verify. A
     typed package keeps the repo's own slot and the picks follow it in the
     tail; with none typed, the first pick takes the slot. Naming anything makes
     the scan verify exactly the named packages of that repo, which is why a
     typed name and a pick are both kept. A pick equal to a typed name, with or
     without its prefix, counts once. */
  var PREFIX = /^(pypi|npm|nuget|cli):/i;
  /* the same package, typed with or without its ecosystem prefix; two prefixed
     names are the same only if the ecosystems agree too */
  function samePackage(a, b) {
    if (PREFIX.test(a) && PREFIX.test(b)) return a.toLowerCase() === b.toLowerCase();
    return a.replace(PREFIX, "").toLowerCase() === b.replace(PREFIX, "").toLowerCase();
  }
  function alignPackages(reposRaw, pkgsRaw, extraRaw, picks) {
    var seen = [], aligned = [], tail = [];
    function clean(v) { return String(v || "").trim(); }
    reposRaw.forEach(function (raw, i) {
      var repo = clean(raw), pkg = clean(pkgsRaw[i]);
      var chosen = ((picks || [])[i] || []).map(clean).filter(Boolean);
      if (repo && seen.indexOf(repo) < 0) {
        seen.push(repo);
        if (!pkg && chosen.length) pkg = chosen.shift();
        aligned.push(pkg);
        tail = tail.concat(chosen);
      } else if (pkg) tail.push(pkg);
    });
    (extraRaw || []).forEach(function (raw) { if (clean(raw)) tail.push(clean(raw)); });
    var named = aligned.filter(Boolean), kept = [];
    tail.forEach(function (p) {
      if (kept.concat(named).some(function (k) { return samePackage(k, p); })) return;
      kept.push(p);
    });
    var out = aligned.concat(kept);
    while (out.length && !out[out.length - 1]) out.pop();
    return out;
  }

  /* A `cli:` package is installed from a GitHub repo's release binaries, so the
     scan needs such a repo; the server says the same, but not until Start. */
  var GITHUB = /github\.com[/:][\w.-]+\/[\w.-]+/i;
  function cliNeedsGithub(packages, repos) {
    var cli = packages.filter(function (p) { return /^cli:/i.test(p); });
    if (!cli.length || repos.some(function (r) { return GITHUB.test(r); })) return [];
    return [cli[0] + " needs the GitHub repository that publishes its release binaries - add it as a repository (https://github.com/org/name)."];
  }

  /* What a repo's detection left ticked, for alignPackages: null when every
     box is ticked (send nothing, the scan chooses), else the ticked specs. */
  function detectedPicks(specs, ticked) {
    var on = specs.filter(function (s) { return ticked.indexOf(s) >= 0; });
    return on.length === specs.length ? null : on;
  }

  var ECO_LABEL = { pypi: "PyPI", npm: "npm", nuget: "NuGet", cli: "CLI \u00b7 GitHub release binaries" };
  var OTHER_LABEL = { rust: "Rust", go: "Go", java: "Java", swift: "Swift", ruby: "Ruby", php: "PHP" };

  /* "relate-db   PyPI · import relate · surfaces/relate-python" as {name, meta}. */
  function detectedLine(pkg) {
    var meta = [ECO_LABEL[pkg.ecosystem] || pkg.ecosystem];
    if (pkg.ecosystem === "cli") { if (pkg.latest) meta.push("latest " + pkg.latest); }
    else if (pkg.import_name && pkg.import_name !== pkg.name) meta.push("import " + pkg.import_name);
    if (pkg.path) meta.push(pkg.path);
    return { name: pkg.name, meta: meta.join(" · ") };
  }

  function otherNote(other) {
    if (!other || !other.length) return "";
    return "Also contains " + andList(other.map(function (o) { return OTHER_LABEL[o] || o; })) +
           " — no checker for " + (other.length === 1 ? "it" : "them") + " yet.";
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

  /* What a run did not check. {total, checked, unchecked, reasons: [{code, label,
     n, detail}]} from a report's `coverage` block, or null when the report has
     none (reports from before it was recorded) or nothing was extracted. */
  function coverageView(cov) {
    if (!cov || !cov.tiers) return null;
    var t = cov.tiers, checked = (t.execute || 0) + (t.structural || 0);
    var unchecked = t.unverifiable || 0, total = checked + unchecked;
    if (!total) return null;
    var langs = Object.keys(cov.no_checker || {}).sort(function (a, b) {
      return (cov.no_checker[b] - cov.no_checker[a]) || a.localeCompare(b);
    });
    var reasons = Object.keys(cov.unverifiable || {}).sort(function (a, b) {
      return (cov.unverifiable[b] - cov.unverifiable[a]) || a.localeCompare(b);
    }).map(function (code) {
      var detail = code === "no-checker" ? langs.slice(0, 4).map(function (l) {
        return l + " " + cov.no_checker[l];
      }).join(", ") + (langs.length > 4 ? ", …" : "") : "";
      return { code: code, label: (cov.labels || {})[code] || code, n: cov.unverifiable[code], detail: detail };
    });
    return { total: total, checked: checked, unchecked: unchecked, reasons: reasons };
  }

  /* "186 snippets · 14 checked · 172 not checked: 120 use no scanned package · 35 not code" */
  function coverageSentence(view) {
    var s = plural(view.total, "snippet") + " · " + view.checked + " checked · " +
            view.unchecked + " not checked";
    if (!view.unchecked) return s;
    return s + ": " + view.reasons.map(function (r) {
      return r.n + " " + r.label + (r.detail ? " (" + r.detail + ")" : "");
    }).join(" · ");
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
    // `coverage` is optional: older reports never had it, and is left absent
    if (rep.coverage && typeof rep.coverage !== "object") delete rep.coverage;
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
    alignPackages: alignPackages, cliNeedsGithub: cliNeedsGithub, detectedPicks: detectedPicks, detectedLine: detectedLine,
    otherNote: otherNote, coverageView: coverageView, coverageSentence: coverageSentence,
    defaultName: defaultName, lastRun: lastRun, cardState: cardState,
    findingsDelta: findingsDelta, activityAt: activityAt, sortScans: sortScans,
    fleetTotals: fleetTotals, pageBarCells: pageBarCells, normalizeReport: normalizeReport,
    timelineGroups: timelineGroups, apiErrors: apiErrors, richText: richText
  };
});
