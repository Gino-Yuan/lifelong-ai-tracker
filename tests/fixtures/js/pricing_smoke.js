// JavaScriptCore runtime test for the Model Pricing modal (run by tests/test_pricing_table.py).
// Usage: jsc stub_dom.js pricing_smoke.js -- <app.js> <pricing-response.json>
// The shared stub DOM, then the real app.js, then a click-through of the pricing table.
const [APP_JS, PRICING_JSON] = arguments;
const PRICING = JSON.parse(readFile(PRICING_JSON));
function ROUTE(url, opts) {
  if (url === "/api/pricing" && !(opts.method === "POST")) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve(PRICING) });
  }
  if (url === "/api/pricing") {
    const body = JSON.parse(opts.body);
    if (!PRICING.models.some((m) => m.model === body.model)) { // like the server: the new row is listed, unused
      PRICING.models.push({ model: body.model, provider: "Other", input: body.input, output: body.output,
        thinking: body.output, cache_read: body.cache_read, cache_write: body.cache_write,
        used: false, effective_from: null, history: [] });
      PRICING.count += 1;
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({
      status: "ok", model: body.model, replaced: false, matched_models: [], unpriced_models: [], reprice: null }) });
  }
  return Promise.reject(new Error("offline stub " + url));
}
load(APP_JS);
drainMicrotasks();

// ---------------------------------------------------------------- scenario
const $ = (id) => document.getElementById(id);
const modelRows = () => $("pricing-body").children.filter((r) => r.attrs["data-model"]);
const groupRows = () => $("pricing-body").children.filter((r) => r.className === "group");
const rowText = (r) => r.children.map((c) => c.textContent).join(" | ");

$("open-pricing").fire("click");
drainMicrotasks();
check($("pricing-dialog").open, "pricing dialog opens");
check(focused === $("pricing-search"), "search box focused");
// Used only is on by default; this price list comes from an empty ledger, so nothing is used
check($("pricing-used").checked === true, "Used only on by default");
check(modelRows().length === 0 && !$("pricing-empty").hidden &&
  $("pricing-empty").textContent === "No priced model has been used yet. Turn off Used only to see every model.",
  "used-only empty state: " + $("pricing-empty").textContent);
check($("pricing-count").textContent === "0 used of 7 models", "used count " + $("pricing-count").textContent);
check(provBox.children[0].textContent === "All0", "provider counts follow Used only " + provBox.children[0].textContent);
$("pricing-used").checked = false;
$("pricing-used").fire("change");
check(modelRows().length === 7, "7 model rows, got " + modelRows().length);
check(groupRows().map((g) => g.textContent).join(",") === "Claude,Gemini,GPT,Other", "group rows " + groupRows().map((g) => g.textContent));
check($("pricing-count").textContent === "7 models", "count " + $("pricing-count").textContent);
check($("pricing-updated").textContent === "Nov 26, 2026", "updated " + $("pricing-updated").textContent);
check($("pricing-unit").textContent === "USD per 1M tokens", "unit");
const sonnet = modelRows().find((r) => r.attrs["data-model"] === "claude-sonnet-4-5");
LOG.push("row: " + rowText(sonnet));
check(rowText(sonnet) === "claude-sonnet-4-5 | 3.00 | 15.00 | 15.00 | 0.30 | 3.75 | Edit", "sonnet row text");
const luna = modelRows().find((r) => r.attrs["data-model"] === "gpt-6-luna");
check(rowText(luna) === "gpt-6-luna | 0.10 | 0.50 | 0.50 | 0.01 | 0.00 | Edit", "luna row " + rowText(luna));
check(luna.children[5].className === "num zero", "zero cell muted");
const btns = provBox.children;
check(btns.map((b) => b.textContent).join(",") === "All7,Claude4,Gemini1,GPT1,Other1", "provider counts " + btns.map((b) => b.textContent));
check(!btns[4].hidden, "Other shown when it has models");

btns[3].fire("click");
check(modelRows().map((r) => r.attrs["data-model"]).join() === "gpt-6-luna" && groupRows().length === 0, "GPT filter");
check(btns[3].attrs["aria-pressed"] === "true" && btns[0].attrs["aria-pressed"] === "false", "GPT pressed");
btns[0].fire("click");
$("pricing-search").value = "NEON";
$("pricing-search").fire("input");
check(modelRows().map((r) => r.attrs["data-model"]).join() === "gemini-4-neon" && groupRows().length === 0, "search is case-insensitive");
$("pricing-search").value = "zzz";
$("pricing-search").fire("input");
check(modelRows().length === 0 && !$("pricing-empty").hidden && $("pricing-empty").textContent.includes("zzz"), "empty search message");
$("pricing-search").value = "";
$("pricing-search").fire("input");
check(modelRows().length === 7 && $("pricing-empty").hidden, "search cleared");

