# Security policy

## Reporting a vulnerability
Please **do not open a public issue** for security problems. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository. Include steps to reproduce and the affected version or commit.

## What this is
A gateway that holds your model providers' API keys and lets many apps use them through their own limited keys. The model below
is what it enforces, and where it stops.

## Security model

**Provider keys**
- Encrypted with a random data key that is stored only wrapped under a key derived from the admin password (and optionally a
  platform secret, `LLMGW_ENCRYPTION_KEY`). A database dump alone reveals none of them. After a restart the vault is locked until
  a sign-in or the platform secret unlocks it; real providers then answer 503 `vault_locked` and the echo provider keeps working.
- **The key is bound to the origin it was saved with** (scheme, host, port and path prefix of the provider URL). It is decrypted
  only to build one upstream request, and only when the provider's current URL has that same origin; otherwise nothing is sent
  (`key_origin_mismatch`, the same 502 as any unreachable target, with no key in the message). Changing the URL through the API
  needs the key entered again in the same request (or `clear_key`); the stored key is never reused for another destination. Rows
  created before this rule were bound to their then-current URL by a migration. The key is sent only in the `Authorization`
  header of that request to that origin, targets carry no credentials or headers of their own, and redirects are refused. What
  this does not do: anyone who can read the database and has the admin password or `LLMGW_ENCRYPTION_KEY` can still decrypt the
  keys, and the provider host you saved is trusted with the key (including whoever controls its DNS, within the SSRF rules).
- The key is decrypted only to build one upstream request. It is never returned by any endpoint (only its last four characters),
  never written to the usage table, the audit trail, logs or metrics, and any provider text passed back to a caller, or shown in a
  test result, goes through the kit's `redact` and is truncated. A test scans every table, the metrics, the log and every response
  for a key that the mock provider echoes back in a 200, a 400, a 500 and a connection error.
- Provider URLs are validated when saved and **again on every call** (the guard resolves the name each time, so DNS that starts
  pointing at a private address after the save is refused): `https` only (unless private URLs are explicitly allowed), no
  embedded credentials, no loopback, private, link-local (cloud metadata) or carrier-grade-NAT addresses, no redirects, bounded
  responses (8 MB), bounded time (`LLMGW_UPSTREAM_TIMEOUT`). Limitation: the host name is resolved once for the check and again by
  the HTTP client; use egress rules if that matters.

**Two kinds of credential that never cross**
- *Client keys* (`lg_...`) are random, shown once and stored as SHA-256 hashes. They carry only `llm:use`, are refused on every
  `/api` route and on `/metrics`, and a client sees only the models it is allowed. A revoked key, a disabled client or a changed
  limit takes effect on the next request in every process (the lookup is not cached). A rotation may leave the old key working for a
  grace period an operator chooses, never longer.
- *Operator credentials* (browser sessions, `lgt_` tokens, the static key, single-sign-on tokens of people) are refused on `/v1`,
  including the wildcard `*` ones (the local admin and the static key hold `*`). Cookies are not credentials on `/v1`.
- A service token from the identity provider with scope `llmgw.use` is accepted only if its subject names a **registered, enabled
  client**, and then that client's models, rate limit and budget apply.
- Bad keys on `/v1` are counted per address (30 per minute) and then answered 429; a valid key is never blocked by other callers'
  failures. Keys are 256-bit random values, so guessing is not the realistic threat.

**What is stored about a request**
- **No prompts, completions, embedding inputs or embedding outputs, anywhere.** A usage row holds the client, key id, alias,
  provider, upstream model, token counts, estimated cost, latency, status, error class and attempt count. Unknown model names from a
  caller are not stored or used as metric labels. A test sends a marker string in prompts (accepted, rejected, failing and echo
  requests) and scans every table for it; another does the same for tool arguments and tool results.
- Prompts and answers do pass through the gateway's memory, and to the provider you chose: the gateway is as trustworthy as the
  host it runs on and the providers behind it.

**The data plane's shape**
- Only an allow-list of fields is forwarded; unknown fields (`user`, legacy `functions`, provider-specific passthrough) are refused
  with 400, `stream: true` is refused, `n` must be 1, messages carry text (plus assistant `tool_calls` and `role: tool` results,
  nothing multimodal), values are range checked.
