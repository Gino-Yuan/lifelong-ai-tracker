// JavaScriptCore runtime test for the Plan Spent & ROI card's API / subscription split
// (run by tests/test_open_source_base.py).
// Usage: jsc stub_dom.js billing_smoke.js -- <app.js> <recorded.json> <mode> <hash>
// <recorded.json> holds real responses (overview, tools, timeseries, ...) for one mode:
// split (plans + an API source) | apionly (no subscriptions file) | none (no API source).
const [APP_JS, RECORDED, MODE, HASH] = arguments;
const R = JSON.parse(readFile(RECORDED));
location.hash = HASH;
function ROUTE(url) {
  const key = url.split("?")[0].replace("/api/", "");
  if (key in R) return jsonResponse(R[key]);
  return Promise.reject(new Error("offline stub " + url));
}
load(APP_JS);
drainMicrotasks(); drainMicrotasks(); drainMicrotasks();

const $ = (id) => document.getElementById(id);
const card = $("kpi-roi-card"), split = $("kpi-roi-split");
if (MODE === "split") {
  check(!card.hidden, "card shown");
  check($("kpi-roi-spent").textContent === "$17.08" && $("kpi-roi-unit").textContent === "spent",
    "plan spent " + $("kpi-roi-spent").textContent);
  check($("kpi-roi").textContent === "0.64× value","multiple from plan usage only " + $("kpi-roi").textContent);
  check($("kpi-roi-sub").textContent.startsWith("$11.00 plan value · Net loss $6.08 · "), "sub " + $("kpi-roi-sub").textContent);
  check(!split.hidden && split.textContent === "Subscription: $11.00 · Direct API: $3.00", "split line " + split.textContent);
  const t = card.title;
  check(t.includes("Subscription: $11.00 API-equivalent value used under plans (counts toward ROI)"), "tooltip subscription");
  check(t.includes("Direct API: $3.00 billed per token by 1 API source (not in ROI)"), "tooltip direct api");
  check(t.includes("  Claude Code: $3.00 · 1 request"), "tooltip per tool");
  check(t.includes("Out of pocket: $17.08 plans + $3.00 API = $20.08"), "tooltip out of pocket " + JSON.stringify(t));
} else if (MODE === "apionly") {
  check(!card.hidden, "card shown for API-only usage");
  check($("kpi-roi-spent").textContent === "$3.00" && $("kpi-roi-unit").textContent === "direct API", "api spent");
  check($("kpi-roi").textContent === "Pay as you go", "no multiple without a plan");
  check($("kpi-roi-sub").textContent === "No subscription plan in subscriptions.json", "sub " + $("kpi-roi-sub").textContent);
  check(split.textContent === "Subscription: $0.00 · Direct API: $3.00" && !split.hidden, "split " + split.textContent);
  check(!card.title.includes("Out of pocket"), "no plan total without a plan");
} else {
  check(!card.hidden && split.hidden && split.textContent === "", "no split line without API sources");
  check($("kpi-roi-sub").textContent.startsWith("$14.00 API value · "), "classic wording " + $("kpi-roi-sub").textContent);
  check(!card.title.includes("Direct API"), "classic tooltip");
}
check(errors.length === 0, "no errors: " + errors.join(" / "));
print(LOG.join("\n"));
print(errors.length ? "SMOKE_FAIL " + errors.length : "SMOKE_OK");
