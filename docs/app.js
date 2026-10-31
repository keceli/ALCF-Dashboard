/* ALCF Dashboard: renders the snapshot written by collector/alcf_dashboard.py. */
(function () {
  "use strict";

  var DATA_URL = "data/dashboard.json";
  var SAMPLE_URL = "data/sample.json";
  var REFRESH_MS = 5 * 60 * 1000;
  var STATE_ORDER = ["active", "queued", "held", "new", "unknown", "failed", "canceled", "completed"];

  var current = null;

  // ---------------------------------------------------------------- helpers

  function $(id) { return document.getElementById(id); }

  // Build DOM nodes; all text goes through textContent so data cannot inject HTML.
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (attrs[k] === null || attrs[k] === undefined) return;
        if (k === "text") el.textContent = attrs[k];
        else if (k === "class") el.className = attrs[k];
        else if (k === "style") el.setAttribute("style", attrs[k]);
        else el.setAttribute(k, attrs[k]);
      });
    }
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }

  function append(el, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(el, c); }); return; }
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

  function cssToken(value) { return String(value || "").toLowerCase().replace(/[^a-z0-9_-]/g, ""); }

  function badge(value) { return h("span", { "class": "badge " + cssToken(value), text: value || "unknown" }); }

  function fmtNumber(n, digits) {
    if (n === null || n === undefined || n === "") return "–";
    return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits === undefined ? 1 : digits });
  }

  function fmtBytes(n) {
    if (n === null || n === undefined) return "–";
    var units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"];
    var i = 0;
    n = Number(n);
    while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i === 0 ? 0 : 1) + " " + units[i];
  }

  function fmtAmount(n, unit) {
    if (unit === "bytes") return fmtBytes(n);
    if (unit === "node_hours") return fmtNumber(n, 0) + " node-h";
    return fmtNumber(n, 0) + (unit && unit !== "inodes" ? " " + unit : unit === "inodes" ? " files" : "");
  }

  function fmtDuration(s) {
    if (s === null || s === undefined) return "–";
    s = Math.round(Number(s));
    var d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), mm = Math.floor((s % 3600) / 60);
    var core = String(hh).padStart(2, "0") + ":" + String(mm).padStart(2, "0");
    return d ? d + "d " + core : core;
  }

  function parseTime(t) {
    if (!t) return null;
    var d = new Date(t);
    return isNaN(d.getTime()) ? null : d;
  }

  function fmtTime(t) {
    var d = parseTime(t);
    if (!d) return t || "–";
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function fmtAgo(t) {
    var d = parseTime(t);
    if (!d) return "never";
    var s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }

  function pct(used, total) {
    if (!total || used === null || used === undefined) return null;
    return Math.max(0, (100 * Number(used)) / Number(total));
  }

  function bar(used, total, userUsed) {
    var p = pct(used, total);
    var level = p === null ? "" : p >= 90 ? " bad" : p >= 75 ? " warn" : "";
    var el = h("div", { "class": "bar" + level, title: p === null ? "no limit" : p.toFixed(1) + "% used" },
      h("span", { style: "width:" + Math.min(100, p || 0).toFixed(1) + "%" }));
    var up = pct(userUsed, total);
    if (up !== null) el.appendChild(h("span", { "class": "user", style: "width:" + Math.min(100, up).toFixed(1) + "%" }));
    return el;
  }

  function table(el, columns, rows, emptyText) {
    clear(el);
    if (!rows.length) {
      el.appendChild(h("tbody", null, h("tr", null, h("td", { "class": "empty", text: emptyText }))));
      return;
    }
    el.appendChild(h("thead", null, h("tr", null, columns.map(function (c) {
      return h("th", { "class": c.num ? "num" : null, text: c.title });
    }))));
    el.appendChild(h("tbody", null, rows.map(function (row) {
      return h("tr", null, columns.map(function (c) {
        var v = c.render(row);
        return h("td", { "class": c.num ? "num" : c.wrap ? "wrap" : null, title: c.tip ? c.tip(row) : null }, v);
      }));
    })));
  }

  // ------------------------------------------------------------ data model

  // The same job can be reported by the IRI API and by qstat over SSH; merge them.
  function mergeJobs(jobs) {
    var byKey = {};
    var order = [];
    jobs.forEach(function (job) {
      var key = (job.machine || "") + "/" + String(job.id).split(".")[0];
      if (!byKey[key]) { byKey[key] = Object.assign({ sources: [] }, job); order.push(key); }
      var merged = byKey[key];
      Object.keys(job).forEach(function (k) {
        if (merged[k] === null || merged[k] === undefined || merged[k] === "unknown") merged[k] = job[k];
      });
      if (merged.sources.indexOf(job.source) < 0) merged.sources.push(job.source);
    });
    return order.map(function (k) { return byKey[k]; });
  }

  function stateRank(state) {
    var i = STATE_ORDER.indexOf(state);
    return i < 0 ? STATE_ORDER.length : i;
  }

  function machinesOf(data) {
    var set = {};
    (data.jobs || []).forEach(function (j) { if (j.machine) set[j.machine] = true; });
    (data.quotas || []).forEach(function (q) { if (q.machine) set[q.machine] = true; });
    Object.keys(data.sources || {}).forEach(function (s) { if (s.indexOf("ssh:") === 0) set[s.slice(4)] = true; });
    ["aurora", "polaris", "crux"].forEach(function (m) { set[m] = true; });
    return Object.keys(set).sort();
  }

  function resourceFor(data, machine) {
    var found = null;
    (data.resources || []).forEach(function (r) {
      var name = String(r.name || "").toLowerCase();
      if (!found && name.indexOf(machine) >= 0 && (r.endpoints || []).indexOf("compute") >= 0) found = r;
    });
    (data.resources || []).forEach(function (r) {
      if (!found && String(r.name || "").toLowerCase() === machine) found = r;
    });
    return found;
  }

  // --------------------------------------------------------------- render

  function render(data, isSample) {
    current = data;
    data.jobs = mergeJobs(data.jobs || []);

    $("meta-user").textContent = data.user ? "User: " + data.user : "";
    $("meta-updated").textContent = "Snapshot: " + fmtTime(data.generated_at) + " (" + fmtAgo(data.generated_at) + ")";
    $("meta-updated").title = data.generated_at || "";

    var banner = $("banner");
    var failed = Object.keys(data.sources || {}).filter(function (k) { return !data.sources[k].ok; });
    if (isSample) {
      banner.hidden = false;
      banner.textContent = "Showing sample data. Run collector/alcf_dashboard.py to generate docs/data/dashboard.json " +
        "(see README) and publish it to replace this demo.";
    } else if (failed.length) {
      banner.hidden = false;
      banner.textContent = "Some data sources failed during the last collection: " + failed.join(", ") +
        ". Data from those sources may be missing or stale — see Data sources below.";
    } else {
      banner.hidden = true;
    }

    renderSummary(data);
    renderSystems(data);
    renderJobFilters(data);
    renderJobs();
    renderProjects(data);
    renderQuotas(data);
    renderSources(data);
  }

  function renderSummary(data) {
    var jobs = data.jobs;
    var count = function (s) { return jobs.filter(function (j) { return j.state === s; }).length; };
    var nodes = jobs.filter(function (j) { return j.state === "active"; })
      .reduce(function (a, j) { return a + (Number(j.nodes) || 0); }, 0);
    var compute = [];
    (data.projects || []).forEach(function (p) {
      (p.allocations || []).forEach(function (a) { if (a.unit === "node_hours") compute.push(a); });
    });
    var lowest = null;
    compute.forEach(function (a) {
      var left = Number(a.allocation) - Number(a.usage);
      if (a.allocation && (lowest === null || left / a.allocation < lowest.frac)) lowest = { a: a, left: left, frac: left / a.allocation };
    });
    var fullest = null;
    (data.quotas || []).forEach(function (q) {
      var p = pct(q.used_bytes, q.quota_bytes);
      if (p !== null && (fullest === null || p > fullest.p)) fullest = { q: q, p: p };
    });

    var cards = [
      { label: "Running jobs", value: count("active"), detail: fmtNumber(nodes, 0) + " nodes in use" },
      { label: "Queued / held", value: count("queued") + count("held") + count("new"), detail: count("held") + " held" },
      { label: "Projects", value: (data.projects || []).length, detail: compute.length + " compute allocations" },
      lowest ? { label: "Lowest balance", value: Math.round(100 * lowest.frac) + "%",
        detail: fmtAmount(lowest.left, "node_hours") + " left on " + (lowest.a.capability || "?") } : null,
      fullest ? { label: "Fullest quota", value: Math.round(fullest.p) + "%",
        detail: fullest.q.name + " on " + fullest.q.filesystem + " (" + fullest.q.machine + ")" } : null
    ];
    var el = clear($("summary"));
    cards.forEach(function (c) {
      if (!c) return;
      el.appendChild(h("div", { "class": "card" },
        h("div", { "class": "label", text: c.label }),
        h("div", { "class": "value", text: String(c.value) }),
        h("div", { "class": "detail", text: c.detail })));
    });
  }

  function renderSystems(data) {
    var el = clear($("systems"));
    machinesOf(data).forEach(function (m) {
      var res = resourceFor(data, m);
      var jobs = data.jobs.filter(function (j) { return j.machine === m; });
      var running = jobs.filter(function (j) { return j.state === "active"; }).length;
      var queued = jobs.filter(function (j) { return ["queued", "held", "new"].indexOf(j.state) >= 0; }).length;
      var ssh = (data.sources || {})["ssh:" + m];
      el.appendChild(h("div", { "class": "card" },
        h("div", { "class": "label", text: m }),
        h("div", null, res ? badge(res.status) : h("span", { "class": "muted", text: "status n/a" })),
        h("div", { "class": "detail", text: running + " running · " + queued + " queued" }),
        ssh ? h("div", { "class": "detail", text: "ssh: " + (ssh.ok ? "ok" : "failed") + " " + fmtAgo(ssh.collected_at) }) : null));
    });

    var inc = clear($("incidents"));
    (data.incidents || []).forEach(function (i) {
      inc.appendChild(h("div", { "class": "incident " + cssToken(i.status) },
        h("strong", { text: i.name || i.id }), " ", badge(i.status), " ",
        h("span", { "class": "muted", text: (i.type || "") + " · " + fmtTime(i.start) + " → " + (i.end ? fmtTime(i.end) : "ongoing") }),
        i.description ? h("div", { text: i.description }) : null));
    });
  }

  function renderJobFilters(data) {
    var fill = function (select, values) {
      var keep = select.value;
      while (select.options.length > 1) select.remove(1);
      values.forEach(function (v) { select.appendChild(h("option", { value: v, text: v })); });
      if (values.indexOf(keep) >= 0) select.value = keep;
    };
    fill($("filter-machine"), machinesOf(data));
    var states = {};
    data.jobs.forEach(function (j) { states[j.state] = true; });
    fill($("filter-state"), Object.keys(states).sort(function (a, b) { return stateRank(a) - stateRank(b); }));
  }

  function renderJobs() {
    if (!current) return;
    var machine = $("filter-machine").value;
    var state = $("filter-state").value;
    var text = $("filter-text").value.trim().toLowerCase();
    var rows = current.jobs.filter(function (j) {
      if (machine && j.machine !== machine) return false;
      if (state && j.state !== state) return false;
      if (text) {
        var hay = [j.id, j.name, j.project, j.queue, j.message].join(" ").toLowerCase();
        if (hay.indexOf(text) < 0) return false;
      }
      return true;
    }).sort(function (a, b) {
      return stateRank(a.state) - stateRank(b.state) || String(a.machine).localeCompare(String(b.machine)) ||
        String(b.id).localeCompare(String(a.id), undefined, { numeric: true });
    });

    table($("jobs"), [
      { title: "Machine", render: function (j) { return j.machine; } },
      { title: "Job ID", render: function (j) { return j.id; } },
      { title: "Name", render: function (j) { return j.name || "–"; } },
      { title: "State", render: function (j) { return badge(j.state); }, tip: function (j) { return j.message; } },
      { title: "Project", render: function (j) { return j.project || "–"; } },
      { title: "Queue", render: function (j) { return j.queue || "–"; } },
      { title: "Nodes", num: true, render: function (j) { return fmtNumber(j.nodes, 0); } },
      { title: "Walltime", num: true, render: function (j) {
        return j.used_walltime_s !== null && j.used_walltime_s !== undefined ?
          fmtDuration(j.used_walltime_s) + " / " + fmtDuration(j.walltime_s) : fmtDuration(j.walltime_s);
      } },
      { title: "Submitted", render: function (j) { return fmtTime(j.submitted); } },
      { title: "Start", render: function (j) {
        return j.started ? fmtTime(j.started) : j.estimated_start ? "est. " + fmtTime(j.estimated_start) : "–";
      } },
      { title: "Comment", wrap: true, render: function (j) { return j.message || ""; } },
      { title: "Source", render: function (j) { return (j.sources || [j.source]).join(", "); } }
    ], rows, current.jobs.length ? "No jobs match the filters." : "No jobs.");
  }

  function renderProjects(data) {
    var el = clear($("projects"));
    var projects = (data.projects || []).slice().sort(function (a, b) { return String(a.name).localeCompare(String(b.name)); });
    if (!projects.length) {
      el.appendChild(h("p", { "class": "empty", text: "No project data (requires the IRI API)." }));
      return;
    }
    projects.forEach(function (p) {
      var t = h("table");
      var rows = (p.allocations || []).slice().sort(function (a, b) {
        return (a.unit === "node_hours" ? 0 : 1) - (b.unit === "node_hours" ? 0 : 1);
      });
      table(t, [
        { title: "Resource", render: function (a) { return a.capability || "–"; } },
        { title: "Used", num: true, render: function (a) { return fmtAmount(a.usage, a.unit); } },
        { title: "Allocation", num: true, render: function (a) { return fmtAmount(a.allocation, a.unit); } },
        { title: "Remaining", num: true, render: function (a) {
          return a.allocation ? fmtAmount(Number(a.allocation) - Number(a.usage), a.unit) : "–";
        } },
        { title: "", render: function (a) { return bar(a.usage, a.allocation, a.user_usage); },
          tip: function (a) { return "green: project usage, blue: your usage"; } },
        { title: "%", num: true, render: function (a) {
          var v = pct(a.usage, a.allocation); return v === null ? "–" : v.toFixed(1) + "%";
        } },
        { title: "Your usage", num: true, render: function (a) { return fmtAmount(a.user_usage, a.unit); } }
      ], rows, "No allocations.");
      el.appendChild(h("div", { "class": "project" },
        h("h3", { text: p.name + (p.id && p.id !== p.name ? " (" + p.id + ")" : "") }),
        p.description ? h("p", { "class": "desc", text: p.description }) : null,
        h("div", { "class": "table-wrap" }, t)));
    });
  }

  function renderQuotas(data) {
    var rows = (data.quotas || []).map(function (q) {
      return { where: q.machine, name: q.name, kind: q.scope || q.type, fs: q.filesystem,
        used: q.used_bytes, quota: q.quota_bytes, unit: "bytes", grace: q.grace, source: q.source };
    });
    (data.projects || []).forEach(function (p) {
      (p.allocations || []).forEach(function (a) {
        if (a.unit !== "bytes" && a.unit !== "inodes") return;
        rows.push({ where: "IRI", name: p.name, kind: "project", fs: a.capability, used: a.usage,
          quota: a.allocation, unit: a.unit, grace: null, source: p.source });
      });
    });
    rows.sort(function (a, b) { return (pct(b.used, b.quota) || 0) - (pct(a.used, a.quota) || 0); });
    table($("quotas"), [
      { title: "Machine", render: function (r) { return r.where || "–"; } },
      { title: "Name", render: function (r) { return r.name; } },
      { title: "Type", render: function (r) { return r.kind || "–"; } },
      { title: "Filesystem", render: function (r) { return r.fs || "–"; } },
      { title: "Used", num: true, render: function (r) { return fmtAmount(r.used, r.unit); } },
      { title: "Quota", num: true, render: function (r) { return r.quota ? fmtAmount(r.quota, r.unit) : "none"; } },
      { title: "", render: function (r) { return bar(r.used, r.quota); } },
      { title: "%", num: true, render: function (r) { var v = pct(r.used, r.quota); return v === null ? "–" : v.toFixed(1) + "%"; } },
      { title: "Grace", render: function (r) { return r.grace || ""; } },
      { title: "Source", render: function (r) { return r.source; } }
    ], rows, "No quota data.");
  }

  function renderSources(data) {
    var rows = Object.keys(data.sources || {}).sort().map(function (k) {
      return Object.assign({ name: k }, data.sources[k]);
    });
    table($("sources"), [
      { title: "Source", render: function (s) { return s.name; } },
      { title: "Status", render: function (s) { return badge(s.ok ? (s.errors && s.errors.length ? "warn" : "ok") : "bad"); } },
      { title: "Collected", render: function (s) { return fmtTime(s.collected_at) + " (" + fmtAgo(s.collected_at) + ")"; } },
      { title: "Last success", render: function (s) { return s.last_success ? fmtAgo(s.last_success) : "never"; } },
      { title: "Details", wrap: true, render: function (s) {
        return [s.error].concat(s.errors || []).filter(Boolean).join("; ") || "";
      } }
    ], rows, "No data sources recorded.");
  }

  // ----------------------------------------------------------------- load

  function fetchJson(url) {
    return fetch(url + "?t=" + Date.now(), { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(url + ": HTTP " + r.status);
      return r.json();
    });
  }

  function load() {
    return fetchJson(DATA_URL)
      .then(function (d) { render(d, false); })
      .catch(function () {
        return fetchJson(SAMPLE_URL).then(function (d) { render(d, true); });
      })
      .catch(function (err) {
        var banner = $("banner");
        banner.hidden = false;
        banner.textContent = "Could not load dashboard data: " + err.message;
      });
  }

  ["filter-machine", "filter-state"].forEach(function (id) { $(id).addEventListener("change", renderJobs); });
  $("filter-text").addEventListener("input", renderJobs);
  $("reload").addEventListener("click", load);
  load();
  setInterval(load, REFRESH_MS);
})();