- **Tool calling is bounded and never executed here.** Function tools only (other types are refused); at most 64 tools, 16 KB per
  tool, 128 KB in total, names limited to `[A-Za-z0-9_-]{1,64}` and unique, schemas nested at most 8 levels, at most 64 tool calls of
  at most 64 KB of arguments per message, every `tool_call_id` must answer a call of the message before it, `tool_choice` may only name
  a listed tool. The gateway never runs a tool and never parses or evaluates arguments; it forwards them as opaque strings, and the
  caller's application decides what to execute (treat model-chosen arguments as untrusted input there). Tool schemas, calls,
  arguments and results are prompt content: they are counted for budgets but never stored, logged or used as metric labels (a test
  scans every table for marker strings in tool arguments and results). Bodies over 512 KB are refused with
  413 (also when chunked, without reading past the limit). Responses over 8 MB are refused.
- **`extra_body` is operator-controlled.** The only way a provider-specific field reaches an upstream is the operator-set
  `extra_body` of an alias target (permission `models:manage`); a client cannot send one and its fields never override it. It cannot
  set the gateway-owned keys `model`, `messages`, `input`, `stream`, `user`, `n` (refused at save, dropped again at call time),
  must be a plain JSON object of at most 4 KB and 4 levels, and is merged after the client's fields. A provider's secrets are
  never in it (the key stays encrypted on the provider and is added only to the Authorization header), and changes are written
  to the audit trail with their content, so operators must not paste a secret into it. It does not widen what a client can read:
  responses are passed through as before (including any `reasoning` text the provider returns, which is model output).
- The response's `model` is rewritten to the alias; the upstream's model name is not exposed.

**Operators**
- First run: nobody has an account; the server prints a one-time setup code to its log, and whoever presents it sets the admin
  password (salted scrypt, 12 characters minimum). Browser sessions are `HttpOnly` `SameSite` cookies (`Secure` over HTTPS) whose
  SHA-256 is stored, last 12 hours and end on password change or sign-out. Browser writes need a custom header
  (`X-LLMGW-CSRF`).
- Operator tokens are shown once, hashed, revocable and scoped (`read`, `metrics`, `full`).
- **Single sign-on** (optional, `LLMGW_OIDC_ISSUER`): tokens are verified locally against the identity provider's JWKS (algorithm
  allow-list, issuer, audience, expiry); only this app's roles count and they map to permissions in `core.py`. Browser sign-in is
  authorization code + PKCE with state and nonce. A person with no role here is refused.
- Failed operator credentials are rate limited (10 per minute per address, in memory per process; add proxy limits too).
- Every change to providers, aliases, clients, keys, tokens, settings and the admin password is written to the **audit trail**
  with who made it. Secrets are never recorded.

**Hardening**
- Strict `Content-Security-Policy` (no inline script or style), `nosniff`, no framing, no referrer, no caching. All text reaches the
  page through `textContent`. CSV exports neutralise cells that start with `=`, `+`, `-` or `@`.
- The container runs as a non-root user and holds no credentials; the database URL arrives at runtime.
- **Masking durability**: the read-only database role's REVOKEs are re-asserted every 5 minutes (grants after startup would otherwise reopen the secret tables); gauge `masking_drift_tables` and `/readyz` `masking` show drift. A new grant can be readable for up to one interval.

## Known limits
- **The request rate limit is per process and in memory**; with N replicas a client can reach N times its limit. So are the
  failed-credential limits.
- **Budgets are checked before the call**, so requests in flight can overshoot; token counts without a provider `usage` block are
  estimates (characters / 4); prices are entered by an admin. Cost is an estimate, not an invoice.
- No streaming and no multimodal input, by design. Tool calling is supported for function tools only (see above).
- Behind a reverse proxy every caller may share one address, which makes the per-address failed-credential limits coarse
  (valid data-plane keys are unaffected, operator sign-in attempts are not): set proxy-level limits too.
- One audit trail row per change; it records that a key was issued or revoked, not its value.
