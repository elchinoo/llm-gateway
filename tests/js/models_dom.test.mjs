// Fake-DOM test of the model-alias editor (static/models.js): typing into "Extra request fields (JSON)" must survive every timer and
// every refresh the app can fire, presets must fill the box, and bad JSON must be caught in the browser before anything is sent.
import assert from "node:assert/strict";
import { register } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { install } from "./fakedom.mjs";

const here = path.dirname(new URL(import.meta.url).pathname);
register(pathToFileURL(path.join(here, "loader.mjs")).href, { data: { static: process.env.STATIC_DIR, kit: process.env.KIT_DIR, stubs: path.join(here, "stubs") } });
const dom = install();

const providers = { items: [{ id: 1, name: "OpenRouter", kind: "openai", enabled: true }, { id: 2, name: "echo", kind: "echo", enabled: true }] };
const alias = { alias: "qwen", kind: "chat", description: "", enabled: true, embedding_dim: 768, price_prompt_per_m: null, price_completion_per_m: null,
  targets: [{ position: 0, provider_id: 1, upstream_model: "qwen/qwen3.7-flash", extra_body: { reasoning: { enabled: false } }, provider_name: "OpenRouter", provider_enabled: true },
            { position: 1, provider_id: 2, upstream_model: "echo-1", extra_body: {}, provider_name: "echo", provider_enabled: true }] };
globalThis.__api = (url, opts) => {
  if (url === "/api/models") return { items: [alias], default_embedding_dim: 768 };
  if (url === "/api/providers") return providers;
  return {};
};
const { initModels } = await import("/static/models.js");
const { PRESETS, parseExtraBody } = await import("/static/extrabody.js");

const form = dom.byId["form-body"];
const textareas = () => form.all((e) => e.tagName === "TEXTAREA");
const button = (label) => form.all((e) => e.tagName === "BUTTON" && e.textContent === label);
const notes = () => form.all((e) => e.tagName === "P" && e.getAttribute("role") === "status").map((e) => e.textContent);
const type = (el, text) => { el.value = text; el.dispatch("input"); };
const submit = async () => { form.all((e) => e.tagName === "FORM")[0].dispatch("submit"); await new Promise((r) => setImmediate(r)); };

// 1. The list shows which targets carry extra fields; Edit opens the editor with the saved JSON in the box.
await initModels();
const list = dom.byId["view-models"];
assert.ok(list.textContent.includes("extra fields"), "the list flags targets with extra fields");
list.all((e) => e.tagName === "BUTTON" && e.textContent === "Edit")[0].dispatch("click");
assert.equal(dom.byId.form.open, true);
const [first, second] = textareas();
assert.deepEqual(JSON.parse(first.value), { reasoning: { enabled: false } });
assert.equal(second.value, "", "an empty target shows an empty box, not {}");

// 2. Typing survives every timer and every refresh the app can trigger (app.js re-runs the tab on hashchange / apg-unlocked).
type(second, '{"reasoning_effort": "low"');          // half typed: invalid
assert.match(notes()[1], /Not valid JSON/);
type(second, '{"reasoning_effort": "low"}\n');
assert.equal(notes()[1], "");
dom.fireTimers(5);                                   // the toast timer and anything else that is scheduled
await initModels();                                  // what route() does on hashchange / apg-unlocked / after a toast
dom.fireTimers(5);
assert.equal(textareas()[1], second, "the same textarea element is still in the dialog");
assert.ok(second.isConnectedTo(form));
assert.equal(second.value, '{"reasoning_effort": "low"}\n', "the typed text is intact, byte for byte");

// 3. A redraw inside the editor (reorder, remove, add a fallback) keeps what was typed, and it moves with its target.
button("↑")[1].dispatch("click");                   // the second target moves up (the first one's button is disabled)
let [a, b] = textareas();
assert.equal(a.value, '{"reasoning_effort": "low"}\n');
assert.deepEqual(JSON.parse(b.value), { reasoning: { enabled: false } });
button("Add a fallback target")[0].dispatch("click");
assert.equal(textareas().length, 3);
assert.equal(textareas()[0].value, '{"reasoning_effort": "low"}\n');
button("Remove")[2].dispatch("click");               // remove the new, empty one
assert.equal(textareas().length, 2);

// 4. Presets: exactly the three labelled one-click buttons, each fills the box with the right JSON and clears the warning.
assert.deepEqual(PRESETS.map((p) => p.label), ["Turn thinking off: OpenRouter", "Turn thinking off: vLLM/SGLang (Qwen3)", "Limit reasoning: OpenAI style"]);
type(textareas()[0], "");
button("Turn thinking off: OpenRouter")[0].dispatch("click");
assert.deepEqual(JSON.parse(textareas()[0].value), { reasoning: { enabled: false } });
type(textareas()[0], "");
button("Turn thinking off: vLLM/SGLang (Qwen3)")[0].dispatch("click");
assert.deepEqual(JSON.parse(textareas()[0].value), { chat_template_kwargs: { enable_thinking: false } });
type(textareas()[0], "");
button("Limit reasoning: OpenAI style")[0].dispatch("click");
assert.deepEqual(JSON.parse(textareas()[0].value), { reasoning_effort: "low" });
dom.fireTimers(5);
assert.deepEqual(JSON.parse(textareas()[0].value), { reasoning_effort: "low" }, "a preset survives timers too");

// 5. Client-side validation blocks the save and says why; nothing is sent.
const before = globalThis.__calls.filter((c) => c.opts.method === "PUT").length;
for (const [text, msg] of [['{"model": "x"}', /Cannot set model/], ["[1]", /JSON object/], ["null", /JSON object/], ["{oops", /Not valid JSON/],
  ['{"a":{"b":{"c":{"d":{"e":1}}}}}', /too deeply/], [`{"v": "${"x".repeat(5000)}"}`, /Larger than 4 KB/], ['{"messages": []}', /Cannot set messages/]]) {
  type(textareas()[0], text);
  assert.match(notes()[0], msg, text);
  await submit();
  assert.equal(globalThis.__calls.filter((c) => c.opts.method === "PUT").length, before, "no request is sent for: " + text.slice(0, 30));
  assert.equal(textareas()[0].value, text, "the invalid text is left for the operator to fix");
}
assert.equal(parseExtraBody("   ").value && Object.keys(parseExtraBody("   ").value).length, 0, "blank means no extra fields");

// 6. A valid form saves extra_body as an object per target (blank box -> {}).
type(textareas()[0], '{"thinking": {"type": "disabled"}}');
type(textareas()[1], "");
await submit();
const put = globalThis.__calls.filter((c) => c.opts.method === "PUT").at(-1);
assert.equal(put.url, "/api/models/qwen");
assert.deepEqual(put.opts.json.targets.map((t) => t.extra_body), [{ thinking: { type: "disabled" } }, {}]);
assert.deepEqual(put.opts.json.targets.map((t) => t.upstream_model), ["echo-1", "qwen/qwen3.7-flash"], "the reorder is saved with each target's own fields");
assert.ok(put.opts.json.targets.every((t) => Object.keys(t).sort().join() === "extra_body,provider_id,upstream_model"), "no UI-only fields leak into the request");
console.log("models_dom: ok");
