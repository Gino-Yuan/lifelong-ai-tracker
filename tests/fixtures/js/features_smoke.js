// JavaScriptCore runtime test for Cache Saved / ROI / Personal Records / Group variants /
// keyboard shortcuts / Sync Now / Subscriptions (run by tests/test_enhancements.py).
// Usage: jsc stub_dom.js features_smoke.js -- <app.js> <recorded-responses.json>
// The JSON holds real API responses recorded from a fixture ledger plus the expected texts.
const [APP_JS, RESP_JSON] = arguments;
const FX = JSON.parse(readFile(RESP_JSON));
const R = FX.responses;
const X = FX.expect;

let syncReply = null;   // { status, body } for the next POST /api/sync (default: R.sync)
let syncPending = null; // resolves the in-flight POST /api/sync
let noPlans = false;    // after saving no paid plans the overview has no subscription
let noPlanDays = false; // paid plans exist but none overlaps the range
let feesError = null;   // simulate a broken subscriptions.json
let subsReadonly = false;
let subsSaved = null;   // the last real POST /api/subscriptions reply
let scoped = false;     // serve the overview recorded for the requested range x tool
const clone = (o) => JSON.parse(JSON.stringify(o));

function ROUTE(url, opts) {
  const path = url.split("?")[0];
  if (opts.method === "POST") {
    if (path === "/api/sync") {
      return new Promise((resolve) => {
        syncPending = () => {
          const r = syncReply || { status: 200, body: R.sync };
          resolve({ ok: r.status < 300, status: r.status, statusText: String(r.status), json: () => Promise.resolve(r.body) });
        };
      });
    }
    if (path === "/api/subscriptions") {
      const reply = R.subs_posts[opts.body]; // recorded from the real server for this exact body
      if (!reply) return jsonResponse({ detail: "unexpected body " + opts.body }, 422);
      subsSaved = clone(reply);
      noPlans = reply.tools.every((t) => !t.periods.some((p) => p.monthly_usd > 0));
      return jsonResponse(clone(reply));
    }
    return Promise.reject(new Error("unexpected POST " + path));
  }
  if (path === "/api/breakdown") {
    return jsonResponse(url.includes("group_variants=true") ? R.breakdown_grouped : R.breakdown);
  }
  if (path === "/api/overview") {
    const q = new URLSearchParams(url.split("?")[1] || "");
    const scopeKey = `${q.get("range")}|${q.get("tool") || ""}`;
    // (an unrecorded in-between scope, e.g. while switching tool then range, gets the default overview)
    const ov = clone(noPlanDays ? R.overview_no_plan_days
      : scoped && R.overview_scoped[scopeKey] ? R.overview_scoped[scopeKey] : R.overview);
    if (noPlans) ov.subscription = null;
    if (feesError) { ov.subscription = null; ov.subscription_error = feesError; }
    return jsonResponse(ov);
  }
  if (path === "/api/subscriptions") {
    const s = clone(subsSaved || R.subscriptions);
    if (subsReadonly) s.writable = false;
    return jsonResponse(s);
  }
  const key = { "/api/tools": "tools", "/api/timeseries": "timeseries", "/api/hourly": "hourly",
    "/api/heatmap": "heatmap", "/api/pricing": "pricing" }[path];
  if (key) return jsonResponse(R[key]);
  return Promise.reject(new Error("offline stub " + url));
}

location.hash = "#range=7d"; // the dashboard boots from the URL hash (no group -> variants grouped)
load(APP_JS);
const settle = () => { for (let i = 0; i < 4; i++) drainMicrotasks(); };
settle();

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const gets = (from, path) => requests.slice(from)
  .filter((r) => r.url.split("?")[0] === path && !(r.opts && r.opts.method)).map((r) => r.url);
