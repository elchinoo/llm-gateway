/**
 * Recorder and rehearsal for docs/VIDEO-SCRIPT.md. Performs the script's on-screen actions in real Chrome, with video.
 *
 *   cd docs/video && npm install && npx playwright test            # slow pacing, writes out/**.webm
 *   PACE=0.05 NOVIDEO=1 npx playwright test                         # fast rehearsal with all assertions
 *
 * Everything is local and throwaway: a Postgres cluster on 127.0.0.1 (database name ends in "test", no unix socket), the gateway on a free
 * port started from ../../.venv, and a fake always-503 provider on 127.0.0.1. No real key, no live service. The "terminal" scenes are drawn
 * as an on-screen terminal panel that shows the real command and the real response of the real gateway.
 * Narration cut (driven by docs/video/build.py): SCENE_INFO=<durations.json> makes every scene follow the measured audio (actions are anchored to
 * the start of the narration sentence they illustrate), SCENE_DURATIONS=<json> gives the minimum length of each scene (audio + 0.7 s), and
 * TIMINGS_OUT=<file> receives the scene start times. LLMGW_PORT_RANGE=lo-hi keeps every port the run opens inside that range.
 * Needs the sibling checkout ecosystem/e2e (its harness starts Postgres and the process); override with E2E_HARNESS=/path/to/harness.
 */
import { test, expect, Locator, Page } from '@playwright/test';
import * as http from 'node:http';
import * as net from 'node:net';
import * as path from 'node:path';
import * as fs from 'node:fs';

const HARNESS = process.env.E2E_HARNESS || path.resolve(__dirname, '../../../ecosystem/e2e/harness');
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { startCluster } = require(path.join(HARNESS, 'pg'));
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { baseEnv, extractSetupCode, runOnce, startProcess } = require(path.join(HARNESS, 'proc'));
// eslint-disable-next-line @typescript-eslint/no-var-requires
const harnessUtil = require(path.join(HARNESS, 'util'));
if (process.env.LLMGW_PORT_RANGE) {
  // the harness asks the OS for any free port; hand out ports from the given range instead (also to its Postgres helper)
  const [lo, hi] = process.env.LLMGW_PORT_RANGE.split('-').map(Number);
  const taken = new Set<number>();
  harnessUtil.freePort = async (): Promise<number> => {
    for (let p = lo; p <= hi; p++) {
      if (taken.has(p)) continue;
      const ok = await new Promise<boolean>((res) => {
        const srv = net.createServer();
        srv.once('error', () => res(false));
        srv.listen(p, '127.0.0.1', () => srv.close(() => res(true)));
      });
      if (ok) { taken.add(p); return p; }
    }
    throw new Error(`no free port in ${lo}-${hi}`);
  };
}
const { adminPassword, freePort, waitFor } = harnessUtil;

const REPO = path.resolve(__dirname, '../..');
const PACE = Number(process.env.PACE ?? '1');
const pause = (s: number) => new Promise((r) => setTimeout(r, s * 1000 * PACE));

// ---------------------------------------------------------------- narration timing (only active when build.py sets SCENE_INFO)

const readJson = (f?: string) => (f ? JSON.parse(fs.readFileSync(f, 'utf8')) : null);
const INFO: Record<string, { audio: number; offsets: number[] }> | null = readJson(process.env.SCENE_INFO);
const MIN: Record<string, number> | null = readJson(process.env.SCENE_DURATIONS);
const LEAD = 0.25; // on-screen change a little before the sentence it illustrates
const marks: Record<number, number> = {};
let curScene = 0;
let curT0 = 0;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
/** Start a scene: closes the previous one (holds it until audio + 0.7 s have passed) and records the start time. */
async function scene(n: number) {
  await endScene();
  curScene = n;
  curT0 = Date.now();
  marks[n] = curT0;
}
async function endScene() {
  if (!curScene || !MIN) return;
  const need = MIN[String(curScene)] * 1000 - (Date.now() - curT0);
  if (need > 0) await sleep(need);
  else console.log(`scene ${curScene}: actions overran the minimum by ${(-need / 1000).toFixed(1)} s`);
}
/** Wait until the narration sentence k of the current scene begins (rehearsal mode: a short pause). */
async function at(k: number, fallback = 1) {
  if (!INFO) { await pause(fallback); return; }
  const off = INFO[String(curScene)].offsets[k];
  if (off === undefined) throw new Error(`scene ${curScene} has no narration sentence ${k}`);
  const wait = curT0 + (off - LEAD) * 1000 - Date.now();
  if (wait > 0) await sleep(wait);
  else if (wait < -1000) console.log(`scene ${curScene}: sentence ${k} reached ${(-wait / 1000).toFixed(1)} s late`);
}
const hold = (k: number, fallback = 1) => () => at(k, fallback);
/** Hold a callout until the narration of the current scene is nearly finished. */
const holdEnd = (fallback = 2) => async () => {
  if (!INFO) { await pause(fallback); return; }
  const wait = curT0 + INFO[String(curScene)].audio * 1000 - 300 - Date.now();
  if (wait > 0) await sleep(wait);
};

