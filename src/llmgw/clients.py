"""Gateway clients (one per app) and their keys. Only SHA-256 hashes of keys are stored; a key is shown once.

A client key (`lg_...`) is a separate kind of principal: it reaches only the data plane. Lookups hit the database on every
request, so a revoked key, a disabled client or a changed limit takes effect at once, in every process.
"""
import re
import secrets

from apgkit.auth import Principal
from apgkit.ids import sha256
from psycopg.types.json import Jsonb

from . import core
from .core import CLIENT_KEY_PREFIX

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
ALIAS_OR_STAR = re.compile(r"^(\*|[A-Za-z0-9][A-Za-z0-9._:-]{0,59})$")


class ClientError(ValueError):
    pass


_COLS = "id, name, description, allowed_aliases, rpm_limit, monthly_token_budget, budget_mode, enabled, created_at, updated_at"


def month_start_sql() -> str:
    return "(date_trunc('month', timezone('UTC', now())) AT TIME ZONE 'UTC')"


def _check_aliases(conn, allowed: list[str]) -> list[str]:
    allowed = list(dict.fromkeys(a.strip() for a in allowed))
    if not allowed or any(not ALIAS_OR_STAR.match(a) for a in allowed):
        raise ClientError("allowed_aliases must be ['*'] or a list of model aliases")
    if "*" in allowed:
        return ["*"]
    known = {r["alias"] for r in conn.execute("SELECT alias FROM model_alias")}
    missing = [a for a in allowed if a not in known]
    if missing:
        raise ClientError(f"unknown model alias: {missing[0]}")
    return allowed


def list_clients(conn) -> list[dict]:
    rows = conn.execute(
        f"SELECT {_COLS}, "
        "(SELECT count(*) FROM client_key k WHERE k.client_id=c.id AND (k.revoked_at IS NULL OR k.revoked_at > now())) AS active_keys, "
        f"(SELECT coalesce(sum(total_tokens),0) FROM usage_event u WHERE u.client_id=c.id AND u.created_at >= {month_start_sql()})::bigint AS tokens_month, "
        f"(SELECT count(*) FROM usage_event u WHERE u.client_id=c.id AND u.created_at >= {month_start_sql()}) AS requests_month, "
        f"(SELECT coalesce(sum(cost_usd),0) FROM usage_event u WHERE u.client_id=c.id AND u.created_at >= {month_start_sql()})::float AS cost_month "
        "FROM client c ORDER BY name").fetchall()
    return rows


def get(conn, client_id: int) -> dict:
    row = conn.execute(f"SELECT {_COLS} FROM client WHERE id=%s", (client_id,)).fetchone()
    if not row:
        raise ClientError("client not found")
    return row


