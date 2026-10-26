import { api } from "/kit/api.js";
import { $, confirmDialog, copy, field, fill, fmtDate, h, toast } from "/kit/ui.js";

export async function initSettings() {
  const box = $("view-settings");
  fill(box, h("h1", {}, "Settings"), h("div", { id: "set-defaults", class: "settings-block" }), h("div", { id: "set-security", class: "settings-block" }));
  renderDefaults();
  renderSecurity();
}

// ---- defaults ---------------------------------------------------------------------------------------
async function renderDefaults() {
  const box = $("set-defaults");
  let s;
  try { s = await api("/api/settings"); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const rpm = h("input", { id: "st-rpm", type: "number", min: "1", max: "100000", value: s.default_rpm });
  const dim = h("input", { id: "st-dim", type: "number", min: "1", max: "8192", value: s.default_embedding_dim });
  const keep = h("input", { id: "st-keep", type: "number", min: "0", max: "3650", value: s.usage_retention_days });
  const err = h("p", { class: "error", role: "alert" });
  fill(box, h("h2", {}, "Defaults"),
    h("p", { class: "muted" }, "Applied when you create a client or an embedding alias without choosing a value. Usage rows hold no prompts or completions; the retention setting only limits how long the counts are kept (0 keeps everything; otherwise at least 32 days, because the monthly budget is computed from them)."),
    h("form", { class: "card", onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      try { await api("/api/settings", { method: "PUT", json: { default_rpm: Number(rpm.value), default_embedding_dim: Number(dim.value), usage_retention_days: Number(keep.value) } }); toast("Saved"); } catch (ex) { err.textContent = ex.message; }
    } }, h("div", { class: "grid3" }, field("Default requests per minute for a new client", rpm), field("Default embedding size for a new alias", dim), field("Keep usage rows for (days, 0 = forever)", keep)),
    h("button", { class: "primary", type: "submit" }, "Save"), err));
}

// ---- security -----------------------------------------------------------------------------------
async function renderSecurity() {
  const box = $("set-security");
  let tokens, session;
  try { [tokens, session] = await Promise.all([api("/api/tokens"), api("/api/session")]); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const reveal = h("div", { hidden: true });
  const tName = h("input", { id: "tk-name", maxlength: "60", required: true, placeholder: "e.g. dashboards" });
  const tScope = h("select", { id: "tk-scope" }, h("option", { value: "read" }, "Read-only (usage and configuration; recommended)"), h("option", { value: "metrics" }, "Metrics only (scrape /metrics)"), h("option", { value: "full" }, "Full operator access"));
  const tErr = h("p", { class: "error", role: "alert" });
  const table = h("div", {});
  async function renderTable() {
    const list = await api("/api/tokens");
    fill(table, list.length ? h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Name", "Token", "Access", "Created", "Last used", ""].map((t) => h("th", {}, t || h("span", { class: "sr-only" }, "Actions"))))),
      h("tbody", {}, list.map((t) => h("tr", {}, h("td", { class: "title" }, t.name), h("td", {}, h("code", {}, `${t.prefix}…`)), h("td", {}, t.scope), h("td", {}, fmtDate(t.created_at)), h("td", {}, fmtDate(t.last_used_at)),
        h("td", { class: "actions" }, t.revoked_at ? h("span", { class: "muted" }, "revoked") : h("button", { class: "danger small", type: "button", onclick: async () => {
          if (!(await confirmDialog(`Revoke "${t.name}"? Anything using it stops working immediately.`, { title: "Revoke token", confirmLabel: "Revoke", danger: true }))) return;
          try { await api(`/api/tokens/${t.id}`, { method: "DELETE" }); toast("Token revoked"); renderTable(); } catch (err) { toast(err.message, true); }
        } }, "Revoke"))))))) : h("p", { class: "hint" }, "No tokens yet."));
  }
  const tokenForm = h("form", { class: "grid-inline", onsubmit: async (e) => {
    e.preventDefault();
    tErr.textContent = "";
    try {
      const t = await api("/api/tokens", { method: "POST", json: { name: tName.value, scope: tScope.value } });
      tName.value = "";
      reveal.replaceChildren(h("p", { class: "warn-text" }, "Copy this token now. It is shown only once; only a hash is stored."),
        h("div", { class: "codewrap" }, h("pre", { class: "code" }, h("code", {}, t.token)), h("button", { class: "secondary small", type: "button", onclick: () => copy(t.token, "Token copied") }, "Copy")));
      reveal.hidden = false;
      renderTable();
    } catch (ex) { tErr.textContent = ex.message; }
  } }, field("Name", tName), field("Access", tScope), h("button", { class: "primary", type: "submit" }, "Create token"));

  const has = session.has_admin_password;
  const cur = h("input", { id: "pw-cur", type: "password", autocomplete: "current-password" });
  const nw = h("input", { id: "pw-new", type: "password", autocomplete: "new-password", minlength: "12", required: true });
  const nw2 = h("input", { id: "pw-new2", type: "password", autocomplete: "new-password", minlength: "12", required: true });
  const pErr = h("p", { class: "error", role: "alert" });
  const pwForm = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    pErr.textContent = "";
    if (nw.value !== nw2.value) { pErr.textContent = "The new passwords do not match."; return; }
    try {
      await api("/api/admin-password", { method: "POST", json: { current: has ? cur.value : null, new: nw.value } });
      cur.value = nw.value = nw2.value = "";
      toast("Password saved. You were signed out everywhere.");
      window.dispatchEvent(new CustomEvent("apg-auth-lost"));
    } catch (ex) { pErr.textContent = ex.message; }
  } }, has ? field("Current password", cur) : null, field(has ? "New password (12+ characters)" : "Admin password (12+ characters)", nw), field("Repeat", nw2),
  h("button", { class: "primary", type: "submit" }, has ? "Change password" : "Set admin password"), pErr);

  fill(box, h("h2", {}, "Security"),
    h("h3", {}, "Admin password"),
    h("p", { class: "muted" }, has ? "Changing it signs every browser out and re-protects the stored provider keys." : "No admin password is set. Set one to store provider keys encrypted and to sign in without the server's API key."),
    h("div", { class: "card" }, pwForm),
    h("h3", {}, "Operator tokens"),
    h("p", { class: "muted" }, "For dashboards and Prometheus. Apps use client keys under Clients; those reach only /v1, and these tokens can never call /v1."),
    h("div", { class: "card" }, tokenForm, tErr), reveal, table);
  renderTable();
}
