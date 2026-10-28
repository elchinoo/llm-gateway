# LLM gateway: complete guide

This is the long-form guide to the LLM gateway (`llmgw`, version 0.1.0). The [README](../README.md) is the short tour, [API.md](../API.md)
lists every endpoint and [SECURITY.md](../SECURITY.md) states the security model. This guide ties them together, adds how the gateway
fits the rest of the ecosystem, and says what was verified against the code and what was not (see [Known limitations](#16-known-limitations-and-unverified-points)).

Sections:

1. [Purpose and audience](#1-purpose-and-audience)
2. [How it fits the ecosystem](#2-how-it-fits-the-ecosystem)
3. [Architecture](#3-architecture)
4. [Install](#4-install)
5. [Configuration reference](#5-configuration-reference)
6. [First-run walkthrough](#6-first-run-walkthrough)
7. [Feature-by-feature usage](#7-feature-by-feature-usage)
8. [Roles and permissions](#8-roles-and-permissions)
9. [Security notes](#9-security-notes)
10. [Operations](#10-operations)
11. [Troubleshooting](#11-troubleshooting)
12. [Testing and development](#12-testing-and-development)
13. [AIDB and AgentPG integration](#13-aidb-and-agentpg-integration)
13b. [EDB and AgentPG capabilities used by this app](#13b-edb-and-agentpg-capabilities-used-by-this-app)
14. [Data model](#14-data-model)
15. [Command reference](#15-command-reference)
16. [Known limitations and unverified points](#16-known-limitations-and-unverified-points)

---

## 1. Purpose and audience

The gateway is one OpenAI-compatible endpoint (`/v1`) in front of your model providers, backed by Postgres. Provider API keys live in
one place, encrypted. Each application gets its own revocable, rate-limited, budgeted gateway key (`lg_...`) and is configured as an
ordinary "OpenAI-compatible account" whose base URL is `https://<gateway>/v1`. The applications need no code change.

What you get from putting it between your apps and your models:

- one place to rotate a provider key,
- a per-app request rate limit and monthly token budget,
- usage and estimated spend per app, per model alias, per provider and per day,
- failover between providers behind one alias, and
- the ability to move every app to another model by editing one alias.

It is for:

- **Operators and platform owners** who run several AI-using apps and want one door, one audit trail and one place for secrets.
- **App developers** who want to call chat or embedding models with an OpenAI client and not hold provider keys.
- **Demo and test authors**: the built-in `echo` provider needs no network and no key, so the whole fleet runs end to end without a real model.

It is deliberately small. It does not stream and does not forward images, and it forwards only function tool calls (API.md), and does not store prompts or completions
(see [Known limitations](#16-known-limitations-and-unverified-points)).

## 2. How it fits the ecosystem

The gateway is built on `agentpg-kit` (`apgkit`): the kit supplies the vault, the authenticator, single sign-on (relying party and
token verifier), the SSRF guard (`NetGuard`), the HTTP client used for providers (`LLMClient`), migrations, Prometheus plumbing, the
security-headers middleware and the shared UI scripts and stylesheet (`/kit/*`). `src/llmgw/core.py` is where those blocks are
instantiated and where the gateway's permissions are defined.

In `ecosystem/ecosystem.yaml` the gateway is the app `llmgw` (image `llm-gateway`, env prefix `LLMGW`, generated secret
`LLMGW_ENCRYPTION_KEY`, roles `admin`, `operator`, `viewer`, exposed scopes `llmgw.use`, `llmgw.read`, `llmgw.metrics`). Every app
that has `llm: true` in the manifest is a consumer.

```mermaid
flowchart LR
    subgraph consumers[Consumers]
      TD[TrustDesk]
      PL[PagerLens]
      PB[PayBroker]
      SF[ShopFront]
      SIM[CityPulse Simulator]
    end
    ID[identity<br/>OIDC provider]
    ECO[ecoctl]
    GW[LLM gateway<br/>/v1 data plane<br/>/api control plane]
    PRV[(OpenAI / OpenRouter /<br/>other OpenAI-compatible)]
    PGL[PagerLens agent<br/>scrapes /metrics]

    TD -- "lg_ key" --> GW
    PL -- "lg_ key" --> GW
    PB -- "lg_ key" --> GW
    SF -- "lg_ key" --> GW
    SIM -- "lg_ key, or service token llmgw.use" --> GW
    GW -- "provider key from the vault" --> PRV
    ID -. "SSO for people; JWKS for service tokens" .-> GW
    ECO -- "static operator key LLMGW_API_KEY:<br/>create clients, sync limits, test" --> GW
    PGL -- "metrics-scoped token" --> GW
```

### 2.1 Interaction with each project

| Project | How it talks to the gateway | Credential | Scope or permission | Configuration (verified where stated) |
|---|---|---|---|---|
| **TrustDesk** | Chat (AI pre-validation, ask) and embeddings (policy retrieval) through its AI account | `lg_` client key of the client `trustdesk` | `llm:use` (data plane only) | Reads `LLM_BASE_URL`, `LLM_API_KEY` (or `/etc/trustdesk/llm_api_key`) in `trustdesk/src/trustdesk/config.py`. `ecoctl` also sets `LLM_CHAT_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_DIM` |
| **PagerLens** | Chat (investigation) and embeddings (retrieval) | `lg_` key of client `pagerlens` | `llm:use` | Reads `LLM_BASE_URL`, `LLM_API_KEY` in `pagerlens/src/pagerlens/config.py`; the CLI can use the account `env` |
| **PayBroker** | Chat (AI triage of flagged payments) | `lg_` key of client `paybroker` | `llm:use` | Reads `LLM_BASE_URL`, `LLM_API_KEY` in `paybroker/src/paybroker/config.py` |
| **ShopFront** | Semantic search and the order-support assistant (per the manifest comment) | `lg_` key of client `shopfront` (the manifest sets `llm: true`) | `llm:use` | Wired by `ecoctl` with the same five variables. Its own source was not read for this guide |
| **CityPulse Simulator (cpsim)** | The `llmgw` workload kind sends batches of chat or embedding calls | Either a client key in `LLMGW_CLIENT_KEY`, or an identity-provider service token with scope `llmgw.use`. A static key wins over a minted token | `llm:use` / scope `llmgw.use`. With a service token, the token's subject (`cpsim-service`) must equal the name of a **registered, enabled client** in the gateway | Manifest: `calls: {llmgw: [llmgw.use]}`, so `ecoctl` injects `LLMGW_URL`, `CPSIM_SERVICE_CLIENT_ID/_SECRET` |
| **identity** | The gateway is a **relying party**: people sign in with authorization code + PKCE, the roles claim maps to permissions. Service tokens are verified locally against identity's JWKS. The gateway never calls identity except for sign-in and JWKS | OIDC client id `llmgw` (the audience of tokens) | Roles `admin`, `operator`, `viewer`; scopes `llmgw.use`, `llmgw.read`, `llmgw.metrics` | `LLMGW_OIDC_ISSUER`, `LLMGW_OIDC_CLIENT_ID`, `LLMGW_OIDC_CLIENT_SECRET[_FILE]`, `LLMGW_LOCAL_LOGIN`; redirect URI `https://<gateway>/auth/callback` |
| **ecoctl** | Provisions the gateway sandbox; then uses the static key `LLMGW_API_KEY` to call `/api/clients` (list, create, PATCH), `/api/clients/{id}/keys` (list, create, rotate, delete) and to run a tiny chat call per app (`ecoctl status`, `ecoctl llm test`) | Static operator key (never accepted on `/v1`; the test call uses the app's own `lg_` key) | Full operator (`*`) | Sets `LLMGW_API_KEY`, `LLMGW_ENCRYPTION_KEY` (generated), `LLMGW_DEFAULT_CHAT_ALIAS`, `LLMGW_DEFAULT_EMBED_ALIAS`, `LLMGW_DEFAULT_EMBED_DIM` (from `llm:` in the manifest) and the `LLMGW_OIDC_*` variables |
| **agentpg-kit** | Library: vault, auth, SSO, netguard, migrations, metrics, UI | n/a | n/a | Pinned in the image (`KIT_URL` build arg, v0.2.5); `agentpg-kit>=0.2.5` in `pyproject.toml` |
| **PagerLens agent** | Can scrape `/metrics` with a metrics-scoped token (`metrics:scrape`); the gateway exposes the kit's shared metric families so one preset reads every app | `lgt_` token with scope `metrics`, or an `llmgw.metrics` service token | `metrics:scrape` | Registered by `ecoctl` as an app with `calls`; the scrape configuration lives in that project and was not verified here |

What `ecoctl apply` does for each consumer (from `ecosystem/src/ecoctl/plan.py` and `llm_client.py`):

1. Creates the gateway with `LLMGW_API_KEY` (a generated static operator key kept in `ecosystem/.ecosystem/state.json`) and the default
   alias names.
2. Before each consumer exists, calls `POST /api/clients` with the app's name, the two default aliases, the rpm and the monthly budget
   and keeps the returned `lg_` key. Limits are applied only when the client is first created; `ecoctl llm sync` compares and, with
   `--apply`, changes limits and aliases with `PATCH /api/clients/{id}`.
3. Creates the app's sandbox with `LLM_BASE_URL=https://<gateway>/v1`, `LLM_API_KEY`, `LLM_CHAT_MODEL`, `EMBEDDING_MODEL`,
   `EMBEDDING_DIM`.
4. A lost state file means a lost key: the next `apply` rotates the active key (`grace_minutes: 0`) and reports it; recreate the app.

**URLs change when a sandbox is recreated.** Never copy a live URL into a document or a client. Apps get the gateway URL at creation;
`ecoctl recreate llmgw` recreates the apps that use it.

**Embedding size.** The apps read `EMBEDDING_DIM` and the gateway's echo embedding uses `LLMGW_DEFAULT_EMBED_DIM`; `ecoctl` sets both from
`llm.embed_dimensions` in `ecosystem.yaml`. 768 is the built-in echo default (the Settings default, `ecoctl`'s built-in default, and what the
README and the Connect page quote for `echo-embed`). The fleet manifest in the repository sets 1536, the size of the real model `text-embedding-3-small`
that `embed-default` is meant to point to. Both are correct for their case. The value must be identical in the app and behind the `embed-default` alias: with the
manifest's 1536, the echo placeholder also returns 1536 numbers; if you deploy a manifest that says 768, a real model behind the alias must also return 768.

## 3. Architecture

One FastAPI process (`llmgw.api:app`, served by uvicorn) holds two APIs and a static dashboard. Postgres holds everything durable.

```mermaid
flowchart TB
    subgraph proc[llmgw process]
      direction TB
      SG[SecurityGuard middleware<br/>CSP headers, CSRF header on /api,<br/>64 KB body cap on /api]
      subgraph dp["Data plane /v1"]
        DC[data_client<br/>lg_ key or llmgw.use token]
        GWY[gateway.handle<br/>validate, rate limit, alias,<br/>allow-list, budget]
        CT[call targets in order<br/>failover]
        PRVD[providers.call<br/>vault decrypt, NetGuard,<br/>extra_body merge]
        MET[usage.record + Prometheus]
      end
      subgraph cp["Control plane /api"]
        AUTH[Authenticator<br/>session, lgt_ token,<br/>static key, SSO JWT]
        ADM[api_admin routes<br/>providers, models, clients,<br/>usage, audit, settings]
      end
      UI[Dashboard static files<br/>+ /kit/* shared UI]
      HK[housekeeping thread<br/>hourly usage retention]
    end
    DB[(Postgres<br/>provider, model_alias, alias_target,<br/>client, client_key, usage_event,<br/>audit_event, app_setting, sessions)]
    UP[(Upstream providers)]

    SG --> DC --> GWY --> CT --> PRVD --> UP
    GWY --> MET --> DB
    SG --> AUTH --> ADM --> DB
    DC --> DB
    GWY --> DB
    HK --> DB
    UI -. served at / .-> SG
```

Request path of `POST /v1/chat/completions` (from `api.py` and `gateway.py`):

1. **Authenticate** (`data_client`): extract the bearer. A `lg_` key is looked up by SHA-256 hash on every request (no cache), with
   `last_used_at` refreshed at most once a minute. A service token must carry `llm:use`, must not hold `*`, and its subject must name a
   registered client. Anything else is `403 wrong_credential`. Unknown keys are counted per address (30 per minute, then `429 too_many_failures`).
2. **Read the body** with a 512 KB cap, enforced on the declared length and while streaming chunks.
3. **Client checks**: disabled client is `403 client_disabled`; per-client sliding 60 s request window (in memory, per process) is `429 rate_limit_exceeded`.
4. **Validate**: JSON object, `stream: true` refused, unknown fields refused, field ranges checked, text-only messages.
5. **Resolve the alias**: unknown or disabled is `404 model_not_found`; wrong kind for the endpoint is `400 wrong_model_kind`; client not
   allowed that alias is `403 model_not_allowed`.
6. **Budget**: sums `usage_event.total_tokens` for the current UTC month. At or over the budget: `hard` is `429 budget_exceeded` with
   `Retry-After` to the next month; `soft` lets it through and flags the row `over_budget`.
7. **Targets in order** (1 to 5, disabled targets skipped). For each: decrypt the provider key (or answer 503 `vault_locked`), merge the
   operator's `extra_body`, set `model` to the upstream model, call through the SSRF guard (no redirects, 8 MB cap, timeout
   `LLMGW_UPSTREAM_TIMEOUT`). Retry on the next target after: vault locked, unreachable, URL rejected, too large, bad answer, 429, 5xx.
   A 4xx other than 429 stops the loop: 400/413/422 pass through as `upstream_rejected`, other 4xx become `502 upstream_misconfigured`.
8. **Meter and answer**: the response `model` is rewritten to the alias; tokens come from the provider's `usage` or are estimated
   (characters / 4); one `usage_event` row is written (a failure to write is logged without content and never fails the request).

The control plane (`/api`) uses the kit's `Authenticator`: browser session cookie (`lg_session`, HttpOnly, 12 hours), local operator
tokens (`lgt_`, scope `read`, `metrics` or `full`), the static key, and verified identity tokens. Client keys never pass it.

Source layout:

| File | Responsibility |
|---|---|
| `core.py` | kit instances; `PERMISSIONS`, `ROLE_PERMISSIONS`, `SCOPE_PERMISSIONS` |
| `api.py` | app, lifespan (seeding, vault unlock, housekeeping), `/healthz`, `/readyz`, `/metrics`, `/v1`, setup, login, SSO, tokens |
| `api_admin.py` | `/api` routes for providers, models, clients, keys, usage, audit, settings |
| `gateway.py` | request validation, rate window, token accounting, failover loop |
| `providers.py` | provider CRUD, the single upstream call, the echo provider, the Test call |
| `aliases.py` | alias and target storage, `extra_body` validation, seeding of echo and default aliases |
| `clients.py` | clients, keys, key lookup (the `lg_` bearer resolver) |
| `usage.py` | metering, summaries, CSV, retention pruning |
| `settings.py` | the three instance defaults |
| `metrics.py` | the gateway's Prometheus families |
| `migrate.py`, `migrations/` | `001_init.sql`, `002_extra_body_reasoning.sql` (plus the kit's auth schema) |
| `static/` | the dashboard (vanilla JS modules, no inline script or style) |

## 4. Install

Python 3.11 or later is required (`requires-python >=3.11`); the container image uses Python 3.14. No Postgres extension is needed
(the table definitions use only core features); any Postgres 17 works, and CI uses `pgvector/pgvector:pg17`.

### 4.1 Local development with a throwaway Postgres

Requires Docker and [uv](https://docs.astral.sh/uv/). The `pyproject.toml` points `agentpg-kit` at the sibling checkout
`../agentpg-kit` for local work, so clone both side by side.

```bash
cd llm-gateway
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # paste the result as POSTGRES_PASSWORD in .env
docker compose up -d db                       # plain postgres:17 on 127.0.0.1:5437, volume pgdata
uv venv && uv pip install -e ".[dev]"
set -a; . ./.env; set +a
.venv/bin/llmgw migrate                       # applies SQL migrations and the kit's auth tables
.venv/bin/llmgw serve                         # http://localhost:8000
```

`.env` is git-ignored. `DATABASE_URL` in `.env.example` is `postgresql://llmgw:${POSTGRES_PASSWORD}@127.0.0.1:5437/llmgw`, which relies on
the shell expanding `${POSTGRES_PASSWORD}` when you source the file. To throw everything away: `docker compose down -v`
(removes the `pgdata` volume).

The first start prints a one-time setup code on stderr:

```
=== LLM gateway first-run setup ===
Open the app in your browser and enter this one-time code to create the admin password:
    <code>
(It changes every time the server restarts until setup is complete.)
```

### 4.2 Docker Compose (everything in containers)

```bash
cp .env.example .env     # set POSTGRES_PASSWORD
docker compose up --build
docker compose logs api  # the setup code
```

`docker-compose.yml` has two services: `db` (postgres:17, healthcheck `pg_isready`) and `api` (built from the `Dockerfile`, runs
`llmgw start`, publishes `127.0.0.1:8000`, waits for the database to be healthy). Compose passes `DATABASE_URL` to `api` itself.
Both ports bind to 127.0.0.1 only. This file is for local use; it sets no vault secret, so real provider keys need the admin password
to be set and are locked again after a container restart (see [Vault](#104-the-vault-and-restarts)).

`llmgw start` is the container entrypoint: it runs migrations (retrying the connection with backoff, so a waking database is
tolerated), then serves. If migration fails it prints the cause to stderr and exits 1.

The image (`Dockerfile`): `python:3.14-slim`, installs the pinned kit release then the app, runs as the non-root user `app`, exposes 8000,
sets `HOST=0.0.0.0`, `PORT=8000`, sets `SSL_CERT_FILE` to the system CA bundle, and has a `HEALTHCHECK` that requests `/healthz`.
CI (`.github/workflows/build-image.yml`) builds and publishes `ghcr.io/elchinoo/llm-gateway` on pushes to `main`, `v*` tags, nightly and
on manual dispatch, after a test job against a Postgres service.

### 4.3 Deploy on AgentPG with ecoctl

In the ecosystem the gateway is created for you. From `ecosystem/`:

```bash
.venv/bin/ecoctl validate
.venv/bin/ecoctl plan                         # every command and API call; changes nothing
.venv/bin/ecoctl apply --only identity,trustdesk,llmgw --execute   # example stage: gateway plus its first consumer
.venv/bin/ecoctl status                       # health of each app, each LLM client, one real call per client
.venv/bin/ecoctl llm test trustdesk           # one tiny chat call with that app's own key
```

`ecoctl` (see `ecosystem/docs/DEPLOY.md`, section 9) generates `LLMGW_API_KEY` and `LLMGW_ENCRYPTION_KEY`, registers the gateway in
identity (client id `llmgw`), creates the sandbox with the `LLMGW_OIDC_*` variables, creates one client per consuming app, and gives
each app its key. It needs the `agentpg` CLI to be logged in and creates databases and sandboxes on your account.

By hand, without `ecoctl`:

```bash
agentpg sandbox create llm-gateway --image ghcr.io/elchinoo/llm-gateway:latest --instance <instance-id> --port 8000 \
  --env LLMGW_PUBLIC_URL=https://<sandbox-url>
agentpg sandbox logs <sandbox-id>        # the one-time setup code
```

A sandbox that pauses itself answers an empty `503`: `agentpg sandbox resume <sandbox-id>`. AgentPG sandboxes speak HTTP and
Postgres only, which is all the gateway needs. When the database is scaled to zero, connections retry with backoff, `/readyz` answers
`503 waking-database`, and the data plane answers `503 service_unavailable` with `Retry-After: 5`.

## 5. Configuration reference

Every variable below was found in the gateway's code or in the kit code it calls. Secrets with a `[_FILE]` suffix can also be supplied as a
file: the value of `<NAME>_FILE` is a path whose first line is the value (kit `read_secret`).

### 5.1 Gateway variables

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | none | Postgres connection string. If unset, `DB_EXTERNAL_DSN`, then `PSQL_CONNECTION_STRING` are tried. If all are empty, libpq falls back to `PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` (which linked AgentPG sandboxes provide) |
| `LLMGW_PUBLIC_URL` | the request's own address | Base URL used for the SSO redirect and shown on the Connect page. Trailing slash removed. Set it behind a proxy or on a sandbox |
| `LLMGW_ENCRYPTION_KEY` / `_FILE` (file default `/etc/llmgw/encryption_key`) | empty | Platform secret that lets the vault unlock itself at startup and after every restart. Needed for real providers on an SSO-only deployment (no admin password to wrap the key) |
| `LLMGW_API_KEY` / `_FILE` (file default `/etc/llmgw/api_key`) | empty | Optional static full-access **operator** key. Used by `ecoctl`. Never accepted on `/v1`. Can sign in to a browser session through `POST /api/login {api_key}` |
| `LLMGW_DEFAULT_CHAT_ALIAS` | `chat-default` | Name of the default chat alias seeded on echo. Read at seeding time only; an invalid alias name falls back to the default |
| `LLMGW_DEFAULT_EMBED_ALIAS` | `embed-default` | Name of the default embedding alias. Same rules |
| `LLMGW_DEFAULT_EMBED_DIM` | the Settings default (768) | Size of the echo embedding of the default embedding alias; must be an integer 1 to 8192, otherwise ignored |
| `LLMGW_ALLOW_PRIVATE_URLS` | off | Allow provider URLs that are `http` or on private networks (local model server, local testing only). Link-local (cloud metadata) stays blocked (kit netguard) |
| `LLMGW_UPSTREAM_TIMEOUT` | `60` | Seconds before a provider call times out; clamped to 1 to 120; a non-number falls back to 60 |
| `LLMGW_MAX_CONCURRENT` | `64` | Size of the worker thread limiter: requests served at once. Each in-flight call holds one worker thread |
| `LLMGW_POOL_MAX` | `20` | Maximum size of the connection pool used by the data plane |
| `LLMGW_OIDC_ISSUER` | empty (SSO off) | The identity provider's URL. Setting it turns single sign-on and service-token verification on |
| `LLMGW_OIDC_CLIENT_ID` | `llmgw` | This app's client id at the provider, and the audience tokens must carry |
| `LLMGW_OIDC_CLIENT_SECRET` / `_FILE` | empty | The client secret. Empty means a public client (PKCE only) |
| `LLMGW_LOCAL_LOGIN` | `true` | When SSO is on, `false` removes the admin-password and static-key logins (break-glass off) |

Runtime (not prefixed): `HOST` (default `0.0.0.0`) and `PORT` (default `8000`) for uvicorn; `SSL_CERT_FILE` is set by the image.

`.env.example` also contains `POSTGRES_PASSWORD`, used only by `docker-compose.yml` for the local database.

### 5.2 Settings stored in the database (Settings page or `PUT /api/settings`)

| Setting | Default | Range | Meaning |
|---|---|---|---|
| `default_rpm` | 60 | 1 to 100,000 | Requests per minute given to a new client that does not choose one |
| `default_embedding_dim` | 768 | 1 to 8192 | Embedding size for a new embedding alias (and for the seeded default embedding alias unless `LLMGW_DEFAULT_EMBED_DIM` is set); applies to the echo provider |
| `usage_retention_days` | 0 (keep everything) | 0, or 32 to 3650 | Usage rows older than this are deleted once an hour. At least 32 days when set, because the monthly budget is computed from these rows |

### 5.3 Limits compiled into the code

| Limit | Value |
|---|---|
| Data-plane request body | 512 KB |
| `/api` request body | 64 KB (kit guard default) |
| Upstream response | 8 MB |
| Messages per chat request | 500 |
| Embedding inputs per request | 256 |
| `max_tokens` | 1 to 131,072 |
| Targets per alias | 1 to 5 |
| `extra_body` | plain JSON object, at most 4 KB, at most 4 levels deep; reserved keys `model, messages, input, stream, user, n` refused |
| Failed `/v1` keys per address | 30 per minute (in memory, per process) |
| Failed operator credentials per address | 10 per minute (kit limiter, in memory, per process) |
| Browser session lifetime | 12 hours |
| Admin password | 12 to 256 characters (SECURITY.md states 12 minimum; the 256 maximum is in the kit) |
| Client name | 1 to 40 characters, lower-case letters, digits, `.`, `_`, `-`; starts with a letter or digit |
| Alias name | 1 to 60 characters, letters, digits, `.`, `_`, `:`, `-` |
| Provider name | 1 to 60 characters, unique ignoring case |

## 6. First-run walkthrough

Assumes the local install of section 4.1 or 4.2, running on `http://localhost:8000`.

1. **Open the dashboard.** It shows the setup screen. Enter the one-time code from the console (or `docker compose logs api`) and choose
   an admin password of at least 12 characters. You are signed in. The password also protects the vault: provider keys are encrypted with a data key that is wrapped under it.
2. **Look at what was seeded.** On the very first start the gateway creates one `echo` provider, the demo aliases `echo-chat` and
   `echo-embed`, and the default aliases `chat-default` and `embed-default`, all on echo. Open **Providers** and **Models** to see them.
3. **Create a client for your first app.** **Clients > New client**: name it after the app (for example `demo-app`), leave the models on
   "All models" or pick some, keep or change the requests-per-minute value, optionally set a monthly token budget and its mode (hard or
   soft), then **Create client and issue a key**. The key (`lg_...`) is shown once; copy it.
4. **Call a model.**

   ```bash
   curl -s localhost:8000/v1/chat/completions -H "Authorization: Bearer lg_..." -H "Content-Type: application/json" \
     -d '{"model": "echo-chat", "messages": [{"role": "user", "content": "hello"}]}'
   ```

   The reply content is `Echo from echo-1: hello`. The `model` field in the response is the alias (`echo-chat`), not the upstream name.
5. **Embeddings.**

   ```bash
   curl -s localhost:8000/v1/embeddings -H "Authorization: Bearer lg_..." -H "Content-Type: application/json" \
     -d '{"model": "echo-embed", "input": ["first text", "second text"]}'
   ```

   Each embedding is a unit vector of 768 numbers derived from a hash of the text; the same text always gives the same vector.
6. **See the usage.** **Overview** shows the requests, tokens and the client's line. Prompts and answers are not stored.
7. **Add a real provider** (optional). **Providers > Add a provider**: choose a preset (OpenAI `https://api.openai.com/v1`, OpenRouter
   `https://openrouter.ai/api/v1`, or another OpenAI-compatible endpoint), paste the key, save, then **Test** with an upstream model
   name. Then **Models**: open `chat-default`, change its target to that provider and model, save. Every app wired to `chat-default`
   now uses it, with no change to any app, client or key.

## 7. Feature-by-feature usage

### 7.1 Dashboard

Served at `/`. The navigation shows only the tabs the signed-in person may use (the permission needed is in brackets):
Overview (`usage:read`), Clients, Models, Providers, Connect (`config:read`), Audit (`audit:read`), Settings (`settings:manage`). A
banner at the top shows the vault state; deep links are `#overview`, `#clients`, `#models`, `#providers`, `#audit`, `#settings`, `#connect`.

**Overview.** Totals for a period (7 days by default), requests per day with errors in red, top models (tokens per alias), per-client spend against the
monthly budget, a models table (requests, thinking tokens, estimated cost), and errors by class. A banner lists clients that
used up a hard budget or are over a soft one. With no traffic the page says so.

**Clients.** One row per app: this month's tokens and requests, budget used. **New client** takes name, description, models it may use
("All models" or specific aliases), requests per minute (or "No request limit"), monthly token budget and the budget mode. A client row opens a
detail window with limits (editable), an **Enabled** switch (a disabled client's keys are refused with 403), and the key table:
prefix, name, created, last used, status. Per key: **Rotate** (asks for grace minutes; the new key is shown once and the old one stops after the grace period) and **Revoke** (immediate). **Delete client** removes it and its keys; its usage history stays, unattributed
(the client reference becomes null, the name is kept).

**Models.** The aliases and their ordered targets. **New model alias** takes alias, description, kind (chat or embedding), embedding size
(echo only), prices in USD per million prompt and completion tokens, up to 5 targets (provider + upstream model, reorder with the
arrow, **Add a fallback target**), an **Enabled** switch, and per target **Extra request fields (JSON)** with three one-click presets
(see [Thinking models](#76-thinking-models-reasoning-per-target)). Bad JSON is caught in the browser before anything is sent.

**Providers.** **Add a provider** (preset, base URL, API key, enabled). Each row shows the key's last four characters only, how many aliases use it,
and buttons to edit, **Test**, delete. A provider cannot be deleted while an alias targets it. Editing lets you replace the key or tick "Remove the stored key".
The echo provider has no URL and no key. With tools in the request it answers in plain text and never calls a tool, so apps that always
send `tools` (PagerLens's chat, for one) work on echo. To test a tool round trip, make the last user message `call:<tool> {json}`: echo
then returns that tool call (`finish_reason: "tool_calls"`) once, and plain text after the tool result. Details in API.md.

**Audit.** Who changed what and when, with details (never keys or passwords); **Load more** pages backwards.

**Settings.** The three defaults; **Operator tokens** (create with scope `read`, `metrics` or `full`; shown once; revoke);
the admin password (set or change; changing signs every browser out and re-protects the stored provider keys).

**Connect.** Copy-paste setup for TrustDesk, PagerLens and PayBroker, curl examples with your URL filled in, a note on service tokens
and on Prometheus.

### 7.2 Data plane API

Details and error table: [API.md](API.md). Summary:

| Call | Notes |
|---|---|
| `POST /v1/chat/completions` | Allowed fields only: `model, messages, max_tokens, temperature, top_p, stop, response_format, seed, presence_penalty, frequency_penalty, n` (1 only). Messages are `{role: system\|user\|assistant\|developer, content: string or [{type: "text", text}], name?}`. `stream: true` is 400 |
| `POST /v1/embeddings` | `model, input` (string, or up to 256 strings), `dimensions` (1 to 8192), `encoding_format` (`float` or `base64`) |
| `GET /v1/models`, `GET /v1/models/{alias}` | The enabled aliases this client may use, each with `id, object, created, owned_by: "llmgw", kind` |

Use any OpenAI client by setting its base URL to `https://<gateway>/v1` and its API key to the `lg_` key; use an alias as the model name. Errors are
always `{"error": {"message", "type", "code"}}`.

### 7.3 Control plane API

[API.md](API.md) lists every route with the permission it needs. Browser sessions must send the header `X-LLMGW-CSRF` on writes; bearer
callers need not. Bodies are JSON, unknown fields are 422, `/api` bodies over 64 KB are 413. Example with an operator token:

```bash
curl -s -X POST "$GATEWAY/api/clients" -H "Authorization: Bearer lgt_..." -H "Content-Type: application/json" \
  -d '{"name":"demo-app","allowed_aliases":["chat-default","embed-default"],"rpm_limit":60,"monthly_token_budget":1000000,"budget_mode":"hard"}'
```

The response holds the client and its first key (`issue_key` defaults to true).

### 7.4 Clients, limits, budgets and key rotation

- A client's `allowed_aliases` is `["*"]` or a list of existing aliases (names are checked on save).
- `rpm_limit`: omitted means the instance default; `unlimited_rpm: true` on create (or `rpm_limit: null` on PATCH) means no limit.
- `monthly_token_budget`: the sum of `total_tokens` of the client's rows in the current **UTC month**. `null` removes the budget.
  `hard` refuses at or over the budget with 429 `budget_exceeded` and `Retry-After` to the start of next month; `soft` allows it, marks
  rows `over_budget` and shows a banner. The check happens before the call, so in-flight requests can overshoot.
- Rotation: `POST /api/clients/{id}/keys/{key_id}/rotate {"grace_minutes": n}` (0 to 10,080) issues a new key with the same name and sets the old key's revocation
  time to now plus the grace period. A rotation never extends an earlier revocation.
- A service token with scope `llmgw.use` is accepted for the client whose **name equals the token's subject**; that client's aliases, rpm and budget apply, but it has no `key_id` in usage rows.

### 7.5 Aliases, failover and prices

An alias maps a name to 1 to 5 ordered targets. Targets are tried in order; the next one is used after a 5xx, a 429, a timeout, a
connection error, a malformed answer, a rejected URL, an oversize response or a locked vault, and never after another 4xx. The same target is never retried. If every
target is disabled the answer is `503 no_available_target`. If a provider answers 401, 403 or 404 the client gets `502 upstream_misconfigured` and an administrator must fix
the provider account. Prices (USD per 1M tokens) only feed the cost estimate: `prompt/1e6 * price_prompt + completion/1e6 * price_completion`.

Repointing the default aliases is the one switch for the whole fleet: Models, open `chat-default`, change the target. Seeding is idempotent: an
alias that already exists is left as the operator made it, and an alias or echo provider you delete is not recreated.

### 7.6 Thinking models (reasoning, per target)

A reasoning model spends completion tokens on thinking, so a small `max_tokens` can come back with empty `content` and `finish_reason: "length"`. The fix is per alias target: **Extra request fields (JSON)**,
merged shallowly into the upstream body after the client's fields, so the operator wins. Presets on the Models page:

| Preset | `extra_body` |
|---|---|
| Turn thinking off: OpenRouter | `{"reasoning": {"enabled": false}}` |
| Turn thinking off: vLLM/SGLang (Qwen3) | `{"chat_template_kwargs": {"enable_thinking": false}}` |
| Limit reasoning: OpenAI style | `{"reasoning_effort": "low"}` |

Reserved keys, size and depth limits are in section 5.3. The echo provider ignores it. It is stored and audited as written, so never put a secret in it.
When the provider reports `usage.completion_tokens_details.reasoning_tokens` it is stored as `reasoning_tokens` (inside `completion_tokens`),
shown as "Thinking", included in `/api/usage`, the CSV, and `llmgw_tokens_total{kind="reasoning"}`. A 200 with empty or null `content` plus a `reasoning` or `reasoning_content` field passes through unchanged
with the header `x-llmgw-note: empty-content-reasoning-only` and increments `llmgw_empty_content_total{alias}`.

### 7.7 Usage, CSV and metrics

- `GET /api/usage?group=client|alias|provider|day&days=&client=&alias=` (days 1 to 366, default 30): requests, errors, prompt, completion,
  reasoning and total tokens, estimated requests, cost, average and p95 latency, over-budget requests.
- `GET /api/usage.csv` is the same as CSV (cells starting with `=`, `+`, `-`, `@` are prefixed with a quote).
- `GET /api/usage/recent?limit=&errors_only=` returns the latest rows (metadata only).
- `GET /metrics` (permission `metrics:scrape`): the kit's shared families (`http_requests_total`, `http_request_duration_seconds`, `db_up`, `app_info`, `process_*`)
  plus `llmgw_requests_total{client,alias,status}`, `llmgw_tokens_total{client,alias,kind}` (kind prompt, completion, reasoning),
  `llmgw_upstream_latency_seconds{provider}` (not observed for echo), `llmgw_budget_rejections_total{client}`, `llmgw_failovers_total`, `llmgw_empty_content_total{alias}`.
  Unknown model names sent by callers never become a label (the alias label is `-`).

### 7.8 MCP tools

The gateway has **no MCP server and exposes no MCP tools**. (A search of `src/` for MCP finds nothing.) Agents and scripts use the
control plane API with an operator token, and applications use the data plane. The ecosystem's MCP surfaces belong to other projects.

### 7.9 Database insights (dashboard: Database)

The **Database** tab (`GET /api/insights?order=total|mean|calls&limit=1..200`, permission `settings:manage`, i.e. admin only, because statement text shows table names and the
shape of the workload) mounts the kit's `initInsights` panel: the slowest and most called statements (`edb_stat_monitor`, with `pg_stat_statements` as a fallback),
`edb_pg_tuner` configuration recommendations and `query_advisor` index hints, all read from the gateway's own AgentPG database. Where an extension or a grant is missing
(plain Postgres) each part says "Not available on this database." with the reason and the route still answers `200`. `/metrics` also exports `db_insights_available{app="llmgw"}`,
`db_insights_top_query_mean_seconds{app,queryid}`, `db_insights_statement_calls` and `db_insights_tuner_recommendations` (cached 30 s), so PagerLens can show and alert on the
gateway's database. Not verified here: the EDB extensions are not installed on the local test Postgres, so only the degraded path ran (route, metrics, and a fake-DOM test of both
renderings); the populated path is covered by the kit's own tests.

### 7.10 What a read-only database user and AgentPG's restricted MCP can see

`src/llmgw/hardening.py` runs after every migration (`llmgw migrate` and `llmgw start`, through `migrate.run(after_migrate=...)`) and uses the kit's `apgkit.masking`
on the group role `a1p_readonly` (members: users from `agentpg db-users create --permission read`; the restricted MCP server of `agentpg mcp serve` runs as such a user). It does nothing where the role does not exist.

| Table | A read-only user gets |
|---|---|
| `provider` | `permission denied`; use `provider_masked`: id, name, kind, base URL, `has_key` (boolean), enabled, dates. **No ciphertext, no last four characters.** |
| `client_key` (SHA-256 of every client key) | `permission denied` |
| `app_setting` (settings, vault store: wrapped data key, admin password hash), `web_session`, `api_token`, `oidc_login` | `permission denied` |
| `client`, `model_alias`, `alias_target`, `usage_event`, `audit_event`, migrations | readable. The gateway holds no personal data: clients are applications, usage rows are metadata only (never a prompt or a completion). |

Limits: only the table owner can REVOKE (checked, with a logged warning if it did not take); default privileges give `a1p_readonly` SELECT on every NEW table, so a table added by a
later migration is readable until it is listed in `MASKED_VIEWS` or `DENIED_TABLES` (a test fails when a column named like a hash, secret, token or ciphertext is readable). The app's own owner connection,
`a1p_readwrite` members and the hosted MCP endpoint reached with `agentpg token` (a read-write session, see `ecosystem/docs/06`) are not restricted.

## 8. Roles and permissions

Standalone, the first-run admin holds everything (`*`). With single sign-on, roles come from the identity provider's `roles` claim, and only roles of this app count.

| Role | Permissions |
|---|---|
| `admin` | everything |
| `operator` | `usage:read`, `config:read`, `audit:read`, `clients:manage`, `models:manage` (not provider keys, settings, tokens, vault unlock) |
| `viewer` | `usage:read`, `config:read` |

| Permission | Allows |
|---|---|
| `usage:read` | overview, usage, CSV, recent rows |
| `config:read` | list providers, aliases, clients, keys (never secrets) |
| `audit:read` | the audit trail |
| `clients:manage` | create, change, delete clients; issue, rotate, revoke keys |
| `models:manage` | create, change, delete aliases |
| `providers:manage` | add, change, delete, test providers; store their keys |
| `settings:manage` | settings, operator tokens, admin password, unlock the vault |
| `metrics:scrape` | `/metrics` |
| `llm:use` | the data plane |

Scopes (service clients and local tokens): `read` = `usage:read` + `config:read`; `metrics` = `metrics:scrape`; `full` = `*`; `llmgw.use` = `llm:use`;
`llmgw.read` = read; `llmgw.metrics` = `metrics:scrape`.

A person who signs in with no role here is refused ("your account has no role in the LLM gateway"). Operator credentials (sessions, `lgt_` tokens, the static key,
people's tokens, anything holding `*`) are refused on `/v1`; client keys are refused on every `/api` route and `/metrics`.

## 9. Security notes

The full model is in [SECURITY.md](../SECURITY.md). The points an operator should remember:

- Provider keys are encrypted with a random data key that is stored only wrapped under the admin password and, if set, `LLMGW_ENCRYPTION_KEY`.
  A database dump alone reveals none. Keys are never returned (only the last four characters), logged, put in metrics, usage or audit rows; provider error text goes through `redact`.
- Provider URLs are checked when saved **and on every call**: `https` only (unless private URLs are allowed), no embedded credentials, no loopback, private, link-local or carrier-grade-NAT addresses, no redirects.
  The host name is resolved once for the check and again by the HTTP client; use egress rules if that matters.
- Client keys are 256-bit random values with prefix `lg_`, shown once, stored as SHA-256. The lookup is not cached, so revocation, disabling and limit changes apply on the next request in every process.
- No prompts, completions or embedding inputs or outputs are stored anywhere. They do pass through the gateway's memory and to the provider you chose.
- Only an allow-list of fields is forwarded; provider-specific fields come from the operator's `extra_body`, never from a client.
- Strict CSP (no inline script or style), `nosniff`, no framing, no referrer, no caching; CSRF header on browser writes to `/api/`.
- The container runs as a non-root user and holds no credentials; the database URL arrives at runtime.
- Treat `LLMGW_API_KEY` as a full-admin credential. Prefer operator tokens (`lgt_`), which are scoped, revocable and audited at creation.
- Report vulnerabilities privately through GitHub's private vulnerability reporting, not a public issue.

## 10. Operations

### 10.1 Health and readiness

| Endpoint | Auth | Meaning |
|---|---|---|
| `GET /healthz` | none | Liveness: `{"status": "ok", "version": ...}`. Does not touch the database. Used by the image `HEALTHCHECK` |
| `GET /readyz` | none | `{"status": "ready"}` after `SELECT 1`; `503 {"status": "waking-database"}` otherwise (no connection details) |
| `GET /api/session` | none | State for the UI: `setup_required`, `logged_in` or `logged_out`, vault status, whether password and key login are offered, SSO info |
| `GET /metrics` | `metrics:scrape` | Prometheus, including `db_up` evaluated at scrape time and the `db_insights_*` gauges ([7.9](#79-database-insights-dashboard-database)) |

### 10.2 Backup and restore

All durable state is in Postgres: back up the database (`pg_dump`, or the platform's own backups). A dump holds provider keys only in encrypted form, but it also holds the
**wrapped data key**, so keep the admin password (and `LLMGW_ENCRYPTION_KEY` if used) safe: without one of them the stored provider keys
cannot be decrypted and must be re-entered. Client key hashes are in the dump; the keys themselves are not recoverable by design. For an `ecoctl` fleet, `ecosystem/.ecosystem/state.json` holds the
gateway's `LLMGW_API_KEY`, `LLMGW_ENCRYPTION_KEY` and every app's `lg_` key; back that up too (it is the only copy of the app keys).
Restoring into a new database and starting `llmgw start` is enough; migrations are idempotent.

### 10.3 Rotation

- **App key**: Clients > the client > **Rotate** with a grace period long enough for the app to be redeployed; or `POST .../rotate`. With `ecoctl`: `ecoctl apply --execute --rotate` rotates every app's key (and identity secrets, old keys stop at once), then `ecoctl recreate <app>`.
- **Provider key**: Providers > Edit > paste the new key > Save, then Test. The change is immediate; no app is touched.
- **Operator token**: Settings > Operator tokens > create the new one, then revoke the old.
- **Admin password**: Settings > Admin password. Signs everyone out and re-wraps the data key.
- **Vault platform secret** (`LLMGW_ENCRYPTION_KEY`): changing it without the vault being unlocked will leave the platform unlock failing; sign in with the admin password first. The procedure for replacing it was not exercised for this guide.

### 10.4 The vault and restarts

After a restart the vault is **locked** until the admin signs in with the password, `POST /api/vault/unlock`, or until `LLMGW_ENCRYPTION_KEY` unlocks it at startup. While locked,
real providers answer `503 vault_locked` and the **echo provider keeps working**. On an SSO-only deployment (federation on, vault not yet initialised) the gateway initialises
the vault with the platform secret on its first start. `llmgw reset-admin` is the recovery path: it forgets the admin password, clears the vault and deletes all stored provider keys; the setup-code flow is
active again and provider keys must be re-entered.

### 10.5 Housekeeping

A background thread deletes usage rows older than `usage_retention_days` once an hour (nothing when 0). Failed sign-in counters and request windows are in memory and reset on restart.

### 10.6 Scaling notes

The request-rate limit and the failed-credential limits are per process; N replicas allow roughly N times the limit. Each in-flight request holds a worker thread (`LLMGW_MAX_CONCURRENT`) and a pooled connection is taken only
for short queries (`LLMGW_POOL_MAX`).

## 11. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Empty `503` from the URL | AgentPG sandbox paused | `agentpg sandbox resume <sandbox-id>`, then retry |
| `503 service_unavailable` (data plane), `/readyz` `waking-database` | Database scaled to zero or unreachable | Wait and retry (`Retry-After: 5`); check `DATABASE_URL` |
| Container exits at start, "startup failed while migrating" | Database unreachable or wrong credentials | Check the database URL and that Postgres is up; read stderr |
| No setup code visible | Setup already completed, or SSO is on (no local setup) | With SSO use the identity login; to restart setup use `llmgw reset-admin` |
| `401 missing_api_key` / `invalid_api_key` | No bearer, wrong key, or the key was revoked or its grace period ended | Check `Authorization: Bearer lg_...`; issue a new key |
| `429 too_many_failures` | 30 bad keys in a minute from one address (a proxy can make this shared) | Wait a minute; fix the key |
| `403 wrong_credential` | An operator credential (session, `lgt_`, static key, user token) was used on `/v1` | Use a client key or a `llmgw.use` service token |
| `403 client_not_registered` | Service token accepted, but no client is named like its subject | Create a client with exactly that name |
| `403 model_not_allowed` | The client's allowed aliases lack this alias | Edit the client; with `ecoctl`, `ecoctl llm sync <app> --apply` adds missing default aliases |
| `403 client_disabled` | The client is switched off | Clients > the client > Enabled |
| `404 model_not_found` | Alias unknown or disabled; an app is sending a provider model name, not an alias | `GET /v1/models`; use an alias |
| `400 unknown_field` | The app sends `user`, `stream_options`, `functions` or another unsupported field | Remove it; the gateway cannot forward it |
| `400 invalid_tools` / `invalid_tool_choice` / `invalid_parallel_tool_calls` | A tool is not a function tool, a name is not `[A-Za-z0-9_-]{1,64}`, a schema is too big or deep, `tool_choice` names an unlisted tool, or it is sent without `tools` | Fix the request; limits are in API.md |
| `400 invalid_messages` on a tool message | A `role: tool` message has no `tool_call_id`, answers a call twice, or the assistant message before it has no matching `tool_calls` | Send the assistant message with its `tool_calls` and one result per call id, in order |
| `400 stream_not_supported` | The app asked for streaming | Disable streaming in the app |
| `429 rate_limit_exceeded` | The client's rpm window is full | Wait for `Retry-After`, or raise the client's limit |
| `429 budget_exceeded` | Hard monthly budget used up (UTC month) | Raise or remove the budget, switch to soft, or wait for next month |
| `429 upstream_rate_limited` | Every target answered 429 | Add a fallback target or a provider with more headroom |
| `502 upstream_misconfigured` | Provider answered 401, 403 or 404 (bad key, wrong model name, or, as seen on the fleet, HTTP 402 for credit) | Providers > Test; fix key, model name or account credit |
| `502 upstream_unavailable` | Every target failed (5xx, timeout, connection, bad answer, rejected URL) | Read the message (class and redacted detail); check URL and provider status |
| `503 vault_locked` | Vault locked after a restart | Sign in with the admin password, Unlock, or set `LLMGW_ENCRYPTION_KEY`; echo still works |
| `503 no_available_target` | Every target of the alias is disabled | Enable a provider or add a target |
| "base URL rejected" when saving a provider | SSRF guard: `http`, private or loopback address | Use https, or set `LLMGW_ALLOW_PRIVATE_URLS=true` for local testing |
| "set an admin password first" when saving a provider key | Vault not initialised | Set the admin password in Settings, or start with `LLMGW_ENCRYPTION_KEY` |
| Empty `content` with `finish_reason: "length"` | Thinking model used the token budget | Set `extra_body` for that target (presets) or raise `max_tokens`; look for `x-llmgw-note` |
| Cost shows `$0` | No prices on the alias, or the calls were echo | Enter prices on the alias |
| Token counts look rounded | The provider sent no `usage`; counts are estimates (chars / 4), rows marked estimated | Expected |
| SSO login says "no role in the LLM gateway" | The person has no `admin`, `operator` or `viewer` role for this app | Grant a role in identity (or an `ecoctl` group) |
| SSO redirect fails | `LLMGW_PUBLIC_URL` wrong, or the redirect URI at identity differs from `https://<gateway>/auth/callback` | Fix `LLMGW_PUBLIC_URL`; `ecoctl apply` repoints the URI |
| App can reach `/v1` locally but not from the app | The app refuses `http` or private URLs | Use the gateway's https URL, or the app's allow-private switch (`TRUSTDESK_ALLOW_PRIVATE_LLM_URLS`, `PAGERLENS_ALLOW_PRIVATE_LLM_URLS`, `PAYBROKER_ALLOW_PRIVATE_URLS`) |
| Embedding dimension mismatch in an app | App `EMBEDDING_DIM` differs from the alias size | Make the real embedding model's size, the echo size (`embedding_dim`) and the app's setting equal |

## 12. Testing and development

```bash
make lint          # ruff check src tests (line length 110; rules E F I B UP)
make test          # pytest -q; no database: validation, token maths, echo provider, rate window, extra_body, UI fake-DOM (needs node)
make test-db       # whole gateway against Postgres; needs POSTGRES_PASSWORD exported and the compose db running
```

`make test-db` drops and recreates a database named `llmgw_test` in the compose Postgres and sets `TEST_DATABASE_URL` for that run. Database tests are
skipped when `TEST_DATABASE_URL` is unset (`conftest.py`). The test fixtures wipe all tables between tests, then re-seed echo, lock the vault and clear the static key.

`tests/test_pagerlens_ask_e2e.py` runs the gateway in process on a loopback port with the echo provider and drives PagerLens's Ask through it (a real tool round trip). It needs PagerLens's interpreter and a second database; it skips itself otherwise (the command is in the file's docstring).

Test files: `tests/test_pure.py` (validation including tools and tool messages, accounting, echo and its tool hook, rate window, CSV, `extra_body`, reasoning detection), `tests/test_db.py` (the gateway against Postgres, including the
secret-leak and marker-in-prompt scans), `tests/test_hardening.py` (kit version, insights route and degradation, metrics, a real read-only login role: masked `provider_masked`, secret tables denied), `tests/test_federation_db.py` (roles, service scopes, SSO login, vault from platform secret), `tests/test_ui.py` with `tests/js/*.test.mjs` (fake-DOM tests of the dashboard under node, and a check
that the static JS uses no native dialogs). CI runs the tests against a `pgvector/pgvector:pg17` service before building the image.

Development notes: the code style is plain functions and plain SQL (no ORM); migrations are numbered SQL files applied by the kit runner plus the kit's auth schema; a new permission is
added in `core.py` and then named in a route's `require(...)`.

## 13. AIDB and AgentPG integration

**AgentPG (verified).** The gateway runs as an AgentPG sandbox on its own AgentPG database instance, needs no extension, speaks HTTP and Postgres only, tolerates a database scaled to zero (connect retries, `/readyz`
and `503` with `Retry-After`) and uses `PG*` variables when no DSN is given. Everything durable is ordinary tables in that Postgres.

**AIDB (not implemented in this project).** The code contains no AIDB calls. The planned role, from `ecosystem/docs/03-aidb-integration.md`, section 5.5:

- The gateway **stays** in the architecture. AIDB registers remote models as OpenAI-compatible endpoints, so `aidb.create_model(...)` can point at `https://<gateway>/v1` with a dedicated gateway client (for example one named `aidb`).
  Provider keys, budgets, failover and per-consumer usage then stay in one place, and the alias choice per consumer stays in the gateway.
- `ecoctl` is planned to register the models against the gateway as part of an `aidb: true` manifest key.
- Open points named in that document: whether tool calling works for the chosen provider type (the gateway now forwards OpenAI function tools; whether the model supports them is the provider's business), and that providers using native Responses or Anthropic APIs bypass the gateway's OpenAI surface.
  The document says to test this early. The second is a limitation of the gateway as it stands (section 16).
- No gateway-to-AIDB path has been exercised or tested; this is a plan, not a feature.

## 13b. EDB and AgentPG capabilities used by this app

| Capability | How the gateway uses it |
|---|---|
| EDB `edb_stat_monitor`, `edb_pg_tuner`, `query_advisor` (`pg_stat_statements` fallback) | **Database** tab and `GET /api/insights`; `db_insights_*` gauges on `/metrics` ([7.9](#79-database-insights-dashboard-database)). Degrades to "not available" on plain Postgres. |
| AgentPG read-only role `a1p_readonly`, `agentpg db-users`, restricted MCP (`agentpg mcp serve`) | `provider_masked` view and REVOKE on every table with hashes or ciphertext ([7.10](#710-what-a-read-only-database-user-and-agentpgs-restricted-mcp-can-see)). Database-enforced, no `data_redaction` extension. |
| AgentPG sandbox, instance, scale-to-zero wake, environment injection | Deployment ([section 13](#13-aidb-and-agentpg-integration)) |
| Not used | AIDB (role grants missing on the platform), pgvector, PostGIS |

## 14. Data model

Created by `001_init.sql` and `002_extra_body_reasoning.sql` (the kit adds its own tables for settings, sessions, operator tokens and OIDC state):

| Table | Holds |
|---|---|
| `provider` | name, kind (`openai` or `echo`), base URL, encrypted key, last four characters, enabled |
| `model_alias` | alias, kind (`chat` or `embedding`), enabled, embedding size, prices per million tokens |
| `alias_target` | ordered targets (`position`, provider, upstream model, `extra_body` jsonb); provider delete is restricted while referenced |
| `client` | name, allowed aliases (jsonb), rpm limit, monthly token budget, budget mode, enabled |
| `client_key` | prefix, SHA-256 hash, name, created, last used, revoked-at (future values are grace periods) |
| `usage_event` | one metadata row per request (client, key id, alias, provider, upstream model, token counts, `reasoning_tokens`, estimated flag, cost, latency, status, error class, attempts, over-budget flag) |
| `audit_event` | actor, action, target, JSON detail |
| `app_setting` | kit key/value store (settings, `seeded` markers, vault wrapping material) |
| `provider_masked` | view for the read-only role (created after each migration by `hardening.py`, not by a migration file) |

## 15. Command reference

| Command | Does |
|---|---|
| `llmgw migrate` | apply SQL migrations and the kit's auth tables; prints what was applied |
| `llmgw serve` | run the gateway (uvicorn, `HOST`, `PORT`); does not migrate |
| `llmgw start` | container entrypoint: migrate, then serve; exit 1 if migration fails |
| `llmgw reset-admin` | forget the admin password, clear the vault, delete stored provider keys |

## 16. Known limitations and unverified points

Limitations of the gateway (by design, stated in the code and SECURITY.md):

- No streaming (`stream: true` is 400), function tools only (no other tool types, no legacy `functions`), no images or audio, `n` must be 1, no pass-through of provider-specific client fields.
- The request rate limit and the failed-credential limits are in memory and per process.
- Budgets are checked before the call; token counts without a provider `usage` block are estimates; prices are entered by hand; cost is not an invoice.
- Behind a reverse proxy all callers may share one address, which makes the per-address failure limits coarse.
- Failover is only to the next target; one target is never retried.
- Chat and embeddings only. There is no rate limit by tokens per minute and no per-alias budget.
- A request holds a worker thread for as long as the provider takes.

Points that could not be verified from the code for this guide:

- Embedding size: 768 is the echo default, 1536 is the fleet manifest's value for a real model (section 2). Check the manifest you deploy with.
- ShopFront's use of the gateway is taken from the manifest comment; its source was not read.
- How the PagerLens agent scrapes `/metrics` (which token, which interval) lives in the PagerLens and ecoctl projects.
- The default grace value pre-filled in the Rotate dialog is not stated here (the API default is 0).
- The period selector on Overview: the page defaults to 7 days (`days` in `overview.js`); the control for changing it was not read in detail.
- Unlocking the vault in the browser: the kit shows a banner "Provider keys are locked." with an Admin password field and an Unlock button (read in `agentpg-kit/src/apgkit/static/session.js`); this was not seen running. The API (`POST /api/vault/unlock`) is verified in the gateway code.
- The procedure for replacing `LLMGW_ENCRYPTION_KEY` on a live gateway was not exercised.
- Nothing was run for this guide: no tests, no server, no live service. All statements come from reading the source, tests and sibling projects.