def create(conn, *, name: str, description: str, allowed_aliases: list[str], rpm_limit: int | None, monthly_token_budget: int | None,
           budget_mode: str) -> int:
    name = name.strip().lower()
    if not NAME_RE.match(name):
        raise ClientError("name must be 1-40 characters: lower-case letters, digits, '.', '_' or '-'")
    if conn.execute("SELECT 1 FROM client WHERE name=%s", (name,)).fetchone():
        raise ClientError("a client with that name already exists")
    allowed = _check_aliases(conn, allowed_aliases)
    row = conn.execute("INSERT INTO client (name, description, allowed_aliases, rpm_limit, monthly_token_budget, budget_mode) "
                       "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                       (name, description.strip(), Jsonb(allowed), rpm_limit, monthly_token_budget, budget_mode)).fetchone()
    conn.commit()
    return row["id"]


def update(conn, client_id: int, fields: dict) -> dict:
    """`fields` holds only what the caller sent (None is a real value for rpm_limit and monthly_token_budget)."""
    cur = get(conn, client_id)
    if "allowed_aliases" in fields:
        fields["allowed_aliases"] = Jsonb(_check_aliases(conn, fields["allowed_aliases"]))
    if "description" in fields:
        fields["description"] = fields["description"].strip()
    if fields:
        sets = ", ".join(f"{k}=%s" for k in fields)
        conn.execute(f"UPDATE client SET {sets}, updated_at=now() WHERE id=%s", (*fields.values(), client_id))
        conn.commit()
    return cur


def delete(conn, client_id: int) -> None:
    get(conn, client_id)
    conn.execute("DELETE FROM client WHERE id=%s", (client_id,))
    conn.commit()


# ---- keys ---------------------------------------------------------------------------------------------------
def create_key(conn, client_id: int, name: str = "") -> tuple[int, str]:
    get(conn, client_id)
    raw = CLIENT_KEY_PREFIX + secrets.token_urlsafe(32)
    row = conn.execute("INSERT INTO client_key (client_id, name, prefix, key_hash) VALUES (%s,%s,%s,%s) RETURNING id",
                       (client_id, name.strip()[:60], raw[:11], sha256(raw))).fetchone()
    conn.commit()
    return row["id"], raw


def list_keys(conn, client_id: int) -> list[dict]:
    return conn.execute("SELECT id, name, prefix, created_at, last_used_at, revoked_at, (revoked_at IS NOT NULL AND revoked_at <= now()) AS revoked "
                        "FROM client_key WHERE client_id=%s ORDER BY id DESC", (client_id,)).fetchall()


def revoke_key(conn, client_id: int, key_id: int, grace_minutes: int = 0) -> bool:
    """Revoke now, or (for a rotation) after a grace period so the app can switch over. Never extends an earlier revocation."""
    row = conn.execute("UPDATE client_key SET revoked_at = now() + make_interval(mins => %s) WHERE id=%s AND client_id=%s "
                       "AND (revoked_at IS NULL OR revoked_at > now() + make_interval(mins => %s)) RETURNING id",
                       (grace_minutes, key_id, client_id, grace_minutes)).fetchone()
    conn.commit()
    return row is not None


def rotate_key(conn, client_id: int, key_id: int, grace_minutes: int = 0) -> tuple[int, str]:
    old = conn.execute("SELECT name FROM client_key WHERE id=%s AND client_id=%s AND (revoked_at IS NULL OR revoked_at > now())",
                       (key_id, client_id)).fetchone()
    if not old:
        raise ClientError("key not found or already revoked")
    new_id, raw = create_key(conn, client_id, old["name"])
    revoke_key(conn, client_id, key_id, grace_minutes)
    return new_id, raw


def key_principal(conn, raw: str) -> Principal | None:
    """Look a client key up. `last_used_at` is refreshed at most once a minute so the hot path does not write per request."""
    row = conn.execute(
        "SELECT k.id AS key_id, k.last_used_at, c.id, c.name, c.allowed_aliases, c.rpm_limit, c.monthly_token_budget, c.budget_mode, c.enabled "
        "FROM client_key k JOIN client c ON c.id = k.client_id WHERE k.key_hash=%s AND (k.revoked_at IS NULL OR k.revoked_at > now())",
        (sha256(raw),)).fetchone()
    if not row:
        conn.rollback()
        return None
    conn.execute("UPDATE client_key SET last_used_at = now() WHERE id=%s AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')",
                 (row["key_id"],))
    conn.commit()
    client = {k: row[k] for k in ("id", "name", "allowed_aliases", "rpm_limit", "monthly_token_budget", "budget_mode", "enabled")}
    return Principal("client", row["name"], frozenset({"llm:use"}), scopes=frozenset({"client"}), name=row["name"], ident=row["key_id"],
                     extra={"client": client})


def client_by_name(conn, name: str) -> dict | None:
    row = conn.execute("SELECT id, name, allowed_aliases, rpm_limit, monthly_token_budget, budget_mode, enabled FROM client WHERE name=%s",
                       (name,)).fetchone()
    conn.rollback()
    return row


def _resolve(raw: str) -> Principal | None:
    with core.db.pool().connection() as conn:
        return key_principal(conn, raw)


core.auth.bearer_resolvers[CLIENT_KEY_PREFIX] = _resolve
