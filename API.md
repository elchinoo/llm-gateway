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
`presence_penalty`, `frequency_penalty`, `n` (only 1). `stream: true` is `400 stream_not_supported` (`false` is accepted).
Each message is `{role: system|user|assistant|developer, content: string | [{type: "text", text}], name?}`. Returns the
provider's chat completion with `model` set to the alias and the provider's `usage`.

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
| 400 | `unknown_field`, `stream_not_supported`, `invalid_json`, `invalid_messages`, `invalid_<field>`, `wrong_model_kind`, `upstream_rejected` | the request is wrong (`upstream_rejected` passes a provider's 400, 413 or 422 on, redacted, without failing over) |
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

### What is metered
One usage row per chat or embeddings request (including refusals once the client is known). Tokens are the provider's `usage`; if it
is missing or not sane they are estimated as characters / 4 and the row is marked `estimated`. A request that fails without
returning tokens counts zero tokens and no cost. Prompts and answers are never stored.

## Control plane (`/api`)

Permissions: `usage:read`, `config:read`, `audit:read`, `clients:manage`, `models:manage`, `providers:manage`, `settings:manage`,
`metrics:scrape`, `llm:use`. Local token scopes: `read` (`usage:read`, `config:read`), `metrics`, `full`. Service scopes:
`llmgw.use`, `llmgw.read`, `llmgw.metrics`. Browser sessions send a custom `X-LLMGW-CSRF` header on writes; bearer-token calls
don't need it. Bodies are JSON and unknown fields are rejected (422); bodies over 64 KB are refused (413).

| Call | Needs | Notes |
|---|---|---|
| `GET /api/session` | none | what the UI should show; the caller's roles and permissions |
| `GET /api/overview?days=` | `usage:read` | totals, per day, per client with budget use this month, top models, errors, budget alerts |
| `GET /api/usage?group=client\|alias\|provider\|day&days=&client=&alias=` | `usage:read` | requests, errors, tokens, estimated requests, cost, average and p95 latency |
| `GET /api/usage.csv?...` | `usage:read` | the same as CSV |
| `GET /api/usage/recent?limit=&errors_only=` | `usage:read` | the latest metering rows (metadata only) |
| `GET /api/providers` | `config:read` | keys are never returned (`has_key`, `api_key_last4`) |
| `POST /api/providers` `{name, kind: openai\|echo, base_url, api_key, enabled}` | `providers:manage` | the URL is checked by the SSRF guard; the key needs the vault |
| `PUT /api/providers/{id}` `{name, base_url, api_key?, clear_key, enabled}`, `DELETE /api/providers/{id}` | `providers:manage` | delete is `409` while an alias uses it |
| `POST /api/providers/{id}/test` `{model}` | `providers:manage` | one tiny call; not metered |
| `GET /api/models` | `config:read` | aliases with their ordered targets |
| `PUT /api/models/{alias}` `{kind: chat\|embedding, description, enabled, embedding_dim, price_prompt_per_m, price_completion_per_m, targets: [{provider_id, upstream_model}]}`, `DELETE /api/models/{alias}` | `models:manage` | 1 to 5 targets; prices are USD per 1M tokens; `embedding_dim` applies to the echo provider |
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
| `GET /metrics` | `metrics:scrape` | Prometheus |
| `GET /healthz`, `GET /readyz` | none | liveness; readiness (503 while the database wakes) |
