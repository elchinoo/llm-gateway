import { api } from "/kit/api.js";
import { can } from "/kit/session.js";
import { $, confirmDialog, copy, field, fill, fmtDate, h, promptDialog, toast } from "/kit/ui.js";
import { compact, meter, num, usd } from "/static/common.js";

const optInt = (v) => (v === "" || v == null ? null : Number(v));

export async function initClients() {
  const box = $("view-clients");
  fill(box, h("p", { class: "loading" }, "Loading…"));
  let list;
  try { list = await api("/api/clients"); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  fill(box,
    h("div", { class: "row" }, h("h1", {}, "Clients"), can("clients:manage") ? h("button", { class: "primary", type: "button", onclick: () => newClient(list) }, "New client") : null),
    h("p", { class: "muted" }, "A client is one app that uses the gateway (TrustDesk, PagerLens, PayBroker...). It has its own revocable keys, the models it may use, a requests-per-minute limit and a monthly token budget."),
    h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Client", "Models", "Limit", "This month", "Budget", "Keys", "Status"].map((t) => h("th", {}, t || h("span", { class: "sr-only" }, "Actions"))))),
      h("tbody", {}, list.items.map((c) => h("tr", { class: "clickable", tabindex: "0", onclick: () => openClient(c.id), onkeydown: (e) => { if (e.key === "Enter") openClient(c.id); } },
        h("td", { class: "title" }, c.name), h("td", { class: "wrap" }, c.allowed_aliases.includes("*") ? "all" : c.allowed_aliases.join(", ")),
        h("td", {}, c.rpm_limit ? `${c.rpm_limit}/min` : "unlimited"),
        h("td", {}, `${compact(c.tokens_month)} tokens · ${num(c.requests_month)} req · ${usd(c.cost_month)}`),
        h("td", {}, c.monthly_token_budget ? [meter(c.tokens_month / c.monthly_token_budget, "Budget used"), ` ${compact(c.monthly_token_budget)} `, h("span", { class: "muted" }, c.budget_mode)] : h("span", { class: "muted" }, "none")),
        h("td", { class: "num" }, c.active_keys), h("td", {}, h("span", { class: `badge ${c.enabled ? "ok" : "bad"}` }, c.enabled ? "active" : "disabled")))))),
      list.items.length ? null : h("p", { class: "empty" }, "No clients yet. Create one to get a gateway key.")));
}

async function aliasChoices() {
  const m = await api("/api/models");
  return m.items.map((a) => a.alias);
}

function aliasPicker(all, chosen) {
  const star = h("input", { type: "checkbox", id: "al-all", checked: chosen.includes("*") });
  const boxes = all.map((a) => h("label", {}, h("input", { type: "checkbox", value: a, checked: chosen.includes(a) }), a));
  const wrap = h("div", { class: "checkgrid" }, h("label", {}, star, "All models"), ...boxes);
  wrap.value = () => (star.checked ? ["*"] : [...wrap.querySelectorAll("input[value]:checked")].map((i) => i.value));
  return wrap;
}