// ---------------------------------------------------------------- on-screen helpers (top layer, so they also sit above <dialog>s)

const OVERLAY_JS = `
// gateway keys and tokens (lg_..., lgt_...) are never legible on screen: blur them the moment they are rendered
new MutationObserver(() => {
  document.querySelectorAll('pre.code code').forEach((n) => { if (/^\\s*lgt?_/.test(n.textContent || '')) n.style.filter = 'blur(9px)'; });
}).observe(document, { childList: true, subtree: true, characterData: true });
window.__vid = {
  panel(id, html, css) {
    let el = document.getElementById(id);
    if (!el) { el = document.createElement('div'); el.id = id; el.setAttribute('popover', 'manual'); document.body.append(el); }
    el.style.cssText = 'margin:0;border:0;padding:0;inset:0;width:100vw;height:100vh;max-width:none;max-height:none;' + css;
    el.innerHTML = html;
    // the gateway's CSP blocks inline style attributes in parsed markup; re-apply them through the CSSOM, which is allowed
    el.querySelectorAll('[style]').forEach((n) => { n.style.cssText = n.getAttribute('style'); });
    if (!el.matches(':popover-open')) el.showPopover();
    return el;
  },
  hide(id) { const el = document.getElementById(id); if (el) { if (el.matches(':popover-open')) el.hidePopover(); el.style.display = 'none'; } },
};`;

async function prepare(page: Page) {
  await page.addInitScript(OVERLAY_JS);
}

/** Full-screen title card or diagram (HTML given by the caller). */
async function card(page: Page, html: string) {
  await page.evaluate(
    ([h]) => (window as any).__vid.panel('vid-card', h, 'background:#0d1b2a;color:#f4f7fb;font:32px/1.4 system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:28px;text-align:center'),
    [html],
  );
}
const hideCard = (page: Page) => page.evaluate(() => (window as any).__vid.hide('vid-card'));

let shotN = 0;
/** A callout: a label bar at the bottom of the screen; with a selector it also outlines that element. */
async function callout(page: Page, text: string, target?: string | Locator, seconds: number | (() => Promise<void>) = 2.5) {
  const loc = typeof target === 'string' ? page.locator(target).first() : target?.first();
  const rect = loc ? await loc.boundingBox() : null;
  await page.evaluate(
    ([t, rect]) => {
      const v = (window as any).__vid;
      const id = 'vid-callout-' + Math.random().toString(36).slice(2, 7);
      const el = v.panel(id, '<div style="position:fixed;left:50%;bottom:48px;transform:translateX(-50%);background:#ffd23f;color:#111;font:600 30px system-ui,sans-serif;padding:14px 28px;border-radius:12px;box-shadow:0 6px 30px #0008">' + t.replace(/</g, '&lt;') + '</div>', 'background:transparent;pointer-events:none;');
      (window as any).__callouts = [...((window as any).__callouts || []), id];
      if (rect) {
        {
          const r = rect as { x: number; y: number; width: number; height: number };
          const box = document.createElement('div');
          box.style.cssText = 'position:fixed;left:' + (r.x - 8) + 'px;top:' + (r.y - 8) + 'px;width:' + (r.width + 16) + 'px;height:' + (r.height + 16) + 'px;border:5px solid #ffd23f;border-radius:10px;box-shadow:0 0 0 9999px #0006;';
          el.append(box);
        }
      }
    },
    [text, rect] as const,
  );
  if (process.env.SHOTS) await page.screenshot({ path: path.join(process.env.SHOTS, `callout-${String(++shotN).padStart(2, '0')}.png`) });
  if (typeof seconds === 'function') await seconds(); else await pause(seconds);
  await page.evaluate(() => { for (const id of (window as any).__callouts || []) { (window as any).__vid.hide(id); document.getElementById(id)?.remove(); } (window as any).__callouts = []; });
}

