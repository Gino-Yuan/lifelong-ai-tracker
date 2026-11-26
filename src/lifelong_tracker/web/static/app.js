/* Lifelong AI Tracker dashboard — vanilla JS + inline SVG, same-origin requests only. */
"use strict";

(() => {
  // ------------------------------------------------------------------ constants
  // Fixed category order and hues (identity follows the category, never its rank).
  const CATS = [
    { key: "input_tokens", label: "Input", color: "#6aa1ff" },
    { key: "output_tokens", label: "Output", color: "#f2a65a" },
    { key: "thinking_tokens", label: "Thinking", color: "#b98cff" },
    { key: "cache_read_tokens", label: "Cache Read", color: "#4cc9a6" },
    { key: "cache_write_tokens", label: "Cache Write", color: "#ef6f8f" },
  ];
  const ACCENT = "#7aa2ff";
  const HEAT = ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"]; // one hue, light->dark
  const RANGES = ["today", "7d", "30d", "90d", "ytd", "last_year", "lifelong", "custom"]; // keys 1-8
  const GRANS = ["auto", "day", "week", "month"];
  const VIEWS = ["combined", "agent"];
  // Agent line colors follow the tool id (never its rank), from the category palette.
  const TOOL_COLORS = { "claude-code": "#f2a65a", antigravity: "#4cc9a6", t3: "#b98cff", opencode: "#ef6f8f", codex: "#1a9a48" };
  const OTHER_TOOL_COLOR = "#8a929c";
  const MAX_DOTS = 62; // draw point markers only while they stay legible
  const DIM_LABEL = { model: "Model", tool: "Tool", project: "Project" };
  const SVGNS = "http://www.w3.org/2000/svg";
  const RANK_ROWS = 15;

  const state = {
    range: "30d", start: null, end: null, // start / end: applied custom range (YYYY-MM-DD)
    metric: "tokens", by: "model", year: null,
    tool: null, model: null, tools: [],
    gran: "auto", view: "combined", group: true, syncing: false,
    seq: 0, hseq: 0, tseq: 0, data: {},
  };
  // "By Agent" only makes sense across tools; a single-tool scope is always one line.
  const splitByAgent = () => state.view === "agent" && !state.tool;

  // ------------------------------------------------------------------ formatting
  const nf = new Intl.NumberFormat("en-US");
  const fmtInt = (n) => nf.format(Math.round(n || 0));
  function fmtCompact(n) {
    n = n || 0;
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(Math.round(n));
  }
  function fmtUsd(x) {
    x = x || 0;
    if (x === 0) return "$0.00";
    if (Math.abs(x) >= 1) {
      return "$" + x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return "$" + x.toFixed(Math.abs(x) >= 0.01 ? 3 : 4);
  }
  const fmtPct = (x) => ((x || 0) * 100).toFixed(1) + "%";
  const HIT_SUB = "Cache read ÷ (input + cache read + cache write)";
  function parseDay(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  const fmtDay = (iso, opts) =>
    parseDay(iso).toLocaleDateString("en-US", opts || { month: "short", day: "numeric", year: "numeric" });
  const plural = (n, word) => `${fmtInt(n)} ${word}${n === 1 ? "" : "s"}`;
  // "Oct 1 – Oct 31, 2026" / "Dec 15, 2025 – Jan 14, 2026" / "Oct 1, 2026" (same as the API)
  function fmtSpan(a, b) {
    if (a === b) return fmtDay(b);
    const md = { month: "short", day: "numeric" };
    return a.slice(0, 4) === b.slice(0, 4) ? `${fmtDay(a, md)} – ${fmtDay(b)}` : `${fmtDay(a)} – ${fmtDay(b)}`;
  }
  // Period-over-period magnitude, one decimal, half away from zero: 18.44 -> "18.4"
  const fmtDelta = (pct) => (Math.round(Math.abs(pct) * 10) / 10)
    .toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

  // ------------------------------------------------------------------ calendar days
  const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
  const pad2 = (n) => String(n).padStart(2, "0");
  const isoOf = (d) => `${String(d.getFullYear()).padStart(4, "0")}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  // A real calendar day in YYYY-MM-DD form (2026-02-30 is not one).
  const isRealDay = (s) => typeof s === "string" && ISO_DAY.test(s) && isoOf(parseDay(s)) === s;
  function addDays(iso, n) {
    const d = parseDay(iso);
    d.setDate(d.getDate() + n);
    return isoOf(d);
  }
  // Default custom window: the last 30 days, ending on the server's "today" once known.
  function defaultCustom() {
    const ov = state.data.ov;
    const end = ov && isRealDay(ov.today) ? ov.today : isoOf(new Date());
    return { start: addDays(end, -29), end };
  }

  // ------------------------------------------------------------------ dom helpers
  const $ = (id) => document.getElementById(id);
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "style") Object.assign(el.style, v); // CSSOM: allowed by the CSP
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c == null) continue;
      el.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  function svg(tag, attrs) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
    return el;
  }
  const swatch = (color) => h("span", { class: "swatch", style: { background: color } });
  const emptyState = (msg) => h("div", { class: "empty", text: msg });

  // ------------------------------------------------------------------ tooltip
  const tip = $("tooltip");
  function showTip(evt, title, rows) {
    tip.replaceChildren(h("div", { class: "tt-title", text: title }));
    for (const r of rows) {
      if (r === "sep") {
        tip.append(h("div", { class: "tt-sep" }));
        continue;
      }
      const label = h("span", { class: "l" }, r.color ? swatch(r.color) : null, r.label);
      tip.append(h("div", { class: "tt-row" }, label, h("span", { class: "v", text: r.value })));
    }
    tip.hidden = false;
    moveTip(evt);
  }
  function moveTip(evt) {
    const pad = 14;
    const r = tip.getBoundingClientRect();
    let x = evt.clientX + pad;
    let y = evt.clientY + pad;
    if (x + r.width > window.innerWidth - 8) x = evt.clientX - r.width - pad;
    if (y + r.height > window.innerHeight - 8) y = evt.clientY - r.height - pad;
    tip.style.left = Math.max(8, x) + "px";
    tip.style.top = Math.max(8, y) + "px";
  }
  const hideTip = () => { tip.hidden = true; };
  function bindTip(el, build, onEnter, onLeave) {
    el.addEventListener("mouseenter", (e) => { onEnter && onEnter(); const [t, rows] = build(); showTip(e, t, rows); });
    el.addEventListener("mousemove", moveTip);
    el.addEventListener("mouseleave", () => { onLeave && onLeave(); hideTip(); });
  }

  // ------------------------------------------------------------------ data
  async function api(path) {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`);
    return res.json();
  }

  // Query string for the current scope (range + optional tool / model).
  function qs(extra, withRange = true) {
    const p = new URLSearchParams(extra || {});
    if (withRange) {
      p.set("range", state.range);
      if (state.range === "custom") {
        p.set("start", state.start);
        p.set("end", state.end);
      }
    }
    if (state.tool) p.set("tool", state.tool);
    if (state.model) p.set("model", state.model);
    return p.toString();
  }
  const toolLabel = (id) => (state.tools.find((t) => t.tool === id) || {}).label || id;
  function scopeText() {
    return [state.tool ? toolLabel(state.tool) : null, state.model].filter(Boolean).join(" · ");
  }

  async function loadTools() {
    try {
      const res = await api("/api/tools");
      state.tools = res.tools;
      renderToolControl();
    } catch (err) {
      showError(err);
    }
  }

  // Re-query everything that depends on the scope (tool / model / range).
  function loadAll() {
    loadRange();
    loadHeatmap();
  }

  // Model rows may be merged into families (-low / -high / dated releases) on request.
  const groupedBreakdown = () => state.by === "model" && state.group;
  const breakdownUrl = () =>
    `/api/breakdown?${qs(groupedBreakdown() ? { by: state.by, group_variants: "true" } : { by: state.by })}`;

  const trendUrl = () =>
    `/api/timeseries?${qs({ granularity: state.gran, split_by: splitByAgent() ? "tool" : "none" })}`;

  async function loadRange() {
    const seq = ++state.seq;
    const tseq = ++state.tseq;
    syncControls();
    try {
      const [ov, ts, bd, hr] = await Promise.all([
        api(`/api/overview?${qs()}`),
        api(trendUrl()),
        api(breakdownUrl()),
        api(`/api/hourly?${qs()}`),
      ]);
      if (seq !== state.seq) return; // a newer range was requested meanwhile
      Object.assign(state.data, { ov, bd, hr });
      renderOverview(ov);
      if (tseq === state.tseq) { // granularity / view may have changed meanwhile
        state.data.ts = ts;
        renderTrend();
      }
      renderBreakdown(bd);
      renderHourly(hr);
    } catch (err) {
      showError(err);
    }
  }

  // Granularity / view changes only re-query the trend.
  async function loadTrend() {
    const tseq = ++state.tseq;
    syncControls();
    try {
      const ts = await api(trendUrl());
      if (tseq !== state.tseq) return;
      state.data.ts = ts;
      renderTrend();
    } catch (err) {
      showError(err);
    }
  }

  async function loadBreakdown() {
    const seq = ++state.seq;
    syncControls();
    try {
      const bd = await api(breakdownUrl());
      if (seq !== state.seq) return;
      state.data.bd = bd;
      renderBreakdown(bd);
    } catch (err) {
      showError(err);
    }
  }

  async function loadHeatmap() {
    const seq = ++state.hseq;
    try {
      const hm = await api(`/api/heatmap?${qs(state.year ? { year: state.year } : {}, false)}`);
      if (seq !== state.hseq) return;
      state.year = hm.year;
      state.data.hm = hm;
      renderYearSelect(hm);
      renderHeatmap(hm);
    } catch (err) {
      showError(err);
    }
  }

  // ------------------------------------------------------------------ controls
  function syncControls() {
    for (const b of $("range-control").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(b.dataset.range === state.range));
    }
    const custom = state.range === "custom";
    $("custom-range").hidden = !custom;
    if (custom) { // show what is applied; unapplied edits are dropped on any reload
      $("custom-start").value = state.start;
      $("custom-end").value = state.end;
      setCustomError(null);
    }
    for (const b of $("trend-toggle").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(b.dataset.metric === state.metric));
    }
    for (const b of $("rank-tabs").querySelectorAll("button")) {
      b.setAttribute("aria-selected", String(b.dataset.by === state.by));
    }
    for (const b of $("tool-control").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String((b.dataset.tool || null) === state.tool));
    }
    for (const b of $("gran-toggle").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(b.dataset.gran === state.gran));
    }
    const view = splitByAgent() ? "agent" : "combined";
    for (const b of $("view-toggle").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(b.dataset.view === view));
      const locked = Boolean(state.tool) && b.dataset.view === "agent";
      b.disabled = locked;
      b.title = locked ? "Available when All Tools is selected" : "";
    }
    $("export-csv").href = `/api/export?${qs({ format: "csv" })}`;
    $("export-json").href = `/api/export?${qs({ format: "json" })}`;
    $("export-card").href = `/api/card?${qs({ download: "1" })}`;
    $("rank-key-head").textContent = groupedBreakdown() ? "Model family" : DIM_LABEL[state.by];
    const groupBox = $("group-variants");
    groupBox.checked = state.group;
    groupBox.disabled = state.by !== "model";
    groupBox.setAttribute("aria-checked", String(state.group));
    $("group-switch").classList.toggle("disabled", state.by !== "model");
    $("filterbar").hidden = !state.model;
    $("model-chip-value").textContent = state.model || "";
    writeHash();
  }

  // ------------------------------------------------------------------ url hash
  // #range=30d&tool=antigravity&model=gemini-4-neon-low&gran=week&view=agent&group=0
  function hashString() {
    const p = new URLSearchParams({ range: state.range });
    if (state.range === "custom") {
      p.set("start", state.start);
      p.set("end", state.end);
    }
    if (state.tool) p.set("tool", state.tool);
    if (state.model) p.set("model", state.model);
    if (state.gran !== "auto") p.set("gran", state.gran);
    if (state.view !== "combined") p.set("view", state.view);
    if (!state.group) p.set("group", "0"); // on by default; only "off" is written
    return "#" + p.toString();
  }
  function writeHash() {
    const next = hashString();
    if (location.hash !== next) history.replaceState(null, "", next);
  }
  function readHash() {
    const p = new URLSearchParams(location.hash.replace(/^#/, ""));
    const r = p.get("range");
    state.range = r && RANGES.includes(r) ? r : state.range;
    if (state.range === "custom") {
      const s = p.get("start"), e = p.get("end");
      const ok = isRealDay(s) && isRealDay(e) && e >= s;
      Object.assign(state, ok ? { start: s, end: e } : defaultCustom());
    }
    const t = (p.get("tool") || "").trim().toLowerCase().replace(/_/g, "-");
    state.tool = /^[a-z0-9-]{1,64}$/.test(t) && t !== "all" ? t : null;
    const m = (p.get("model") || "").trim();
    state.model = m && m !== "all" && m.length <= 256 ? m : null;
    const g = p.get("gran");
    state.gran = GRANS.includes(g) ? g : "auto";
    const v = p.get("view");
    state.view = VIEWS.includes(v) ? v : "combined";
    state.group = p.get("group") !== "0"; // missing (or a legacy group=1) means on
  }

  function renderToolControl() {
    const ids = state.tools.map((t) => t.tool);
    if (state.tool && !ids.includes(state.tool)) ids.push(state.tool); // keep a hash-selected tool visible
    $("tool-control").replaceChildren(
      h("button", { type: "button", "data-tool": "", text: "All Tools" }),
      ...ids.map((id) => {
        const t = state.tools.find((x) => x.tool === id);
        return h("button", {
          type: "button", "data-tool": id, text: toolLabel(id),
          title: t ? `${fmtCompact(t.tokens)} tokens · ${plural(t.events, "request")}` : "No data yet",
        });
      }));
    syncControls();
  }

  function setScope(next) {
    let changed = false;
    for (const k of ["tool", "model"]) {
      if (k in next && next[k] !== state[k]) {
        state[k] = next[k];
        changed = true;
      }
    }
    if (changed) loadAll();
  }

  // ------------------------------------------------------------------ range
  // Presets switch at once. Custom opens Start / End (prefilled with the last 30 days,
  // or the range applied before) and loads it; Apply / Enter reloads with edited dates.
  function setRange(r, focus) {
    if (!RANGES.includes(r)) return;
    if (r !== state.range) {
      if (r === "custom" && !(isRealDay(state.start) && isRealDay(state.end))) Object.assign(state, defaultCustom());
      state.range = r;
      loadRange();
    }
    if (r === "custom" && focus) $("custom-start").focus();
  }
  function setCustomError(msg, field) {
    const el = $("custom-error");
    el.textContent = msg || "";
    el.hidden = !msg;
    for (const id of ["custom-start", "custom-end"]) {
      $(id).setAttribute("aria-invalid", String(Boolean(msg) && (!field || field === id)));
    }
  }
  function applyCustom() {
    const start = $("custom-start").value, end = $("custom-end").value;
    if (!isRealDay(start)) return setCustomError("Enter a valid start date", "custom-start");
    if (!isRealDay(end)) return setCustomError("Enter a valid end date", "custom-end");
    if (end < start) return setCustomError("End must be on or after Start", "custom-end");
    setCustomError(null);
    if (state.range === "custom" && start === state.start && end === state.end) return;
    Object.assign(state, { range: "custom", start, end });
    loadRange();
  }

  function initControls() {
    readHash();

    $("tool-control").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-tool]");
      if (b) setScope({ tool: b.dataset.tool || null });
    });
    $("model-chip-clear").addEventListener("click", () => setScope({ model: null }));
    window.addEventListener("hashchange", () => {
      if (location.hash === hashString()) return;
      readHash();
      renderToolControl();
      loadAll();
    });
    $("range-control").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-range]");
      if (b) setRange(b.dataset.range);
    });
    $("custom-range").addEventListener("submit", (e) => {
      e.preventDefault();
      applyCustom();
    });
    for (const id of ["custom-start", "custom-end"]) {
      $(id).addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault(); // one path for Enter: no implicit form submission on top
        applyCustom();
      });
    }
    $("trend-toggle").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-metric]");
      if (!b || b.dataset.metric === state.metric) return;
      state.metric = b.dataset.metric;
      syncControls();
      renderTrend();
    });
    $("gran-toggle").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-gran]");
      if (!b || b.dataset.gran === state.gran) return;
      state.gran = b.dataset.gran;
      loadTrend();
    });
    $("view-toggle").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-view]");
      if (!b || b.disabled || b.dataset.view === state.view) return;
      state.view = b.dataset.view;
      loadTrend();
    });
    $("rank-tabs").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-by]");
      if (!b || b.dataset.by === state.by) return;
      state.by = b.dataset.by;
      loadBreakdown();
    });
    $("group-variants").addEventListener("change", (e) => {
      state.group = e.target.checked;
      loadBreakdown();
    });
    $("sync-now").addEventListener("click", syncNow);
    document.addEventListener("keydown", onShortcut);
    $("heat-year").addEventListener("change", (e) => {
      state.year = Number(e.target.value);
      loadHeatmap();
    });
    let t;
    window.addEventListener("resize", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        renderTrend();
        if (state.data.hr) renderHourly(state.data.hr);
      }, 150);
    });
  }

  // ------------------------------------------------------------------ alerts
  function renderMessage(text) {
    // `code` spans from the API message become <code> elements
    return text.split("`").map((part, i) => (i % 2 ? h("code", { text: part }) : part));
  }
  function setAlert(kind, title, messages, unpriced) {
    const box = $("alert");
    const actions = $("alert-actions");
    actions.replaceChildren();
    actions.hidden = true;
    if (!messages.length) {
      box.hidden = true;
      return;
    }
    box.classList.toggle("error", kind === "error");
    $("alert-title").textContent = title;
    $("alert-list").replaceChildren(...messages.map((m) => h("li", null, renderMessage(m))));
    if (unpriced && unpriced.length) {
      actions.append(...unpriced.map((u) => {
        const btn = h("button", { type: "button", class: "btn btn-xs btn-primary", text: "Set Price" });
        btn.addEventListener("click", () => openPriceDialog(u.model, u.base_model));
        return h("span", { class: "unpriced-chip", title: `${fmtInt(u.tokens)} tokens · ${plural(u.events, "request")}` },
          h("code", { text: u.model }), btn);
      }));
      actions.hidden = false;
    }
    box.hidden = false;
  }
  function showError(err) {
    console.error(err);
    setAlert("error", "Could not load data", [String(err.message || err)]);
  }

  // ------------------------------------------------------------------ overview
  function renderOverview(ov) {
    const t = ov.totals;
    $("kpi-cost").textContent = fmtUsd(t.cost_usd);
    $("kpi-cost-sub").textContent =
      plural(t.events, "request") + (t.unpriced_events ? ` · ${fmtInt(t.unpriced_events)} unpriced` : "");
    $("kpi-tokens").textContent = fmtCompact(t.total_tokens);
    $("kpi-tokens-sub").textContent = `${fmtInt(t.total_tokens)} tokens · ${fmtPct(t.output_ratio)} output`;
    const cmp = ov.comparison;
    renderDelta("kpi-cost-delta", cmp, cmp && cmp.cost_pct_change, t.cost_usd, cmp && fmtUsd(cmp.prev_cost_usd));
    renderDelta("kpi-tokens-delta", cmp, cmp && cmp.tokens_pct_change, t.total_tokens,
      cmp && `${fmtInt(cmp.prev_total_tokens)} tokens`);
    const noBreakdown = t.cache_hit_rate == null && t.events > 0;
    $("kpi-hit").textContent = noBreakdown ? "—" : fmtPct(t.cache_hit_rate);
    $("kpi-hit-sub").textContent = noBreakdown ? "No cache breakdown" : HIT_SUB;
    $("kpi-avg").textContent = fmtUsd(t.cost_per_mtok);
    const cs = ov.cache_savings || { usd: 0, cache_read_tokens: 0, unpriced_cache_read_tokens: 0 };
    $("kpi-saved").textContent = fmtUsd(cs.usd);
    $("kpi-saved-sub").textContent = cs.cache_read_tokens
      ? `${fmtCompact(cs.cache_read_tokens)} cache-read tokens` +
        (cs.unpriced_cache_read_tokens ? ` · ${fmtCompact(cs.unpriced_cache_read_tokens)} unpriced` : "")
      : cs.unpriced_cache_read_tokens ? `${fmtCompact(cs.unpriced_cache_read_tokens)} cache reads are unpriced`
        : "No cache reads in this range";
    renderRoi(ov.subscription, ov.subscription_error, ov.billing);
    renderRecords(ov.records);
    const s = ov.streak;
    $("kpi-streak").textContent = plural(s.current, "day");
    $("kpi-streak-sub").textContent = `Longest ${plural(s.longest, "day")} · ${plural(s.active_days, "active day")}`;

    const a = ov.alerts;
    const title = a.needs_pricing && a.needs_reprice ? "Pricing needs attention"
      : a.needs_pricing ? "Unpriced models" : "Costs out of date";
    setAlert("warn", title, a.messages, ov.unpriced_models);

    // token mix
    const bar = $("mix-bar");
    const legend = $("mix-legend");
    bar.replaceChildren();
    legend.replaceChildren();
    const scope = scopeText();
    $("mix-meta").textContent = `${ov.range_label}${scope ? " · " + scope : ""} · ${fmtInt(t.total_tokens)} tokens`;
    ov.categories.forEach((c, i) => {
      const color = CATS[i].color;
      if (c.tokens > 0) {
        const seg = h("div", { class: "seg", style: { background: color, flexGrow: String(c.tokens) } });
        bindTip(seg, () => [c.label, [
          { label: "Tokens", value: fmtInt(c.tokens), color },
          { label: "Share", value: fmtPct(c.share) },
        ]]);
        bar.append(seg);
      }
      legend.append(h("div", { class: "mix-item" },
        swatch(color),
        h("span", { class: "name", text: c.label }),
        h("span", { class: "pct", text: fmtPct(c.share) }),
        h("span", { class: "val", text: fmtInt(c.tokens) })));
    });
    bar.setAttribute("aria-label",
      "Token category shares: " + ov.categories.map((c) => `${c.label} ${fmtPct(c.share)}`).join(", "));
    $("foot-generated").textContent = `Updated ${new Date(ov.generated_at).toLocaleString("en-US")}`;
  }

  function fmtMultiple(m) {
    if (!Number.isFinite(m)) return "–";
    return (m >= 100 ? m.toFixed(0) : m >= 10 ? m.toFixed(1) : m.toFixed(2)) + "×";
  }
  // Period-over-period badge: "↑ 18.4% vs prev 30d"; "New vs …" when the previous
  // period had nothing and this one has something; hidden for lifelong or 0 -> 0.
  function renderDelta(id, cmp, pct, current, prevText) {
    const el = $(id);
    el.classList.remove("new");
    let text = null;
    if (cmp && pct != null) {
      const mag = fmtDelta(pct);
      const flat = Number(mag.replace(/,/g, "")) === 0;
      text = `${flat ? "→" : pct > 0 ? "↑" : "↓"} ${mag}% vs ${cmp.prev_label}`;
    } else if (cmp && current > 0) {
      text = `New vs ${cmp.prev_label}`;
      el.classList.add("new");
    }
    el.hidden = !text;
    el.textContent = text || "";
    if (text) el.title = `${cmp.prev_label} (${fmtSpan(cmp.prev_start, cmp.prev_end)}): ${prevText}`;
    else el.removeAttribute("title");
  }

  // Plan Spent & ROI card: what the plans actually cost in this range (prorated by
  // overlap days), the value multiple, and API value / net saved underneath.
  function planNet(sub) {
    const net = sub.value_usd - sub.plan_cost_usd;
    return net >= 0 ? `Saved ${fmtUsd(net)}` : `Net loss ${fmtUsd(-net)}`;
  }
  // With sources marked "billing": "api", their pay-as-you-go spend is shown apart
  // ("Direct API") and kept out of the plan's value multiple ("Subscription").
  function billingSplit(sub, billing) {
    const split = $("kpi-roi-split");
    if (!billing) {
      split.hidden = true;
      split.textContent = "";
      return "";
    }
    const subValue = sub ? sub.value_usd : 0;
    split.hidden = false;
    split.textContent = `Subscription: ${fmtUsd(subValue)} · Direct API: ${fmtUsd(billing.api_direct_spend_usd)}`;
    const lines = [
      "",
      `Subscription: ${fmtUsd(subValue)} API-equivalent value used under plans (counts toward ROI)`,
      `Direct API: ${fmtUsd(billing.api_direct_spend_usd)} billed per token by ${plural(billing.api_sources, "API source")} (not in ROI)`,
      ...billing.tools.map((t) => `  ${t.label}: ${fmtUsd(t.api_spend_usd)} · ${plural(t.events, "request")}`),
    ];
    if (billing.api_unpriced_events) lines.push(`${fmtInt(billing.api_unpriced_events)} unpriced API requests are not counted`);
    if (sub) lines.push(`Out of pocket: ${fmtUsd(sub.plan_cost_usd)} plans + ${fmtUsd(billing.api_direct_spend_usd)} API = ` +
      fmtUsd(sub.plan_cost_usd + billing.api_direct_spend_usd));
    return lines.join("\n");
  }
  function renderRoi(sub, error, billing) {
    const card = $("kpi-roi-card");
    const unit = $("kpi-roi-unit");
    if (error) {
      card.hidden = false;
      $("kpi-roi-spent").textContent = "–";
      unit.hidden = true;
      $("kpi-roi").textContent = "–";
      $("kpi-roi-sub").textContent = `subscriptions.json: ${error}`;
      card.title = billingSplit(null, billing).trim();
      return;
    }
    card.hidden = !sub && !billing;
    if (!sub && !billing) { billingSplit(null, null); return; }
    unit.hidden = false;
    if (!sub) { // API-only: nothing to divide by, so no multiple
      $("kpi-roi-spent").textContent = fmtUsd(billing.api_direct_spend_usd);
      unit.textContent = "direct API";
      $("kpi-roi").textContent = "Pay as you go";
      $("kpi-roi-sub").textContent = "No subscription plan in subscriptions.json";
      card.title = billingSplit(null, billing).trim();
      return;
    }
    unit.textContent = "spent";
    $("kpi-roi-spent").textContent = fmtUsd(sub.plan_cost_usd);
    if (sub.multiple == null) { // paid plans exist, but none covers a day of this range
      $("kpi-roi").textContent = "–";
      $("kpi-roi-sub").textContent = `No paid plan in this range · ${plural(sub.days, "day")}`;
    } else {
      $("kpi-roi").textContent = `${fmtMultiple(sub.multiple)} value`;
      $("kpi-roi-sub").textContent =
        `${fmtUsd(sub.value_usd)} ${billing ? "plan" : "API"} value · ${planNet(sub)} · ${plural(sub.days, "day")}`;
    }
    card.title = sub.tools.map((x) => `${x.label}: ${x.monthly_usd > 0 ? fmtUsd(x.monthly_usd) + "/mo" : "no active plan"}` +
      ` · ${plural(x.paid_days, "paid day")} · ${fmtUsd(x.plan_cost_usd)} plan · ${fmtUsd(x.value_usd)} value`).join("\n") +
      (sub.unpriced_events ? `\n${fmtInt(sub.unpriced_events)} unpriced requests are not counted` : "") +
      billingSplit(sub, billing);
  }

  const fmtDayShort = (iso) => fmtDay(iso, { month: "short", day: "numeric", year: "numeric" });
  const costText = (c) => (c == null ? "unpriced" : fmtUsd(c));
  function setRecord(id, value, sub) {
    $(id).textContent = value;
    $(`${id}-sub`).textContent = sub;
  }
  function renderRecords(rec) {
    rec = rec || {};
    const none = "No usage in this range yet";
    const p = rec.peak_day;
    if (p) {
      const pc = rec.peak_cost_day;
      const extra = pc && pc.date !== p.date ? ` · top cost ${fmtUsd(pc.cost_usd)} on ${fmtDay(pc.date, { month: "short", day: "numeric" })}` : "";
      setRecord("rec-peak", `${fmtCompact(p.total_tokens)} tokens`, `${fmtDayShort(p.date)} · ${costText(p.cost_usd)}${extra}`);
    } else setRecord("rec-peak", "–", none);
    const s = rec.heaviest_session;
    if (s) setRecord("rec-session", `${fmtCompact(s.total_tokens)} tokens`,
      `${toolLabel(s.tool)} · ${fmtDayShort(s.date)} · ${costText(s.cost_usd)}`);
    else setRecord("rec-session", "–", none);
    const st = rec.longest_streak;
    if (st) setRecord("rec-streak", plural(st.days, "day"),
      st.start === st.end ? fmtDayShort(st.start) : `${fmtDayShort(st.start)} – ${fmtDayShort(st.end)}`);
    else setRecord("rec-streak", "–", none);
  }

  // ------------------------------------------------------------------ chart utils
  function niceMax(v) {
    if (!(v > 0)) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(v)));
    const f = v / exp;
    const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
    return nice * exp;
  }
  function topRoundedPath(x, y, w, hh, r) {
    r = Math.max(0, Math.min(r, w / 2, hh));
    return `M${x},${y + hh}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hh}Z`;
  }
  function drawYAxis(root, m, innerW, innerH, max, fmt) {
    for (let i = 0; i <= 4; i++) {
      const v = (max * i) / 4;
      const y = m.t + innerH - (innerH * i) / 4;
      root.append(svg("line", { class: i === 0 ? "baseline" : "grid-line", x1: m.l, x2: m.l + innerW, y1: y, y2: y }));
      const label = svg("text", { class: "tick-label", x: m.l - 8, y: y + 4, "text-anchor": "end" });
      label.textContent = fmt(v);
      root.append(label);
    }
  }

  // ------------------------------------------------------------------ trend (line chart)
  function periodTitle(unit, start) {
    if (unit === "day") return fmtDay(start, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
    if (unit === "week") return "Week of " + fmtDay(start);
    return fmtDay(start, { month: "long", year: "numeric" });
  }
  function periodTick(unit, start) {
    if (unit === "month") return fmtDay(start, { month: "short", year: "2-digit" });
    return fmtDay(start, { month: "short", day: "numeric" });
  }
  const UNIT_TITLE = { day: "Daily Trend", week: "Weekly Trend", month: "Monthly Trend" };
  let gradSeq = 0;

  function trendSeries(ts) {
    if (Array.isArray(ts.series)) {
      return ts.series.map((s) => ({
        id: s.tool, label: s.label, color: TOOL_COLORS[s.tool] || OTHER_TOOL_COLOR,
        points: s.points, totals: s.totals,
      }));
    }
    return [{ id: "all", label: scopeText() || "All Tools", color: ACCENT, points: ts.points, totals: null }];
  }

  function renderTrend() {
    const ts = state.data.ts;
    const host = $("trend-chart");
    const legend = $("trend-legend");
    if (!ts) return;
    const unit = ts.granularity;
    const tokens = state.metric === "tokens";
    const val = (p) => (tokens ? p.total_tokens : p.cost_usd);
    const exact = tokens ? fmtInt : fmtUsd;
    const points = ts.points;
    const split = Array.isArray(ts.series);
    const series = trendSeries(ts);
    const scope = scopeText();
    $("trend-title").textContent = UNIT_TITLE[unit] || "Trend";
    $("trend-meta").textContent = `${ts.range_label}${scope ? " · " + scope : ""} · `
      + `${tokens ? "tokens" : "equivalent cost"} per ${unit}${split ? " · by agent" : ""}`;

    // Legend only when there is more than one line; it carries the per-agent period total.
    legend.hidden = !split;
    legend.replaceChildren(...(split ? series.map((s) => h("span", null,
      h("span", { class: "swatch line", style: { background: s.color } }),
      s.label,
      h("span", { class: "legend-val", text: tokens ? fmtCompact(s.totals.total_tokens) : fmtUsd(s.totals.cost_usd) })))
      : []));

    const maxVal = Math.max(0, ...series.flatMap((s) => s.points.map(val)));
    if (!points.length || maxVal === 0) {
      const hasTokens = points.some((p) => p.total_tokens > 0);
      host.replaceChildren(emptyState(
        !tokens && hasTokens ? "No priced usage in this range — use Set Price on unpriced models to see cost."
          : split && !series.length ? "No agent has usage in this range yet." : "No usage in this range yet."));
      return;
    }

    const width = Math.max(320, host.clientWidth || 800);
    const height = 260;
    const m = { t: 14, r: 12, b: 26, l: 60 };
    const innerW = width - m.l - m.r;
    const innerH = height - m.t - m.b;
    const max = niceMax(maxVal);
    const n = points.length;
    const band = innerW / n;
    const xAt = (i) => m.l + band * (i + 0.5);
    const yAt = (v) => m.t + innerH - (v / max) * innerH;
    const baseY = m.t + innerH;
    const root = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": `${UNIT_TITLE[unit] || "Trend"}, ${tokens ? "tokens" : "cost"}${split ? ", one line per agent" : ""}` });
    drawYAxis(root, m, innerW, innerH, max, tokens ? fmtCompact : fmtUsd);

    // areas first (under every line), then lines, then markers
    const defs = svg("defs");
    root.append(defs);
    const areaOpacity = split ? 0.10 : 0.26;
    series.forEach((s) => {
      const id = `trend-grad-${++gradSeq}`;
      const grad = svg("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 });
      grad.append(svg("stop", { offset: "0%", "stop-color": s.color, "stop-opacity": areaOpacity }));
      grad.append(svg("stop", { offset: "100%", "stop-color": s.color, "stop-opacity": 0 }));
      defs.append(grad);
      if (n > 1) {
        const line = s.points.map((p, i) => `${i ? "L" : "M"}${xAt(i).toFixed(1)},${yAt(val(p)).toFixed(1)}`).join("");
        root.append(svg("path", {
          class: "trend-area", fill: `url(#${id})`,
          d: `${line}L${xAt(n - 1).toFixed(1)},${baseY}L${xAt(0).toFixed(1)},${baseY}Z`,
        }));
      }
    });
    series.forEach((s) => {
      if (n > 1) {
        const line = s.points.map((p, i) => `${i ? "L" : "M"}${xAt(i).toFixed(1)},${yAt(val(p)).toFixed(1)}`).join("");
        root.append(svg("path", { class: "trend-line", d: line, stroke: s.color }));
      }
      if (n <= MAX_DOTS) {
        s.points.forEach((p, i) => {
          root.append(svg("circle", { class: "dot", cx: xAt(i).toFixed(1), cy: yAt(val(p)).toFixed(1), r: 3, fill: s.color }));
        });
      }
    });

    // x tick labels (about 6)
    const every = Math.max(1, Math.ceil(n / 6));
    points.forEach((p, i) => {
      if (i % every === 0 || i === n - 1) {
        if (i === n - 1 && i % every !== 0 && i % every < every / 2) return;
        const label = svg("text", { class: "tick-label", x: xAt(i), y: height - 8, "text-anchor": "middle" });
        label.textContent = periodTick(unit, p.start);
        root.append(label);
      }
    });

    // hover: vertical guide + enlarged focus markers, full-height hit bands
    const guide = svg("line", { class: "guide", x1: 0, x2: 0, y1: m.t, y2: baseY, visibility: "hidden" });
    const focus = series.map((s) => svg("circle", { class: "dot focus", r: 5, fill: s.color, visibility: "hidden" }));
    root.append(guide, ...focus);
    const showFocus = (i) => {
      const x = xAt(i).toFixed(1);
      guide.setAttribute("x1", x);
      guide.setAttribute("x2", x);
      guide.setAttribute("visibility", "visible");
      focus.forEach((c, k) => {
        c.setAttribute("cx", x);
        c.setAttribute("cy", yAt(val(series[k].points[i])).toFixed(1));
        c.setAttribute("visibility", "visible");
      });
    };
    const hideFocus = () => {
      guide.setAttribute("visibility", "hidden");
      focus.forEach((c) => c.setAttribute("visibility", "hidden"));
    };

    points.forEach((p, i) => {
      const hit = svg("rect", { class: "hover-band", x: m.l + i * band, y: m.t, width: band, height: innerH });
      bindTip(hit, () => {
        let rows;
        if (split) {
          rows = [
            ...series.map((s) => ({ label: s.label, value: exact(val(s.points[i])), color: s.color })),
            "sep",
            { label: tokens ? "Total tokens" : "Total cost", value: exact(val(p)) },
            tokens ? { label: "Cost", value: fmtUsd(p.cost_usd) } : { label: "Tokens", value: fmtInt(p.total_tokens) },
            { label: "Requests", value: fmtInt(p.events) },
          ];
        } else if (tokens) {
          rows = [
            ...CATS.map((c) => ({ label: c.label, value: fmtInt(p[c.key]), color: c.color })),
            "sep",
            { label: "Total tokens", value: fmtInt(p.total_tokens) },
            { label: "Cost", value: fmtUsd(p.cost_usd) },
            { label: "Requests", value: fmtInt(p.events) },
          ];
        } else {
          rows = [
            { label: "Cost", value: fmtUsd(p.cost_usd), color: ACCENT },
            "sep",
            { label: "Tokens", value: fmtInt(p.total_tokens) },
            { label: "Requests", value: fmtInt(p.events) },
          ];
        }
        return [periodTitle(unit, p.start), rows];
      }, () => { hit.classList.add("active"); showFocus(i); }, () => { hit.classList.remove("active"); hideFocus(); });
      root.append(hit);
    });
    host.replaceChildren(root);
  }

  // ------------------------------------------------------------------ breakdown
  function renderBreakdown(bd) {
    const body = $("rank-table").querySelector("tbody");
    const rows = bd.rows.slice(0, RANK_ROWS);
    if (!rows.length) {
      body.replaceChildren(h("tr", null, h("td", { colspan: "5", class: "empty-row", text: "No usage in this range yet." })));
      $("rank-foot").textContent = "";
      return;
    }
    const isModel = bd.by === "model";
    const isTool = bd.by === "tool";
    const grouped = Boolean(bd.group_variants);
    body.replaceChildren(...rows.map((r) => {
      let priceBtn = null;
      if (isModel && r.unpriced_events) {
        priceBtn = h("button", { type: "button", class: "flag-btn", text: "Set Price",
          title: `Set a price for ${r.key}` });
        priceBtn.addEventListener("click", (e) => {
          e.stopPropagation(); // do not also focus the row
          hideTip();
          openPriceDialog(r.key, r.base_model);
        });
      }
      const variantNote = grouped && r.variant_count > 1
        ? h("span", { class: "variant-badge", text: `×${r.variant_count}` }) : null;
      const keyCell = h("td", { class: "key", title: grouped ? (r.variants || []).join("\n") : r.key },
        isTool ? (r.label || r.key) : r.key, variantNote,
        isModel && r.unpriced_events ? h("span", { class: "flag", text: "unpriced" }) : null, priceBtn);
      const share = h("td", null, h("div", { class: "sharebar" },
        h("div", { class: "track" }, h("div", { class: "fill", style: { width: (r.token_share * 100).toFixed(2) + "%" } })),
        h("span", { class: "pct", text: fmtPct(r.token_share) })));
      const active = (isModel && r.key === state.model) || (isTool && r.key === state.tool);
      // A family is not one exact model, so grouped rows do not focus the dashboard.
      const clickable = (isModel && !grouped) || isTool;
      const tr = h("tr", {
        class: clickable ? "clickable" + (active ? " active" : "") : null,
        tabindex: clickable ? "0" : null,
        "aria-selected": clickable ? String(active) : null,
      }, keyCell, share,
        h("td", { class: "num", title: fmtInt(r.total_tokens), text: fmtCompact(r.total_tokens) }),
        h("td", { class: "num", text: fmtUsd(r.cost_usd) }),
        h("td", { class: "num", text: r.cache_hit_rate == null ? "—" : fmtPct(r.cache_hit_rate),
          title: r.cache_hit_rate == null ? "No cache breakdown" : undefined }));
      if (clickable) {
        // Clicking a row focuses the whole dashboard on it; clicking the focused row again clears it.
        const activate = () => {
          hideTip();
          if (isModel) setScope({ model: active ? null : r.key });
          else setScope({ tool: active ? null : r.key });
        };
        tr.addEventListener("click", activate);
        tr.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            activate();
          }
        });
      }
      bindTip(tr, () => [r.key, [
        ...CATS.map((c) => ({ label: c.label, value: fmtInt(r[c.key]), color: c.color })),
        "sep",
        { label: "Requests", value: fmtInt(r.events) },
        { label: "Cost share", value: fmtPct(r.cost_share) },
        { label: "Avg $ / 1M", value: fmtUsd(r.cost_per_mtok) },
        ...(grouped ? ["sep", ...(r.variants || []).map((v) => ({ label: "Variant", value: v }))] : []),
      ]]);
      return tr;
    }));
    const more = bd.rows.length > rows.length ? ` of ${fmtInt(bd.rows.length)}` : "";
    const hint = isModel && grouped ? " · variants merged; turn off Group variants to focus one model"
      : isModel ? (state.model ? " · click the row again to show all models" : " · click a model to focus every chart on it")
      : isTool ? " · click a tool to filter" : "";
    $("rank-foot").textContent =
      `Top ${rows.length}${more} by tokens · shares relative to ${bd.range === "custom" ? bd.range_label : bd.range_label.toLowerCase()} total${hint}`;
  }

  // ------------------------------------------------------------------ hourly
  function renderHourly(hr) {
    const host = $("hour-chart");
    const maxVal = Math.max(0, ...hr.hours.map((x) => x.tokens));
    if (maxVal === 0) {
      $("hour-meta").textContent = "Local time";
      host.replaceChildren(emptyState("No usage in this range yet."));
      return;
    }
    const peak = hr.hours.reduce((a, b) => (b.tokens > a.tokens ? b : a));
    $("hour-meta").textContent = `Peak ${String(peak.hour).padStart(2, "0")}:00 · local time`;

    const width = Math.max(280, host.clientWidth || 420);
    const height = 250;
    const m = { t: 10, r: 6, b: 26, l: 48 };
    const innerW = width - m.l - m.r;
    const innerH = height - m.t - m.b;
    const max = niceMax(maxVal);
    const root = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": "Tokens by hour of day" });
    drawYAxis(root, m, innerW, innerH, max, fmtCompact);
    const band = innerW / 24;
    const barW = Math.max(2, band * 0.66);
    const baseY = m.t + innerH;
    hr.hours.forEach((x, i) => {
      const hgt = x.tokens > 0 ? Math.max(1, (x.tokens / max) * innerH) : 0;
      const bx = m.l + i * band + (band - barW) / 2;
      if (hgt > 0) {
        root.append(svg("path", { d: topRoundedPath(bx, baseY - hgt, barW, hgt, barW >= 6 ? 4 : 1), fill: ACCENT }));
      }
      if (i % 3 === 0) {
        const t = svg("text", { class: "tick-label", x: m.l + i * band + band / 2, y: height - 8, "text-anchor": "middle" });
        t.textContent = String(i).padStart(2, "0");
        root.append(t);
      }
    });
    hr.hours.forEach((x, i) => {
      const hit = svg("rect", { class: "hover-band", x: m.l + i * band, y: m.t, width: band, height: innerH });
      const hh = String(x.hour).padStart(2, "0");
      bindTip(hit, () => [`${hh}:00 – ${hh}:59`, [
        { label: "Tokens", value: fmtInt(x.tokens) },
        { label: "Requests", value: fmtInt(x.events) },
        { label: "Share", value: fmtPct(x.tokens / hr.hours.reduce((a, b) => a + b.tokens, 0)) },
      ]], () => hit.classList.add("active"), () => hit.classList.remove("active"));
      root.append(hit);
    });
    host.replaceChildren(root);
  }

  // ------------------------------------------------------------------ heatmap
  function renderYearSelect(hm) {
    const sel = $("heat-year");
    sel.replaceChildren(...[...hm.years].sort((a, b) => b - a).map((y) =>
      h("option", { value: String(y), selected: y === hm.year, text: String(y) })));
  }
  function quantile(sorted, p) {
    if (!sorted.length) return 0;
    const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil(p * sorted.length) - 1));
    return sorted[idx];
  }
  function renderHeatmap(hm) {
    const host = $("heat-chart");
    $("heat-meta").textContent =
      `${fmtCompact(hm.total_tokens)} tokens · ${plural(hm.active_days, "active day")} in ${hm.year}`;
    const nonzero = hm.days.map((d) => d.tokens).filter((v) => v > 0).sort((a, b) => a - b);
    const q = [0.25, 0.5, 0.75].map((p) => quantile(nonzero, p));
    const level = (v) => (v <= 0 ? 0 : v <= q[0] ? 1 : v <= q[1] ? 2 : v <= q[2] ? 3 : 4);

    const cell = 11;
    const step = 14;
    const left = 32;
    const top = 20;
    const startDow = parseDay(hm.days[0].date).getDay(); // 0 = Sunday (GitHub layout)
    const weeks = Math.ceil((startDow + hm.days.length) / 7);
    const width = left + weeks * step;
    const height = top + 7 * step;
    const root = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": `Daily activity in ${hm.year}` });

    [["Mon", 1], ["Wed", 3], ["Fri", 5]].forEach(([name, row]) => {
      const t = svg("text", { class: "tick-label", x: 0, y: top + row * step + cell - 1 });
      t.textContent = name;
      root.append(t);
    });
    let lastMonthCol = -10;
    hm.days.forEach((d, i) => {
      const idx = startDow + i;
      const col = Math.floor(idx / 7);
      const row = idx % 7;
      if (d.date.endsWith("-01") && col - lastMonthCol >= 3) {
        const t = svg("text", { class: "tick-label", x: left + col * step, y: top - 7 });
        t.textContent = fmtDay(d.date, { month: "short" });
        root.append(t);
        lastMonthCol = col;
      }
      const future = d.date > hm.today;
      const rect = svg("rect", {
        class: "heat-cell", x: left + col * step, y: top + row * step, width: cell, height: cell, rx: 2,
        fill: HEAT[level(d.tokens)], "fill-opacity": future ? 0.35 : 1,
      });
      bindTip(rect, () => [fmtDay(d.date, { weekday: "short", month: "short", day: "numeric", year: "numeric" }), [
        { label: "Tokens", value: fmtInt(d.tokens) },
        { label: "Requests", value: fmtInt(d.events) },
        { label: "Cost", value: fmtUsd(d.cost_usd) },
      ]]);
      root.append(rect);
    });
    host.replaceChildren(root);

    $("heat-legend").replaceChildren(
      h("span", { class: "lbl", text: "Less" }),
      ...HEAT.map((c) => h("span", { class: "swatch", style: { background: c } })),
      h("span", { class: "lbl", text: "More" }));
  }

  // ------------------------------------------------------------------ set price
  const PRICE_FIELDS = ["input", "output", "thinking", "cache_read", "cache_write"];
  const dlg = $("price-dialog");
  const priceTarget = { model: null, base: null, mode: "set" };
  const PRICE_TITLES = { set: "Set Price", edit: "Edit Price", add: "Add Model" };

  // mode "set": price an unpriced ledger model (optionally its base model);
  // "edit": change an existing pricing.json row, prefilled with its current price;
  // "add": type a new model name.
  function openPriceDialog(model, base, opts = {}) {
    const mode = opts.mode || "set";
    priceTarget.mode = mode;
    priceTarget.model = model || "";
    priceTarget.base = mode === "set" ? base || null : null;
    $("price-title").textContent = PRICE_TITLES[mode];
    $("price-model").textContent = priceTarget.model;
    $("price-model").hidden = mode === "add";
    $("price-model-input").hidden = mode !== "add";
    $("price-model-input").value = "";
    $("price-base-row").hidden = !priceTarget.base;
    $("price-base").checked = Boolean(priceTarget.base);
    $("price-base-name").textContent = priceTarget.base || "";
    const prefill = opts.prefill || {};
    for (const f of PRICE_FIELDS) {
      const v = prefill[f];
      $(`price-${f}`).value = typeof v === "number" && Number.isFinite(v) ? String(v) : "";
    }
    setPriceError(null);
    $("price-save").disabled = false;
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
    (mode === "add" ? $("price-model-input") : $("price-input")).focus();
  }
  function closePriceDialog() {
    if (typeof dlg.close === "function") dlg.close();
    else dlg.removeAttribute("open");
  }
  function setPriceError(msg) {
    const el = $("price-error");
    el.textContent = msg || "";
    el.hidden = !msg;
  }
  function errorText(detail) {
    if (Array.isArray(detail)) {
      return detail.map((d) => {
        const field = (d.loc || []).filter((x) => x !== "body").join(".");
        return field ? `${field}: ${d.msg}` : d.msg;
      }).join("; ");
    }
    return String(detail || "Request failed");
  }

  let toastTimer;
  function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 6000);
  }

  async function savePrice(e) {
    e.preventDefault(); // never a real form submission (CSP form-action 'none')
    let model = $("price-base").checked && priceTarget.base ? priceTarget.base : priceTarget.model;
    if (priceTarget.mode === "add") {
      model = $("price-model-input").value.trim();
      if (!model) {
        setPriceError("Enter a model name, e.g. gpt-6-luna.");
        $("price-model-input").focus();
        return;
      }
    }
    const payload = { model };
    for (const f of PRICE_FIELDS) {
      const raw = $(`price-${f}`).value.trim();
      if (raw === "") {
        if (f === "thinking") continue;
        setPriceError(`Enter a price for ${f.replace("_", " ")} (use 0 if it is free).`);
        $(`price-${f}`).focus();
        return;
      }
      const v = Number(raw);
      if (!Number.isFinite(v) || v < 0) {
        setPriceError(`${f.replace("_", " ")} must be a number ≥ 0.`);
        $(`price-${f}`).focus();
        return;
      }
      payload[f] = v;
    }
    setPriceError(null);
    $("price-save").disabled = true;
    try {
      const res = await fetch("/api/pricing", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setPriceError(errorText(body.detail) || `${res.status} ${res.statusText}`);
        return;
      }
      closePriceDialog();
      const rp = body.reprice;
      const n = body.matched_models.length;
      toast(`${body.replaced ? "Updated" : "Saved"} ${body.model} · ${plural(n, "model")} priced` +
        (rp ? ` · total ${fmtUsd(rp.cost_before)} → ${fmtUsd(rp.cost_after)}` : ""));
      if (pricingOpen()) {
        pricing.flash = body.model;
        pricing.keep = body.model; // stays listed under Used only even before it is used
        loadPricing();
      }
      loadTools();
      loadAll();
    } catch (err) {
      setPriceError(String(err.message || err));
    } finally {
      $("price-save").disabled = false;
    }
  }

  $("price-form").addEventListener("submit", savePrice);
  $("price-cancel").addEventListener("click", closePriceDialog);
  dlg.addEventListener("click", (e) => { if (e.target === dlg) closePriceDialog(); }); // backdrop

  // ------------------------------------------------------------------ pricing table
  const pdlg = $("pricing-dialog");
  const PROVIDER_ORDER = ["Claude", "Gemini", "GPT", "Other"];
  // usedOnly (default on): list only price rows the ledger has used. `keep` is a model
  // just added / edited here, shown even when unused until the dialog closes.
  const pricing = { data: null, provider: "", q: "", seq: 0, flash: null, tab: "models", usedOnly: true, keep: null };
  const pricingOpen = () => pdlg.open || pdlg.hasAttribute("open");

  function fmtPrice(v) {
    if (typeof v !== "number" || !Number.isFinite(v)) return "—";
    return v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  }

  async function loadPricing() {
    const my = ++pricing.seq;
    try {
      const res = await api("/api/pricing");
      if (my !== pricing.seq) return;
      pricing.data = res;
      renderPricing();
    } catch (err) {
      if (my !== pricing.seq) return;
      $("pricing-body").replaceChildren();
      const empty = $("pricing-empty");
      empty.textContent = `Could not load prices: ${err.message || err}`;
      empty.hidden = false;
    }
  }

  const pricingVisible = (m) => !pricing.usedOnly || m.used === true || m.model === pricing.keep;
  function pricingRows() {
    const models = (pricing.data && pricing.data.models) || [];
    const q = pricing.q.trim().toLowerCase();
    return models.filter((m) => pricingVisible(m) &&
      (!pricing.provider || m.provider === pricing.provider) &&
      (!q || m.model.toLowerCase().includes(q)));
  }

  const fmtDayLong = (iso) => fmtDay(iso, { month: "short", day: "numeric", year: "numeric" });
  function priceHistoryTitle(m) {
    const rows = (m.history || []).map((s) =>
      `${s.effective_from ? "From " + fmtDayLong(s.effective_from) : "Before " + fmtDayLong(m.effective_from)}: ` +
      `input ${fmtPrice(s.input)} · output ${fmtPrice(s.output)} · cache read ${fmtPrice(s.cache_read)}`);
    return [`Current price since ${fmtDayLong(m.effective_from)}`, ...rows].join("\n");
  }

  function renderPricing() {
    const d = pricing.data;
    if (!d) return;
    $("pricing-unit").textContent = d.unit_label || "USD per 1M tokens";
    $("pricing-updated").textContent = d.updated ? fmtDayLong(d.updated) : "—";
    const usedCount = d.used_count || 0;
    $("pricing-count").textContent = pricing.usedOnly
      ? `${fmtInt(usedCount)} used of ${plural(d.count, "model")}` : plural(d.count, "model");
    $("pricing-used").checked = pricing.usedOnly;

    // provider counts follow the Used only switch
    const counts = {};
    let shown = 0;
    for (const m of d.models || []) {
      if (!pricingVisible(m)) continue;
      counts[m.provider] = (counts[m.provider] || 0) + 1;
      shown += 1;
    }
    for (const b of $("pricing-provider").querySelectorAll("button")) {
      const p = b.dataset.provider;
      const n = p ? counts[p] || 0 : shown;
      if (p === "Other") b.hidden = n === 0 && pricing.provider !== "Other";
      b.setAttribute("aria-pressed", String(p === pricing.provider));
      b.replaceChildren(p || "All", h("span", { class: "count", text: String(n) }));
    }

    const rows = pricingRows();
    const body = $("pricing-body");
    const grouped = !pricing.provider && !pricing.q.trim();
    const out = [];
    let group = null;
    for (const m of rows) {
      if (grouped && m.provider !== group) {
        group = m.provider;
        out.push(h("tr", { class: "group" }, h("td", { colspan: "7", text: group })));
      }
      const cells = PRICE_FIELDS.map((f) =>
        h("td", { class: m[f] === 0 ? "num zero" : "num", text: fmtPrice(m[f]) }));
      const edit = h("button", {
        type: "button", class: "btn btn-xs", text: "Edit", "aria-label": `Edit price of ${m.model}`,
      });
      edit.addEventListener("click", () => openPriceDialog(m.model, null, { mode: "edit", prefill: m }));
      const name = h("td", { class: "model", text: m.model });
      if (m.effective_from) { // dated prices: say since when the shown price applies
        name.append(h("span", { class: "since", text: `since ${fmtDayLong(m.effective_from)}` +
          ((m.history || []).length ? ` · ${plural(m.history.length + 1, "price period")}` : "") }));
        name.title = priceHistoryTitle(m);
      }
      const tr = h("tr", { "data-model": m.model, class: m.model === pricing.flash ? "flash" : null },
        name, ...cells, h("td", { class: "act" }, edit));
      out.push(tr);
    }
    body.replaceChildren(...out);
    const empty = $("pricing-empty");
    empty.hidden = rows.length > 0;
    if (!rows.length) {
      const hiddenNote = pricing.usedOnly && d.count ? " Turn off Used only to see every model." : "";
      empty.textContent = pricing.q.trim()
        ? `No ${pricing.usedOnly ? "used " : ""}models match “${pricing.q.trim()}”.${hiddenNote}`
        : !d.count ? "No models in pricing.json yet — use + Add Model."
        : pricing.provider ? `No used ${pricing.provider} models yet.${hiddenNote}`
        : `No priced model has been used yet.${hiddenNote}`;
    }
    if (pricing.flash) {
      const hit = body.querySelector("tr.flash");
      if (hit && typeof hit.scrollIntoView === "function") hit.scrollIntoView({ block: "nearest" });
      pricing.flash = null;
    }
  }

  function setPricingTab(tab) {
    pricing.tab = tab;
    for (const b of $("pricing-tabs").querySelectorAll("button")) {
      b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    }
    $("pricing-models-pane").hidden = tab !== "models";
    $("pricing-subs-pane").hidden = tab !== "subs";
    if (tab === "subs") loadSubscriptions();
  }
  function openPricing(tab) {
    if (typeof pdlg.showModal === "function") pdlg.showModal();
    else pdlg.setAttribute("open", "");
    setPricingTab(tab === "subs" ? "subs" : "models");
    loadPricing();
    if (pricing.tab === "models") $("pricing-search").focus();
  }

  // ------------------------------------------------------------------ subscriptions
  const subs = { data: null, draft: null, seq: 0 };
  function setSubsError(msg) {
    $("subs-error").textContent = msg || "";
    $("subs-error").hidden = !msg;
  }
  async function loadSubscriptions() {
    const my = ++subs.seq;
    try {
      const res = await api("/api/subscriptions");
      if (my !== subs.seq) return;
      subs.data = res;
      subs.draft = draftFromData(res); // reopening the tab discards unsaved edits
      renderSubscriptions();
    } catch (err) {
      setSubsError(`Could not load subscriptions: ${err.message || err}`);
    }
  }
  const nextDay = (iso) => addDays(iso, 1); // isRealDay / addDays: see "calendar days" above
  // draft: {tool: [{start, end, fee}]} as strings, exactly what the inputs show
  function draftFromData(d) {
    const draft = {};
    for (const x of d.tools) {
      draft[x.tool] = x.periods.map((p) => ({ start: p.start || "", end: p.end || "", fee: String(p.monthly_usd) }));
    }
    return draft;
  }
  function readDraft() {
    const d = subs.data;
    const draft = {};
    for (const x of d.tools) {
      draft[x.tool] = (subs.draft[x.tool] || []).map((_, i) => ({
        start: String($(`sub-${x.tool}-${i}-start`).value).trim(),
        end: String($(`sub-${x.tool}-${i}-end`).value).trim(),
        fee: String($(`sub-${x.tool}-${i}-fee`).value).trim(),
      }));
    }
    return draft;
  }
  function toolStatus(x) {
    if (x.monthly_usd > 0) return `${fmtUsd(x.monthly_usd)}/mo now`;
    const t = subs.data.today;
    const paid = x.periods.filter((p) => p.monthly_usd > 0);
    if (!paid.length) return x.periods.length ? "Free plan" : "No plan";
    const next = paid.find((p) => p.start && p.start > t);
    if (next) return `Starts ${fmtDayShort(next.start)}`;
    const ended = paid.filter((p) => p.end && p.end < t).pop();
    return ended ? `Ended ${fmtDayShort(ended.end)}` : "No plan today";
  }
  function periodRow(x, p, i, writable) {
    const id = (f) => `sub-${x.tool}-${i}-${f}`;
    const legacy = !p.start && !p.end && subs.draft[x.tool].length === 1 && x.legacy;
    return h("div", { class: "sub-row", "data-tool": x.tool, "data-index": String(i) },
      h("label", { class: "field" }, h("span", null, "Start"),
        h("input", { type: "date", id: id("start"), value: p.start, disabled: !writable, required: true })),
      h("label", { class: "field" }, h("span", null, "End", h("em", { text: "empty = Ongoing" })),
        h("input", { type: "date", id: id("end"), value: p.end, disabled: !writable })),
      h("label", { class: "field" }, h("span", null, "USD / month"),
        h("input", { type: "number", id: id("fee"), min: "0", max: String(subs.data.max_monthly_usd), step: "any",
          inputmode: "decimal", value: p.fee, disabled: !writable })),
      h("button", { type: "button", class: "btn btn-xs sub-remove", "data-tool": x.tool, "data-index": String(i),
        "aria-label": `Remove ${x.label} period ${i + 1}`, disabled: !writable, text: "Remove" }),
      legacy ? h("p", { class: "sub-note", text: "No dates: counts every day. Add a start date to limit it." }) : null);
  }
  function renderSubscriptions() {
    const d = subs.data;
    if (!d) return;
    if (!subs.draft) subs.draft = draftFromData(d);
    $("subs-list").replaceChildren(...d.tools.map((x) => {
      const rows = subs.draft[x.tool] || [];
      return h("section", { class: "sub-tool", "data-tool": x.tool },
        h("header", { class: "sub-head" },
          h("h3", { text: x.label }),
          h("span", { class: "sub-status", id: `sub-${x.tool}-status`, text: toolStatus(x) }),
          h("button", { type: "button", class: "btn btn-xs sub-add", id: `sub-${x.tool}-add`, "data-tool": x.tool,
            disabled: !d.writable || rows.length >= d.max_periods, text: "+ Add period" })),
        rows.length ? h("div", { class: "sub-rows" }, rows.map((p, i) => periodRow(x, p, i, d.writable)))
          : h("p", { class: "sub-empty", text: "No plan periods." }));
    }));
    $("subs-save").disabled = !d.writable;
    setSubsError(d.error ? `subscriptions.json: ${d.error} (saving replaces it)` :
      d.writable ? null : "This server was started without a subscriptions file, so plans are read-only.");
  }
  function addPeriod(tool) {
    const draft = readDraft();
    const rows = draft[tool];
    const prev = rows.filter((r) => r.start).sort((a, b) => (a.start < b.start ? -1 : 1)).pop();
    const start = prev ? (prev.end && isRealDay(prev.end) ? nextDay(prev.end) : "") : subs.data.today;
    rows.push({ start, end: "", fee: prev ? prev.fee : "" });
    subs.draft = draft;
    renderSubscriptions();
    $(`sub-${tool}-${rows.length - 1}-${start ? "fee" : "start"}`).focus();
  }
  function removePeriod(tool, index) {
    const draft = readDraft();
    draft[tool].splice(index, 1);
    subs.draft = draft;
    renderSubscriptions();
  }
  // -> {subscriptions} body, or {error, focus} for the first problem found
  function buildPlans(draft) {
    const d = subs.data;
    const out = {};
    for (const x of d.tools) {
      const rows = draft[x.tool] || [];
      const where = (i) => (rows.length > 1 ? `${x.label} period ${i + 1}` : x.label);
      const periods = [];
      for (let i = 0; i < rows.length; i++) {
        const r = rows[i];
        const fee = r.fee === "" ? NaN : Number(r.fee);
        if (!Number.isFinite(fee) || fee < 0 || fee > d.max_monthly_usd) {
          return { error: `${where(i)}: enter a monthly fee between 0 and ${fmtInt(d.max_monthly_usd)}.`, focus: `sub-${x.tool}-${i}-fee` };
        }
        if (!r.start && !r.end && rows.length === 1) { out[x.tool] = fee; break; } // undated (legacy) plan
        if (!isRealDay(r.start)) return { error: `${where(i)}: enter a start date.`, focus: `sub-${x.tool}-${i}-start` };
        if (r.end && !isRealDay(r.end)) return { error: `${where(i)}: the end date is not valid.`, focus: `sub-${x.tool}-${i}-end` };
        if (r.end && r.end < r.start) return { error: `${where(i)}: the end date is before the start date.`, focus: `sub-${x.tool}-${i}-end` };
        periods.push({ i, start: r.start, end: r.end || null, monthly_usd: fee });
      }
      if (x.tool in out) continue;
      periods.sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0));
      for (let k = 1; k < periods.length; k++) {
        const a = periods[k - 1], b = periods[k];
        if (a.end == null || b.start <= a.end) {
          return { error: `${x.label}: periods overlap. A period after ${a.end ? fmtDayShort(a.end) : "an ongoing one"} must start ` +
            (a.end ? `on ${fmtDayShort(nextDay(a.end))} or later.` : "after it ends; give the ongoing one an end date."),
            focus: `sub-${x.tool}-${a.end == null ? a.i : b.i}-${a.end == null ? "end" : "start"}` };
        }
      }
      if (periods.length) out[x.tool] = periods.map(({ start, end, monthly_usd }) => ({ start, end, monthly_usd }));
    }
    return { subscriptions: out };
  }
  async function saveSubscriptions(e) {
    e.preventDefault(); // CSP form-action 'none': never a real submission
    const d = subs.data;
    if (!d || !d.writable) return;
    subs.draft = readDraft();
    const plan = buildPlans(subs.draft);
    if (plan.error) {
      setSubsError(plan.error);
      $(plan.focus).focus();
      return;
    }
    setSubsError(null);
    $("subs-save").disabled = true;
    try {
      const res = await fetch("/api/subscriptions", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ subscriptions: plan.subscriptions }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setSubsError(errorText(body.detail) || `${res.status} ${res.statusText}`);
        return;
      }
      subs.data = body;
      subs.draft = draftFromData(body);
      renderSubscriptions();
      const active = body.tools.filter((x) => x.monthly_usd > 0);
      const anyPaid = body.tools.some((x) => x.periods.some((p) => p.monthly_usd > 0));
      toast(active.length ? `Subscriptions saved · ${active.map((x) => `${x.label} ${fmtUsd(x.monthly_usd)}/mo`).join(" · ")}`
        : anyPaid ? "Subscriptions saved · no plan active today"
          : "Subscriptions saved · no paid plans, ROI hidden");
      loadAll();
    } catch (err) {
      setSubsError(String(err.message || err));
    } finally {
      $("subs-save").disabled = !(subs.data && subs.data.writable);
    }
  }

  // ------------------------------------------------------------------ sync now
  async function syncNow() {
    if (state.syncing) return;
    const btn = $("sync-now");
    state.syncing = true;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.textContent = "Syncing…";
    try {
      const res = await fetch("/api/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: "{}",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(`Sync failed · ${errorText(body.detail) || res.status + " " + res.statusText}`);
        return;
      }
      if (body.status === "not_configured") {
        toast(body.message);
        return;
      }
      const tot = body.totals;
      const parts = body.by_tool.filter((x) => x.inserted || x.updated)
        .map((x) => `${x.label} +${fmtInt(x.inserted)}` + (x.updated ? ` / ${fmtInt(x.updated)} updated` : ""));
      const failed = body.sources.filter((x) => x.status === "failed");
      toast(`${failed.length ? "Synced with errors" : "Synced"} · ${fmtInt(tot.inserted)} new · ${fmtInt(tot.updated)} updated` +
        (parts.length ? ` · ${parts.join(", ")}` : " · nothing new") +
        (failed.length ? ` · failed: ${failed.map((x) => x.name).join(", ")}` : ""));
      loadTools();
      loadAll();
    } catch (err) {
      toast(`Sync failed · ${err.message || err}`);
    } finally {
      state.syncing = false;
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      btn.textContent = "Sync Now";
    }
  }

  // ------------------------------------------------------------------ keyboard shortcuts
  // 1-8 range (8 = Custom) · P pricing · S sync · Esc clears the model focus. Never while typing or
  // while a dialog is open (a dialog handles its own Esc natively).
  const dialogOpen = () => [dlg, pdlg].some((d) => d.open || d.hasAttribute("open"));
  function isTyping(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toUpperCase();
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || Boolean(el.isContentEditable);
  }
  function onShortcut(e) {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.repeat) return;
    if (isTyping(e.target) || dialogOpen()) return;
    const key = e.key;
    if (/^[1-8]$/.test(key)) {
      e.preventDefault();
      setRange(RANGES[Number(key) - 1], true);
    } else if (key === "p" || key === "P") {
      e.preventDefault();
      openPricing();
    } else if (key === "s" || key === "S") {
      e.preventDefault();
      syncNow();
    } else if (key === "Escape") {
      if (state.model) {
        e.preventDefault();
        setScope({ model: null });
      }
    }
  }
  function closePricing() {
    if (typeof pdlg.close === "function") pdlg.close();
    else pdlg.removeAttribute("open");
    pricing.keep = null;
  }
  pdlg.addEventListener("close", () => { pricing.keep = null; }); // Esc closes natively

  $("open-pricing").addEventListener("click", () => openPricing());
  $("pricing-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b && b.dataset.tab !== pricing.tab) setPricingTab(b.dataset.tab);
  });
  $("subs-form").addEventListener("submit", saveSubscriptions);
  $("subs-list").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn || btn.disabled || !subs.data || !subs.data.writable) return;
    if (btn.classList.contains("sub-add")) addPeriod(btn.dataset.tool);
    else if (btn.classList.contains("sub-remove")) removePeriod(btn.dataset.tool, Number(btn.dataset.index));
  });
  $("pricing-close").addEventListener("click", closePricing);
  pdlg.addEventListener("click", (e) => { if (e.target === pdlg) closePricing(); }); // backdrop
  $("pricing-add").addEventListener("click", () => openPriceDialog("", null, { mode: "add" }));
  $("pricing-search").addEventListener("input", (e) => {
    pricing.q = e.target.value;
    renderPricing();
  });
  $("pricing-used").checked = pricing.usedOnly;
  $("pricing-used").addEventListener("change", (e) => {
    pricing.usedOnly = !!e.target.checked;
    renderPricing();
  });
  $("pricing-provider").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-provider]");
    if (!b) return;
    pricing.provider = b.dataset.provider;
    renderPricing();
  });

  // ------------------------------------------------------------------ boot
  initControls();
  loadTools();
  loadAll();
})();
