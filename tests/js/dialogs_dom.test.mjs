// Fake-DOM test: deleting a model alias goes through the kit's confirmDialog (never window.confirm), a native <dialog> opened with
// showModal() on top of the editor's own modal <dialog>. Cancel, Escape (the native 'cancel') and a backdrop click send nothing, confirm
// sends the DELETE, and neither the typed-in editor nor the dialog is wiped by timers or a list refresh.
import assert from "node:assert/strict";
import { register } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { install } from "./fakedom.mjs";

const here = path.dirname(new URL(import.meta.url).pathname);
register(pathToFileURL(path.join(here, "loader.mjs")).href, { data: { static: process.env.STATIC_DIR, kit: process.env.KIT_DIR, stubs: path.join(here, "stubs") } });
const dom = install();
for (const name of ["confirm", "alert", "prompt"]) globalThis[name] = () => { throw new Error(`native ${name}() must not be called`); };

const alias = { alias: "qwen", kind: "chat", description: "", enabled: true, embedding_dim: 768, price_prompt_per_m: null, price_completion_per_m: null,
  targets: [{ position: 0, provider_id: 1, upstream_model: "m", extra_body: {}, provider_name: "p", provider_enabled: true }] };
globalThis.__api = (url) => (url === "/api/models" ? { items: [alias], default_embedding_dim: 768 } : url === "/api/providers" ? { items: [{ id: 1, name: "p", kind: "openai", enabled: true }] } : {});
const { initModels } = await import("/static/models.js");

const body = dom.byId["form-body"];
const body_ = globalThis.document.body;
const tick = () => new Promise((r) => setImmediate(r));
const overlay = () => body_.children.find((c) => c.tagName === "DIALOG" && c.getAttribute("role") === "alertdialog");
const dialogButton = (label) => overlay().all((e) => e.tagName === "BUTTON" && e.textContent === label)[0];
const deletes = () => globalThis.__calls.filter((c) => c.opts.method === "DELETE");

await initModels();
dom.byId["view-models"].all((e) => e.tagName === "BUTTON" && e.textContent === "Edit")[0].dispatch("click");
const del = () => body.all((e) => e.tagName === "BUTTON" && e.textContent === "Delete")[0];

// Cancel: the dialog shows the message, the button is danger-styled, and nothing is sent.
del().dispatch("click"); await tick();
assert.ok(overlay(), "a modal dialog is shown");
assert.equal(overlay().className, "confirm-dialog");
assert.ok(overlay().open && dom.modals.at(-1) === overlay(), "a native <dialog> opened with showModal(), above the editor's modal dialog");
assert.equal(dom.modals.length, 2, "the editor dialog is still open underneath");
assert.match(overlay().textContent, /Delete "qwen"\? Apps asking for it will get 404\./);
assert.equal(dialogButton("Delete").className, "danger-solid");
dom.fireTimers(5); await initModels(); dom.fireTimers(5);
assert.ok(overlay(), "timers and a list refresh do not remove the dialog");
assert.equal(del().dispatch("click"), null, "the editor underneath is inert while the confirmation is open");
dialogButton("Cancel").dispatch("click"); await tick();
assert.equal(overlay(), undefined);
assert.equal(dom.modals.length, 1, "closing the confirmation leaves the editor open");
assert.equal(deletes().length, 0, "cancel sends nothing");

// Escape: the native 'cancel' event closes it without deleting; a backdrop click (the dialog element itself) does too.
del().dispatch("click"); await tick();
overlay().escape(); await tick();
assert.equal(overlay(), undefined, "Escape (cancel) closes the confirmation");
assert.equal(dom.modals.length, 1);
del().dispatch("click"); await tick();
const dlg = overlay();
dlg.dispatch("click", { target: dlg }); await tick();
assert.equal(overlay(), undefined, "a backdrop click closes the confirmation");
assert.equal(deletes().length, 0, "Escape and backdrop send nothing");

// Confirm: the DELETE goes out.
del().dispatch("click"); await tick();
dialogButton("Delete").dispatch("click"); await tick(); await tick();
assert.deepEqual(deletes().map((c) => c.url), ["/api/models/qwen"]);
assert.equal(overlay(), undefined, "the confirmation is removed after confirming");
console.log("dialogs_dom: ok");