/** The demo terminal: a dark panel that shows commands and their real output. */
class Term {
  private lines: string[] = [];
  constructor(private page: Page) {}
  private async draw() {
    const html = '<pre style="margin:0;padding:48px 64px;font:26px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all">' +
      this.lines.join('\n').replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</pre>';
    await this.page.evaluate(([h]) => (window as any).__vid.panel('vid-term', h, 'background:#0b0f14;color:#d6e2ee;overflow:auto'), [html]);
  }
  async run(cmd: string, output: string) {
    this.lines.push('$ ' + cmd);
    await this.draw();
    await pause(1.2);
    this.lines.push(output, '');
    await this.draw();
  }
  async note(text: string) { this.lines.push(text); await this.draw(); }
  async close() { await this.page.evaluate(() => (window as any).__vid.hide('vid-term')); this.lines = []; }
}

// ---------------------------------------------------------------- fake flaky provider and gateway client

function startFlaky(port: number): Promise<http.Server> {
  const srv = http.createServer((req, res) => {
    req.on('data', () => {});
    req.on('end', () => { res.writeHead(503, { 'content-type': 'application/json' }); res.end('{"error": "overloaded"}'); });
  });
  return new Promise((resolve) => srv.listen(port, '127.0.0.1', () => resolve(srv)));
}

const chatBody = (model: string, content: string) => JSON.stringify({ model, messages: [{ role: 'user', content }] });

