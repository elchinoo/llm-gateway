import { api } from "/kit/api.js";
import { bootSession, can, refreshSession, renderLogin, renderSetup, renderVaultBanner, state } from "/kit/session.js";
import { initInsights } from "/kit/insights.js";
import { $, h } from "/kit/ui.js";
import { initAudit } from "/static/audit.js";
import { initClients } from "/static/clients.js";
import { initConnect } from "/static/connect.js";
import { initModels } from "/static/models.js";
import { initOverview } from "/static/overview.js";
import { initProviders } from "/static/providers.js";
import { initSettings } from "/static/settings.js";

const TABS = ["overview", "clients", "models", "providers", "audit", "settings", "database", "connect"];
const INIT = { overview: initOverview, clients: initClients, models: initModels, providers: initProviders, audit: initAudit, settings: initSettings, database: () => initInsights($("view-database"), api), connect: initConnect };
const NEEDS = { overview: "usage:read", clients: "config:read", models: "config:read", providers: "config:read", audit: "audit:read", settings: "settings:manage", database: "settings:manage", connect: "config:read" };
const SECTIONS = ["auth-card", "vault-banner", "nav", "signout", ...TABS.map((t) => "view-" + t)];

function hideAll() { for (const id of SECTIONS) $(id).hidden = true; }

async function boot(notice = "") {
  hideAll();
  $("auth-card").hidden = false;   // bootSession paints the loading state (and a retry on failure) here
  const s = await bootSession($("auth-card"), { text: "Loading\u2026", onReady: (s) => {
    if (s.state === "setup_required") renderSetup(() => boot(), "the LLM gateway");
    else if (s.state === "logged_out") renderLogin(() => boot(), notice, "the LLM gateway");
    else showApp();
  } });
  if (!s) $("auth-card").append(h("p", { class: "hint" }, "If this server runs on a platform that pauses idle apps, resume it first (for AgentPG: agentpg sandbox resume <sandbox-id>), then retry."));
}

function showApp() {
  $("auth-card").hidden = true;
  for (const a of document.querySelectorAll("#nav a")) a.hidden = !can(NEEDS[a.dataset.tab]);
  $("nav").hidden = false;
  $("signout").hidden = false;
  renderVaultBanner();
  route();
}

function route() {
  if (!state.session || state.session.state !== "logged_in") return;
  const allowed = TABS.filter((t) => can(NEEDS[t]));
  const want = location.hash.slice(1);
  const tab = allowed.includes(want) ? want : (allowed[0] || "overview");
  for (const t of TABS) $("view-" + t).hidden = t !== tab;
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.dataset.tab === tab);
  INIT[tab]();
}

$("signout").addEventListener("click", async () => {
  let out = {};
  try { out = await api("/api/logout", { method: "POST" }); } catch { /* the cookie is cleared server-side regardless */ }
  if (out.logout_url) { location.assign(out.logout_url); return; }
  boot();
});
for (const id of ["detail", "form"]) $(id + "-close").addEventListener("click", () => $(id).close());

let lostTimer;
window.addEventListener("apg-auth-lost", () => {
  clearTimeout(lostTimer);
  lostTimer = setTimeout(() => boot("Your session ended. Please sign in again."), 50);
});
window.addEventListener("apg-vault-locked", async () => { try { await refreshSession(); renderVaultBanner(); } catch { /* ignore */ } });
window.addEventListener("apg-unlocked", route);
window.addEventListener("hashchange", route);

boot();
