"""Model aliases: the name a client asks for, mapped to an ordered list of (provider, upstream model) targets."""
import re

ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,59}$")
MAX_TARGETS = 5


class AliasError(ValueError):
    pass


def list_aliases(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM model_alias ORDER BY alias").fetchall()
    targets = conn.execute("SELECT t.alias, t.position, t.provider_id, t.upstream_model, p.name AS provider_name, p.kind AS provider_kind, "
                           "p.enabled AS provider_enabled FROM alias_target t JOIN provider p ON p.id = t.provider_id "
                           "ORDER BY t.alias, t.position").fetchall()
    for r in rows:
        r["targets"] = [{k: t[k] for k in ("position", "provider_id", "upstream_model", "provider_name", "provider_kind", "provider_enabled")}
                        for t in targets if t["alias"] == r["alias"]]
        for k in ("price_prompt_per_m", "price_completion_per_m"):
            r[k] = float(r[k]) if r[k] is not None else None
    return rows


def get(conn, alias: str) -> dict:
    for r in list_aliases(conn):
        if r["alias"] == alias:
            return r
    raise AliasError("no such model alias")


def upsert(conn, alias: str, *, kind: str, description: str, enabled: bool, embedding_dim: int, price_prompt_per_m: float | None,
           price_completion_per_m: float | None, targets: list[dict]) -> bool:
    """Create or replace an alias and its targets. Returns True if it was created."""
    if not ALIAS_RE.match(alias):
        raise AliasError("alias must be 1-60 characters: letters, digits, '.', '_', ':' or '-'")
    if kind not in {"chat", "embedding"}:
        raise AliasError("kind must be 'chat' or 'embedding'")
    if not 1 <= len(targets) <= MAX_TARGETS:
        raise AliasError(f"an alias needs between 1 and {MAX_TARGETS} targets")
    for t in targets:
        if not conn.execute("SELECT 1 FROM provider WHERE id=%s", (t["provider_id"],)).fetchone():
            raise AliasError(f"provider {t['provider_id']} does not exist")
    existed = conn.execute("SELECT 1 FROM model_alias WHERE alias=%s", (alias,)).fetchone() is not None
    conn.execute(
        "INSERT INTO model_alias (alias, kind, description, enabled, embedding_dim, price_prompt_per_m, price_completion_per_m) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (alias) DO UPDATE SET kind=EXCLUDED.kind, description=EXCLUDED.description, "
        "enabled=EXCLUDED.enabled, embedding_dim=EXCLUDED.embedding_dim, price_prompt_per_m=EXCLUDED.price_prompt_per_m, "
        "price_completion_per_m=EXCLUDED.price_completion_per_m, updated_at=now()",
        (alias, kind, description.strip(), enabled, embedding_dim, price_prompt_per_m, price_completion_per_m))
    conn.execute("DELETE FROM alias_target WHERE alias=%s", (alias,))
    for i, t in enumerate(targets):
        conn.execute("INSERT INTO alias_target (alias, position, provider_id, upstream_model) VALUES (%s,%s,%s,%s)",
                     (alias, i, t["provider_id"], t["upstream_model"].strip()))
    conn.commit()
    return not existed


def delete(conn, alias: str) -> None:
    get(conn, alias)
    conn.execute("DELETE FROM model_alias WHERE alias=%s", (alias,))
    conn.commit()


def seed_echo(conn) -> bool:
    """First start: one `echo` provider and two aliases so the fleet can be wired up immediately. Runs once (a setting
    remembers it), so an admin who deletes the demo is not surprised by it coming back."""
    from apgkit import settings_store as store
    if store.get(conn, "seeded") or conn.execute("SELECT 1 FROM provider LIMIT 1").fetchone():
        return False
    pid = conn.execute("INSERT INTO provider (name, kind) VALUES ('echo', 'echo') RETURNING id").fetchone()["id"]
    for alias, kind, model in (("echo-chat", "chat", "echo-1"), ("echo-embed", "embedding", "echo-embed-1")):
        conn.execute("INSERT INTO model_alias (alias, kind, description) VALUES (%s,%s,%s)",
                     (alias, kind, "built-in demo model: no network, no key"))
        conn.execute("INSERT INTO alias_target (alias, position, provider_id, upstream_model) VALUES (%s,0,%s,%s)", (alias, pid, model))
    store.put(conn, "seeded", "1")
    conn.commit()
    return True
