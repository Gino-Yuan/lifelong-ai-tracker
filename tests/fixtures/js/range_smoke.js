// JavaScriptCore runtime test for the Custom date range and the KPI period-over-period
// badges (run by tests/test_custom_range.py).
// Usage: jsc stub_dom.js range_smoke.js -- <app.js> <recorded.json> <main|badhash|reversedhash>
// The JSON holds real API responses recorded from a fixture ledger plus the expected texts.
const [APP_JS, RESP_JSON, MODE] = arguments;
const FX = JSON.parse(readFile(RESP_JSON));
const R = FX.responses;
const X = FX.expect;
const clone = (o) => JSON.parse(JSON.stringify(o));
const unknownScopes = [];

const keyOf = (q) => {
  const custom = q.get("range") === "custom";
  return `${q.get("range")}|${custom ? q.get("start") : ""}|${custom ? q.get("end") : ""}|${q.get("tool") || ""}`;
};
const LIFELONG = "lifelong|||";

function ROUTE(url, opts) {
  const path = url.split("?")[0];
  const q = new URLSearchParams(url.split("?")[1] || "");
  if (opts.method) return Promise.reject(new Error("unexpected " + opts.method + " " + path));
  if (path === "/api/overview" || path === "/api/breakdown") {
    const table = path === "/api/overview" ? R.overviews : R.breakdowns;
    const k = keyOf(q);
    if (!table[k]) unknownScopes.push(k);
    return jsonResponse(clone(table[k] || table[LIFELONG]));
  }
  const key = { "/api/tools": "tools", "/api/timeseries": "timeseries", "/api/hourly": "hourly",
    "/api/heatmap": "heatmap", "/api/pricing": "pricing" }[path];
  if (key) return jsonResponse(clone(R[key]));
  return Promise.reject(new Error("offline stub " + url));
}

// static markup app.js finds by selector / tag: the range buttons and the date form
const rangeButtons = ["today", "7d", "30d", "90d", "ytd", "last_year", "lifelong", "custom"].map((r) => {
  const b = new Node_("button"); b.setAttribute("data-range", r); b.textContent = r;
  document.getElementById("range-control").append(b);
  return b;
});
const customForm = new Node_("form"); customForm.setAttribute("id", "custom-range"); customForm.hidden = true;
for (const id of ["custom-start", "custom-end"]) {
  const i = new Node_("input"); i.setAttribute("id", id); customForm.append(i);
}

location.hash = {
  main: "#range=custom&start=2026-10-01&end=2026-10-31",
  fresh: "#range=30d",
  badhash: "#range=custom&start=2026-02-30&end=2026-03-01",
  reversedhash: "#range=custom&start=2026-10-31&end=2026-10-01",
}[MODE];
load(APP_JS);
const settle = () => { for (let i = 0; i < 4; i++) drainMicrotasks(); };
settle();

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const gets = (from, path) => requests.slice(from)
  .filter((r) => r.url.split("?")[0] === path && !(r.opts && r.opts.method)).map((r) => r.url);
const params = (url) => new URLSearchParams(url.split("?")[1] || "");
const last = (arr) => arr[arr.length - 1];
const hashParams = () => new URLSearchParams(location.hash);
const rangeButton = (r) => rangeButtons.find((b) => b.dataset.range === r);
const pressed = (r) => rangeButton(r).attrs["aria-pressed"] === "true";
const isoOf = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const dayDiff = (a, b) => Math.round((new Date(b + "T12:00:00") - new Date(a + "T12:00:00")) / 86400000);

// the badge state on screen vs the expected one from Python
function badgeState(id) {
  const el = $(id);
  return el.hidden ? { hidden: true }
    : { hidden: false, new: el.classList.contains("new"), text: el.textContent, title: el.title };
}
function checkBadges(k, what) {
  const want = X.badges[k];
  for (const kind of ["cost", "tokens"]) {
    const got = badgeState(`kpi-${kind}-delta`);
    check(JSON.stringify(got) === JSON.stringify(want[kind]),
      `${kind} badge for ${k}${what ? " (" + what + ")" : ""}: ${JSON.stringify(got)} vs ${JSON.stringify(want[kind])}`);
  }
}
function lastOverviewKey() { return keyOf(params(last(gets(0, "/api/overview")))); }

