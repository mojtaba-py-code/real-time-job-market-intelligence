/* Job Market Intelligence dashboard.
 *
 * Deliberately dependency-free: the API sets a strict Content-Security-Policy
 * that forbids third-party scripts, and a dashboard that only needs bar, line
 * and donut charts does not justify a charting library. Every chart is plain
 * SVG built from the API response.
 */
(function () {
  "use strict";

  var state = {
    windowDays: 30,
    country: "",
    segment: "",
    apiKey: window.localStorage.getItem("jobintel.apiKey") || "",
    skill: "",
    loading: false,
  };

  var el = function (id) { return document.getElementById(id); };

  /* ---------------------------------------------------------------- */
  /* API access                                                        */
  /* ---------------------------------------------------------------- */
  function apiUrl(path, extra) {
    var url = new URL(path, window.location.origin);
    var params = url.searchParams;
    params.set("window_days", String(state.windowDays));
    if (state.country) params.set("country_code", state.country);
    if (state.segment) params.set("segment", state.segment);
    Object.keys(extra || {}).forEach(function (key) {
      var value = extra[key];
      if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
    });
    return url.toString();
  }

  function request(path, extra) {
    var headers = { Accept: "application/json" };
    if (state.apiKey) headers["X-API-Key"] = state.apiKey;
    return fetch(apiUrl(path, extra), { headers: headers }).then(function (response) {
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          var message = (body && body.error && body.error.message) || response.statusText;
          throw new Error(path + ": " + message);
        });
      }
      return response.json();
    });
  }

  /* ---------------------------------------------------------------- */
  /* Formatting                                                        */
  /* ---------------------------------------------------------------- */
  function number(value) {
    if (value === null || value === undefined) return "—";
    return Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function percent(value, digits) {
    if (value === null || value === undefined) return "—";
    return (Number(value) * 100).toFixed(digits === undefined ? 1 : digits) + "%";
  }

  function signed(value) {
    if (value === null || value === undefined) return "—";
    var rounded = Number(value).toFixed(1);
    return (Number(value) > 0 ? "+" : "") + rounded + "%";
  }

  function money(value, currency) {
    if (value === null || value === undefined) return "—";
    return (currency || "USD") + " " + number(Math.round(Number(value)));
  }

  function titleCase(value) {
    return String(value || "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  function text(node, value) { node.textContent = value; return node; }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

  /* ---------------------------------------------------------------- */
  /* SVG helpers                                                       */
  /* ---------------------------------------------------------------- */
  var NS = "http://www.w3.org/2000/svg";

  function svg(width, height) {
    var node = document.createElementNS(NS, "svg");
    node.setAttribute("viewBox", "0 0 " + width + " " + height);
    node.setAttribute("width", "100%");
    node.setAttribute("role", "img");
    return node;
  }

  function shape(name, attrs) {
    var node = document.createElementNS(NS, name);
    Object.keys(attrs).forEach(function (key) { node.setAttribute(key, String(attrs[key])); });
    return node;
  }

  function label(x, y, value, opts) {
    var node = shape("text", {
      x: x, y: y,
      fill: (opts && opts.fill) || "var(--text-muted)",
      "font-size": (opts && opts.size) || 10,
      "text-anchor": (opts && opts.anchor) || "start",
    });
    node.textContent = value;
    return node;
  }

  function emptyState(container, message) {
    clear(container).appendChild(text(document.createElement("p"), message)).className = "empty";
  }

  /* ---------------------------------------------------------------- */
  /* Charts                                                            */
  /* ---------------------------------------------------------------- */
  function lineChart(container, points, average) {
    clear(container);
    if (!points || points.length < 2) return emptyState(container, "Not enough data for a time series.");

    var W = 760, H = 240, padL = 44, padR = 12, padT = 12, padB = 26;
    var innerW = W - padL - padR, innerH = H - padT - padB;
    var max = Math.max.apply(null, points.map(function (p) { return p.value; })) || 1;
    var node = svg(W, H);

    for (var g = 0; g <= 4; g++) {
      var y = padT + (innerH / 4) * g;
      node.appendChild(shape("line", { x1: padL, y1: y, x2: W - padR, y2: y, stroke: "var(--border)", "stroke-width": 1 }));
      node.appendChild(label(padL - 6, y + 3, number(max * (1 - g / 4)), { anchor: "end" }));
    }

    var xAt = function (i) { return padL + (innerW * i) / (points.length - 1); };
    var yAt = function (v) { return padT + innerH - (innerH * v) / max; };

    var area = "M" + xAt(0) + "," + yAt(points[0].value);
    points.forEach(function (p, i) { area += " L" + xAt(i) + "," + yAt(p.value); });
    node.appendChild(shape("path", {
      d: area + " L" + xAt(points.length - 1) + "," + (padT + innerH) + " L" + padL + "," + (padT + innerH) + " Z",
      fill: "var(--accent-soft)", stroke: "none",
    }));
    node.appendChild(shape("path", { d: area, fill: "none", stroke: "var(--accent)", "stroke-width": 2 }));

    if (average && average.length === points.length) {
      var line = "M" + xAt(0) + "," + yAt(average[0].value);
      average.forEach(function (p, i) { line += " L" + xAt(i) + "," + yAt(p.value); });
      node.appendChild(shape("path", { d: line, fill: "none", stroke: "var(--up)", "stroke-width": 1.5, "stroke-dasharray": "4 3" }));
    }

    var step = Math.max(1, Math.floor(points.length / 6));
    points.forEach(function (p, i) {
      if (i % step === 0 || i === points.length - 1) {
        node.appendChild(label(xAt(i), H - 8, String(p.day).slice(5), { anchor: "middle" }));
      }
    });
    container.appendChild(node);
  }

  function barChart(container, rows, opts) {
    clear(container);
    if (!rows || !rows.length) return emptyState(container, "No data in this window.");

    var options = opts || {};
    var rowH = 24, padL = options.labelWidth || 130, padR = 56;
    var W = 640, H = rows.length * rowH + 8;
    var max = Math.max.apply(null, rows.map(function (r) { return r.value; })) || 1;
    var node = svg(W, H);

    rows.forEach(function (row, i) {
      var y = i * rowH + 4;
      var width = Math.max(2, ((W - padL - padR) * row.value) / max);
      node.appendChild(label(padL - 8, y + 13, row.label, { anchor: "end", fill: "var(--text)", size: 11 }));
      node.appendChild(shape("rect", {
        x: padL, y: y + 3, width: width, height: rowH - 10, rx: 3,
        fill: row.color || "var(--accent)", opacity: 0.85,
      }));
      node.appendChild(label(padL + width + 6, y + 13, row.display || number(row.value), { size: 11 }));
    });
    container.appendChild(node);
  }

  function donutChart(container, slices) {
    clear(container);
    var total = slices.reduce(function (sum, s) { return sum + s.value; }, 0);
    if (!total) return emptyState(container, "No data in this window.");

    var size = 220, r = 82, cx = size / 2, cy = size / 2, node = svg(size, size);
    var angle = -Math.PI / 2;

    slices.forEach(function (slice) {
      if (!slice.value) return;
      var sweep = (slice.value / total) * Math.PI * 2;
      var end = angle + sweep;
      var large = sweep > Math.PI ? 1 : 0;
      var path = [
        "M", cx + r * Math.cos(angle), cy + r * Math.sin(angle),
        "A", r, r, 0, large, 1, cx + r * Math.cos(end), cy + r * Math.sin(end),
      ].join(" ");
      node.appendChild(shape("path", { d: path, fill: "none", stroke: slice.color, "stroke-width": 26 }));
      angle = end;
    });

    var top = slices.slice().sort(function (a, b) { return b.value - a.value; })[0];
    node.appendChild(label(cx, cy - 2, percent(top.value / total, 0), { anchor: "middle", fill: "var(--text)", size: 20 }));
    node.appendChild(label(cx, cy + 16, titleCase(top.key), { anchor: "middle", size: 11 }));
    container.appendChild(node);
  }

  /* ---------------------------------------------------------------- */
  /* Renderers                                                         */
  /* ---------------------------------------------------------------- */
  function renderOverview(data) {
    text(el("kpi-active"), number(data.active_jobs));
    text(el("kpi-active-sub"), number(data.total_jobs) + " collected in total");
    text(el("kpi-today"), number(data.new_jobs_today));
    text(el("kpi-today-sub"), "window: " + data.window_days + " days");
    text(el("kpi-week"), number(data.new_jobs_this_week));
    text(el("kpi-companies"), number(data.companies_hiring));
    text(el("kpi-countries"), number(data.countries_covered) + " countries");

    if (data.top_skill) {
      text(el("kpi-skill"), data.top_skill.name);
      text(el("kpi-skill-sub"), number(data.top_skill.job_count) + " postings · " + percent(data.top_skill.share));
    }
    if (data.fastest_growing_skill) {
      var trend = data.fastest_growing_skill;
      var window30 = (trend.windows || []).filter(function (w) { return w.days === 30; })[0] || (trend.windows || [])[0];
      text(el("kpi-growth"), trend.name);
      text(el("kpi-growth-sub"), window30 ? signed(window30.change_pct) + " over " + window30.days + " days" : "");
    }
    text(el("kpi-salary"), data.median_salary ? money(data.median_salary, "USD") : "—");
    text(el("kpi-salary-sub"), data.average_salary ? "mean " + money(data.average_salary, "USD") : "no disclosed salaries");
    text(el("kpi-remote"), percent(data.remote_share));
    text(el("kpi-quality"), "data quality " + percent(data.data_quality_score, 0));

    text(el("subtitle"),
      number(data.active_jobs) + " active postings · generated " +
      new Date(data.generated_at).toLocaleString());

    if (data.volume) lineChart(el("chart-volume"), data.volume.points, data.volume.moving_average);
  }

  function renderSkills(skills) {
    barChart(el("chart-skills"), skills.slice(0, 12).map(function (s) {
      return { label: s.name, value: s.job_count, display: number(s.job_count) + " (" + percent(s.share, 0) + ")" };
    }));

    var select = el("skill-select");
    var previous = state.skill;
    clear(select);
    skills.slice(0, 40).forEach(function (s) {
      var option = document.createElement("option");
      option.value = s.slug;
      option.textContent = s.name;
      select.appendChild(option);
    });
    if (skills.length) {
      state.skill = skills.some(function (s) { return s.slug === previous; }) ? previous : skills[0].slug;
      select.value = state.skill;
    }
  }

  function renderTrends(trends) {
    var container = clear(el("list-trends"));
    if (!trends.length) return emptyState(container, "Not enough history for trends yet.");
    trends.forEach(function (trend) {
      var window30 = (trend.windows || []).filter(function (w) { return w.days === 30; })[0] || (trend.windows || [])[0];
      var row = document.createElement("div");
      row.className = "row";
      row.appendChild(text(document.createElement("span"), trend.name)).className = "name";
      row.appendChild(text(document.createElement("span"), window30 ? signed(window30.change_pct) : "—")).className = "value";
      var badge = text(document.createElement("span"), titleCase(trend.direction));
      badge.className = "badge " + trend.direction;
      row.appendChild(badge);
      container.appendChild(row);
    });
  }

  function renderEmerging(items) {
    var container = clear(el("list-emerging"));
    if (!items.length) return emptyState(container, "No technology cleared the emergence thresholds.");
    items.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "row";
      row.appendChild(text(document.createElement("span"), item.name)).className = "name";
      row.appendChild(text(document.createElement("span"),
        number(item.current_count) + " jobs · " + (item.growth_pct === null ? "new" : signed(item.growth_pct))
      )).className = "value";
      var badge = text(document.createElement("span"), "conf " + percent(item.confidence, 0));
      badge.className = "badge emerging";
      row.appendChild(badge);
      container.appendChild(row);
    });
  }

  function renderRemote(data) {
    donutChart(el("chart-remote"), [
      { key: "remote", value: data.counts.remote || 0, color: "var(--accent)" },
      { key: "hybrid", value: data.counts.hybrid || 0, color: "var(--up)" },
      { key: "onsite", value: data.counts.onsite || 0, color: "var(--warn)" },
      { key: "unknown", value: data.counts.unknown || 0, color: "var(--border)" },
    ]);
  }

  function renderSeniority(data) {
    var order = ["intern", "entry", "junior", "mid", "senior", "lead", "principal", "manager", "director", "executive", "unknown"];
    var rows = order
      .filter(function (level) { return (data.counts[level] || 0) > 0; })
      .map(function (level) { return { label: titleCase(level), value: data.counts[level] }; });
    barChart(el("chart-seniority"), rows, { labelWidth: 90 });
  }

  function renderCountries(rows) {
    barChart(el("chart-countries"), rows.slice(0, 10).map(function (row) {
      return {
        label: row.country || row.country_code || "Unknown",
        value: row.job_count,
        display: number(row.job_count) + " · " + percent(row.remote_share, 0) + " remote",
      };
    }), { labelWidth: 120 });
  }

  function renderSalary(stats) {
    var container = el("chart-salary");
    if (!stats || !stats.sample_size || !stats.median) {
      return emptyState(container, "Too few disclosed salaries in this window to publish statistics.");
    }
    barChart(container, [
      { label: "25th percentile", value: stats.p25 || 0, display: money(stats.p25, stats.currency) },
      { label: "Median", value: stats.median || 0, display: money(stats.median, stats.currency) },
      { label: "Mean", value: stats.average || 0, display: money(stats.average, stats.currency) },
      { label: "75th percentile", value: stats.p75 || 0, display: money(stats.p75, stats.currency) },
      { label: "90th percentile", value: stats.p90 || 0, display: money(stats.p90, stats.currency) },
    ], { labelWidth: 120 });

    var note = document.createElement("p");
    note.className = "hint";
    note.textContent = "Based on " + number(stats.sample_size) + " postings that disclose pay (" +
      percent(stats.observed_share, 0) + " of the window).";
    container.appendChild(note);
  }

  function renderCompanies(rows) {
    var container = clear(el("table-companies"));
    if (!rows.length) return emptyState(container, "No companies in this window.");

    var table = document.createElement("table");
    var head = table.createTHead().insertRow();
    ["Company", "Open postings", "Last 30 days", "Growth", "Remote share"].forEach(function (title, index) {
      var th = document.createElement("th");
      th.textContent = title;
      if (index > 0) th.className = "num";
      head.appendChild(th);
    });

    var body = table.createTBody();
    rows.forEach(function (row) {
      var tr = body.insertRow();
      tr.insertCell().textContent = row.name;
      [number(row.active_job_count), number(row.jobs_last_30_days), signed(row.hiring_growth_pct), percent(row.remote_share, 0)]
        .forEach(function (value) {
          var cell = tr.insertCell();
          cell.textContent = value;
          cell.className = "num";
        });
    });
    container.appendChild(table);
  }

  function renderExplorer(view) {
    var container = clear(el("explorer"));
    if (!view) return emptyState(container, "No data for this skill in the selected window.");

    function card(title, rows) {
      var node = document.createElement("div");
      node.className = "card";
      node.appendChild(text(document.createElement("h3"), title));
      rows.forEach(function (row) {
        var line = document.createElement("div");
        line.className = "row";
        line.appendChild(text(document.createElement("span"), row[0])).className = "name";
        line.appendChild(text(document.createElement("span"), row[1])).className = "value";
        node.appendChild(line);
      });
      container.appendChild(node);
    }

    var trendRows = (view.trend.windows || []).map(function (w) {
      return [w.days + "-day change", signed(w.change_pct) + " (" + number(w.current_count) + " jobs)"];
    });
    trendRows.push(["Direction", titleCase(view.trend.direction)]);
    trendRows.push(["Confidence", percent(view.trend.confidence, 0)]);
    card("Demand — " + view.skill.name, trendRows);

    card("Related skills", (view.related_skills || []).slice(0, 6).map(function (pair) {
      var other = pair.skill_a === view.skill.slug ? pair.skill_b : pair.skill_a;
      return [other, "lift " + pair.lift.toFixed(2)];
    }));

    card("Top companies", (view.top_companies || []).slice(0, 6).map(function (c) {
      return [c.name, number(c.active_job_count) + " jobs"];
    }));

    card("Top locations", (view.top_locations || []).slice(0, 6).map(function (l) {
      return [l.country || l.country_code || "Unknown", number(l.job_count) + " jobs"];
    }));

    var salary = view.salary || {};
    card("Salary", salary.median
      ? [["Median", money(salary.median, salary.currency)],
         ["25th–75th", money(salary.p25, salary.currency) + " – " + money(salary.p75, salary.currency)],
         ["Sample", number(salary.sample_size) + " postings"]]
      : [["Median", "not enough disclosed salaries"]]);

    card("Work arrangement", view.remote
      ? [["Remote", percent(view.remote.remote_share)],
         ["Hybrid", percent(view.remote.hybrid_share)],
         ["On-site", percent(view.remote.onsite_share)]]
      : []);
  }

  /* ---------------------------------------------------------------- */
  /* Orchestration                                                     */
  /* ---------------------------------------------------------------- */
  function banner(message, kind) {
    var node = el("banner");
    if (!message) { node.hidden = true; return; }
    node.hidden = false;
    node.dataset.kind = kind || "warning";
    node.textContent = message;
  }

  function loadExplorer() {
    if (!state.skill) return Promise.resolve();
    return request("/skills/" + encodeURIComponent(state.skill) + "/explorer")
      .then(renderExplorer)
      .catch(function () { renderExplorer(null); });
  }

  function refresh() {
    if (state.loading) return;
    state.loading = true;
    el("refresh").disabled = true;
    banner("");

    Promise.all([
      request("/analytics/market"),
      request("/analytics/skills", { limit: 40 }),
      request("/analytics/skills/trends", { limit: 10 }),
      request("/analytics/emerging-skills", { limit: 8 }),
      request("/analytics/remote"),
      request("/analytics/seniority"),
      request("/analytics/locations", { by: "country", limit: 12 }),
      request("/analytics/salaries"),
      request("/analytics/companies", { limit: 12 }),
    ])
      .then(function (results) {
        renderOverview(results[0]);
        renderSkills(results[1]);
        renderTrends(results[2]);
        renderEmerging(results[3]);
        renderRemote(results[4]);
        renderSeniority(results[5]);
        renderCountries(results[6]);
        renderSalary(results[7]);
        renderCompanies(results[8]);
        populateCountries(results[6]);
        return loadExplorer();
      })
      .catch(function (error) {
        banner(error.message + " — check that the API is running and that your key is valid.", "error");
      })
      .finally(function () {
        state.loading = false;
        el("refresh").disabled = false;
      });
  }

  function populateCountries(rows) {
    var select = el("country-select");
    if (select.dataset.filled === "1") return;
    rows.forEach(function (row) {
      if (!row.country_code) return;
      var option = document.createElement("option");
      option.value = row.country_code;
      option.textContent = row.country || row.country_code;
      select.appendChild(option);
    });
    select.dataset.filled = "1";
  }

  function populateSegments() {
    var segments = [
      "backend_engineering", "frontend_engineering", "fullstack_engineering",
      "data_engineering", "data_science", "ai_ml", "devops", "cloud_engineering",
      "cybersecurity", "qa", "mobile_development", "embedded", "product", "other",
    ];
    var select = el("segment-select");
    segments.forEach(function (segment) {
      var option = document.createElement("option");
      option.value = segment;
      option.textContent = titleCase(segment);
      select.appendChild(option);
    });
  }

  function bind() {
    el("window-select").addEventListener("change", function (event) {
      state.windowDays = Number(event.target.value);
      refresh();
    });
    el("country-select").addEventListener("change", function (event) {
      state.country = event.target.value;
      refresh();
    });
    el("segment-select").addEventListener("change", function (event) {
      state.segment = event.target.value;
      refresh();
    });
    el("skill-select").addEventListener("change", function (event) {
      state.skill = event.target.value;
      loadExplorer();
    });
    el("api-key").addEventListener("change", function (event) {
      state.apiKey = event.target.value.trim();
      window.localStorage.setItem("jobintel.apiKey", state.apiKey);
      refresh();
    });
    el("refresh").addEventListener("click", refresh);
  }

  document.addEventListener("DOMContentLoaded", function () {
    el("api-key").value = state.apiKey;
    populateSegments();
    bind();
    refresh();
  });
})();
