import { api } from "/kit/api.js";
import { can } from "/kit/session.js";
import { $, confirmDialog, field, fill, h, toast } from "/kit/ui.js";
import { PRESETS, parseExtraBody, pretty } from "/static/extrabody.js";

const price = (v) => (v == null ? "n/a" : `$${v}`);
const optNum = (v) => (v === "" || v == null ? null : Number(v));

export async function initModels() {
  const box = $("view-models");
  fill(box, h("p", { class: "loading" }, "Loading…"));
  let m, p;
  try { [m, p] = await Promise.all([api("/api/models"), api("/api/providers")]); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const manage = can("models:manage");
  fill(box,
    h("div", { class: "row" }, h("h1", {}, "Models"), manage ? h("button", { class: "primary", type: "button", onclick: () => editAlias(m, p, null) }, "New model alias") : null),
    h("p", { class: "muted" }, "Apps ask for an alias (for example chat-default). The alias maps to an ordered list of targets: the first is tried first, and the gateway moves on to the next on a 5xx, a 429, a timeout or a connection error (never on other 4xx errors). Swap the targets to change the model without touching any app. Prices are entered by you, in USD per 1M tokens, and only feed cost estimates."),
    h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Alias", "Kind", "Targets (in order)", "Price in / out per 1M", "Status", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, m.items.map((a) => h("tr", {}, h("td", { class: "title" }, a.alias, a.description ? h("div", { class: "hint" }, a.description) : null), h("td", {}, a.kind + (a.kind === "embedding" ? ` (${a.embedding_dim} dims on echo)` : "")),
        h("td", { class: "wrap" }, h("ol", { class: "timeline" }, a.targets.map((t) => h("li", {}, `${t.provider_name} · ${t.upstream_model}`, t.provider_enabled ? null : h("span", { class: "badge bad" }, "provider disabled"), Object.keys(t.extra_body || {}).length ? h("span", { class: "badge plain", title: JSON.stringify(t.extra_body) }, "extra fields") : null)))),
        h("td", {}, `${price(a.price_prompt_per_m)} / ${price(a.price_completion_per_m)}`), h("td", {}, h("span", { class: `badge ${a.enabled ? "ok" : "bad"}` }, a.enabled ? "enabled" : "disabled")),
        h("td", { class: "actions" }, manage ? h("button", { class: "secondary small", type: "button", onclick: () => editAlias(m, p, a) }, "Edit") : null))))),
      m.items.length ? null : h("p", { class: "empty" }, "No model aliases yet.")));
}