async function newClient(list) {
  let all;
  try { all = await aliasChoices(); } catch (err) { toast(err.message, true); return; }
  const name = h("input", { id: "c-name", maxlength: "40", required: true, placeholder: "e.g. trustdesk", pattern: "[a-z0-9][a-z0-9._\\-]*" });
  const desc = h("input", { id: "c-desc", maxlength: "200" });
  const rpm = h("input", { id: "c-rpm", type: "number", min: "1", placeholder: `default ${list.default_rpm}` });
  const unl = h("input", { id: "c-unl", type: "checkbox" });
  const budget = h("input", { id: "c-budget", type: "number", min: "1", placeholder: "no budget" });
  const mode = h("select", { id: "c-mode" }, h("option", { value: "hard" }, "Hard: refuse requests once used up"), h("option", { value: "soft" }, "Soft: allow, but flag and show a banner"));
  const picker = aliasPicker(all, ["*"]);
  const err = h("p", { class: "error", role: "alert" });
  fill($("form-body"), h("h2", {}, "New client"), h("form", { onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    try {
      const r = await api("/api/clients", { method: "POST", json: { name: name.value, description: desc.value, allowed_aliases: picker.value(), rpm_limit: optInt(rpm.value), unlimited_rpm: unl.checked,
        monthly_token_budget: optInt(budget.value), budget_mode: mode.value } });
      $("form").close();
      await initClients();
      openClient(r.client.id, { title: "Client created, first key issued.", key: r.key.key });
    } catch (ex) { err.textContent = ex.message; }
  } }, field("Name (lower case; the key that app's token must carry as its subject)", name), field("Description", desc), field("Models it may use", picker),
  h("div", { class: "grid2" }, field("Requests per minute", rpm), h("label", { class: "check" }, unl, " No request limit")),
  h("div", { class: "grid2" }, field("Monthly token budget", budget), field("When the budget is used up", mode)),
  h("button", { class: "primary", type: "submit" }, "Create client and issue a key"), err));
  $("form").showModal();
  name.focus();
}

