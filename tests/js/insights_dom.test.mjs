// Fake-DOM test of the Database tab: app.js mounts the kit's initInsights into #view-database and the panel renders the payload
// of GET /api/insights (a populated one, then a plain-Postgres one where every source says "not available").
import assert from "node:assert/strict";
import { register } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { El, install } from "./fakedom.mjs";

const here = path.dirname(new URL(import.meta.url).pathname);
register(pathToFileURL(path.join(here, "loader.mjs")).href, { data: { static: process.env.STATIC_DIR, kit: process.env.KIT_DIR, stubs: path.join(here, "stubs") } });
const dom = install();
const section = new El("section");
section.id = "view-database";
dom.byId["view-database"] = section;

const populated = {
  sources: { edb_stat_monitor: { available: true, version: "2.1" } },
  top_queries: { available: true, source: "edb_stat_monitor", order: "total", since: null, text_hidden: 0,
    queries: [{ queryid: "1", query: "SELECT * FROM usage_event WHERE client_id = $1", calls: 10, total_ms: 50, mean_ms: 5, max_ms: 9, rows: 100, since: null }] },
  tuner: { available: true, recommendations: [{ setting: "shared_buffers", value: "186 MB", raw: "shared_buffers = '186 MB'" }], global_stats: null },
  advisor: { available: true, suggestions: [] },
};
const plain = {
  sources: {},
  top_queries: { available: false, reason: "no statistics extension" },
  tuner: { available: false, reason: "edb_pg_tuner is not installed" },
  advisor: { available: false, reason: "query_advisor is not installed" },
};
let payload = populated;
globalThis.__api = (url) => { assert.match(url, /^\/api\/insights\?limit=50$/); return payload; };
globalThis.__calls = [];
const { initInsights } = await import("/kit/insights.js");
const root = dom.byId["view-database"];

await initInsights(root, (await import("/kit/api.js")).api);
assert.ok(root.textContent.includes("Query insights") && root.textContent.includes("usage_event"), "top queries are shown");
assert.ok(root.textContent.includes("shared_buffers"), "tuner recommendations are shown");

payload = plain;
await initInsights(root, (await import("/kit/api.js")).api);
assert.ok(root.textContent.includes("Not available on this database."), "an honest not-available state");
assert.ok(root.textContent.includes("edb_pg_tuner is not installed"), "with the server's reason");
console.log("insights_dom ok");