if (MODE === "fresh") {
  // A page that never applied a custom window: Custom prefills the last 30 days ending on
  // the server's today (from the overview already loaded) and loads them at once.
  check(pressed("30d") && $("custom-range").hidden, "boots on 30D with the date inputs hidden");
  const mark = requests.length;
  rangeButton("custom").fire("click"); settle();
  const o = gets(mark, "/api/overview");
  check(o.length === 1 && keyOf(params(o[0])) === "custom|2026-10-28|2026-11-26|" && X.today === "2026-11-26" &&
    $("custom-start").value === "2026-10-28" && $("custom-end").value === "2026-11-26" && !$("custom-range").hidden &&
    location.hash === "#range=custom&start=2026-10-28&end=2026-11-26",
    "Custom prefills the last 30 days ending on the server's today " + JSON.stringify([o, location.hash]));
  checkBadges("custom|2026-10-28|2026-11-26|");
  check(unknownScopes.length === 0, "every requested scope was recorded: " + unknownScopes.join(", "));
  check(errors.length === 0, "no errors: " + errors.join(" / "));
  print(LOG.join("\n"));
  print(errors.length ? "SMOKE_FAIL " + errors.length : "SMOKE_OK");
} else if (MODE !== "main") {
  // An impossible or reversed custom range in the hash is replaced, before any request,
  // by the last 30 days (no server "today" yet: the browser's clock).
  const o = gets(0, "/api/overview");
  const q = params(o[0] || "");
  const s = q.get("start"), e = q.get("end");
  check(o.length === 1 && q.get("range") === "custom" && e === isoOf(new Date()) && dayDiff(s, e) === 29 &&
    hashParams().get("start") === s && hashParams().get("end") === e && pressed("custom") && !$("custom-range").hidden,
    "falls back to the last 30 days " + JSON.stringify([o, location.hash]));
  check(errors.length === 0, "no errors: " + errors.join(" / "));
  print(LOG.join("\n"));
  print(errors.length ? "SMOKE_FAIL " + errors.length : "SMOKE_OK");
} else {

// ---------------------------------------------------------------- boot from a custom hash
const OCT = "custom|2026-10-01|2026-10-31|";
const ov0 = gets(0, "/api/overview");
check(ov0.length === 1 && keyOf(params(ov0[0])) === OCT, "boot from a custom hash requests that window " + ov0);
for (const p of ["/api/timeseries", "/api/breakdown", "/api/hourly"]) {
  const u = last(gets(0, p));
  check(params(u).get("range") === "custom" && params(u).get("start") === "2026-10-01" && params(u).get("end") === "2026-10-31",
    `${p} carries start / end: ${u}`);
}
check(!params(last(gets(0, "/api/heatmap"))).has("start"), "the heatmap stays year-based (no start / end)");
check(pressed("custom") && !pressed("30d"), "Custom button pressed");
check(!$("custom-range").hidden && $("custom-start").value === "2026-10-01" && $("custom-end").value === "2026-10-31",
  "dates shown in the Start / End inputs");
check(location.hash === "#range=custom&start=2026-10-01&end=2026-10-31", "hash kept: " + location.hash);
check($("mix-meta").textContent.startsWith(X.labels[OCT] + " · "), "range label in the token mix " + $("mix-meta").textContent);
check($("mix-meta").textContent.startsWith("Oct 1 – Oct 31, 2026"), "custom label reads like Oct 1 – Oct 31, 2026");
check($("rank-foot").textContent.includes("relative to Oct 1 – Oct 31, 2026 total"), "ranking footer keeps the date label " + $("rank-foot").textContent);
checkBadges(OCT);
check(/^[↑↓→] [\d,]+\.\d% vs prev 31d$/.test($("kpi-cost-delta").textContent), "badge format " + $("kpi-cost-delta").textContent);
check($("kpi-cost-delta").title.startsWith("prev 31d (Aug 31 – Sep 30, 2026): $"), "cost tooltip has the exact previous amount " + $("kpi-cost-delta").title);
check(/ tokens$/.test($("kpi-tokens-delta").title) && $("kpi-tokens-delta").title.includes("21,000,000"), "token tooltip has the exact previous count " + $("kpi-tokens-delta").title);
for (const [id, fmt] of [["export-csv", "csv"], ["export-json", "json"], ["export-card", null]]) {
  const q = params($(id).href);
  check(q.get("range") === "custom" && q.get("start") === "2026-10-01" && q.get("end") === "2026-10-31" && (!fmt || q.get("format") === fmt),
    `export links carry the dates: ${id} ${$(id).href}`);
}

// ---------------------------------------------------------------- presets
let mark = requests.length;
keydown("3"); settle();
let o = last(gets(mark, "/api/overview"));
check(params(o).get("range") === "30d" && !params(o).has("start") && !params(o).has("end") &&
  gets(mark, "/api/breakdown").every((u) => !params(u).has("start")) && gets(mark, "/api/timeseries").every((u) => !params(u).has("start")),
  "preset drops start/end from every request " + o);
check(location.hash === "#range=30d" && $("custom-range").hidden && pressed("30d") && !pressed("custom"),
  "preset drops start/end from the hash and hides the inputs " + location.hash);
check(!params($("export-csv").href).has("start"), "preset export link has no dates");
checkBadges("30d|||");
check($("kpi-cost-delta").textContent.startsWith("↓ ") && !$("kpi-cost-delta").classList.contains("new"), "a fall shows ↓");

keydown("2"); settle();
checkBadges("7d|||");
check($("kpi-tokens-delta").textContent === "New vs prev 7d" && $("kpi-tokens-delta").classList.contains("new"),
  "New badge when the previous period is 0 and this one is not");
check($("kpi-tokens-delta").title === "prev 7d (Nov 13 – Nov 19, 2026): 0 tokens", "New tooltip " + $("kpi-tokens-delta").title);

keydown("1"); settle();
checkBadges("today|||");
check($("kpi-cost-delta").hidden && $("kpi-tokens-delta").hidden && !$("kpi-tokens-delta").hasAttribute("title"),
  "badges hidden for today when both periods are 0");

keydown("7"); settle();
checkBadges(LIFELONG);
check($("kpi-cost-delta").hidden && $("kpi-tokens-delta").hidden && $("kpi-cost-delta").textContent === "",
  "badges hidden for lifelong (no previous period)");

keydown("5"); settle();
checkBadges("ytd|||");
check($("kpi-cost-delta").textContent.endsWith(" vs prior YTD"), "YTD compares with the prior YTD " + $("kpi-cost-delta").textContent);
keydown("6"); settle();
checkBadges("last_year|||");
check($("kpi-tokens-delta").textContent.endsWith(" vs 2024"), "Last Year compares with 2024 " + $("kpi-tokens-delta").textContent);

// ---------------------------------------------------------------- Custom remembers its window
keydown("7"); settle();
mark = requests.length;
rangeButton("custom").fire("click"); settle();
o = last(gets(mark, "/api/overview"));
check(keyOf(params(o)) === OCT && pressed("custom"), "Custom remembers the window applied before " + o);

// ---------------------------------------------------------------- Apply / Enter / validation
const DEF = "custom|2026-10-28|2026-11-26|";
$("custom-start").value = "2026-10-28";
$("custom-end").value = "2026-11-26";
mark = requests.length;
$("custom-range").fire("submit"); settle();
o = gets(mark, "/api/overview");
check(o.length === 1 && keyOf(params(o[0])) === DEF && location.hash === "#range=custom&start=2026-10-28&end=2026-11-26",
  "Apply reloads with the edited dates " + o + " " + location.hash);
check(gets(mark, "/api/timeseries").length === 1 && gets(mark, "/api/breakdown").length === 1 && gets(mark, "/api/hourly").length === 1,
  "Apply reloads the whole page (trend, ranking, hours)");
checkBadges(DEF, "after Apply");

mark = requests.length;
$("custom-range").fire("submit"); settle();
check(gets(mark, "/api/overview").length === 0, "Apply with unchanged dates does not reload");

const FLAT = "custom|2026-09-10|2026-09-19|";
$("custom-start").value = "2026-09-10";
$("custom-end").value = "2026-09-19";
mark = requests.length;
let enterPrevented = false;
$("custom-end").fire("keydown", { key: "Enter", preventDefault() { enterPrevented = true; } }); settle();
o = gets(mark, "/api/overview");
check(o.length === 1 && keyOf(params(o[0])) === FLAT && enterPrevented, "Enter reloads with the edited dates " + o);
checkBadges(FLAT);
check($("kpi-tokens-delta").textContent === "→ 0.0% vs prev 10d", "flat badge " + $("kpi-tokens-delta").textContent);
mark = requests.length;
$("custom-start").fire("keydown", { key: "a" }); settle();
check(gets(mark, "/api/overview").length === 0, "other keys in the inputs do nothing");

// end before start
$("custom-start").value = "2026-10-31";
$("custom-end").value = "2026-10-01";
mark = requests.length;
$("custom-range").fire("submit"); settle();
check(gets(mark, "/api/overview").length === 0 && !$("custom-error").hidden &&
  $("custom-error").textContent === "End must be on or after Start" && $("custom-end").attrs["aria-invalid"] === "true",
  "end before start is refused in the page " + $("custom-error").textContent);
check(location.hash === "#range=custom&start=2026-09-10&end=2026-09-19", "a refused Apply keeps the applied range in the hash");
// impossible / empty dates
for (const [s, e, field, msg] of [["2026-02-30", "2026-03-01", "custom-start", "Enter a valid start date"],
                                  ["2026-10-01", "", "custom-end", "Enter a valid end date"],
                                  ["", "2026-10-01", "custom-start", "Enter a valid start date"],
                                  ["2025-02-29", "2025-03-01", "custom-start", "Enter a valid start date"]]) {
  $("custom-start").value = s;
  $("custom-end").value = e;
  mark = requests.length;
  $("custom-range").fire("submit"); settle();
  check(gets(mark, "/api/overview").length === 0 && $("custom-error").textContent === msg && $(field).attrs["aria-invalid"] === "true",
    `impossible date ${s || "(empty)"} / ${e || "(empty)"} is refused: ${$("custom-error").textContent}`);
}
// a good Apply clears the error
$("custom-start").value = "2026-10-01";
$("custom-end").value = "2026-10-31";
$("custom-range").fire("submit"); settle();
check($("custom-error").hidden && $("custom-start").attrs["aria-invalid"] === "false" && lastOverviewKey() === OCT,
  "a valid Apply clears the error");

// ---------------------------------------------------------------- key 8
keydown("3"); settle();
focused = null;
mark = requests.length;
const ev8 = keydown("8"); settle();
check(ev8.defaultPrevented && keyOf(params(last(gets(mark, "/api/overview")))) === OCT && pressed("custom") &&
  !$("custom-range").hidden && focused === $("custom-start"), "key 8 switches to Custom and focuses Start");
focused = null;
mark = requests.length;
keydown("8"); settle();
check(gets(mark, "/api/overview").length === 0 && focused === $("custom-start"), "key 8 again only focuses Start");
mark = requests.length;
keydown("3", $("custom-start")); settle();
check(gets(mark, "/api/overview").length === 0 && pressed("custom"), "digits typed into a date input are not shortcuts");

// ---------------------------------------------------------------- tool filter keeps the window
const toolButton = (id) => $("tool-control").children.find((b) => (b.dataset.tool || "") === id);
mark = requests.length;
toolButton("claude-code").fire("click"); settle();
o = last(gets(mark, "/api/overview"));
check(keyOf(params(o)) === "custom|2026-10-01|2026-10-31|claude-code" &&
  location.hash === "#range=custom&start=2026-10-01&end=2026-10-31&tool=claude-code",
  "custom follows the tool filter " + o + " " + location.hash);
checkBadges("custom|2026-10-01|2026-10-31|claude-code");
toolButton("").fire("click"); settle();
check(lastOverviewKey() === OCT, "back to All Tools keeps the custom window");

check(unknownScopes.length === 0, "every requested scope was recorded: " + unknownScopes.join(", "));
check(errors.length === 0, "no errors: " + errors.join(" / "));
print(LOG.join("\n"));
print(errors.length ? "SMOKE_FAIL " + errors.length : "SMOKE_OK");
}