async function openClient(id, revealed) {
  fill($("detail-body"), h("p", { class: "loading" }, "Loading…"));
  if (!$("detail").open) $("detail").showModal();
  let list, keys, all;
  try { [list, keys, all] = await Promise.all([api("/api/clients"), api(`/api/clients/${id}/keys`), aliasChoices()]); } catch (err) { fill($("detail-body"), h("p", { class: "error" }, err.message)); return; }
  const c = list.items.find((x) => x.id === id);
  if (!c) { fill($("detail-body"), h("p", { class: "error" }, "That client no longer exists.")); return; }
  const manage = can("clients:manage");
  const reload = () => { openClient(id); initClients(); };
  const reveal = h("div", { hidden: true });
  const show = (title, secret) => {
    reveal.replaceChildren(h("p", { class: "warn-text" }, `${title} Copy it now: it is shown only once and only a hash is stored.`),
      h("div", { class: "codewrap" }, h("pre", { class: "code" }, h("code", {}, secret)), h("button", { class: "secondary small", type: "button", onclick: () => copy(secret, "Key copied") }, "Copy")));
    reveal.hidden = false;
  };
  if (revealed) show(revealed.title, revealed.key);

  const desc = h("input", { id: "cd-desc", maxlength: "200", value: c.description });
  const rpm = h("input", { id: "cd-rpm", type: "number", min: "1", value: c.rpm_limit ?? "", placeholder: "unlimited" });
  const budget = h("input", { id: "cd-budget", type: "number", min: "1", value: c.monthly_token_budget ?? "", placeholder: "no budget" });
  const mode = h("select", { id: "cd-mode" }, h("option", { value: "hard", selected: c.budget_mode === "hard" }, "Hard: refuse once used up"), h("option", { value: "soft", selected: c.budget_mode === "soft" }, "Soft: allow and flag"));
  const enabled = h("input", { id: "cd-enabled", type: "checkbox", checked: c.enabled });
  const picker = aliasPicker(all, c.allowed_aliases);
  const err = h("p", { class: "error", role: "alert" });
  const form = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    try {
      await api(`/api/clients/${id}`, { method: "PATCH", json: { description: desc.value, allowed_aliases: picker.value(), rpm_limit: optInt(rpm.value), monthly_token_budget: optInt(budget.value), budget_mode: mode.value, enabled: enabled.checked } });
      toast("Saved");
      reload();
    } catch (ex) { err.textContent = ex.message; }
  } }, h("h3", {}, "Limits and access"), field("Description", desc), field("Models it may use", picker),
  h("div", { class: "grid3" }, field("Requests per minute (empty: unlimited)", rpm), field("Monthly token budget (UTC month)", budget), field("Budget mode", mode)),
  h("label", { class: "check" }, enabled, " Enabled (a disabled client's keys are refused with 403)"),
  manage ? h("button", { class: "primary", type: "submit" }, "Save") : null, err);
  if (!manage) for (const el of form.querySelectorAll("input,select")) el.disabled = true;

  const kname = h("input", { id: "key-name", maxlength: "60", placeholder: "e.g. production" });
  const status = (k) => (k.revoked ? h("span", { class: "muted" }, "revoked") : k.revoked_at ? h("span", { class: "muted" }, `revokes ${new Date(k.revoked_at).toLocaleString()}`) : null);
  const keyCard = h("div", { class: "card" }, h("h3", {}, "Gateway keys"),
    manage ? h("form", { class: "grid-inline", onsubmit: async (e) => {
      e.preventDefault();
      try { const k = await api(`/api/clients/${id}/keys`, { method: "POST", json: { name: kname.value } }); kname.value = ""; initClients(); openClient(id, { title: "New key.", key: k.key }); } catch (ex) { toast(ex.message, true); }
    } }, field("Name", kname), h("span", {}), h("button", { class: "primary", type: "submit" }, "Issue key")) : null,
    reveal,
    h("table", {}, h("thead", {}, h("tr", {}, ...["Key", "Name", "Created", "Last used", ""].map((t) => h("th", {}, t || h("span", { class: "sr-only" }, "Actions"))))),
      h("tbody", {}, keys.items.map((k) => h("tr", {}, h("td", {}, h("code", {}, `${k.prefix}…`)), h("td", {}, k.name), h("td", {}, fmtDate(k.created_at)), h("td", {}, fmtDate(k.last_used_at)),
        h("td", { class: "actions" }, status(k) || (manage ? [h("button", { class: "secondary small", type: "button", onclick: async () => {
          const grace = await promptDialog("Rotate: issue a new key and revoke this one. Minutes to keep the old key working so the app can switch (0 = revoke now):", { title: "Rotate key", label: "Grace minutes", inputType: "number", min: 0, defaultValue: "10", required: true, confirmLabel: "Rotate" });
          if (grace === null) return;
          try { const r = await api(`/api/clients/${id}/keys/${k.id}/rotate`, { method: "POST", json: { grace_minutes: Number(grace) || 0 } }); initClients(); openClient(id, { title: "Rotated: this is the new key.", key: r.key }); } catch (ex) { toast(ex.message, true); }
        } }, "Rotate"), " ", h("button", { class: "danger small", type: "button", onclick: async () => {
          if (!(await confirmDialog("Revoke this key? The app using it stops working immediately.", { title: "Revoke key", confirmLabel: "Revoke", danger: true }))) return;
          try { await api(`/api/clients/${id}/keys/${k.id}`, { method: "DELETE" }); reload(); } catch (ex) { toast(ex.message, true); }
        } }, "Revoke")] : null)))))));
  fill($("detail-body"),
    h("div", { class: "row" }, h("h2", {}, c.name), h("span", { class: `badge ${c.enabled ? "ok" : "bad"}` }, c.enabled ? "active" : "disabled")),
    h("p", { class: "muted" }, `This month: ${num(c.tokens_month)} tokens, ${num(c.requests_month)} requests, ${usd(c.cost_month)} estimated. A service token from the identity provider is accepted for this client when its subject is "${c.name}".`),
    keyCard, form,
    manage ? h("div", { class: "btns" }, h("button", { class: "danger", type: "button", onclick: async () => {
      if (!(await confirmDialog(`Delete "${c.name}" and all its keys? Its usage history stays, unattributed.`, { title: "Delete client", confirmLabel: "Delete", danger: true }))) return;
      try { await api(`/api/clients/${id}`, { method: "DELETE" }); $("detail").close(); initClients(); } catch (ex) { toast(ex.message, true); }
    } }, "Delete client")) : null);
}
