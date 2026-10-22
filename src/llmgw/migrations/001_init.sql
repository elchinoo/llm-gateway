CREATE TABLE IF NOT EXISTS app_setting (key text PRIMARY KEY, value text NOT NULL);

-- Upstream accounts. The API key is encrypted by the vault; `echo` is built in and needs neither network nor key.
CREATE TABLE provider (
    id             bigserial PRIMARY KEY,
    name           text NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    kind           text NOT NULL CHECK (kind IN ('openai', 'echo')),
    base_url       text,
    api_key_enc    text,
    api_key_last4  text,
    enabled        boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX provider_name_key ON provider (lower(name));

-- What clients ask for by name. Prices are entered by an admin (USD per 1M tokens) and only feed cost estimates.
CREATE TABLE model_alias (
    alias                   text PRIMARY KEY CHECK (alias ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,59}$'),
    kind                    text NOT NULL CHECK (kind IN ('chat', 'embedding')),
    description             text NOT NULL DEFAULT '',
    enabled                 boolean NOT NULL DEFAULT true,
    embedding_dim           integer NOT NULL DEFAULT 768 CHECK (embedding_dim BETWEEN 1 AND 8192),
    price_prompt_per_m      numeric(14, 6) CHECK (price_prompt_per_m >= 0),
    price_completion_per_m  numeric(14, 6) CHECK (price_completion_per_m >= 0),
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

-- Ordered failover list: position 0 is tried first.
CREATE TABLE alias_target (
    alias           text NOT NULL REFERENCES model_alias (alias) ON DELETE CASCADE ON UPDATE CASCADE,
    position        integer NOT NULL CHECK (position >= 0),
    provider_id     bigint NOT NULL REFERENCES provider (id) ON DELETE RESTRICT,
    upstream_model  text NOT NULL CHECK (length(upstream_model) BETWEEN 1 AND 120),
    PRIMARY KEY (alias, position)
);

-- One per app that uses the gateway. Names are lower-case so they can double as metric labels and token subjects.
CREATE TABLE client (
    id                    bigserial PRIMARY KEY,
    name                  text NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9._-]{0,39}$'),
    description           text NOT NULL DEFAULT '',
    allowed_aliases       jsonb NOT NULL DEFAULT '["*"]',
    rpm_limit             integer CHECK (rpm_limit >= 1),
    monthly_token_budget  bigint CHECK (monthly_token_budget >= 1),
    budget_mode           text NOT NULL DEFAULT 'hard' CHECK (budget_mode IN ('hard', 'soft')),
    enabled               boolean NOT NULL DEFAULT true,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- Only the SHA-256 of a key is stored. A key stops working when revoked_at passes (a rotation may leave a grace period).
CREATE TABLE client_key (
    id           bigserial PRIMARY KEY,
    client_id    bigint NOT NULL REFERENCES client (id) ON DELETE CASCADE,
    name         text NOT NULL DEFAULT '',
    prefix       text NOT NULL,
    key_hash     text NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);
CREATE INDEX client_key_client ON client_key (client_id);

-- One row per model request. Metadata only: never a prompt, a completion or an embedding input or output.
CREATE TABLE usage_event (
    id                bigserial PRIMARY KEY,
    created_at        timestamptz NOT NULL DEFAULT now(),
    client_id         bigint REFERENCES client (id) ON DELETE SET NULL,
    client_name       text NOT NULL,
    key_id            bigint,
    endpoint          text NOT NULL,
    alias             text,
    provider_name     text,
    upstream_model    text,
    prompt_tokens     integer NOT NULL DEFAULT 0,
    completion_tokens integer NOT NULL DEFAULT 0,
    total_tokens      integer NOT NULL DEFAULT 0,
    estimated         boolean NOT NULL DEFAULT false,
    cost_usd          numeric(14, 6) NOT NULL DEFAULT 0,
    latency_ms        integer NOT NULL DEFAULT 0,
    status            integer NOT NULL,
    error_class       text,
    attempts          integer NOT NULL DEFAULT 0,
    over_budget       boolean NOT NULL DEFAULT false
);
CREATE INDEX usage_event_client_time ON usage_event (client_id, created_at);
CREATE INDEX usage_event_time ON usage_event (created_at);

-- Who changed what and when. Never secrets.
CREATE TABLE audit_event (
    id      bigserial PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    actor   text NOT NULL,
    action  text NOT NULL,
    target  text NOT NULL DEFAULT '',
    detail  jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_event_at ON audit_event (at DESC);
