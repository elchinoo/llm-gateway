"""Read-only / AI hardening, applied after every migration (see the kit's `apgkit.masking`).

AgentPG instances have the group role `a1p_readonly` (SELECT on every new public table by default privilege). Direct read-only database
users and AgentPG's restricted MCP server run as members of it. This hook makes the database itself enforce what they can see:

- `provider` is replaced for that role by `provider_masked` (everything except the encrypted upstream API key and its last four characters);
- tables that hold hashes, ciphertext or live credentials and have no view are REVOKEd from the role.

The gateway stores no personal data (clients are applications, usage rows are metadata only), so there is nothing else to mask.
Idempotent, and quiet where the role does not exist (plain Postgres). Only the table owner can REVOKE: the result is verified.
"""
from __future__ import annotations

import logging

import psycopg
from apgkit import masking

log = logging.getLogger("llmgw.hardening")
ROLE = masking.READONLY_ROLE

MASKED_VIEWS = {
    "provider": ("provider_masked", {"id": "pass", "name": "pass", "kind": "pass", "base_url": "pass", "has_key": "(api_key_enc IS NOT NULL)",
                                      "enabled": "pass", "created_at": "pass", "updated_at": "pass"}),     # api_key_enc / api_key_last4 are not exposed
}
DENIED_TABLES = (
    "client_key",             # SHA-256 of every client key (and its prefix)
    "app_setting",            # kit settings and the vault store (wrapped data key, admin password hash)
    "web_session", "api_token", "oidc_login",     # session / operator token hashes, PKCE verifiers
)
# Base tables that must stay closed to the role at all times: the denied ones plus the masked table (its view is readable, the base is not).
# The re-assert loop (api.lifespan) counts drift on these and re-runs `apply` every 5 minutes: grants made after startup
# (`agentpg db-users create`, default privileges) reopen base tables otherwise.
REASSERT_DENY = tuple(MASKED_VIEWS) + DENIED_TABLES
REASSERT_EVERY = 300


def apply(conn: psycopg.Connection, role: str = ROLE) -> dict[str, object]:
    """Mask and deny. Returns {"role_exists", "masked": {table: applied}, "denied": {table: bool}}."""
    out: dict[str, object] = {"role_exists": False, "masked": {}, "denied": {}}
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
        log.info("hardening skipped: role %s does not exist", role)
        return out
    out["role_exists"] = True
    for table, (view, columns) in MASKED_VIEWS.items():
        if conn.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",)).fetchone()["t"]:
            out["masked"][table] = masking.mask_view(conn, table, view, columns, role)["applied"]       # type: ignore[index]
    for table in DENIED_TABLES:
        if not conn.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",)).fetchone()["t"]:
            continue
        conn.execute(f'REVOKE SELECT ON public."{table}" FROM "{role}"')
        conn.commit()
        denied = not conn.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, f"public.{table}")).fetchone()["ok"]
        out["denied"][table] = denied                                                        # type: ignore[index]
        if not denied:
            log.warning("hardening: %s can still read %s (not the owner, or PUBLIC has SELECT)", role, table)
    return out
