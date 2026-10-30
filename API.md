# LLM gateway API

Two APIs share one server. The **data plane** (`/v1`) is OpenAI-compatible and takes **client keys**. The **control plane**
(`/api`) is for the dashboard, scripts and agents and takes sessions, operator tokens or single-sign-on tokens. A credential of
one kind is refused on the other.

## Data plane (`/v1`)

Send `Authorization: Bearer <credential>` where the credential is

| Credential | Notes |
|---|---|
| a client key `lg_...` | issued under Clients, shown once, revocable |
| an identity-provider access token with scope `llmgw.use` | the token's client id (its `sub`) must equal the name of a registered client, else `403 client_not_registered` |

Operator sessions, `lgt_` tokens, the static key and people's tokens are `403 wrong_credential` here, whatever their permissions.

Bodies are JSON, at most **512 KB** (`413 request_too_large`). Requests are **never streamed**. Unknown fields are `400 unknown_field`.
`model` is a **model alias** configured by an admin; `GET /v1/models` lists the ones your client may use.

### `POST /v1/chat/completions`
Allowed fields: `model`, `messages`, `max_tokens`, `temperature`, `top_p`, `stop`, `response_format`, `seed`,
`presence_penalty`, `frequency_penalty`, `n` (only 1), and the tool-calling fields `tools`, `tool_choice`, `parallel_tool_calls`.
`stream: true` is `400 stream_not_supported` (`false` is accepted).
Each message is `{role: system|user|assistant|developer, content: string | [{type: "text", text}], name?}`, plus the two tool shapes
below. Returns the provider's chat completion with `model` set to the alias and the provider's `usage`.

**Tool calling** (OpenAI function calling, no streaming). Everything below is forwarded to the provider unchanged and the provider's
answer comes back unchanged, so `choices[0].message.tool_calls` and `finish_reason: "tool_calls"` arrive as the provider sent them.
- `tools`: 1 to 64 entries, each exactly `{type: "function", function: {name, description?, parameters?, strict?}}`. `name` is 1 to 64
  letters, digits, `_` or `-` and unique; `description` at most 4096 characters; `parameters` a JSON-schema object (`type` `object`)
  nested at most 8 levels; one tool at most 16 KB and all tools at most 128 KB serialized. An empty list is refused. Other tool
  types (retrieval, custom, ...) and the legacy `functions` / `function_call` fields are refused.
- `tool_choice`: `"none"`, `"auto"`, `"required"` or `{type: "function", function: {name}}` naming a tool that is in `tools`. Anything
  except `"none"` needs `tools`.