function editAlias(m, p, a) {
  const alias = h("input", { id: "al-name", maxlength: "60", required: true, value: a?.alias || "", disabled: !!a, placeholder: "e.g. chat-default" });
  const kind = h("select", { id: "al-kind" }, h("option", { value: "chat", selected: a?.kind === "chat" }, "Chat (/v1/chat/completions)"), h("option", { value: "embedding", selected: a?.kind === "embedding" }, "Embedding (/v1/embeddings)"));
  const desc = h("input", { id: "al-desc", maxlength: "200", value: a?.description || "" });
  const dim = h("input", { id: "al-dim", type: "number", min: "1", max: "8192", value: a?.embedding_dim ?? m.default_embedding_dim });
  const pp = h("input", { id: "al-pp", type: "number", min: "0", step: "any", value: a?.price_prompt_per_m ?? "", placeholder: "n/a" });
  const pc = h("input", { id: "al-pc", type: "number", min: "0", step: "any", value: a?.price_completion_per_m ?? "", placeholder: "n/a" });
  const enabled = h("input", { id: "al-enabled", type: "checkbox", checked: a ? a.enabled : true });
  const err = h("p", { class: "error", role: "alert" });
  const rows = h("div", { class: "targets" });
  const targets = (a?.targets || []).map((t) => ({ provider_id: t.provider_id, upstream_model: t.upstream_model, extra: pretty(t.extra_body) }));
  if (!targets.length && p.items.length) targets.push({ provider_id: p.items[0].id, upstream_model: "", extra: "" });
  function draw() {
    fill(rows, ...targets.map((t, i) => {
      const sel = h("select", { "aria-label": `Provider for target ${i + 1}`, onchange: (e) => { t.provider_id = Number(e.target.value); } },
        ...p.items.map((x) => h("option", { value: x.id, selected: x.id === t.provider_id }, `${x.name}${x.kind === "echo" ? " (built-in)" : ""}`)));
      const model = h("input", { "aria-label": `Upstream model for target ${i + 1}`, maxlength: "120", value: t.upstream_model, placeholder: "upstream model name", oninput: (e) => { t.upstream_model = e.target.value; } });
      // The text lives in `targets[i].extra`, not in the textarea, so a redraw (reorder, remove, add) keeps what was typed.
      const note = h("p", { class: "hint", role: "status" });
      const box = h("textarea", { "aria-label": `Extra request fields (JSON) for target ${i + 1}`, rows: "3", spellcheck: "false", class: "mono", placeholder: "{}", value: t.extra,
        oninput: (e) => { t.extra = e.target.value; check(); } });
      const check = () => { const r = parseExtraBody(t.extra); note.textContent = r.error || ""; note.className = r.error ? "error" : "hint"; return !r.error; };
      check();
      const presets = h("div", { class: "row" }, PRESETS.map((x) => h("button", { class: "secondary small", type: "button",
        onclick: () => { t.extra = JSON.stringify({ ...(parseExtraBody(t.extra).value || {}), ...x.value }, null, 2); box.value = t.extra; check(); } }, x.label)));
      return h("div", { class: "target" }, h("div", { class: "row" }, h("span", { class: "muted" }, `${i + 1}.`), sel, model,
        h("button", { class: "secondary small", type: "button", disabled: i === 0, onclick: () => { [targets[i - 1], targets[i]] = [targets[i], targets[i - 1]]; draw(); } }, "↑"),
        h("button", { class: "danger small", type: "button", disabled: targets.length < 2, onclick: () => { targets.splice(i, 1); draw(); } }, "Remove")),
        h("label", { class: "hint" }, "Extra request fields (JSON)"), box, note, presets);
    }), h("button", { class: "secondary small", type: "button", disabled: targets.length >= 5 || !p.items.length, onclick: () => { targets.push({ provider_id: p.items[0].id, upstream_model: "", extra: "" }); draw(); } }, "Add a fallback target"));
  }
  draw();
  fill($("form-body"), h("h2", {}, a ? `Edit ${a.alias}` : "New model alias"), h("form", { onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    const parsed = targets.map((t) => parseExtraBody(t.extra));
    const bad = parsed.findIndex((r) => r.error);
    if (bad >= 0) { err.textContent = `Target ${bad + 1}, extra request fields: ${parsed[bad].error}`; return; }
    const body = { kind: kind.value, description: desc.value, enabled: enabled.checked, embedding_dim: optNum(dim.value), price_prompt_per_m: optNum(pp.value), price_completion_per_m: optNum(pc.value),
      targets: targets.map((t, i) => ({ provider_id: t.provider_id, upstream_model: t.upstream_model, extra_body: parsed[i].value })) };
    try { await api(`/api/models/${encodeURIComponent(alias.value)}`, { method: "PUT", json: body }); $("form").close(); toast("Saved"); initModels(); } catch (ex) { err.textContent = ex.message; }
  } }, field("Alias", alias), field("Description", desc), h("div", { class: "grid2" }, field("Kind", kind), field("Embedding size (the built-in echo provider only)", dim)),
  h("div", { class: "grid2" }, field("USD per 1M prompt tokens", pp), field("USD per 1M completion tokens", pc)),
  h("h3", {}, "Targets, in order"),
  h("p", { class: "hint" }, "Extra request fields are set by you and merged into the request sent to that target, after the app's own fields, so they win. Use them to turn a thinking model's reasoning off or down. They cannot set model, messages, input, stream, user or n, and must never hold a secret."), rows, h("label", { class: "check" }, enabled, " Enabled"),
  h("button", { class: "primary", type: "submit" }, "Save"),
  a ? h("button", { class: "danger", type: "button", onclick: async () => {
    if (!(await confirmDialog(`Delete "${a.alias}"? Apps asking for it will get 404.`, { title: "Delete model alias", confirmLabel: "Delete", danger: true }))) return;
    try { await api(`/api/models/${encodeURIComponent(a.alias)}`, { method: "DELETE" }); $("form").close(); initModels(); } catch (ex) { err.textContent = ex.message; }
  } }, "Delete") : null, err));
  $("form").showModal();
}
