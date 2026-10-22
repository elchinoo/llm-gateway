import { api } from "/kit/api.js";
import { $, fill, h } from "/kit/ui.js";
import { compact, hbars, meter, num, svgEl, usd } from "/static/common.js";

let days = 7;

function dailyBars(rows) {
  if (!rows.length) return h("p", { class: "muted" }, "No requests in this period.");
  const max = Math.max(1, ...rows.map((r) => r.requests));
  const w = Math.max(10, Math.floor(480 / rows.length) - 4);
  const svg = svgEl("svg", { viewBox: "0 0 500 150", class: "chart", role: "img", "aria-label": "Requests per day" });
  rows.forEach((r, i) => {
    const x = i * (w + 4) + 4;
    const total = Math.round((r.requests / max) * 100);
    const bad = Math.round((r.errors / max) * 100);
    svg.append(svgEl("rect", { class: "bar", x, y: 110 - total, width: w, height: total }));
    if (bad) svg.append(svgEl("rect", { class: "bar err", x, y: 110 - bad, width: w, height: bad }));
    if (rows.length <= 14) svg.append(svgEl("text", { x: x + w / 2, y: 128, "text-anchor": "middle" }, String(r.key).slice(5)));
  });
  svg.append(svgEl("text", { x: 250, y: 146, "text-anchor": "middle" }, "requests per day (red: errors)"));
  return svg;
}

function table(headers, rows) {
  return h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...headers.map(([t, cls]) => h("th", { class: cls || false }, t)))),
    h("tbody", {}, rows)));
}

export async function initOverview() {
  const box = $("view-overview");
  fill(box, h("p", { class: "loading" }, "Loading…"));
  let o;
  try { o = await api(`/api/overview?days=${days}`); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const t = o.totals;
  const stat = (value, label, hot, href) => h("a", { class: `stat${hot ? " hot" : ""}`, href: href || "#overview" }, h("strong", {}, value), h("span", {}, label));
  const period = h("select", { "aria-label": "Period", onchange: (e) => { days = Number(e.target.value); initOverview(); } },
    ...[[1, "Last 24 hours"], [7, "Last 7 days"], [30, "Last 30 days"], [90, "Last 90 days"]].map(([v, label]) => h("option", { value: v, selected: v === days }, label)));
  const parts = [h("div", { class: "row" }, h("h1", {}, "Overview"), period)];
  if (o.budget_alerts.hard.length) {
    parts.push(h("div", { class: "alert bad", role: "alert" }, h("strong", {}, "Budget used up: "), `${o.budget_alerts.hard.join(", ")} cannot make requests until next month (hard limit) or until the budget is raised.`));
  }
  if (o.budget_alerts.soft.length) {
    parts.push(h("div", { class: "alert", role: "alert" }, h("strong", {}, "Over budget (soft limit): "), `${o.budget_alerts.soft.join(", ")} keep working, and their requests are flagged in the usage data.`));
  }
  parts.push(h("div", { class: "stats" },
    stat(num(t.requests), "requests"),
    stat(compact(t.total_tokens), `tokens (${compact(t.prompt_tokens)} in, ${compact(t.completion_tokens)} out)`),
    stat(usd(t.cost_usd), "estimated cost"),
    stat(num(t.errors), "errors", t.errors > 0),
    stat(`${t.p95_latency_ms} ms`, `p95 latency (avg ${t.avg_latency_ms})`),
    stat(num(t.estimated_requests), "with estimated tokens", false)));
  if (!t.requests && !o.clients.length) {
    parts.push(h("div", { class: "card" }, h("h3", {}, "No traffic yet"),
      h("p", { class: "muted" }, "Create a client under Clients to get a gateway key, then see Connect for how an app points at this gateway. The built-in echo-chat and echo-embed models need no provider or key, so you can try everything right now.")));
  } else {
    parts.push(h("div", { class: "cols" },
      h("div", { class: "card" }, h("h3", {}, "Requests per day"), dailyBars(o.daily)),
      h("div", { class: "card" }, h("h3", {}, "Top models"), o.models.length ? hbars(o.models.map((m) => ({ label: m.key, value: m.total_tokens })), "Tokens per model alias") : h("p", { class: "muted" }, "No requests yet."))));
    parts.push(h("h3", {}, "Clients: spend against budget"));
    parts.push(table([["Client"], ["Requests", "num"], ["Tokens", "num"], ["Est. cost", "num"], ["This month (tokens)", "num"], ["Budget"], ["Errors", "num"]],
      o.clients.map((c) => h("tr", {}, h("td", { class: "title" }, c.name, c.enabled ? null : h("span", { class: "badge bad" }, "disabled")),
        h("td", { class: "num" }, num(c.requests)), h("td", { class: "num" }, compact(c.tokens)), h("td", { class: "num" }, usd(c.cost_usd)),
        h("td", { class: "num" }, compact(c.tokens_month)),
        h("td", {}, c.budget ? [meter(c.budget_used, `${Math.round(c.budget_used * 100)}% of the monthly budget`), ` ${Math.round(c.budget_used * 100)}% of ${compact(c.budget)} `, h("span", { class: "muted" }, c.budget_mode)] : h("span", { class: "muted" }, "no budget")),
        h("td", { class: "num" }, num(c.errors))))));
    parts.push(h("div", { class: "cols" },
      h("div", {}, h("h3", {}, "Models"), table([["Alias"], ["Requests", "num"], ["Tokens", "num"], ["Est. cost", "num"], ["p95", "num"]],
        o.models.map((m) => h("tr", {}, h("td", { class: "title" }, m.key), h("td", { class: "num" }, num(m.requests)), h("td", { class: "num" }, compact(m.total_tokens)), h("td", { class: "num" }, usd(m.cost_usd)), h("td", { class: "num" }, `${m.p95_latency_ms} ms`))))),
      h("div", {}, h("h3", {}, "Errors"), o.errors.length ? table([["Class"], ["Count", "num"]], o.errors.map((e) => h("tr", {}, h("td", {}, e.error_class), h("td", { class: "num" }, num(e.n))))) : h("p", { class: "muted" }, "No errors in this period."))));
  }
  fill(box, ...parts);
}
