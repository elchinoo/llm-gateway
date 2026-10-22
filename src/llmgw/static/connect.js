import { api } from "/kit/api.js";
import { $, copy, fill, h } from "/kit/ui.js";

const code = (text) => h("div", { class: "codewrap" }, h("pre", { class: "code" }, h("code", {}, text)), h("button", { class: "secondary small", type: "button", onclick: () => copy(text) }, "Copy"));

const ACCOUNT = (url, chat, embed) => `Provider:         Custom / OpenAI-compatible
Name:             LLM gateway
Base URL:         ${url}/v1
API key:          lg_...   (this app's gateway key, from Clients)
Chat model:       ${chat}
Embedding model:  ${embed}   (TrustDesk only)`;

const CURL = (url, chat, embed) => `export GATEWAY=${url}
export GATEWAY_KEY=lg_...

curl -s "$GATEWAY/v1/models" -H "Authorization: Bearer $GATEWAY_KEY"

curl -s "$GATEWAY/v1/chat/completions" -H "Authorization: Bearer $GATEWAY_KEY" -H "Content-Type: application/json" \\
  -d '{"model": "${chat}", "messages": [{"role": "user", "content": "Say hello"}], "max_tokens": 50}'

curl -s "$GATEWAY/v1/embeddings" -H "Authorization: Bearer $GATEWAY_KEY" -H "Content-Type: application/json" \\
  -d '{"model": "${embed}", "input": ["first text", "second text"]}'`;

export async function initConnect() {
  const box = $("view-connect");
  const url = location.origin;
  let chat = "echo-chat", embed = "echo-embed";
  try {
    const m = await api("/api/models");
    chat = m.items.find((a) => a.kind === "chat" && a.enabled && !a.alias.startsWith("echo"))?.alias || chat;
    embed = m.items.find((a) => a.kind === "embedding" && a.enabled && !a.alias.startsWith("echo"))?.alias || embed;
  } catch { /* the defaults are fine */ }
  fill(box,
    h("h1", {}, "Connect"),
    h("p", { class: "muted" }, "The gateway speaks the OpenAI API (chat completions, embeddings and the model list; no streaming). An app needs no code change: it adds an ordinary OpenAI-compatible account that points here, with its own gateway key. Create the client and its key under Clients first."),
    h("h2", { class: "section-title" }, "TrustDesk, PagerLens and PayBroker"),
    h("p", { class: "muted" }, "In the app: Settings, AI accounts, Add an account. The model fields take a model alias from the Models page, not a provider's model name."),
    code(ACCOUNT(url, chat, embed)),
    h("p", { class: "muted" }, "The base URL must be https and reachable from the app's server. For a local http gateway the app needs its allow-private-URLs switch (TRUSTDESK_ALLOW_PRIVATE_LLM_URLS, PAGERLENS_ALLOW_PRIVATE_LLM_URLS, PAYBROKER_ALLOW_PRIVATE_URLS). TrustDesk's embedding dimension must match the alias (the built-in echo-embed returns 768)."),
    h("h2", { class: "section-title" }, "Try it with curl"),
    code(CURL(url, chat, embed)),
    h("p", { class: "muted" }, "Errors come back OpenAI-shaped: ", h("code", {}, '{"error": {"message", "type", "code"}}'), ". 401 bad or revoked key; 403 model not allowed for this client; 404 unknown model alias; 429 rate_limit_exceeded or budget_exceeded (with Retry-After); 502 every target failed; 503 provider keys locked or the database is waking."),
    h("h2", { class: "section-title" }, "Services with a token from the identity provider"),
    h("p", { class: "muted" }, "A service can send its identity-provider access token instead of a key: scope llmgw.use, and the token's client id must equal the name of a registered client."),
    h("p", { class: "muted" }, "Prometheus: scrape ", h("code", {}, `${url}/metrics`), " with a metrics-only token. The shared families plus llmgw_requests_total, llmgw_tokens_total, llmgw_upstream_latency_seconds, llmgw_budget_rejections_total and llmgw_failovers_total are exposed."));
}
