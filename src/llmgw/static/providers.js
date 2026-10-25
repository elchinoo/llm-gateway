import { api } from "/kit/api.js";
import { can } from "/kit/session.js";
import { $, confirmDialog, field, fill, h, toast } from "/kit/ui.js";

export async function initProviders() {
  const box = $("view-providers");
  fill(box, h("p", { class: "loading" }, "Loading…"));
  let d;
  try { d = await api("/api/providers"); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const manage = can("providers:manage");
  fill(box,
    h("div", { class: "row" }, h("h1", {}, "Providers"), manage ? h("button", { class: "primary", type: "button", onclick: () => editProvider(d, null) }, "Add a provider") : null),
    h("p", { class: "muted" }, "The upstream accounts the gateway calls. Provider keys live only here, encrypted, and are never shown again (only the last 4 characters). Every call re-checks the URL against the SSRF guard. The built-in echo provider needs no network and no key."),
    h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Name", "Kind", "Base URL", "Key", "Used by", "Status", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, d.items.map((x) => h("tr", {}, h("td", { class: "title" }, x.name), h("td", {}, x.kind === "echo" ? "echo (built-in)" : "OpenAI-compatible"), h("td", { class: "wrap" }, x.base_url || h("span", { class: "muted" }, "none")),
        h("td", {}, x.kind === "echo" ? h("span", { class: "muted" }, "not needed") : x.has_key ? `…${x.api_key_last4}` : h("span", { class: "muted" }, "no key")),
        h("td", {}, `${x.targets} target${x.targets === 1 ? "" : "s"}`), h("td", {}, h("span", { class: `badge ${x.enabled ? "ok" : "bad"}` }, x.enabled ? "enabled" : "disabled")),
        h("td", { class: "actions" }, manage ? [h("button", { class: "secondary small", type: "button", onclick: () => testProvider(x) }, "Test"), " ",
          h("button", { class: "secondary small", type: "button", onclick: () => editProvider(d, x) }, "Edit"), " ",
          h("button", { class: "danger small", type: "button", onclick: async () => {
            if (!(await confirmDialog(`Delete "${x.name}"?`, { title: "Delete provider", confirmLabel: "Delete", danger: true }))) return;
            try { await api(`/api/providers/${x.id}`, { method: "DELETE" }); initProviders(); } catch (e) { toast(e.message, true); }
          } }, "Delete")] : null))))),
      d.items.length ? null : h("p", { class: "empty" }, "No providers yet.")));
}

function testProvider(x) {
  const model = h("input", { id: "pt-model", maxlength: "120", required: true, placeholder: x.kind === "echo" ? "echo-1" : "e.g. gpt-4o-mini" });
  const out = h("p", { class: "muted", role: "status" });
  fill($("form-body"), h("h2", {}, `Test ${x.name}`), h("p", { class: "muted" }, "Makes one tiny chat call (one token) with the upstream model name you give. It is not metered."),
    h("form", { onsubmit: async (e) => {
      e.preventDefault();
      out.textContent = "Calling…";
      try {
        const r = await api(`/api/providers/${x.id}/test`, { method: "POST", json: { model: model.value } });
        out.textContent = r.ok ? `Works (${r.latency_ms} ms).` : `Failed (${r.latency_ms} ms): ${r.message}`;
        out.className = r.ok ? "muted" : "error";
      } catch (ex) { out.textContent = ex.message; out.className = "error"; }
    } }, field("Upstream model name", model), h("button", { class: "primary", type: "submit" }, "Test")), out);
  $("form").showModal();
  model.focus();
}

function editProvider(d, x) {
  const preset = h("select", { id: "pv-preset" }, ...d.presets.map((p) => h("option", { value: p.id }, p.name)), h("option", { value: "echo" }, "Echo (built-in demo)"));
  const name = h("input", { id: "pv-name", maxlength: "60", required: true, value: x?.name || "" });
  const url = h("input", { id: "pv-url", type: "url", maxlength: "300", value: x?.base_url || d.presets[0].base_url });
  const key = h("input", { id: "pv-key", type: "password", autocomplete: "off", maxlength: "500", placeholder: x?.has_key ? "leave blank to keep the stored key" : "" });
  const clear = h("input", { id: "pv-clear", type: "checkbox" });
  const enabled = h("input", { id: "pv-enabled", type: "checkbox", checked: x ? x.enabled : true });
  const err = h("p", { class: "error", role: "alert" });
  if (x) { preset.value = x.kind === "echo" ? "echo" : "custom"; preset.disabled = true; }
  preset.addEventListener("change", () => { const p = d.presets.find((q) => q.id === preset.value); if (p) url.value = p.base_url; });
  fill($("form-body"), h("h2", {}, x ? "Edit provider" : "Add a provider"), h("form", { onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    const echo = preset.value === "echo";
    try {
      if (x) await api(`/api/providers/${x.id}`, { method: "PUT", json: { name: name.value, base_url: echo ? null : url.value, api_key: key.value || null, clear_key: clear.checked, enabled: enabled.checked } });
      else await api("/api/providers", { method: "POST", json: { name: name.value, kind: echo ? "echo" : "openai", base_url: echo ? null : url.value, api_key: key.value || null, enabled: enabled.checked } });
      key.value = "";
      $("form").close();
      toast("Saved");
      initProviders();
    } catch (ex) { err.textContent = ex.message; }
  } }, field("Provider", preset), field("Name", name), field("Base URL (OpenAI-compatible, https)", url), field("API key", key, "Stored encrypted by the vault; needs the admin password (or the platform secret) to have been set."),
  x?.has_key ? h("label", { class: "check" }, clear, " Remove the stored key") : null, h("label", { class: "check" }, enabled, " Enabled"),
  h("button", { class: "primary", type: "submit" }, "Save"), err));
  $("form").showModal();
}