// Edit -> Set Price dialog prefilled
const sonnet2 = modelRows().find((r) => r.attrs["data-model"] === "claude-sonnet-4-5");
sonnet2.children[6].children[0].fire("click");
check($("price-dialog").open && $("pricing-dialog").open, "edit opens price dialog on top");
check($("price-title").textContent === "Edit Price", "edit title");
check($("price-model").textContent === "claude-sonnet-4-5" && !$("price-model").hidden && $("price-model-input").hidden, "edit shows fixed model");
check(["input", "output", "thinking", "cache_read", "cache_write"].map((f) => $("price-" + f).value).join() === "3,15,15,0.3,3.75", "prefilled values");
check($("price-base-row").hidden, "no base-model row when editing");
$("price-input").value = "2.5";
const before = requests.length;
$("price-form").fire("submit");
drainMicrotasks(); drainMicrotasks();
const post = requests.find((r, i) => i >= before && r.opts && r.opts.method === "POST");
check(post && JSON.stringify(JSON.parse(post.opts.body)) === JSON.stringify({ model: "claude-sonnet-4-5", input: 2.5, output: 15, thinking: 15, cache_read: 0.3, cache_write: 3.75 }), "edit POST payload " + (post && post.opts.body));
check(!$("price-dialog").open && $("pricing-dialog").open, "price dialog closes, table stays");
check(requests.slice(before).filter((r) => r.url === "/api/pricing" && !(r.opts && r.opts.method)).length === 1, "table reloaded after save");
check(requests.slice(before).some((r) => r.url.startsWith("/api/overview")), "dashboard reloaded after save");
check($("toast").textContent.startsWith("Saved claude-sonnet-4-5"), "toast " + $("toast").textContent);

// + Add Model
$("pricing-add").fire("click");
check($("price-title").textContent === "Add Model" && !$("price-model-input").hidden && $("price-model").hidden, "add mode shows name input");
check(focused === $("price-model-input"), "name input focused");
check(["input", "output", "thinking", "cache_read", "cache_write"].every((f) => $("price-" + f).value === ""), "add starts empty");
const b2 = requests.length;
$("price-form").fire("submit");
drainMicrotasks();
check(!$("price-error").hidden && $("price-error").textContent.includes("model name"), "empty name rejected");
check(requests.length === b2, "no request for empty name");
$("price-model-input").value = "  my-local-model  ";
$("price-input").value = "1"; $("price-output").value = "2"; $("price-cache_read").value = "0"; $("price-cache_write").value = "0";
$("price-form").fire("submit");
drainMicrotasks(); drainMicrotasks();
const post2 = requests.find((r, i) => i >= b2 && r.opts && r.opts.method === "POST");
check(post2 && JSON.stringify(JSON.parse(post2.opts.body)) === JSON.stringify({ model: "my-local-model", input: 1, output: 2, cache_read: 0, cache_write: 0 }), "add POST payload " + (post2 && post2.opts.body));


// Used only with a ledger that uses two rows; the model just added stays listed (kept)
for (const m of PRICING.models) m.used = m.model === "claude-sonnet-4-5" || m.model === "gpt-6-luna";
PRICING.used_count = 2;
const sonnetRow = PRICING.models.find((m) => m.model === "claude-sonnet-4-5");
sonnetRow.effective_from = "2026-12-01";
sonnetRow.history = [{ effective_from: null, input: 3, output: 15, thinking: 15, cache_read: 0.3, cache_write: 3.75 }];
$("pricing-used").checked = true;
$("pricing-used").fire("change");
const ids = () => modelRows().map((r) => r.attrs["data-model"]).join();
check(ids() === "claude-sonnet-4-5,gpt-6-luna,my-local-model", "used rows + just-added row " + ids());
check($("pricing-count").textContent === "2 used of 8 models", "count " + $("pricing-count").textContent);
const dated = modelRows()[0].children[0];
check(dated.textContent === "claude-sonnet-4-5since Dec 1, 2026 · 2 price periods", "since chip " + dated.textContent);
check((dated.title || "").includes("Current price since Dec 1, 2026") && dated.title.includes("Before Dec 1, 2026: input 3.00"),
  "price history tooltip " + dated.title);
provBox.children[2].fire("click"); // Gemini: nothing used
check(modelRows().length === 0 && $("pricing-empty").textContent === "No used Gemini models yet. Turn off Used only to see every model.",
  "provider empty state " + $("pricing-empty").textContent);
provBox.children[0].fire("click");
$("pricing-close").fire("click");
check(!$("pricing-dialog").open, "close button");
$("open-pricing").fire("click");
drainMicrotasks();
check(ids() === "claude-sonnet-4-5,gpt-6-luna", "kept row forgotten after closing " + ids());
$("pricing-used").checked = false;
$("pricing-used").fire("change");
check(modelRows().length === 8 && $("pricing-count").textContent === "8 models", "all rows when switched off");
$("pricing-close").fire("click");
check(!$("pricing-dialog").open, "close again");
check(errors.length === 0, "no errors: " + errors.join(" / "));
print(LOG.join("\n"));
print(errors.length ? "SMOKE_FAIL " + errors.length : "SMOKE_OK");
