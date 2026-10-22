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


DEFAULT_CHAT_ALIAS = "chat-default"
DEFAULT_EMBED_ALIAS = "embed-default"
DEFAULTS_NOTE = "default model: points at the built-in echo provider until an admin repoints it at a real one"


def default_alias_names() -> tuple[str, str, int | None]:
    """The default aliases every app is wired to. `LLMGW_DEFAULT_CHAT_ALIAS`, `LLMGW_DEFAULT_EMBED_ALIAS` and
    `LLMGW_DEFAULT_EMBED_DIM` (the size of the echo embedding) rename them; a bad value falls back to the built-in one."""
    import os

    def name(var: str, fallback: str) -> str:
        v = os.environ.get(var, "").strip()
        return v if ALIAS_RE.match(v) else fallback

    raw = os.environ.get("LLMGW_DEFAULT_EMBED_DIM", "").strip()
    dim = int(raw) if raw.isdigit() and 1 <= int(raw) <= 8192 else None
    return name("LLMGW_DEFAULT_CHAT_ALIAS", DEFAULT_CHAT_ALIAS), name("LLMGW_DEFAULT_EMBED_ALIAS", DEFAULT_EMBED_ALIAS), dim


def _insert_defaults(conn, provider_id: int) -> None:
    """Create the default aliases on the echo provider. An alias that already exists is left exactly as it is."""
    from . import settings as cfg
    chat, embed, dim = default_alias_names()
    for alias, kind, model, size in ((chat, "chat", "echo-1", None), (embed, "embedding", "echo-embed-1",
                                                                         dim or cfg.get_all(conn)["default_embedding_dim"])):
        made = conn.execute("INSERT INTO model_alias (alias, kind, description, embedding_dim) VALUES (%s,%s,%s,%s) "
                            "ON CONFLICT (alias) DO NOTHING RETURNING alias", (alias, kind, DEFAULTS_NOTE, size or 768)).fetchone()
        if made:
            conn.execute("INSERT INTO alias_target (alias, position, provider_id, upstream_model) VALUES (%s,0,%s,%s)",
                         (alias, provider_id, model))


def seed_echo(conn) -> bool:
    """First start: one `echo` provider, the demo aliases `echo-chat` and `echo-embed`, and the default aliases
    (`chat-default`, `embed-default`) so the fleet can be wired up immediately and later switched to a real provider by
    repointing the default alias. Runs once (a setting remembers it), so an admin who deletes the demo is not surprised by
    it coming back."""
    from apgkit import settings_store as store
    if store.get(conn, "seeded") or conn.execute("SELECT 1 FROM provider LIMIT 1").fetchone():
        return False
    pid = conn.execute("INSERT INTO provider (name, kind) VALUES ('echo', 'echo') RETURNING id").fetchone()["id"]
    for alias, kind, model in (("echo-chat", "chat", "echo-1"), ("echo-embed", "embedding", "echo-embed-1")):
        conn.execute("INSERT INTO model_alias (alias, kind, description) VALUES (%s,%s,%s)",
                     (alias, kind, "built-in demo model: no network, no key"))
        conn.execute("INSERT INTO alias_target (alias, position, provider_id, upstream_model) VALUES (%s,0,%s,%s)", (alias, pid, model))
    _insert_defaults(conn, pid)
    store.put(conn, "seeded", "1")
    store.put(conn, "seeded_defaults", "1")
    conn.commit()
    return True


def seed_default_aliases(conn) -> bool:
    """Upgrade path for a gateway that was seeded before the default aliases existed: add them once, on the echo provider.
    Idempotent and never destructive: an existing alias keeps whatever an operator made it, an admin who deleted the echo
    provider (or the aliases) does not get them back, and it runs once (a setting remembers it)."""
    from apgkit import settings_store as store
    if store.get(conn, "seeded_defaults"):
        return False
    echo = conn.execute("SELECT id FROM provider WHERE kind='echo' ORDER BY id LIMIT 1").fetchone()
    created = False
    if echo:
        _insert_defaults(conn, echo["id"])
        created = True
    store.put(conn, "seeded_defaults", "1")
    conn.commit()
    return created