test('video script: one door for every model call', async ({ page }, testInfo) => {
  test.setTimeout(20 * 60_000);
  await prepare(page);

  // ---- environment: throwaway Postgres, fake provider, gateway
  const cluster = await startCluster();
  const flakyPort = await freePort();
  const flaky = await startFlaky(flakyPort);
  let gw: any;
  try {
    cluster.createDb('llmgw_video_test');
    const port = await freePort();
    const GW = `http://127.0.0.1:${port}`;
    console.log('ports: postgres', cluster.port, '| fake provider', flakyPort, '| gateway', port);
    const work = fs.mkdtempSync(path.join(cluster.dir, 'work-'));
    const env = { ...baseEnv(work), DATABASE_URL: cluster.url('llmgw_video_test'), PORT: String(port), LLMGW_PUBLIC_URL: GW, LLMGW_ALLOW_PRIVATE_URLS: 'true' };
    const bin = path.join(REPO, '.venv', 'bin', 'llmgw');
    await runOnce(bin, ['migrate'], env, work);
    gw = await startProcess({ name: 'llmgw', cmd: bin, args: ['serve'], env, cwd: work, url: GW });
    const setupCode: string = await waitFor('setup code', async () => extractSetupCode(gw.logs()), 15_000);
    const password: string = adminPassword();

    const post = (p: string, key: string, body: string) =>
      fetch(GW + p, { method: 'POST', headers: { 'content-type': 'application/json', authorization: `Bearer ${key}` }, body });
    const get = (p: string, key: string) => fetch(GW + p, { headers: { authorization: `Bearer ${key}` } });
    const term = new Term(page);

    // ---- scene 1: title card, then the first-run screen
    await page.goto(GW + '/');
    await card(page, '<div style="font-size:84px;font-weight:700">LLM gateway</div><div style="font-size:40px;color:#9fb6cc">one OpenAI-compatible endpoint, on Postgres</div>');
    await scene(1);
    await expect(page.getByRole('heading', { name: 'Set up the LLM gateway' })).toBeVisible();
    await at(5, 4); // "In this video you will see three things"
    await hideCard(page);

    // ---- scene 2: the idea (diagram)
    await scene(2);
    const box = (t: string, s = '') => `<div style="border:3px solid #5aa9e6;border-radius:16px;padding:24px 36px;min-width:300px">${t}<div style="font-size:22px;color:#9fb6cc">${s}</div></div>`;
    const arrow = (t: string, hot = false) => `<div style="font-size:26px;${hot ? 'color:#ffd23f;font-weight:700' : 'color:#9fb6cc'}">${t}<br>&rarr;</div>`;
    const diagram = (hot: 0 | 1 | 2) => `<div style="display:flex;align-items:center;gap:30px">${box('Apps', 'TrustDesk, PagerLens, PayBroker')}${arrow('lg_ key', hot === 1)}${box('LLM gateway', 'limits, budgets, failover')}${arrow('provider key', hot === 2)}${box('Providers', 'real models')}</div>`;
    await card(page, diagram(0));
    await at(2, 3);
    await card(page, diagram(1) + '<div style="font-size:34px;color:#ffd23f">App key: revocable, budgeted</div>');
    await at(4, 5);
    await card(page, diagram(2) + '<div style="font-size:34px;color:#ffd23f">Provider key: encrypted, stored once</div>');
    await at(6, 6);
    await card(page, diagram(0) + '<div style="font-size:34px;color:#9fb6cc">Standard OpenAI format: only an address and a key change</div>');
    await endScene();
    await hideCard(page);

    // ---- scene 3: setup code in the log, then the setup form
    await scene(3);
    await term.note('$ llmgw serve');
    await term.note(gw.logs().split('\n').filter((l: string) => /llmgw|setup|code|^\s{2,}\S/i.test(l)).slice(-8).join('\n'));
    await callout(page, 'One-time setup code', undefined, hold(1, 3));
    await term.close();
    await page.locator('#setup-code').fill(setupCode);
    await page.locator('#setup-pw').fill(password);
    await page.locator('#setup-pw2').fill(password);
    await callout(page, 'Whoever holds the code chooses the admin password', '#setup-code', hold(3, 2));
    await page.locator('#auth-card form button[type=submit], #auth-card button').first().click();
    await expect(page.locator('#nav')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'No traffic yet' })).toBeVisible();
    await callout(page, 'Seeded: echo provider, echo-chat, echo-embed, chat-default, embed-default', undefined, holdEnd(3));
    await pause(2);

    // ---- scene 4: client trustdesk
    await scene(4);
    await page.locator('#nav a[data-tab="clients"]').click();
    await page.getByRole('button', { name: 'New client' }).click();
    const form = page.locator('#form');
    await at(1);
    await form.locator('#c-name').fill('trustdesk');
    await at(2);
    await form.locator('#c-rpm').fill('60');
    await at(3);
    await form.getByRole('button', { name: 'Create client and issue a key' }).click();
    const keyEl = page.locator('#detail pre.code code').first();
    await expect(keyEl).toHaveText(/^lg_/);
    const APP_KEY = (await keyEl.textContent())!.trim();
    await callout(page, 'Shown once. Only a SHA-256 hash is stored.', '#detail pre.code', hold(5, 3));
    await page.locator('#detail .codewrap button', { hasText: 'Copy' }).click().catch(() => {});
    await pause(0.8); // the "Key copied" toast
    await page.locator('#detail-close').click();
    await at(6);

    let r = await post('/v1/chat/completions', APP_KEY, chatBody('echo-chat', 'hello'));
    expect(r.status).toBe(200);
    let j: any = await r.json();
    expect(j.choices[0].message.content).toBe('Echo from echo-1: hello');
    expect(j.model).toBe('echo-chat');
    expect(j.usage.total_tokens).toBeGreaterThan(0);
    await term.run(
      `curl -s $GW/v1/chat/completions -H "Authorization: Bearer $APP_KEY" -H "Content-Type: application/json" -d '{"model":"echo-chat","messages":[{"role":"user","content":"hello"}]}' | jq '.choices[0].message.content, .model, .usage'`,
      [JSON.stringify(j.choices[0].message.content), JSON.stringify(j.model), JSON.stringify(j.usage, null, 2)].join('\n'),
    );
    await at(9);
    await callout(page, 'model = alias', undefined, hold(10, 2));
    await callout(page, 'usage: the token counts the gateway meters', undefined, holdEnd(3));

    // ---- scene 5: models and embeddings
    await scene(5);
    r = await get('/v1/models', APP_KEY);
    const ids = ((await r.json()) as any).data.map((m: any) => m.id);
    expect(ids).toEqual(expect.arrayContaining(['echo-chat', 'echo-embed']));
    await term.run(`curl -s $GW/v1/models -H "Authorization: Bearer $APP_KEY" | jq '.data[].id'`, ids.map((i: string) => JSON.stringify(i)).join('\n'));
    r = await post('/v1/embeddings', APP_KEY, JSON.stringify({ model: 'echo-embed', input: ['first text'] }));
    const e1: any = await r.json();
    r = await post('/v1/embeddings', APP_KEY, JSON.stringify({ model: 'echo-embed', input: ['first text'] }));
    const e2: any = await r.json();
    expect(e1.data[0].embedding.length).toBe(768);
    expect(e2.data[0].embedding).toEqual(e1.data[0].embedding);
    await at(1);
    await term.run(`curl -s $GW/v1/embeddings -H "Authorization: Bearer $APP_KEY" -H "Content-Type: application/json" -d '{"model":"echo-embed","input":["first text"]}' | jq '.data[0].embedding | length'`, String(e1.data[0].embedding.length));
    await at(3);
    await callout(page, '768 numbers', undefined, hold(5, 2));
    await callout(page, 'Chat and embeddings. No streaming.', undefined, holdEnd(3));

    // ---- scene 6: flaky provider and the resilient-chat alias
    await scene(6);
    await term.close();
    await page.locator('#nav a[data-tab="providers"]').click();
    await page.getByRole('button', { name: 'Add a provider' }).click();
    await at(1);
    await form.locator('#pv-name').fill('flaky');
    await form.locator('#pv-url').fill(`http://127.0.0.1:${flakyPort}/v1`);
    await form.locator('#pv-key').fill('not-a-real-key');
    await form.getByRole('button', { name: 'Save' }).click();
    const flakyRow = page.getByRole('row', { name: /flaky/ });
    await expect(flakyRow).toBeVisible();
    await expect(flakyRow).toContainText('…-key'); // last 4 characters of "not-a-real-key"
    await at(2);
    await callout(page, 'Only the last 4 characters are shown', flakyRow.locator('td').nth(3), hold(4, 3));
    await at(4);
    await flakyRow.getByRole('button', { name: 'Test', exact: true }).click();
    await form.locator('#pt-model').fill('flaky-1');
    await form.getByRole('button', { name: 'Test', exact: true }).click();
    await expect(form.locator('p[role=status]')).toContainText(/^Failed/);
    await at(5);
    await callout(page, 'Simulated outage', '#form p[role=status]', hold(7, 3));
    await page.locator('#form-close').click();

    await page.locator('#nav a[data-tab="models"]').click();
    await page.getByRole('button', { name: 'New model alias' }).click();
    await form.locator('#al-name').fill('resilient-chat');
    await at(8);
    await form.getByLabel('Provider for target 1').selectOption({ label: 'flaky' });
    await form.getByLabel('Upstream model for target 1').fill('flaky-1');
    await at(9);
    await form.getByRole('button', { name: 'Add a fallback target' }).click();
    await form.getByLabel('Provider for target 2').selectOption({ label: 'echo (built-in)' });
    await form.getByLabel('Upstream model for target 2').fill('echo-1');
    await at(10);
    await callout(page, 'Ordered targets: 1 to 5', '#form .target', 3);
    await form.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.locator('#toast')).toContainText('Saved');

    // ---- scene 7: failover and the usage proof (operator token, created off camera)
    await scene(7);
    const csrf = (await page.evaluate(() => document.documentElement.getAttribute('data-csrf')))!;
    const tok = await page.context().request.post(GW + '/api/tokens', { data: { name: 'video-read', scope: 'read' }, headers: { [csrf]: '1' } });
    expect(tok.ok()).toBeTruthy();
    const OPS: string = ((await tok.json()) as any).token;
    r = await post('/v1/chat/completions', APP_KEY, chatBody('resilient-chat', 'hello'));
    expect(r.status).toBe(200);
    j = await r.json();
    await term.run(`curl -s $GW/v1/chat/completions -H "Authorization: Bearer $APP_KEY" -H "Content-Type: application/json" -d '{"model":"resilient-chat","messages":[{"role":"user","content":"hello"}]}' | jq '.choices[0].message.content, .model'`,
      [JSON.stringify(j.choices[0].message.content), JSON.stringify(j.model)].join('\n'));
    await at(2);
    const recent: any = await (await get('/api/usage/recent?limit=1', OPS)).json();
    const it = recent.items[0];
    expect(it.alias).toBe('resilient-chat');
    expect(it.provider_name).toBe('echo');
    expect(it.attempts).toBe(2);
    await term.run(`curl -s $GW/api/usage/recent?limit=1 -H "Authorization: Bearer $OPS" | jq '.items[0] | {alias, provider_name, status, attempts}'`,
      JSON.stringify({ alias: it.alias, provider_name: it.provider_name, status: it.status, attempts: it.attempts }, null, 2));
    await callout(page, 'attempts: 2, provider: echo', undefined, hold(4, 3));
    await callout(page, 'Failover on 5xx, 429, timeout, connection error', undefined, hold(6, 3));
    await callout(page, 'No failover on other client errors', undefined, holdEnd(3));

    // ---- scene 8: hard budget of 40 tokens
    await scene(8);
    await term.close();
    await page.locator('#nav a[data-tab="clients"]').click();
    await page.getByRole('button', { name: 'New client' }).click();
    await at(1);
    await form.locator('#c-name').fill('budget-demo');
    await at(2);
    await form.locator('#c-budget').fill('40');
    await form.locator('#c-mode').selectOption('hard');
    await form.getByRole('button', { name: 'Create client and issue a key' }).click();
    await expect(page.locator('#detail-body')).toContainText('budget-demo');
    await expect(keyEl).toHaveText(/^lg_/);
    const LOW_KEY = (await keyEl.textContent())!.trim();
    await at(3);
    await page.locator('#detail-close').click();
    const codes: number[] = [];
    let last: Response | undefined;
    for (let i = 0; i < 6; i++) {
      last = await post('/v1/chat/completions', LOW_KEY, chatBody('echo-chat', 'spend some tokens please'));
      codes.push(last.status);
    }
    expect(codes.some((c) => c >= 500)).toBe(false);
    expect(codes[0]).toBe(200);
    expect(codes).toContain(429);
    await at(4);
    await term.run(`for i in 1 2 3 4 5 6; do curl -s -o /dev/null -w "%{http_code}\\n" $GW/v1/chat/completions -H "Authorization: Bearer $LOW_KEY" ... ; done`, codes.join('\n'));
    await at(5);
    await callout(page, codes.join(', '), undefined, hold(7, 2));
    const rej = await post('/v1/chat/completions', LOW_KEY, chatBody('echo-chat', 'spend some tokens please'));
    const rejBody: any = await rej.json();
    expect(rej.status).toBe(429);
    expect(rejBody.error.code).toBe('budget_exceeded');
    expect(Number(rej.headers.get('retry-after'))).toBeGreaterThan(0);
    await term.run(`curl -s $GW/v1/chat/completions -H "Authorization: Bearer $LOW_KEY" ... | jq .error.code`, JSON.stringify(rejBody.error.code) + `   (Retry-After: ${rej.headers.get('retry-after')} s)`);
    await callout(page, 'budget_exceeded + Retry-After', undefined, hold(9, 3));
    await callout(page, 'Hard = refuse. Soft = allow and flag.', undefined, holdEnd(3));

    // ---- rate limit (not on screen, verified for the narration): a client with 2 requests per minute
    const rl = await page.context().request.post(GW + '/api/clients', { data: { name: 'rpm-probe', allowed_aliases: ['*'], rpm_limit: 2 }, headers: { [csrf]: '1' } });
    const rlKey: string = ((await rl.json()) as any).key.key;
    const rlCodes: number[] = [];
    for (let i = 0; i < 4; i++) rlCodes.push((await post('/v1/chat/completions', rlKey, chatBody('echo-chat', 'hi'))).status);
    expect(rlCodes).toEqual([200, 200, 429, 429]);

    // ---- scene 9: overview banner, tables, audit
    await scene(9);
    await term.close();
    await page.locator('#nav a[data-tab="overview"]').click();
    const banner = page.locator('#view-overview .alert.bad');
    await expect(banner).toContainText('Budget used up: ');
    await expect(banner).toContainText('budget-demo');
    for (const y of [500, 1000, 1500]) { await pause(1); await page.mouse.wheel(0, y); await pause(0.8); }
    await at(3);
    await page.mouse.wheel(0, -5000);
    await pause(0.8);
    await callout(page, 'Budget used up: budget-demo', '#view-overview .alert.bad', hold(4, 3));
    await page.locator('#nav a[data-tab="audit"]').click();
    for (const a of ['provider.create', 'model.create', 'client.create']) await expect(page.locator('#view-audit').getByText(a, { exact: true }).first()).toBeVisible();
    await expect(page.locator('#view-audit')).toContainText(/key\.issue|client\.create/);
    await callout(page, 'Who, what, when. Never secrets.', '#view-audit table', hold(7, 3));
    await page.mouse.wheel(0, 600);
    await callout(page, 'No prompts or completions stored', undefined, holdEnd(3));

    // ---- scene 10: chat-default and Connect
    await scene(10);
    await page.locator('#nav a[data-tab="models"]').click();
    await at(2);
    await page.getByRole('row', { name: /^chat-default/ }).getByRole('button', { name: 'Edit' }).click();
    await expect(page.locator('#form')).toBeVisible();
    await at(3);
    await callout(page, 'Change this one field to move the whole fleet', '#form .target .row', hold(7, 4));
    await page.locator('#form-close').click();
    await page.locator('#nav a[data-tab="connect"]').click();
    await expect(page.locator('#view-connect')).toContainText('768');
    await page.mouse.wheel(0, 700);
    await at(8);
    await callout(page, 'Roles: admin, operator, viewer', undefined, holdEnd(2));

    // ---- scene 11: end card
    await scene(11);
    const recapItems = ['One gateway holds your provider keys', 'Each app: its own key, limits and budget', 'A failing provider fails over to the next target', 'Usage metered per app, no prompts stored', 'One alias edit switches the whole fleet'];
    const recapCard = (k: number) => card(page, '<div style="font-size:60px;font-weight:700">Recap</div>' + recapItems.slice(0, k).map((t) => `<div style="font-size:42px;color:#f4f7fb">${t}</div>`).join(''));
    await recapCard(0);
    for (let k = 1; k <= 5; k++) { await at(k); await recapCard(k); }
    await at(6);
    await card(page, '<div style="font-size:56px;font-weight:700">Try it locally</div><div style="font-size:44px;color:#ffd23f">docker compose up --build</div><div style="font-size:34px;color:#9fb6cc">Sign in with the setup code, use the built-in echo models</div>');
    await at(8);
    await card(page, '<div style="font-size:56px">docs/GUIDE.md &nbsp; API.md</div><div style="font-size:46px;color:#ffd23f">Add a provider, repoint chat-default, done</div><div style="font-size:34px;color:#9fb6cc">Try it: docker compose up --build</div>');
    await endScene();
    await pause(1);
    if (process.env.TIMINGS_OUT) fs.writeFileSync(process.env.TIMINGS_OUT, JSON.stringify({ scene_start_ms: marks, end_ms: Date.now() }));
    await testInfo.attach('summary', { body: JSON.stringify({ budgetCodes: codes, rateLimitCodes: rlCodes, usageAttempts: it.attempts }), contentType: 'application/json' });
    console.log('budget codes', codes.join(','), '| rpm codes', rlCodes.join(','), '| attempts', it.attempts, '| retry-after', rej.headers.get('retry-after'));
  } finally {
    await gw?.stop();
    flaky.close();
    cluster.stop();
  }
});
