<p align="center"><img src="src/llmgw/static/logo.svg" alt="LLM gateway logo" width="96" height="96"></p>

<h1 align="center">LLM gateway</h1>

The ecosystem's **shared LLM gateway**: one OpenAI-compatible endpoint in front of your model providers, on **Postgres**.
Provider API keys live in this one place. Each app (TrustDesk, PagerLens, PayBroker, anything that speaks the OpenAI API) gets
its **own revocable, budgeted, audited gateway key** and configures an ordinary "OpenAI-compatible account" whose base URL is
`https://<gateway>/v1` and whose API key is that gateway key. **Apps need no code change.**

You get one place to rotate a provider key, cap spend, see usage per app, and swap models (for example move every app from one
model to another by editing an alias). Built on [agentpg-kit](https://github.com/elchinoo/agentpg-kit) and the ecosystem
[identity provider](https://github.com/elchinoo/identity).

## What it does

- **OpenAI-compatible data plane** (`/v1`, see [API.md](API.md)): `POST /v1/chat/completions`, `POST /v1/embeddings`,
  `GET /v1/models`. A client sends its key as `Authorization: Bearer lg_...`; a service can instead send an identity-provider
  access token with scope `llmgw.use` whose client id matches a registered client.
- **Model aliases with failover.** Apps ask for an *alias* (`chat-default`); an admin maps it to an ordered list of targets
  (provider account + upstream model). The gateway tries them in order and moves on to the next on a 5xx, a 429, a timeout or a
  connection error, never on any other 4xx.
- **Clients** with the aliases they may use (or `*`), a requests-per-minute limit, a monthly token budget (`hard`: refuse with
  429; `soft`: allow, flag it in usage and show a dashboard banner), several keys each, and key rotation with an optional grace
  period. Keys are shown once and stored as SHA-256 hashes.
- **Metering.** One usage row per request: client, key, alias, the provider and upstream model that answered, prompt,
  completion and total tokens, estimated cost, latency, HTTP status, error class, failover attempts. **Prompts, completions and
  embedding inputs and outputs are never stored.**
- **Providers** with the API key encrypted by the vault, a URL checked by the SSRF guard on save *and on every call*, and a
  Test button that makes one tiny call. A built-in `echo` provider needs no network and no key (a deterministic reply, plausible
  `usage`, and a hash-derived unit-vector embedding of configurable size, 768 by default), so demos and tests need no real
  provider. The first start seeds one `echo` provider, the demo aliases `echo-chat` and `echo-embed`, and the **default aliases
  `chat-default` and `embed-default`** (both on echo, embeddings of 768 numbers) that the whole fleet is wired to (see below).
- **Dashboard** at `/`: Overview (usage per day, per client, spend against budget, top models, errors), Clients, Models,
  Providers, Audit (who changed what, never secrets), Settings, and Connect (copy-paste setup for each app, plus curl).
- **Prometheus** at `/metrics`: the fleet's shared families plus `llmgw_requests_total{client,alias,status}`,
  `llmgw_tokens_total{client,alias,kind}`, `llmgw_upstream_latency_seconds{provider}`, `llmgw_budget_rejections_total{client}`
  and `llmgw_failovers_total`.
- **Sign-in:** standalone (a setup code and an admin password) or **single sign-on** with the identity provider, with roles.

## Quick start

Requires Docker and [uv](https://docs.astral.sh/uv/) (or any Python 3.11+ environment).

```bash
cp .env.example .env              # then set POSTGRES_PASSWORD; .env is git-ignored
docker compose up -d db           # plain postgres:17 on 127.0.0.1:5437
uv venv && uv pip install -e ".[dev]"      # uses the sibling ../agentpg-kit checkout; images use the published kit
set -a; . ./.env; set +a
.venv/bin/llmgw migrate
.venv/bin/llmgw serve             # http://localhost:8000
```

The first start prints a **one-time setup code** to the console. Open http://localhost:8000, enter it and choose an admin
password. Then **Clients > New client** gives you a key, and the built-in echo models work immediately:

```bash
curl -s localhost:8000/v1/chat/completions -H "Authorization: Bearer lg_..." -H "Content-Type: application/json" \
  -d '{"model": "echo-chat", "messages": [{"role": "user", "content": "hello"}]}'
```

Everything in containers: `docker compose up --build`, then read the setup code from `docker compose logs api`.

To use a real provider: **Providers > Add a provider** (base URL and key; the key needs the admin password to have been set, or
`LLMGW_ENCRYPTION_KEY`), **Models > New model alias** pointing at it, then allow that alias for a client.

## Point the fleet at it

In each app open **Settings > AI accounts > Add an account** and fill in the same few fields. Create one client per app first
(name it after the app, for example `trustdesk`) so each app has its own key, limits and usage line.

| Field | Value |
|---|---|
| Provider | Custom / OpenAI-compatible |
| Name | `LLM gateway` |
| Base URL | `https://<gateway>/v1` |
| API key | the app's gateway key (`lg_...`) |
| Chat model | an alias, for example `chat-default` (or `echo-chat` to try it) |
| Embedding model | an alias, for example `embed-default` (TrustDesk only; `echo-embed` to try it) |
| Embedding dimensions | must match what the alias returns (768 for `echo-embed`) |

- **TrustDesk**: AI accounts take a chat model, an embedding model and an embedding dimension.
- **PagerLens** and **PayBroker**: AI accounts take a chat model.
- The base URL must be **https** and reachable from the app's server. For a gateway on `http://` or a private address the app
  needs its allow-private-URLs switch (`TRUSTDESK_ALLOW_PRIVATE_LLM_URLS`, `PAGERLENS_ALLOW_PRIVATE_LLM_URLS`,
  `PAYBROKER_ALLOW_PRIVATE_URLS`).
- The **model name is an alias** from this gateway, not a provider's own model name. To change the model later, edit the alias.

The Connect page shows the same snippets with your URL filled in.

## Default aliases: the one switch for every app

`chat-default` (chat) and `embed-default` (embeddings, 768 dimensions, what TrustDesk and PagerLens expect) exist from the first start,
on the built-in echo provider, so an app wired to them works immediately. To move **every app** to a real model, add the provider
and **repoint the alias** (Models > `chat-default` > change its target, or `PUT /api/models/chat-default`); no app, client or key
changes. Seeding is idempotent and never destructive: a gateway that predates the defaults gets them added once at its next start,
an alias that already exists is left exactly as an operator made it, and an alias or echo provider you delete is not recreated.
`ecoctl apply` (see the ecosystem repository) uses the static `LLMGW_API_KEY` to create one client per app and wires the apps to
these two aliases with no manual step.

## Roles and single sign-on

Standalone, the first-run admin has everything. Federated with the identity provider (`LLMGW_OIDC_ISSUER`, client id `llmgw`,
redirect `https://<gateway>/auth/callback`):

| Role | May |
|---|---|
| `admin` | everything |
| `operator` | read usage, configuration and the audit trail; manage clients (limits, budgets, keys) and model aliases. **Not** provider keys, settings, operator tokens or unlocking the vault |
| `viewer` | read usage and configuration (never keys or secrets); not the audit trail |

| Permission | Meaning |
|---|---|
| `usage:read`, `config:read`, `audit:read` | see usage and the overview; see providers, aliases and clients; read the audit trail |
| `clients:manage`, `models:manage` | clients, limits, budgets and keys; aliases and targets |
| `providers:manage` | add providers, store their keys, test them |
| `settings:manage` | defaults, operator tokens, admin password, unlock the vault |
| `metrics:scrape` | `/metrics` |
| `llm:use` | the data plane |

Service scopes (client credentials): `llmgw.use` (the data plane, for the registered client whose name equals the token's client
id), `llmgw.read`, `llmgw.metrics`. Local operator tokens have scope `read`, `metrics` or `full`.

**Two kinds of credential never cross.** *Client keys* (`lg_...`) and `llmgw.use` service tokens reach only `/v1`. *Operator
credentials* (sessions, `lgt_` tokens, the static key, people's tokens) are refused on `/v1`, even `*` ones: an operator who wants
to call a model uses a client key.

## Configuration

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Postgres connection string (`DB_EXTERNAL_DSN` and the `PG*` variables also work). |
| `LLMGW_PUBLIC_URL` | Base URL used by the SSO callback (default: the request's own address). |
| `LLMGW_ENCRYPTION_KEY[_FILE]` | Platform secret that lets the vault unlock itself after a restart (real providers need it) and lets an SSO-only deployment hold provider keys. |
| `LLMGW_API_KEY[_FILE]` | Optional static full-access operator key. Prefer operator tokens. It is never accepted on `/v1`. |
| `LLMGW_DEFAULT_CHAT_ALIAS`, `LLMGW_DEFAULT_EMBED_ALIAS`, `LLMGW_DEFAULT_EMBED_DIM` | Names of the default aliases seeded on echo (defaults `chat-default`, `embed-default`) and the size of the echo embedding (default: the Settings default, 768). Read at seeding time only. |
| `LLMGW_ALLOW_PRIVATE_URLS` | Allow provider URLs on private networks (a local model server; local testing only). |
| `LLMGW_UPSTREAM_TIMEOUT` | Seconds before a provider call times out (default 60, clamped to 1-120). |
| `LLMGW_MAX_CONCURRENT` | Requests served at once (default 64); each in-flight call holds one worker thread. |
| `LLMGW_POOL_MAX` | Size of the database connection pool used by the data plane (default 20). |
| `LLMGW_OIDC_ISSUER`, `LLMGW_OIDC_CLIENT_ID`, `LLMGW_OIDC_CLIENT_SECRET[_FILE]`, `LLMGW_LOCAL_LOGIN` | Single sign-on. Off unless the issuer is set. `LLMGW_LOCAL_LOGIN=false` removes the admin-password login. |

Defaults you can change in the UI (Settings): requests per minute for a new client (60), embedding size for a new alias (768),
how long to keep usage rows (forever by default; at least 32 days when set).

## Deploy on AgentPG

```bash
agentpg sandbox create llm-gateway --image ghcr.io/elchinoo/llm-gateway:latest --instance <instance-id> --port 8000 \
  --env LLMGW_PUBLIC_URL=https://<sandbox-url>
agentpg sandbox logs <sandbox-id>        # the one-time setup code
```

No Postgres extension is needed. A sandbox that pauses itself answers an empty `503`: `agentpg sandbox resume <sandbox-id>`. After
a restart the vault is locked until you sign in (or `LLMGW_ENCRYPTION_KEY` is set), and while it is locked **real providers answer
503 `vault_locked` but the built-in echo models keep working**. AgentPG sandboxes speak HTTP and Postgres only, which is all this needs.

## Limits worth knowing (read these)

- **No streaming.** `stream: true` is a 400. The fleet's apps never stream, so the gateway reads the whole answer, caps it
  (8 MB), and meters it exactly. `stream: false` is accepted.
- **A small, fixed field allow-list.** Only `model, messages, max_tokens, temperature, top_p, stop, response_format, seed,
  presence_penalty, frequency_penalty, n` (1 only), and for embeddings `input, dimensions, encoding_format` are forwarded.
  Anything else (tools, `user`, provider-specific fields) is a 400. Messages carry text only.
- **The requests-per-minute limit is in memory and per process.** With several replicas a client can reach N times its limit;
  put a proxy limit in front if that matters. Failed-key attempts are limited the same way (30 per minute per address).
- **Token counts are the provider's own `usage` when it sends one.** Otherwise they are **estimates** (characters divided by 4,
  rounded up) and the usage row is marked `estimated`.
- **Prices are entered by an admin** and cost figures are estimates built from them and those token counts; they are not an
  invoice. A call that fails without returning tokens costs nothing.
- **The monthly budget is checked before a call** against the usage rows of the current UTC month, so requests already in
  flight when it is crossed can still finish and slightly overshoot. A soft budget never blocks.
- Failover moves to the next target on 5xx, 429, timeouts, connection errors and malformed answers. It never retries one target:
  the next target is the retry.
- One request is one threadpool worker for as long as the provider takes (bounded by the timeout and `LLMGW_MAX_CONCURRENT`).

## Development

```bash
make lint
make test                                     # no database needed: validation, token maths, echo provider, rate window
make test-db                                  # the whole gateway against Postgres (needs POSTGRES_PASSWORD exported)
```

See [SECURITY.md](SECURITY.md) for the security model and its limits, and [API.md](API.md) for every endpoint.
