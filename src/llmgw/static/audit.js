import { api } from "/kit/api.js";
import { $, fill, h } from "/kit/ui.js";
import { stamp } from "/static/common.js";

export async function initAudit() {
  const box = $("view-audit");
  fill(box, h("p", { class: "loading" }, "Loading…"));
  const body = h("tbody", {});
  const more = h("button", { class: "secondary", type: "button", hidden: true }, "Load more");
  let last = null;
  async function load() {
    let r;
    try { r = await api(`/api/audit?limit=100${last ? `&before=${last}` : ""}`); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
    for (const e of r.items) {
      body.append(h("tr", {}, h("td", {}, stamp(e.at)), h("td", {}, e.actor), h("td", {}, h("code", {}, e.action)), h("td", {}, e.target), h("td", { class: "wrap" }, h("code", { class: "json" }, Object.keys(e.detail).length ? JSON.stringify(e.detail) : ""))));
      last = e.id;
    }
    more.hidden = r.items.length < 100;
  }
  more.addEventListener("click", load);
  fill(box, h("h1", {}, "Audit"), h("p", { class: "muted" }, "Who changed what and when. Keys and passwords are never recorded here."),
    h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["When", "Who", "What", "Target", "Details"].map((t) => h("th", {}, t)))), body)), more);
  await load();
  if (!body.children.length) box.insertBefore(h("p", { class: "empty" }, "Nothing has been changed yet."), more);
}