const posts = (from, path) => requests.slice(from).filter((r) => r.url === path && r.opts && r.opts.method === "POST");
const params = (url) => new URLSearchParams(url.split("?")[1] || "");
const last = (arr) => arr[arr.length - 1];
const tbody = $("rank-table").children[0];
const hashHas = (kv) => location.hash.replace(/^#/, "").split("&").includes(kv);

// ---------------------------------------------------------------- boot from hash
const ov0 = gets(0, "/api/overview");
check(ov0.length === 1 && params(ov0[0]).get("range") === "7d", "hash range=7d drives the first overview " + ov0);
const bd0 = gets(0, "/api/breakdown");
check(bd0.length === 1 && params(bd0[0]).get("by") === "model" && params(bd0[0]).get("group_variants") === "true",
  "no group in the hash requests merged variants by default " + bd0);
check($("group-variants").checked && $("group-variants").attrs["aria-checked"] === "true", "group switch on by default");
check(!location.hash.includes("group"), "default-on is not written to the hash " + location.hash);
check(!$("group-variants").disabled, "switch enabled for the Model tab");
check($("rank-key-head").textContent === "Model family", "header says Model family");

// ---------------------------------------------------------------- KPIs
check($("kpi-saved").textContent === X.saved, "Cache Saved KPI " + $("kpi-saved").textContent + " vs " + X.saved);
check($("kpi-saved-sub").textContent === X.saved_sub, "Cache Saved sub " + $("kpi-saved-sub").textContent);
check(!$("kpi-roi-card").hidden, "ROI card shown when a plan fee > 0");
check($("kpi-roi").textContent === X.roi, "ROI KPI " + $("kpi-roi").textContent + " vs " + X.roi);
check(/^\d+(\.\d+)?× value$/.test($("kpi-roi").textContent), "ROI reads like 14.2× value");
check($("kpi-roi-sub").textContent.endsWith(" · " + X.roi_days), "ROI sub has the prorated days " + $("kpi-roi-sub").textContent);
check($("kpi-roi-card").title === X.roi_title, "ROI tooltip per tool " + JSON.stringify($("kpi-roi-card").title) + " vs " + JSON.stringify(X.roi_title));
check($("kpi-roi-card").title.startsWith("Claude Code: $100.00/mo · "), "tooltip shows the fee in force at the range end");
check($("kpi-roi-spent").textContent === X.roi_spent && !$("kpi-roi-unit").hidden,
  "Plan Spent shows the full amount " + $("kpi-roi-spent").textContent + " vs " + X.roi_spent);
check(/^\$[\d,]+\.\d{2}$/.test($("kpi-roi-spent").textContent) && !$("kpi-roi-spent").textContent.includes("…"),
  "spent amount is a complete dollar figure " + $("kpi-roi-spent").textContent);
check($("kpi-roi-sub").textContent === X.roi_sub, "Plan sub " + JSON.stringify($("kpi-roi-sub").textContent) + " vs " + JSON.stringify(X.roi_sub));
check(/ API value · (Saved|Net loss) \$[\d,.]+ · /.test($("kpi-roi-sub").textContent), "sub has API value and net saved");

// ---------------------------------------------------------------- Plan Spent follows range + tool
const RANGE_ORDER = ["today", "7d", "30d", "90d", "ytd", "last_year", "lifelong"];
const toolButton = (id) => $("tool-control").children.find((b) => (b.dataset.tool || "") === id);
scoped = true;
for (const [rng, tool] of X.plan_scopes) {
  toolButton(tool).fire("click"); settle();
  keydown(String(RANGE_ORDER.indexOf(rng) + 1)); settle();
  const key = `${rng}|${tool}`, want = X.plan_cards[key];
  const o = last(gets(0, "/api/overview"));
  check(params(o).get("range") === rng && (params(o).get("tool") || "") === tool, `overview requested for ${key}: ${o}`);
  if (want.hidden) {
    check($("kpi-roi-card").hidden, `Plan card hidden for ${key} (no plan for that tool)`);
  } else {
    check(!$("kpi-roi-card").hidden && $("kpi-roi-spent").textContent === want.spent && $("kpi-roi").textContent === want.roi &&
      $("kpi-roi-sub").textContent === want.sub,
      `Plan card for ${key}: ` + JSON.stringify([$("kpi-roi-spent").textContent, $("kpi-roi").textContent, $("kpi-roi-sub").textContent]) +
      " vs " + JSON.stringify(want));
  }
}
scoped = false;
toolButton("").fire("click"); settle();
keydown("2"); settle();
check(!$("kpi-roi-card").hidden && $("kpi-roi-spent").textContent === X.roi_spent && !params(last(gets(0, "/api/overview"))).has("tool"),
  "back to All Tools · 7d");

// ---------------------------------------------------------------- records
check($("rec-peak").textContent === X.peak, "peak day " + $("rec-peak").textContent);
check($("rec-peak-sub").textContent.startsWith(X.peak_sub), "peak day date " + $("rec-peak-sub").textContent);
check($("rec-session").textContent === X.session, "heaviest session " + $("rec-session").textContent);
check($("rec-session-sub").textContent.startsWith(X.session_sub), "heaviest session sub " + $("rec-session-sub").textContent);
check(X.forbidden.every((w) => !$("rec-session-sub").textContent.includes(w)) && !$("rec-session-sub").textContent.includes("/"),
  "heaviest session shows no ids / paths / projects");
check($("rec-streak").textContent === X.streak, "longest streak " + $("rec-streak").textContent);
check($("rec-streak-sub").textContent === X.streak_sub, "streak span " + $("rec-streak-sub").textContent);

// ---------------------------------------------------------------- grouped breakdown
const gRows = tbody.children;
check(gRows.length === Math.min(15, R.breakdown_grouped.rows.length), "grouped rows rendered " + gRows.length);
const badges = gRows.map((r) => r.children[0].children.find((c) => c.className === "variant-badge")).filter(Boolean);
check(badges.length === X.multi_variant_families && badges.every((b) => /^×\d+$/.test(b.textContent)),
  "variant badges " + badges.map((b) => b.textContent));
check(gRows.every((r) => !r.className.includes("clickable") && !("tabindex" in r.attrs)), "family rows are not clickable");
check(gRows[0].children[0].attrs.title === R.breakdown_grouped.rows[0].variants.join("\n"), "family tooltip lists variants");
check($("rank-foot").textContent.includes("variants merged"), "foot explains merging");

let mark = requests.length;
$("group-variants").checked = false;
$("group-variants").fire("change");
settle();
const bd1 = gets(mark, "/api/breakdown");
check(bd1.length === 1 && !params(bd1[0]).has("group_variants"), "switch off -> plain breakdown " + bd1);
check(gets(mark, "/api/overview").length === 0, "the switch reloads only the breakdown");
check(hashHas("group=0") && hashHas("range=7d"), "switch off writes group=0 " + location.hash);
check($("rank-key-head").textContent === "Model", "header back to Model");
check(tbody.children.filter((r) => r.className.includes("clickable")).length === Math.min(15, R.breakdown.rows.length),
  "exact-model rows are clickable again");

const rankBtns = $("rank-tabs").children;
rankBtns[1].fire("click"); settle();
check($("group-variants").disabled && $("group-switch").classList.contains("disabled"), "switch disabled for the Tool tab");
check(!params(last(gets(mark, "/api/breakdown"))).has("group_variants") && params(last(gets(mark, "/api/breakdown"))).get("by") === "tool",
  "tool breakdown never asks for variants");
rankBtns[0].fire("click"); settle();
mark = requests.length;
$("group-variants").checked = true;
$("group-variants").fire("change");
settle();
check(params(last(gets(mark, "/api/breakdown"))).get("group_variants") === "true" && !location.hash.includes("group"), "switch on drops group from the hash " + location.hash);
$("group-variants").checked = false;
$("group-variants").fire("change");
settle();

// ---------------------------------------------------------------- keyboard: ranges
const RANGES = ["today", "7d", "30d", "90d", "ytd", "last_year", "lifelong"];
mark = requests.length;
let ev = keydown("3"); settle();
check(ev.defaultPrevented && params(last(gets(mark, "/api/overview"))).get("range") === "30d" && hashHas("range=30d"), "3 -> Last 30 days");
for (let i = 0; i < 7; i++) {
  mark = requests.length;
  keydown(String(i + 1)); settle();
  const o = gets(mark, "/api/overview");
  check(o.length === 1 && params(o[0]).get("range") === RANGES[i] && gets(mark, "/api/breakdown").length === 1,
    `${i + 1} -> ${RANGES[i]}`);
}
mark = requests.length;
keydown("7"); settle();
check(requests.length === mark, "pressing the current range does not reload");
keydown("9"); keydown("0"); keydown("x"); settle(); // 8 is Custom (range_smoke.js)
check(requests.length === mark, "other keys do nothing");

// ignored while typing or with modifiers
mark = requests.length;
for (const tag of ["input", "textarea", "select"]) { keydown("2", new Node_(tag)); keydown("s", new Node_(tag)); keydown("p", new Node_(tag)); }
const editable = new Node_("div"); editable.isContentEditable = true;
keydown("2", editable);
keydown("2", null, { ctrlKey: true }); keydown("2", null, { metaKey: true }); keydown("2", null, { altKey: true });
keydown("2", null, { repeat: true });
settle();
check(requests.length === mark && !$("pricing-dialog").open, "shortcuts ignored while typing / with modifiers / on repeat");

// ---------------------------------------------------------------- keyboard: pricing + dialogs
ev = keydown("P"); settle();
check(ev.defaultPrevented && $("pricing-dialog").open && !$("pricing-models-pane").hidden && $("pricing-subs-pane").hidden,
  "P opens Pricing on the Models tab");
check(gets(mark, "/api/pricing").length === 1, "pricing loaded");
mark = requests.length;
keydown("1"); keydown("s"); keydown("p"); settle();
check(requests.length === mark, "no shortcuts while the Pricing dialog is open");
$("pricing-close").fire("click");
$("price-dialog").open = true;
keydown("2"); settle();
check(requests.length === mark, "no shortcuts while the Set Price dialog is open");
$("price-dialog").open = false;

// ---------------------------------------------------------------- keyboard: Esc clears the model
const firstModel = R.breakdown.rows[0].key;
tbody.children.find((r) => r.className.includes("clickable")).fire("click");
settle();
check(!$("filterbar").hidden && $("model-chip-value").textContent === firstModel, "row click focuses a model");
check(params(last(gets(mark, "/api/overview"))).get("model") === firstModel, "overview filtered by model");
keydown("p"); settle();
keydown("Escape"); settle(); // the open dialog owns Esc (native <dialog> closes itself)
check(!$("filterbar").hidden, "Esc with a dialog open keeps the model filter");
$("pricing-close").fire("click");
mark = requests.length;
ev = keydown("Escape"); settle();
check(ev.defaultPrevented && $("filterbar").hidden && !hashHas("model=" + firstModel), "Esc clears the model filter");
check(!params(last(gets(mark, "/api/overview"))).has("model"), "overview reloaded without the model");
mark = requests.length;
ev = keydown("Escape"); settle();
check(!ev.defaultPrevented && requests.length === mark, "Esc without a model is a no-op");

// ---------------------------------------------------------------- Sync Now
const btn = $("sync-now");
mark = requests.length;
btn.fire("click");
check(btn.disabled && btn.attrs["aria-busy"] === "true" && btn.textContent === "Syncing…", "button shows the loading state");
btn.fire("click"); keydown("s"); settle();
const sp = posts(mark, "/api/sync");
check(sp.length === 1, "one sync at a time from the page, got " + sp.length);
check(sp[0].opts.body === "{}" && sp[0].opts.headers["Content-Type"] === "application/json", "sync POST is JSON (CSRF guard)");
check(btn.disabled, "still busy until the server answers");
syncPending(); settle();
check(!btn.disabled && !("aria-busy" in btn.attrs) && btn.textContent === "Sync Now", "button restored");
check($("toast").textContent === X.sync_toast && !$("toast").hidden, "sync toast " + $("toast").textContent + " vs " + X.sync_toast);
check(gets(mark, "/api/tools").length === 1 && gets(mark, "/api/overview").length === 1 && gets(mark, "/api/heatmap").length === 1,
  "dashboard data refreshed after sync");

mark = requests.length;
syncReply = { status: 200, body: R.sync_partial };
ev = keydown("s");
check(ev.defaultPrevented && posts(mark, "/api/sync").length === 1, "S triggers Sync Now");
syncPending(); settle();
check($("toast").textContent === X.sync_partial_toast, "partial toast " + $("toast").textContent + " vs " + X.sync_partial_toast);

mark = requests.length;
syncReply = { status: 200, body: R.sync_not_configured };
btn.fire("click"); syncPending(); settle();
check($("toast").textContent === R.sync_not_configured.message, "missing sources.json message " + $("toast").textContent);
check(gets(mark, "/api/overview").length === 0, "no reload when nothing was synced");

syncReply = { status: 409, body: { detail: "A sync is already running - try again in a moment." } };
btn.fire("click"); syncPending(); settle();
check($("toast").textContent === "Sync failed · A sync is already running - try again in a moment.", "409 toast " + $("toast").textContent);
check(!btn.disabled && btn.textContent === "Sync Now", "button restored after an error");

// ---------------------------------------------------------------- Subscriptions tab
$("open-pricing").fire("click"); settle();
mark = requests.length;
const tabs = $("pricing-tabs").children;
tabs[1].fire("click"); settle();
check(!$("pricing-subs-pane").hidden && $("pricing-models-pane").hidden, "Subscriptions tab shows its pane");
check(tabs[1].attrs["aria-selected"] === "true" && tabs[0].attrs["aria-selected"] === "false", "tab selection state");
check(gets(mark, "/api/subscriptions").length === 1, "plans loaded");
const sections = () => $("subs-list").children;
const val = (tool, i, f) => $(`sub-${tool}-${i}-${f}`).value;
const setv = (tool, i, f, v) => { $(`sub-${tool}-${i}-${f}`).value = v; };
const rowsOf = (tool) => { const sec = sections().find((x) => x.dataset.tool === tool); return sec.querySelectorAll("div.sub-row"); };
const submitSubs = () => { $("subs-form").fire("submit"); settle(); };
const subsErr = () => $("subs-error").textContent;
check(sections().map((x) => x.dataset.tool).join() === "claude-code,antigravity,opencode,codex,t3", "one section per tool " + sections().map((x) => x.dataset.tool));
check(rowsOf("claude-code").length === 2 && rowsOf("codex").length === 1 && rowsOf("antigravity").length === 0, "period rows per tool");
check([val("claude-code", 0, "start"), val("claude-code", 0, "end"), val("claude-code", 0, "fee")].join() === "2026-01-01,2026-06-30,20", "period 1 prefilled");
check([val("claude-code", 1, "start"), val("claude-code", 1, "end"), val("claude-code", 1, "fee")].join() === "2026-07-01,,100", "ongoing period has an empty end");
check($("sub-claude-code-0-start").attrs.type === "date" && $("sub-claude-code-0-end").attrs.type === "date", "date inputs");
check([val("codex", 0, "start"), val("codex", 0, "end"), val("codex", 0, "fee")].join() === ",,20", "legacy plan: no dates");
check(rowsOf("codex")[0].querySelectorAll("p.sub-note").length === 1, "legacy plan explained");
check($("sub-claude-code-status").textContent === "$100.00/mo now" && $("sub-codex-status").textContent === "$20.00/mo now" &&
  $("sub-antigravity-status").textContent === "No plan", "tool status " + ["claude-code", "codex", "antigravity"].map((t) => $(`sub-${t}-status`).textContent));
check(sections()[1].textContent.includes("No plan periods."), "empty tool says so");
check(!$("subs-save").disabled && $("subs-error").hidden && $("subs-list").querySelectorAll("input").every((i) => !i.disabled), "editable");

// client-side validation mirrors the server's (no request on any of these)
mark = requests.length;
const expectErr = (prefix, focusId, label) => {
  submitSubs();
  check(!$("subs-error").hidden && subsErr().startsWith(prefix) && focused === $(focusId), label + " " + subsErr());
};
setv("claude-code", 1, "fee", "-1");
expectErr("Claude Code period 2: enter a monthly fee", "sub-claude-code-1-fee", "negative fee");
setv("claude-code", 1, "fee", "abc");
expectErr("Claude Code period 2: enter a monthly fee", "sub-claude-code-1-fee", "non-numeric fee");
setv("claude-code", 1, "fee", "10000.5");
expectErr("Claude Code period 2: enter a monthly fee between 0 and 10,000", "sub-claude-code-1-fee", "fee above the cap");
setv("claude-code", 1, "fee", "100");
setv("claude-code", 1, "start", "2026-06-15");
expectErr("Claude Code: periods overlap", "sub-claude-code-1-start", "overlapping periods");
check(subsErr().includes("Jul 1, 2026"), "overlap names the first free day " + subsErr());
setv("claude-code", 1, "start", "2026-06-30");
expectErr("Claude Code: periods overlap", "sub-claude-code-1-start", "touching on the same day is an overlap");
setv("claude-code", 1, "start", "2026-07-01");
setv("claude-code", 0, "end", "");
expectErr("Claude Code: periods overlap", "sub-claude-code-0-end", "an ongoing period before another");
check(subsErr().includes("ongoing"), "ongoing overlap explained " + subsErr());
setv("claude-code", 0, "end", "2025-12-01");
expectErr("Claude Code period 1: the end date is before the start date", "sub-claude-code-0-end", "end before start");
setv("claude-code", 0, "end", "2026-06-30");
setv("claude-code", 0, "start", "2026-02-30");
expectErr("Claude Code period 1: enter a start date", "sub-claude-code-0-start", "impossible calendar date");
setv("claude-code", 0, "start", "");
expectErr("Claude Code period 1: enter a start date", "sub-claude-code-0-start", "missing start");
setv("claude-code", 0, "start", "2026-01-01");
setv("claude-code", 0, "end", "2026-13-01");
expectErr("Claude Code period 1: the end date is not valid", "sub-claude-code-0-end", "bad end date");
setv("claude-code", 0, "end", "2026-06-30");
check(posts(mark, "/api/subscriptions").length === 0, "no request while the form is invalid");

// + Add period / Remove
$("sub-antigravity-add").fire("click");
check(rowsOf("antigravity").length === 1 && val("antigravity", 0, "start") === R.subscriptions.today &&
  val("antigravity", 0, "end") === "" && val("antigravity", 0, "fee") === "", "first period starts today, ongoing");
check(focused === $("sub-antigravity-0-fee"), "focus moves to its fee");
expectErr("Antigravity: enter a monthly fee", "sub-antigravity-0-fee", "a new period needs a fee");
setv("antigravity", 0, "fee", "20");
$("sub-codex-add").fire("click");
check(rowsOf("codex").length === 2 && val("codex", 1, "start") === R.subscriptions.today, "second codex row");
check(val("codex", 0, "fee") === "20", "edits survive a re-render");
expectErr("Codex period 1: enter a start date", "sub-codex-0-start", "an undated plan cannot sit next to dated ones");
rowsOf("codex")[1].querySelectorAll("button.sub-remove")[0].fire("click");
check(rowsOf("codex").length === 1, "Remove drops the row");
setv("codex", 0, "start", "2026-08-01"); setv("codex", 0, "end", "2026-11-10");
setv("claude-code", 1, "end", "2026-10-31");
$("sub-claude-code-add").fire("click");
check(rowsOf("claude-code").length === 3 && val("claude-code", 2, "start") === "2026-11-01" && val("claude-code", 2, "fee") === "100",
  "a new period starts the day after the last one ends and copies its fee " + [val("claude-code", 2, "start"), val("claude-code", 2, "fee")]);
check(focused === $("sub-claude-code-2-fee"), "focus on the new fee");
setv("claude-code", 2, "fee", "200"); // mid-way upgrade

mark = requests.length;
submitSubs();
let subPost = posts(mark, "/api/subscriptions");
check(subPost.length === 1 && subPost[0].opts.body === X.subs_posts[0], "save payload " + (subPost[0] && subPost[0].opts.body) + " vs " + X.subs_posts[0]);
check(subPost[0].opts.headers["Content-Type"] === "application/json", "save is JSON");
check($("toast").textContent === X.subs_toasts[0], "save toast " + $("toast").textContent + " vs " + X.subs_toasts[0]);
check($("subs-error").hidden && !$("subs-save").disabled, "form ready again");
check(gets(mark, "/api/overview").length === 1, "dashboard reloaded so ROI updates");
check(rowsOf("claude-code").length === 3 && val("claude-code", 2, "end") === "" && val("codex", 0, "end") === "2026-11-10", "rows re-rendered from the saved reply");
check($("sub-claude-code-status").textContent === "$200.00/mo now" && $("sub-codex-status").textContent === "Ended Nov 10, 2026",
  "status after save " + [$("sub-claude-code-status").textContent, $("sub-codex-status").textContent]);

// drop everything but the ended Codex plan
for (let i = 2; i >= 0; i--) rowsOf("claude-code")[i].querySelectorAll("button.sub-remove")[0].fire("click");
rowsOf("antigravity")[0].querySelectorAll("button.sub-remove")[0].fire("click");
mark = requests.length;
submitSubs();
subPost = posts(mark, "/api/subscriptions");
check(subPost.length === 1 && subPost[0].opts.body === X.subs_posts[1], "payload with only an ended plan " + (subPost[0] && subPost[0].opts.body));
check($("toast").textContent === X.subs_toasts[1] && X.subs_toasts[1] === "Subscriptions saved · no plan active today", "ended-plan toast " + $("toast").textContent);
noPlanDays = true;
$("pricing-close").fire("click");
keydown("2"); settle();
check(!$("kpi-roi-card").hidden && $("kpi-roi").textContent === "–" && $("kpi-roi-sub").textContent === "No paid plan in this range · 7 days",
  "no plan days in range " + $("kpi-roi-sub").textContent);
check($("kpi-roi-spent").textContent === "$0.00" && !$("kpi-roi-unit").hidden, "nothing spent in a range without plan days");
check($("kpi-roi-card").title === X.roi_title_no_plan_days && X.roi_title_no_plan_days.startsWith("Codex: no active plan · 0 paid days · $0.00 plan"),
  "tooltip for an ended plan " + JSON.stringify($("kpi-roi-card").title));
noPlanDays = false;

keydown("p"); settle();
tabs[1].fire("click"); settle();
check(rowsOf("codex").length === 1 && rowsOf("claude-code").length === 0, "reopening shows the saved plans");
rowsOf("codex")[0].querySelectorAll("button.sub-remove")[0].fire("click");
mark = requests.length;
submitSubs();
subPost = posts(mark, "/api/subscriptions");
check(subPost.length === 1 && subPost[0].opts.body === X.subs_posts[2], "empty payload " + (subPost[0] && subPost[0].opts.body));
check($("toast").textContent === "Subscriptions saved · no paid plans, ROI hidden", "no-plans toast " + $("toast").textContent);
check($("kpi-roi-card").hidden, "ROI card hidden without paid plans");

feesError = "subscriptions file is not valid JSON";
keydown("Escape"); // no-op (no model) - the dialog is still open anyway
$("pricing-close").fire("click");
keydown("6"); settle();
check(!$("kpi-roi-card").hidden && $("kpi-roi").textContent === "–" && $("kpi-roi-sub").textContent.includes("not valid JSON"), "broken subscriptions.json surfaces on the ROI card");
check($("kpi-roi-spent").textContent === "–" && $("kpi-roi-unit").hidden, "no spent figure when the file is broken");
feesError = null;

subsSaved = null; // back to the recorded plans
subsReadonly = true;
keydown("p"); settle();
tabs[1].fire("click"); settle();
check($("subs-save").disabled && $("subs-list").querySelectorAll("input").every((i) => i.disabled) &&
  $("subs-list").querySelectorAll("button").every((b) => b.disabled) && !$("subs-error").hidden, "read-only server disables editing");
mark = requests.length;
$("sub-claude-code-add").fire("click");
submitSubs();
check(rowsOf("claude-code").length === 2 && posts(mark, "/api/subscriptions").length === 0, "read-only: no add, no save");
$("pricing-close").fire("click");
keydown("p"); settle();
check(!$("pricing-models-pane").hidden, "P always opens on the Models tab");
$("pricing-close").fire("click");

check(errors.length === 0, "no errors: " + errors.join(" / "));
print(LOG.join("\n"));
print(errors.length ? "SMOKE_FAIL " + errors.length : "SMOKE_OK");