- `parallel_tool_calls`: a boolean; needs `tools`.
- An assistant message may carry `tool_calls: [{id, type: "function", function: {name, arguments}}]` (1 to 64; `arguments` is the
  provider's JSON text, a string of at most 64 KB; `id` a unique printable string of at most 128 characters) and then `content` may be
  `null` or absent. Keys other than `id`, `type`, `function` (e.g. `index`) in a tool call, and other than `name`, `arguments` inside `function`, are dropped, not forwarded (id, type, function, name and arguments are required). `tool_calls: []` is treated as none. Only assistant messages carry `tool_calls`.
- A `role: "tool"` message is `{role: "tool", tool_call_id, content: string}`, and its `tool_call_id` must answer a tool call of the
  assistant message before it, once. Anything else on these shapes is `400 invalid_messages`; bad `tools`, `tool_choice` or
  `parallel_tool_calls` are `400 invalid_tools`, `invalid_tool_choice`, `invalid_parallel_tool_calls`.
- Metering: when the provider sends no `usage`, the estimate counts the message text, the tool-call names and arguments and the
  serialized tool schemas as prompt characters, and the returned tool-call names and arguments as completion characters. The limits
  (512 KB body, rate, budget, allowed aliases) apply as for any chat request.
- A `message.content` of `null` next to `tool_calls` is a normal answer, never the `x-llmgw-note` reasoning-only case.
- Whether the model supports tools is the provider's business: a provider that does not will answer 400, passed on as
  `400 upstream_rejected`.
- **Echo provider.** A request with `tools` is answered in plain text (`Echo from <model>: <last user message>`), never with a tool call,
  so demos do not crash. For testing a tool round trip only: if the last user message is `call:<tool> {json}` (the arguments are
  optional, default `{}`), that tool is offered, `tool_choice` is not `"none"` and no tool call has happened since that message, the
  reply is one tool call with `finish_reason: "tool_calls"`, `content: null`, `id` `call_echo_<12 hex>` and the arguments as compact
  JSON. After the tool result it answers in text, so the loop ends. A tool that was not offered, arguments that are not a JSON object,
  or any other text give the plain reply; `required` and a named `tool_choice` do not force a call.

### `POST /v1/embeddings`
Allowed fields: `model`, `input` (a string, or a list of up to 256 strings), `dimensions`, `encoding_format` (`float` or `base64`).
Returns `{object: "list", data: [{object: "embedding", index, embedding}], model, usage}`. The built-in `echo-embed` and `embed-default` return
unit vectors of the alias's configured size (768 by default) derived from a hash of the input.

### `GET /v1/models`, `GET /v1/models/{alias}`
`{object: "list", data: [{id, object: "model", created, owned_by, kind}]}`: only the aliases this client may use.

### Errors
Always `{"error": {"message", "type", "code"}}`:

| Status | `code` | When |
|---|---|---|
| 400 | `unknown_field`, `stream_not_supported`, `invalid_json`, `invalid_messages`, `invalid_tools`, `invalid_tool_choice`, `invalid_parallel_tool_calls`, `invalid_<field>`, `wrong_model_kind`, `upstream_rejected` | the request is wrong (`upstream_rejected` passes a provider's 400, 413 or 422 on, redacted, without failing over) |
| 401 | `missing_api_key`, `invalid_api_key` | no key, or a wrong or revoked one |
| 403 | `wrong_credential`, `client_not_registered`, `client_disabled`, `model_not_allowed` | not a data-plane credential; the client may not do that |
| 404 | `model_not_found` | no such alias (or it is disabled) |
| 413 | `request_too_large` | over 512 KB |
| 429 | `rate_limit_exceeded` | this client's requests-per-minute limit; `Retry-After` seconds |
| 429 | `budget_exceeded` | hard monthly token budget used up; `Retry-After` is the seconds to the next UTC month |
| 429 | `upstream_rate_limited` | every target answered 429; `Retry-After` when the provider sent one |
| 429 | `too_many_failures` | too many invalid keys from this address |
| 502 | `upstream_unavailable`, `upstream_misconfigured` | every target failed (5xx, timeout, connection, bad answer); or a provider refused our own key (401/403/404): an administrator must fix the provider account |
| 503 | `vault_locked`, `no_available_target`, `service_unavailable` | provider keys are locked (echo still works); every target is disabled; the database is waking up (`Retry-After`) |

Failover: targets are tried in order; the next one is used after a 5xx, a 429, a timeout, a connection error or a malformed answer,
and never after another 4xx.

### Thinking models
A reasoning model can spend a small `max_tokens` on thinking and return an empty `content`. Operators fix that per alias target with
`extra_body` (see below and the README's "Thinking models"). If a 200 answer still has an empty or null `message.content` while
`reasoning` or `reasoning_content` is present, it is returned unchanged with the response header
`x-llmgw-note: empty-content-reasoning-only`; check `finish_reason: "length"` and raise `max_tokens`, or ask the operator to turn
thinking off for that alias.

### What is metered
One usage row per chat or embeddings request (including refusals once the client is known). Tokens are the provider's `usage`; if it
is missing or not sane they are estimated as characters / 4 and the row is marked `estimated`. A request that fails without
returning tokens counts zero tokens and no cost. Prompts, tool schemas, tool arguments, tool results and answers are never stored. When the provider reports `usage.completion_tokens_details.reasoning_tokens` it is kept as
`reasoning_tokens` (part of the completion tokens, null when not reported).

## Control plane (`/api`)

Permissions: `usage:read`, `config:read`, `audit:read`, `clients:manage`, `models:manage`, `providers:manage`, `settings:manage`,
`metrics:scrape`, `llm:use`. Local token scopes: `read` (`usage:read`, `config:read`), `metrics`, `full`. Service scopes:
`llmgw.use`, `llmgw.read`, `llmgw.metrics`. Browser sessions send a custom `X-LLMGW-CSRF` header on writes; bearer-token calls
don't need it. Bodies are JSON and unknown fields are rejected (422); bodies over 64 KB are refused (413).

| Call | Needs | Notes |
|---|---|---|
| `GET /api/session` | none | what the UI should show; the caller's roles and permissions |
| `GET /api/overview?days=` | `usage:read` | totals, per day, per client with budget use this month, top models, errors, budget alerts |
| `GET /api/usage?group=client\|alias\|provider\|day&days=&client=&alias=` | `usage:read` | requests, errors, tokens (including `reasoning_tokens`, the thinking part of the completion tokens), estimated requests, cost, average and p95 latency |
| `GET /api/usage.csv?...` | `usage:read` | the same as CSV |
| `GET /api/usage/recent?limit=&errors_only=` | `usage:read` | the latest metering rows (metadata only) |
| `GET /api/providers` | `config:read` | keys are never returned (`has_key`, `api_key_last4`) |
| `POST /api/providers` `{name, kind: openai\|echo, base_url, api_key, enabled}` | `providers:manage` | the URL is checked by the SSRF guard; the key needs the vault |
| `PUT /api/providers/{id}` `{name, base_url, api_key?, clear_key, enabled}`, `DELETE /api/providers/{id}` | `providers:manage` | delete is `409` while an alias uses it. A stored key is bound to its URL origin: changing scheme, host, port or path needs `api_key` in the same request (or `clear_key`), else `422` |
| `POST /api/providers/{id}/test` `{model}` | `providers:manage` | one tiny call; not metered |
| `GET /api/models` | `config:read` | aliases with their ordered targets |
| `PUT /api/models/{alias}` `{kind: chat\|embedding, description, enabled, embedding_dim, price_prompt_per_m, price_completion_per_m, targets: [{provider_id, upstream_model, extra_body?}]}`, `DELETE /api/models/{alias}` | `models:manage` | 1 to 5 targets; prices are USD per 1M tokens; `embedding_dim` applies to the echo provider. `extra_body` (optional JSON object, default `{}`, returned by `GET`) is merged into the upstream request after the client's fields and wins over them, e.g. `{"reasoning": {"enabled": false}}`; the keys `model, messages, input, stream, user, n` are refused (422), as are non-objects, more than 4 KB and more than 4 levels of nesting. The echo provider ignores it. Changes are audited |
| `GET /api/clients` | `config:read` | with this month's usage |
| `POST /api/clients` `{name, description, allowed_aliases, rpm_limit, unlimited_rpm, monthly_token_budget, budget_mode: hard\|soft, issue_key}` | `clients:manage` | returns the client and, by default, its first key (shown once) |
| `PATCH /api/clients/{id}`, `DELETE /api/clients/{id}` | `clients:manage` | `rpm_limit: null` is unlimited; `monthly_token_budget: null` removes the budget |
| `GET /api/clients/{id}/keys` | `config:read` | prefixes and dates only |
| `POST /api/clients/{id}/keys` `{name}` | `clients:manage` | returns the key once |
| `DELETE /api/clients/{id}/keys/{key_id}` | `clients:manage` | revoked at once |
| `POST /api/clients/{id}/keys/{key_id}/rotate` `{grace_minutes}` | `clients:manage` | issues a new key; the old one stops after the grace period (default 0) |
| `GET /api/audit?limit=&before=` | `audit:read` | who, what, when; never secrets |
| `GET/PUT /api/settings` | `settings:manage` | `default_rpm`, `default_embedding_dim`, `usage_retention_days` |
| `GET/POST/DELETE /api/tokens` | `settings:manage` | operator tokens (`lgt_`), shown once |
| `POST /api/admin-password`, `POST /api/vault/unlock` | `settings:manage` | |
| `POST /api/vault/rotate` `{password, confirm: true}` | `settings:manage` | replaces the vault data key and re-encrypts provider keys; needs the admin password when one exists (403 otherwise) and `confirm` (422); restart other replicas afterwards |
| `GET /metrics` | `metrics:scrape` | Prometheus |
| `GET /healthz`, `GET /readyz` | none | liveness; readiness (503 while the database wakes) |
