/* Doc Rot Crawler - the dashboard.

   #/              every scan as a card, plus the new-scan dialog
   #/scan/<id>     one scan: the drift detail view (the original index page)

   State lives on the server (docrot/server.py). This file polls it: fast while
   a scan is queued or running, slowly otherwise, so a scan started from the
   CLI still appears. */
(function () {
  "use strict";

  var L = window.DocrotLib, esc = L.esc, plural = L.plural;

  var PAL = { g:"#5a7d3a", G:"#7fa84a", k:"#0d0a14", w:"#e8dfc8", d:"#2a1a10",
              p:"#7b5ea7", P:"#9d7fd0", a:"#8a5a2b", b:"#5c3a1e", y:"#f0a830" };
  var SPRITES = {
    slime:["............","....GGGG....","..GGGGGGGG..",".GGGGGGGGGG.",
           ".GwwGGGGwwG.",".GkkGGGGkkG.",".GGGGGGGGGG.",".GGGGddGGGG.",
           ".gggggggggg.","..gggggggg..","...g.gg.g...","............"],
    wraith:["............","....PPPP....","...PPPPPP...","..PPPPPPPP..",
            "..PwwPPwwP..","..PkkPPkkP..","..PPPPPPPP..","..pppppppp..",
            "..pppppppp..","..pp.pp.pp..","...p..p..p..","............"],
    mimic:["............","..bbbbbbbb..",".bbaaaaaabb.",".bwwwwwwwwb.",
           ".bkkkkkkkkb.",".bwwwwwwwwb.",".bbaaaaaabb.",".baaaaaaaab.",
           ".baaayyaaab.",".baaayyaaab.","..bbbbbbbb..","............"]
  };
  var FAST = 1500, SLOW = 10000;
  var DRIFT_KINDS = ["stale_pin", "signature", "missing_symbol", "runtime"];

  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var scans = [], signatures = {}, reports = {};
  var current = null, shownRun = null, editing = null, pollTimer = null, loaded = false;

  function $(id) { return document.getElementById(id); }
  function byId(id) { return scans.filter(function (s) { return s.id === id; })[0]; }
  function announce(msg) {
    var l = $("live");
    l.textContent = "";
    setTimeout(function () { l.textContent = msg; }, 30);
  }

  /* ================================================================= api */

  function api(method, path, body) {
    return send(path, {
      method: method, cache: "no-store",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined
    });
  }

  /* An exported file is posted as-is; it is JSON Lines, not a JSON document. */
  function postFile(path, text) {
    return send(path, { method: "POST", cache: "no-store",
                        headers: { "Content-Type": "text/plain" }, body: text });
  }

  function send(path, init) {
    return fetch(path, init).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (data) {
        if (!r.ok) {
          var err = new Error("HTTP " + r.status);
          err.status = r.status;
          err.messages = L.apiErrors(data && data.detail);
          throw err;
        }
        return data;
      });
    });
  }

  function upsert(view) {
    var i = scans.map(function (s) { return s.id; }).indexOf(view.id);
    if (i < 0) scans.push(view); else scans[i] = view;
    return view;
  }

  function refresh() {
    clearTimeout(pollTimer);
    return api("GET", "/api/scans").then(function (list) {
      $("offline").hidden = true;
      var firstLoad = !loaded;
      loaded = true;
      var before = scans;
      scans = list;
      if (firstLoad) route();
      else if (current) syncDetail(before);
      else syncList(before);
      renderFleet();
    }).catch(function () {
      $("offline").hidden = false;
    }).then(schedule);
  }

  function schedule() {
    var busy = scans.some(function (s) {
      return s.status.state === "running" || s.status.state === "queued";
    });
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refresh, busy ? FAST : SLOW);
  }

  function loadReport(scan, run) {
    var key = scan.id + ":" + run.id;
    if (reports[key]) return Promise.resolve(reports[key]);
    return api("GET", "/api/scans/" + encodeURIComponent(scan.id) + "/report?run=" +
               encodeURIComponent(run.id))
      .then(function (rep) { reports[key] = L.normalizeReport(rep); return reports[key]; });
  }

  /* ================================================================ list */

  function signature(s) {
    var st = s.status;
    return [s.name, st.state, st.phase, st.log.length, st.error, s.runs.length].join("|");
  }

  function tile(label, value, cls, unit) {
    return '<div class="stat ' + (cls || "") + '"><dt>' + esc(label) + "</dt><dd>" +
           esc(value) + (unit ? '<span class="unit">' + esc(unit) + "</span>" : "") + "</dd></div>";
  }

  function ago(iso, prefix) {
    if (!iso) return "";
    return '<time datetime="' + esc(iso) + '" title="' + esc(L.fmtAbs(iso)) + '" data-ago="' +
           esc(iso) + '" data-prefix="' + esc(prefix || "") + '">' +
           esc((prefix || "") + L.fmtAgo(iso)) + "</time>";
  }

  function sourcesHTML(docs, repos, max) {
    var rows = docs.map(function (u) { return ["docs", u]; })
      .concat(repos.map(function (u) { return ["repo", u]; }));
    var shown = rows.slice(0, max), extra = rows.length - shown.length;
    return '<ul class="srcs-list">' + shown.map(function (r) {
      return '<li class="' + r[0] + '"><span class="src-tag ' + r[0] + '">' + r[0].toUpperCase() +
             '</span><span class="src-val" title="' + esc(r[1]) + '">' + esc(L.shortSrc(r[1])) + "</span></li>";
    }).join("") + (extra ? '<li class="src-more">+ ' + plural(extra, "more source") + "</li>" : "") + "</ul>";
  }

  function pageBar(states) {
    var cells = L.pageBarCells(states), c = states || {};
    var label = cells.length + " pages: " + ((c.fail || 0) + (c.drift || 0)) + " with findings, " +
                (c.block || 0) + " blocked, " + (c.pass || 0) + " clean, " + (c.unlit || 0) + " prose only";
    return '<div class="pagebar" role="img" aria-label="' + label + '" title="' + label + '">' +
           cells.map(function (k) { return '<i class="' + k + '"></i>'; }).join("") + "</div>";
  }

  function phasesHTML(status, withNames) {
    var ph = status.state === "queued" ? -1 : status.phase;
    var segs = L.PHASES.map(function (p, i) {
      return '<i class="' + (i < ph ? "done" : i === ph ? "now" : "") + '"></i>';
    }).join("");
    var label = ph < 0 ? "queued" : L.PHASES[ph];
    return '<p class="phase-lbl"><span>' + label + "</span><span>" +
           (ph < 0 ? "waiting" : "step " + (ph + 1) + " of " + L.PHASES.length) +
           '</span></p><div class="phases" role="progressbar" aria-label="Scan progress" ' +
           'aria-valuemin="0" aria-valuemax="' + L.PHASES.length + '" aria-valuenow="' + (ph + 1) +
           '" aria-valuetext="' + label + '">' + segs + "</div>" +
           (withNames ? '<div class="phase-names" aria-hidden="true">' + L.PHASES.map(function (p, i) {
             return '<span class="' + (i === ph ? "on" : "") + '">' + p + "</span>";
           }).join("") + "</div>" : "");
  }

  function nameHTML(s, onCard) {
    var inner = onCard
      ? '<a class="card-link" href="#/scan/' + esc(s.id) + '">' + esc(s.name) + "</a>"
      : "<span>" + esc(s.name) + "</span>";
    return inner + '<button class="edit" type="button" data-edit="' + esc(s.id) + '" aria-label="Rename ' +
           esc(s.name) + '" title="Rename">&#9998;</button>';
  }

  function lastLine(status) {
    var l = status.log[status.log.length - 1];
    return l ? l[1].trim() : "queued…";
  }

  function cardHTML(s) {
    var state = L.cardState(s), st = s.status, last = L.lastRun(s), h;
    var when = state === "running" ? ago(st.started_at, "started ")
             : state === "queued" ? ago(st.queued_at, "queued ")
             : state === "failed" ? ago(st.finished_at, "failed ")
             : last ? ago(last.at) : ago(s.created_at, "added ");

    h = '<article class="card" data-state="' + state + '"><div class="card-bd">' +
        '<div class="card-top"><span class="state ' + state + '">' +
        (state === "running" ? '<span class="cursor" aria-hidden="true">&#9608;</span> ' : "") +
        L.STATE_LBL[state] + '</span><span class="when">' + when + "</span></div>" +
        '<h3 class="card-name">' + nameHTML(s, true) + "</h3>" +
        sourcesHTML(s.docs, s.repos, 3);

    if (state === "running" || state === "queued") {
      h += "<div>" + phasesHTML(st, false) + '<p class="tail">' + esc(lastLine(st)) + "</p></div>";
    } else {
      if (state === "failed") h += '<p class="fail-msg">' + esc(st.error || "The scan failed.") + "</p>";
      if (state === "unverified") {
        h += '<p class="fail-msg">Nothing was verified: every page with code was blocked, usually by a failed install.</p>';
      }
      if (last) {
        var r = last.stats;
        h += '<div class="card-report' + (state === "failed" ? " stale" : "") + '"><dl class="ribbon mini">' +
             tile("Pages", r.pages) +
             tile("Drift", r.drifted, r.drifted ? "alarm" : "ok") +
             tile("Findings", r.findings, r.findings ? "alarm" : "ok") +
             tile("Snippets", r.snippets) + "</dl>" + pageBar(last.page_states) + "</div>";
      }
    }

    h += '<div class="card-ft">';
    if (state === "running" || state === "queued") {
      h += "<span>run " + (s.runs.length + 1) + " · " + plural(s.docs.length + s.repos.length, "source") + "</span>" +
           '<span class="delta flat">' + (last ? "last run: " + plural(last.stats.findings, "finding") : "first run") + "</span>";
    } else if (state === "failed") {
      h += "<span><b>" + esc(L.fmtAbs(st.finished_at)) + "</b> · re-run from its page</span>" +
           '<span class="delta flat">' + (last ? "showing run of " + esc(L.fmtAbs(last.at)) : "no good run yet") + "</span>";
    } else if (last) {
      var d = L.findingsDelta(s);
      h += "<span><b>" + esc(L.fmtAbs(last.at)) + "</b> · " + last.elapsed + "s · " +
           plural(last.stats.versions || 0, "release") + "</span>" +
           (d === null ? '<span class="delta flat">first run</span>'
            : d > 0 ? '<span class="delta up">&#9650; +' + plural(d, "finding") + " vs last run</span>"
            : d < 0 ? '<span class="delta down">&#9660; ' + plural(-d, "fewer finding") + "</span>"
            : '<span class="delta flat">no change vs last run</span>');
    } else {
      h += "<span>" + plural(s.docs.length + s.repos.length, "source") + '</span><span class="delta flat">never run</span>';
    }
    return h + "</div></div></article>";
  }

  function renderList() {
    $("cards").innerHTML = L.sortScans(scans).map(function (s) {
      signatures[s.id] = signature(s);
      return '<li id="card-' + esc(s.id) + '">' + cardHTML(s) + "</li>";
    }).join("") + '<li><button class="card-add" type="button" data-new>+ New scan' +
      "<small>pair docs sites with the repos they describe</small></button></li>";
    $("scanCount").textContent = plural(scans.length, "doc + repo pair") + " watched";
    $("empty").hidden = scans.length > 0;
  }

  function renderCard(s) {
    var li = $("card-" + s.id);
    if (!li) return renderList();
    signatures[s.id] = signature(s);
    li.innerHTML = cardHTML(s);
  }

  /* Re-render only cards that changed, so a rename in progress survives a poll. */
  function syncList(before) {
    var ids = function (list) { return list.map(function (s) { return s.id; }).sort().join(); };
    if (ids(before) !== ids(scans)) {
      if (!editing) renderList();
      return;
    }
    scans.forEach(function (s) {
      if (signatures[s.id] !== signature(s) && editing !== s.id) {
        var was = before.filter(function (b) { return b.id === s.id; })[0];
        renderCard(s);
        if (was && was.status.state === "running" && s.status.state === "done") {
          var last = L.lastRun(s);
          announce(s.name + " finished: " + plural(last ? last.stats.findings : 0, "finding") + ".");
        }
      }
    });
  }

  function renderFleet() {
    var t = L.fleetTotals(scans);
    var agoTxt = t.lastAt ? L.fmtAgo(t.lastAt) : "—";
    var bare = agoTxt.replace(/ ago$/, "");
    $("fleetRibbon").innerHTML =
      tile("Scans", t.scans, "", t.live ? " · " + t.live + " live" : "") +
      tile("Pages", t.pages) +
      tile("With drift", t.drifted, "alarm") +
      tile("Findings", t.findings, "alarm") +
      tile("Last run", bare, "gold", bare !== agoTxt ? " ago" : "");
  }

  /* ============================================== detail - the index page */

  var report = null, pages = [], curPage = null, curIdx = 0;
  var cv = $("portrait"), cx = cv.getContext("2d"), mapEl = $("map");

  function finding() { return curPage.findings[curIdx]; }

  function renderCrumb(s) {
    if (editing !== s.id) $("crumbName").innerHTML = nameHTML(s, false);
    var st = s.status, last = L.lastRun(s), n = s.runs.length;
    $("crumbRun").innerHTML =
      st.state === "running" ? "run " + (n + 1) + " · " + ago(st.started_at, "started ")
      : st.state === "queued" ? "run " + (n + 1) + " · " + ago(st.queued_at, "queued ")
      : st.state === "failed" ? "attempt failed " + esc(L.fmtAbs(st.finished_at)) +
          (last ? " · showing run " + n + " of " + esc(L.fmtAbs(last.at)) : "")
      : last ? "run " + n + " of " + n + " · " + esc(L.fmtAbs(last.at)) + " · " + ago(last.at)
      : "never run";
    var busy = st.state === "running" || st.state === "queued";
    [["exportLink", "jsonl"], ["exportSarif", "sarif"]].forEach(function (pair) {
      var link = $(pair[0]);
      link.hidden = !last;
      if (!last) return;
      link.href = "/api/scans/" + encodeURIComponent(s.id) + "/findings." + pair[1] +
                  "?run=" + encodeURIComponent(last.id);
      link.setAttribute("download", s.id + "-" + last.id + "." + pair[1]);
    });
    $("rerun").disabled = busy;
    $("rerun").textContent = busy ? "Scanning…" : "Re-run";
  }

  function renderDetail(s) {
    renderCrumb(s);
    var st = s.status, busy = st.state === "running" || st.state === "queued";
    var failed = st.state === "failed", last = L.lastRun(s);
    $("notice").hidden = !(busy || failed || !last);
    $("notice").className = "panel notice" + (failed ? " failed" : "");
    $("reportBody").hidden = busy || !last;

    if (busy || !last) {
      shownRun = null;
      $("sub").innerHTML = "Checking " +
        (s.docs.length ? L.andList(s.docs.map(function (u) { return "<b>" + esc(L.shortSrc(u)) + "</b>"; })) : "the repository docs") +
        (s.repos.length ? " against " + L.andList(s.repos.map(function (u) { return "<b>" + esc(L.shortSrc(u)) + "</b>"; })) : "") +
        (busy ? " &hellip;" : ".");
      $("ribbon").innerHTML = tile("Pages", "—") + tile("With drift", "—") + tile("Findings", "—") +
                              tile("Worst gap", "—") + tile("Versions", "—");
      $("foot").innerHTML = esc(s.docs.concat(s.repos).join(" · "));
    }
    if (busy) return renderRunning(s);
    if (failed || !last) renderFailed(s);
    if (last && shownRun !== last.id) {
      shownRun = last.id;
      loadReport(s, last).then(function (rep) {
        if (current === s.id) renderReport(rep);
      }).catch(function () {
        $("notice").hidden = false;
        $("noticeTitle").textContent = "COULDN'T LOAD THIS RUN'S REPORT";
      });
    }
  }

  function renderRunning(s) {
    var st = s.status, last = L.lastRun(s);
    $("noticeHd").textContent = st.state === "queued" ? "Queued" : "Scan in progress";
    $("noticeNote").innerHTML = st.state === "queued" ? ago(st.queued_at, "queued ") : ago(st.started_at, "started ");
    $("noticeTitle").textContent = st.state === "queued"
      ? "QUEUED — WAITING FOR THE SCAN AHEAD"
      : "SCANNING — " + L.PHASES[st.phase].toUpperCase();
    $("noticePhases").innerHTML = phasesHTML(st, true);
    $("noticeDesc").innerHTML = "Findings, the site map and the release timeline appear here when the run finishes. " +
      (last ? "The previous run found <b>" + plural(last.stats.findings, "finding") + "</b>." : "This is the first run.");
    buildLog(st.log, $("liveLog"), true);
  }

  function renderFailed(s) {
    var st = s.status, last = L.lastRun(s);
    var never = st.state !== "failed";
    $("noticeHd").textContent = never ? "Not run yet" : "Last attempt";
    $("noticeNote").textContent = never ? "" : L.fmtAbs(st.finished_at);
    $("noticeTitle").textContent = never ? "NO RUNS YET" : "SCAN FAILED — NOTHING WAS VERIFIED";
    $("noticePhases").innerHTML = "";
    $("noticeDesc").innerHTML = never ? "Press <b>Re-run</b> to scan it."
      : esc(st.error || "The scan failed.") +
        (last ? " Below is the last good run, from <b>" + esc(L.fmtAbs(last.at)) + "</b>." : "");
    buildLog(st.log, $("liveLog"), false);
  }

  /* Poll result for the scan on screen. */
  function syncDetail(before) {
    var s = byId(current);
    if (!s) { location.hash = "#/"; return; }
    var was = before.filter(function (b) { return b.id === s.id; })[0];
    if (!was || signature(was) !== signature(s)) {
      renderDetail(s);
      if (was && was.status.state === "running" && s.status.state !== "running") {
        announce(s.name + (s.status.state === "done" ? " finished." : " failed."));
      }
    }
    signatures[s.id] = signature(s);
  }

  function renderReport(data) {
    report = data;
    pages = [];
    data.sections.forEach(function (s) {
      s.pages.forEach(function (p) { p.section = s.name; pages.push(p); });
    });
    if (!pages.length) return;

    var st = data.stats;
    var docs = data.docs_urls.map(function (u) { return "<b>" + esc(L.shortSrc(u)) + "</b>"; });
    var pkgs = data.packages.map(function (p) { return "<b>" + esc(p.package) + "</b>"; });
    $("sub").innerHTML =
      "Every claim in " + (docs.length ? L.andList(docs) : "<b>the repository docs</b>") +
      " held up against what " + L.andList(pkgs) + " actually " + (pkgs.length > 1 ? "do" : "does") +
      " &mdash; side by side, and dated.";

    $("ribbon").innerHTML =
      tile("Pages", st.pages) +
      tile("With drift", st.drifted, "alarm") +
      tile("Findings", st.findings, "alarm") +
      tile("Worst gap", st.worst_gap, "gold", "d") +
      tile("Versions", st.versions);

    $("strip").innerHTML =
      "<span><b>" + st.pages + "</b> pages crawled</span>" +
      "<span><b>" + st.snippets + "</b> snippets</span>" +
      "<span><b>" + (st.versions * countChains()) + "</b> sandboxes</span>" +
      "<span class='ok'>0 leaked</span>";

    $("sources").innerHTML = data.sources.map(function (src) {
      var label = esc(L.shortSrc(src.url));
      return '<li class="' + src.kind + '"><span class="src-tag ' + src.kind + '">' + src.kind.toUpperCase() + "</span>" +
        (L.isHttp(src.url)
          ? '<a class="src-val" href="' + esc(src.url) + '" target="_blank" rel="noopener noreferrer" title="' + esc(src.url) + '">' + label + "</a>"
          : '<span class="src-val" title="' + esc(src.url) + '">' + label + "</span>") +
        '<span class="src-n">' + plural(src.pages, "page") + "</span></li>";
    }).join("");

    $("runStamp").textContent = data.strategy + " · " + String(data.generated_at).replace("T", " ");

    $("foot").innerHTML =
      data.docs_urls.map(esc).join(" &middot; ") +
      data.packages.map(function (p) {
        return p.repo_url ? " &middot; " + esc(p.repo_url) + (p.commit ? " @ " + esc(p.branch) + " " + esc(p.commit) : "") : "";
      }).join("") +
      "<br>scan took " + esc(data.elapsed) + "s &middot; generated " + esc(data.generated_at);

    var hints = Object.keys(data.impact || {}).sort(function (a, b) {
      return (data.impact[b].length - data.impact[a].length) || a.localeCompare(b);
    });
    $("sym").value = hints[0] || "";
    $("impactHint").textContent = hints.length ? "Also indexed: " + hints.slice(1, 7).join(" · ") : "";

    buildLog(data.log || [], $("log"), true);
    buildMap();
    buildTimeline();
    selectPage(worstPage());
    runImpact();
  }

  function countChains() {
    var n = 0;
    pages.forEach(function (p) { if (p.sev > 0 || p.st === "pass") n++; });
    return Math.max(n, 1);
  }
  function worstPage() {
    var best = pages[0];
    pages.forEach(function (p) { if (p.sev > best.sev) best = p; });
    return best.id;
  }

  function drawPortrait(t) {
    cx.fillStyle = "#0a0710"; cx.fillRect(0, 0, 19, 19);
    cx.fillStyle = "#1a1426"; cx.fillRect(2, 2, 15, 15);
    cx.fillStyle = "#141021"; cx.fillRect(2, 2, 15, 2);
    var f = curPage && finding();
    var s = SPRITES[f && f.sprite];
    if (s) {
      var bob = reduce ? 0 : (Math.sin(t / 320) > 0 ? 0 : 1);
      for (var r = 0; r < s.length; r++) {
        for (var c = 0; c < s[r].length; c++) {
          var ch = s[r][c];
          if (ch === ".") continue;
          cx.fillStyle = PAL[ch] || "#fff";
          cx.fillRect(4 + c, 3 + r + bob, 1, 1);
        }
      }
    } else {
      cx.fillStyle = "#f0a830"; cx.fillRect(9, 7, 1, 4);
      cx.fillStyle = "#ffd257"; cx.fillRect(9, 6, 1, 2);
      cx.fillStyle = "rgba(240,168,48,.12)"; cx.fillRect(5, 4, 9, 10);
    }
  }
  function portraitLoop(t) {
    if (current && !$("reportBody").hidden) drawPortrait(t);
    requestAnimationFrame(portraitLoop);
  }

  function slabHTML(parts) {
    return (parts || []).map(function (p) {
      return '<span class="' + esc(p[1]) + '">' + esc(p[0]) + "</span>";
    }).join("");
  }

  function renderMeter(days) {
    var SEG = 24, filled = Math.min(SEG, Math.round(days / 7)), html = "";
    for (var i = 0; i < SEG; i++) {
      html += '<i class="' + (i < filled ? (i < 4 ? "fresh" : "stale") : "") + '"></i>';
    }
    $("segs").innerHTML = html;
    var big = $("gapBig");
    if (!days) { big.textContent = "IN STEP — 0 DAYS"; big.className = "big ok"; }
    else { big.textContent = days + " DAYS BEHIND"; big.className = "big"; }
  }

  function renderNav() {
    var n = curPage.findings.length;
    $("counter").innerHTML = "finding <b>" + (curIdx + 1) + "</b> of <b>" + n + "</b>";
    $("prev").disabled = n < 2;
    $("next").disabled = n < 2;
    $("findList").innerHTML = curPage.findings.map(function (f, i) {
      var clean = f.kind === "clean" || f.kind === "prose" || f.kind === "blocked";
      return '<li><button class="find-chip" type="button" data-i="' + i +
             '" data-sev="' + (clean ? "clean" : "drift") +
             '" aria-pressed="' + (i === curIdx) + '"><span class="num">' +
             (i + 1) + "</span>" + esc(f.label) + "</button></li>";
    }).join("");
  }

  function renderFinding() {
    var f = finding();
    $("docSlab").innerHTML = slabHTML(f.doc_parts);
    $("codeSlab").innerHTML = slabHTML(f.code_parts);
    $("docLink").href = L.isHttp(f.doc_url) ? f.doc_url : "#";
    $("docWhere").textContent = f.doc_where;
    $("codeLink").href = L.isHttp(f.code_url) ? f.code_url : "#";
    $("codeWhere").textContent = f.code_where;
    $("docStampLbl").textContent = f.doc_stamp_label;
    $("docStamp").textContent = f.doc_stamp;
    $("codeStampLbl").textContent = f.code_stamp_label;
    $("codeStamp").textContent = f.code_stamp;

    var dmg = $("damage");
    dmg.textContent = f.damage;
    dmg.className = "damage" +
      (f.damage === "NO DAMAGE" || f.damage === "NO TARGET" ? " none" :
       f.damage === "NOT COUNTED" ? " hold" : "");

    renderMeter(f.days_behind || 0);

    var soft = f.kind === "clean" || f.kind === "prose";
    var t = $("findTitle");
    t.textContent = f.title;
    t.className = "find-title" + (soft ? " clear" : f.kind === "blocked" ? " hold" : "");
    // only <b> and <code> survive: everything else in a finding is untrusted
    $("findDesc").innerHTML = L.richText(f.desc);

    var ev = $("evidence");
    ev.textContent = f.evidence;
    ev.className = "evidence" + (soft ? " ok" : f.kind === "blocked" ? " hold" : "");

    $("chips").innerHTML = (f.chips || []).map(function (c) {
      return '<li class="chip ' + esc(c[0]) + '">' + esc(c[1]) + "</li>";
    }).join("");

    renderNav();
    if (reduce) drawPortrait(0);
  }

  function goFinding(i) {
    var n = curPage.findings.length;
    curIdx = ((i % n) + n) % n;
    renderFinding();
  }

  function selectPage(id) {
    var p = pages.filter(function (x) { return x.id === id; })[0];
    if (!p) return;
    curPage = p; curIdx = 0;
    var sec = report.sections.filter(function (s) { return s.name === p.section; })[0];
    var idx = sec ? sec.pages.indexOf(p) : 0;
    $("where").textContent = p.section + " · page " + (idx + 1) + " of " + (sec ? sec.pages.length : 1);
    Array.prototype.forEach.call(mapEl.querySelectorAll(".tile"), function (b) {
      b.setAttribute("aria-current", b.getAttribute("data-id") === id ? "true" : "false");
    });
    renderFinding();
  }

  function buildMap() {
    mapEl.innerHTML = report.sections.map(function (s) {
      // blocked and clean entries share a page's findings list; count only drift
      var n = s.pages.reduce(function (a, p) {
        return a + p.findings.filter(function (f) { return DRIFT_KINDS.indexOf(f.kind) > -1; }).length;
      }, 0);
      var tiles = s.pages.map(function (p) {
        var pips = "";
        if (p.sev > 0) {
          pips = '<span class="pips" aria-hidden="true">';
          for (var i = 0; i < 3; i++) pips += '<i class="' + (i < p.sev ? "" : "off") + '"></i>';
          pips += "</span>";
        }
        return '<button class="tile" type="button" data-id="' + esc(p.id) + '" data-st="' +
               esc(p.st) + '" title="' + esc(p.name) + '" aria-label="' + esc(p.name) +
               " — " + p.findings.length + ' finding(s)"><span aria-hidden="true">' +
               esc(p.tag) + "</span>" + pips + "</button>";
      }).join("");
      return '<div class="sect"><p class="sect-lbl">' + esc(s.name) + "<span>" +
             (n ? n + " finding" + (n === 1 ? "" : "s") : "clean") +
             '</span></p><div class="tiles">' + tiles + "</div></div>";
    }).join("");
  }

  function buildTimeline() {
    var groups = L.timelineGroups(report), multi = report.packages.length > 1;
    $("tlBody").innerHTML = groups.map(function (g) {
      var vs = g.versions;
      var cols = "212px repeat(" + Math.max(vs.length, 1) + ",1fr) 178px";
      var head = '<div class="tl-row tl-head" style="grid-template-columns:' + cols + '">' +
        '<span class="hcap">' + (multi ? esc(g.name) : "Page") + "</span>" + vs.map(function (v) {
          return '<span class="rel">' + esc(v.label) +
                 '<span class="date">' + esc((v.released_at || "").slice(5)) + "</span></span>";
        }).join("") + '<span class="hcap" style="padding-left:12px">Verdict</span></div>';
      return head + g.rows.map(function (row) {
        var cells = row.c.map(function (state, i) {
          var brk = state === "fail" && i > 0 && row.c[i - 1] === "pass";
          var glyph = state === "fail" ? "✗" : state === "na" ? "—" : "";
          return '<span class="life ' + esc(state) + (brk ? " brk" : "") +
                 (row.pin === i ? " pin" : "") + '">' + glyph + "</span>";
        }).join("");
        var vcls = row.ok ? " ok" : row.hold ? " hold" : "";
        return '<div class="tl-row' + (row.pin != null ? " pinned" : "") +
               '" style="grid-template-columns:' + cols + '">' +
               '<span class="tl-name">' + esc(row.n) + "<small>" + esc(row.p) +
               "</small></span>" + cells +
               '<span class="tl-verdict' + vcls + '">' + esc(row.v) + "</span></div>";
      }).join("");
    }).join("");
  }

  function buildLog(lines, el, cursor) {
    el.innerHTML = (lines || []).map(function (l) {
      return '<p class="' + esc(l[0]) + '">' + esc(l[1]) + "</p>";
    }).join("") + (cursor ? '<p class="t-sys">&rsaquo; <span class="cursor">&#9608;</span></p>' : "");
    el.scrollTop = el.scrollHeight;
  }

  function runImpact() {
    var raw = $("sym").value.trim();
    var key = raw.replace(/^\w+\./, "").replace(/\(\)$/, "").toLowerCase();
    var hits = (report.impact || {})[key] || (report.impact || {})[raw.toLowerCase()] || [];
    var out = $("impactOut");

    Array.prototype.forEach.call(mapEl.querySelectorAll(".tile"), function (b) {
      b.classList.toggle("lit", hits.indexOf(b.getAttribute("data-id")) > -1);
    });

    if (!raw) { out.innerHTML = "Type a symbol name to see what breaks."; return; }
    if (!hits.length) {
      out.innerHTML = "No page references <b>" + esc(raw) + "</b>. Safe to rename.";
      return;
    }
    out.innerHTML = "Renaming <b>" + esc(raw) + "</b> breaks <b>" + hits.length +
      "</b> page" + (hits.length === 1 ? "" : "s") + ", highlighted on the map:<ol>" +
      hits.map(function (id) {
        var p = pages.filter(function (x) { return x.id === id; })[0];
        return "<li>" + esc(p ? p.name : id) + "</li>";
      }).join("") + "</ol>";
  }

  $("prev").addEventListener("click", function () { goFinding(curIdx - 1); });
  $("next").addEventListener("click", function () { goFinding(curIdx + 1); });
  $("findList").addEventListener("click", function (e) {
    var b = e.target.closest(".find-chip");
    if (b) goFinding(parseInt(b.getAttribute("data-i"), 10));
  });
  mapEl.addEventListener("click", function (e) {
    var b = e.target.closest(".tile");
    if (b) selectPage(b.getAttribute("data-id"));
  });
  document.addEventListener("keydown", function (e) {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || $("dlg").open || $("importDlg").open) return;
    if (!current || !curPage || $("reportBody").hidden) return;
    if (e.key === "ArrowRight") { e.preventDefault(); goFinding(curIdx + 1); }
    if (e.key === "ArrowLeft") { e.preventDefault(); goFinding(curIdx - 1); }
  });
  $("check").addEventListener("click", runImpact);
  $("sym").addEventListener("keydown", function (e) { if (e.key === "Enter") runImpact(); });

  $("rerun").addEventListener("click", function () {
    var s = byId(current);
    if (!s) return;
    $("rerun").disabled = true;
    api("POST", "/api/scans/" + encodeURIComponent(s.id) + "/runs").then(function (view) {
      upsert(view);
      renderDetail(view);
      announce("Re-running " + view.name + ".");
      refresh();
    }).catch(function (err) {
      announce(err.messages.join(" "));
      refresh();
    });
  });

  /* ============================================================== rename */

  function beginEdit(btn) {
    var s = byId(btn.getAttribute("data-edit")), holder = btn.parentNode;
    if (!s) return;
    var inCrumb = holder.id === "crumbName";
    editing = s.id;
    var inp = document.createElement("input");
    inp.className = "name-input"; inp.type = "text"; inp.maxLength = 60; inp.spellcheck = false;
    inp.value = s.name; inp.setAttribute("aria-label", "Scan name - Enter to save, Escape to cancel");
    holder.innerHTML = "";
    holder.appendChild(inp);
    inp.focus(); inp.select();

    var ended = false;
    function restore(refocus) {
      editing = null;
      var fresh = byId(s.id) || s;
      renderCard(fresh);
      if (current === s.id) { renderCrumb(fresh); document.title = fresh.name + " · Doc Rot Crawler"; }
      var again = (inCrumb ? $("crumbName") : $("card-" + s.id));
      again = again && again.querySelector("[data-edit]");
      if (again && refocus) again.focus();
    }
    function end(save, refocus) {
      if (ended) return;
      ended = true;
      var v = inp.value.trim().replace(/\s+/g, " ");
      if (!save || !v || v === s.name) return restore(refocus);
      s.name = v;                               // optimistic
      restore(refocus);
      api("PATCH", "/api/scans/" + encodeURIComponent(s.id), { name: v }).then(function (view) {
        upsert(view);
        announce("Renamed to " + view.name);
      }).catch(function (err) {
        announce("Rename failed: " + err.messages.join(" "));
        refresh();
      });
    }
    inp.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); end(true, true); }
      if (e.key === "Escape") { e.preventDefault(); end(false, true); }
    });
    inp.addEventListener("blur", function () { end(true, false); });
  }

  document.addEventListener("click", function (e) {
    var ed = e.target.closest("[data-edit]");
    if (ed) { e.preventDefault(); beginEdit(ed); return; }
    if (e.target.closest("[data-new]")) openDialog();
    if (e.target.closest("[data-import]")) openImport();
  });

  /* ============================================================ new scan */

  var dlg = $("dlg"), form = $("scanForm");

  function rowsOf(kind) { return Array.prototype.slice.call($(kind + "Rows").querySelectorAll(".inp")); }

  function addRow(kind, value, focus) {
    var li = document.createElement("li");
    li.className = "src-row";
    li.innerHTML = '<span class="src-tag ' + kind + '" aria-hidden="true">' + kind.toUpperCase() + "</span>" +
      '<input class="inp" type="' + (kind === "docs" ? "url" : "text") + '" name="' + kind +
      '" spellcheck="false" autocomplete="off" placeholder="' +
      (kind === "docs" ? "https://docs.example.com" : "https://github.com/org/repo") + '">' +
      '<button class="rm" type="button" data-rm>&#10005;</button>';
    li.querySelector(".inp").value = value || "";
    $(kind + "Rows").appendChild(li);
    relabel(kind);
    if (focus) li.querySelector(".inp").focus();
  }

  function relabel(kind) {
    var rows = $(kind + "Rows").children, noun = kind === "docs" ? "Docs URL" : "Repository";
    Array.prototype.forEach.call(rows, function (li, i) {
      li.querySelector(".inp").setAttribute("aria-label", noun + " " + (i + 1));
      var rm = li.querySelector(".rm");
      rm.setAttribute("aria-label", "Remove " + noun.toLowerCase() + " " + (i + 1));
      rm.disabled = rows.length === 1;
    });
  }

  function openDialog() {
    form.reset();
    $("docsRows").innerHTML = ""; $("repoRows").innerHTML = "";
    addRow("docs"); addRow("repo");
    $("formErr").textContent = "";
    form.querySelector(".opts").open = false;
    $("submitScan").disabled = false;
    dlg.showModal();
    rowsOf("docs")[0].focus();
  }

  form.addEventListener("click", function (e) {
    var add = e.target.closest("[data-add]");
    if (add) { addRow(add.getAttribute("data-add"), "", true); return; }
    var rm = e.target.closest("[data-rm]");
    if (rm && !rm.disabled) {
      var li = rm.parentNode, list = li.parentNode, kind = list.id === "docsRows" ? "docs" : "repo";
      var next = li.nextElementSibling || li.previousElementSibling;
      list.removeChild(li);
      relabel(kind);
      if (next) next.querySelector(".inp").focus();
    }
  });

  form.addEventListener("paste", function (e) {
    var inp = e.target;
    if (!inp.matches(".src-row .inp")) return;
    var parts = (e.clipboardData || window.clipboardData).getData("text").split(/[\s,]+/).filter(Boolean);
    if (parts.length < 2) return;
    e.preventDefault();
    inp.value = parts[0];
    parts.slice(1).forEach(function (p) { addRow(inp.name, p); });
    relabel(inp.name);
  });

  form.addEventListener("input", function (e) {
    if (e.target.getAttribute("aria-invalid") === "true") e.target.removeAttribute("aria-invalid");
  });

  function showErrors(messages, focusEl) {
    $("formErr").innerHTML = messages.map(esc).join("<br>");
    if (focusEl) focusEl.focus();
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var docInputs = rowsOf("docs"), repoInputs = rowsOf("repo");
    var checked = L.checkSources(docInputs.map(function (i) { return i.value; }),
                                 repoInputs.map(function (i) { return i.value; }));
    docInputs.concat(repoInputs).forEach(function (i) { i.value = i.value.trim(); i.removeAttribute("aria-invalid"); });
    checked.invalid.docs.forEach(function (i) { docInputs[i].setAttribute("aria-invalid", "true"); });
    checked.invalid.repos.forEach(function (i) { repoInputs[i].setAttribute("aria-invalid", "true"); });
    if (checked.errors.length) {
      var bad = form.querySelector('[aria-invalid="true"]') || docInputs[0];
      return showErrors(checked.errors, bad);
    }

    var maxPages = parseInt($("fMax").value, 10);
    var body = {
      name: $("fName").value.trim(),
      docs: checked.docs, repos: checked.repos,
      options: {
        ecosystem: $("fEco").value,
        versions: parseInt($("fVers").value, 10) || 3,
        max_pages: maxPages > 0 ? maxPages : null,
        assume_credentialed: $("fCred").checked
      }
    };
    $("submitScan").disabled = true;
    api("POST", "/api/scans", body).then(function (view) {
      upsert(view);
      dlg.close();
      if (current) location.hash = "#/";
      renderList();
      renderFleet();
      var link = document.querySelector("#card-" + CSS.escape(view.id) + " .card-link");
      if (link) link.focus();
      announce("Scan queued: " + view.name + ", " + plural(view.docs.length + view.repos.length, "source") + ".");
      refresh();
    }).catch(function (err) {
      $("submitScan").disabled = false;
      showErrors(err.messages, null);
    });
  });

  $("cancel").addEventListener("click", function () { dlg.close(); });
  dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });

  /* ============================================================== import */

  var importDlg = $("importDlg"), importForm = $("importForm");

  function openImport() {
    importForm.reset();
    $("importErr").textContent = "";
    $("importSubmit").disabled = false;
    importDlg.showModal();
    $("fFile").focus();
  }

  function importFailed(messages) {
    $("importErr").innerHTML = messages.map(esc).join("<br>");
    $("importSubmit").disabled = false;
  }

  importForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var file = $("fFile").files[0];
    if (!file) return importFailed(["Choose a .jsonl or .sarif file to import."]);

    $("importSubmit").disabled = true;
    $("importErr").textContent = "";
    var reader = new FileReader();
    reader.onerror = function () { importFailed(["That file could not be read."]); };
    reader.onload = function () {
      var query = "?filename=" + encodeURIComponent(file.name) +
                  "&name=" + encodeURIComponent($("fImportName").value.trim());
      postFile("/api/imports" + query, String(reader.result)).then(function (view) {
        upsert(view);
        importDlg.close();
        announce("Imported " + view.name + " from " + file.name + ".");
        location.hash = "#/scan/" + view.id;
        refresh();
      }).catch(function (err) {
        importFailed(err.messages);
      });
    };
    reader.readAsText(file);
  });

  $("importCancel").addEventListener("click", function () { importDlg.close(); });
  importDlg.addEventListener("click", function (e) { if (e.target === importDlg) importDlg.close(); });

  /* ============================================================== router */

  function route() {
    var m = location.hash.match(/^#\/scan\/([\w-]+)/), s = m && byId(m[1]);
    if (s) {
      current = s.id;
      shownRun = null;
      $("viewList").hidden = true;
      $("viewDetail").hidden = false;
      document.title = s.name + " · Doc Rot Crawler";
      renderDetail(s);
      signatures[s.id] = signature(s);
      window.scrollTo(0, 0);
      $("crumbName").focus({ preventScroll: true });
    } else {
      var back = current;
      current = null;
      $("viewDetail").hidden = true;
      $("viewList").hidden = false;
      document.title = "Scans · Doc Rot Crawler";
      renderList();
      renderFleet();
      var link = back && document.querySelector("#card-" + CSS.escape(back) + " .card-link");
      if (link) { link.focus({ preventScroll: true }); link.scrollIntoView({ block: "center" }); }
    }
  }

  window.addEventListener("hashchange", function () { if (loaded) route(); });
  setInterval(function () {
    Array.prototype.forEach.call(document.querySelectorAll("time[data-ago]"), function (t) {
      t.textContent = t.getAttribute("data-prefix") + L.fmtAgo(t.getAttribute("data-ago"));
    });
    renderFleet();
  }, 30000);

  refresh();
  if (reduce) drawPortrait(0); else requestAnimationFrame(portraitLoop);
})();
