"""Row MACs: a database WRITE without the vault data key cannot plant a client, a client key, a provider or a route.

The rows that grant access or carry trust (`client`, `client_key`, `provider`, `model_alias`, `alias_target`) have a `mac` column: an
HMAC under a subkey of the vault data key (agentpg-kit `Vault.row_mac`) over the columns listed in FIELDS. Reads that feed an
authorization or routing decision call `verify_row`; a row with a missing or wrong MAC is treated exactly like a missing row. Writes
sign in the same transaction (`seal`, `update`). Browser sessions and operator API tokens are signed by the kit itself.

Enforced only while the vault is unlocked (always, with sealed boot). While it is locked (development, tests, no vault yet) rows are
accepted as before; writes made then clear the MAC, and `llmgw adopt-rowmacs` (or setting the admin password for the first time) signs
what is there.

Limits (see README, "Row MACs"): a copied older authentic row replays from a backup; deleting rows is still possible; unsigned rows are
ignored, so an attacker's write can at worst deny service; columns outside FIELDS (names, descriptions, prices, timestamps, last4) are
not covered.
"""
import json
import logging
import threading
from datetime import datetime

from prometheus_client import Counter

from . import core

log = logging.getLogger("llmgw.rowmac")

# column kinds: s text/number, json jsonb (sort_keys), ts timestamptz (epoch seconds), bool (0/1)
FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    # who may call, with which limits (name is the subject a service token is matched against)
    "client": (("id", "s"), ("name", "s"), ("allowed_aliases", "json"), ("rpm_limit", "s"), ("monthly_token_budget", "s"),
               ("budget_mode", "s"), ("enabled", "bool")),
    # the credential: its hash, its owner, whether it was revoked
    "client_key": (("id", "s"), ("client_id", "s"), ("key_hash", "s"), ("revoked_at", "ts")),
    # where traffic goes and the credential sent there (the key is encrypted, bound to this row and to the origin it was saved with)
    "provider": (("id", "s"), ("kind", "s"), ("base_url", "s"), ("api_key_enc", "s"), ("key_origin", "s"), ("enabled", "bool")),
    "model_alias": (("alias", "s"), ("kind", "s"), ("enabled", "bool"), ("embedding_dim", "s")),
    # routing: which provider and upstream model an alias reaches, and the operator-set body fields
    "alias_target": (("alias", "s"), ("position", "s"), ("provider_id", "s"), ("upstream_model", "s"), ("extra_body", "json")),
}
PK: dict[str, tuple[str, ...]] = {
    "client": ("id",), "client_key": ("id",), "provider": ("id",), "model_alias": ("alias",), "alias_target": ("alias", "position"),
}
TABLES = tuple(FIELDS)

tampered = Counter("llmgw_tampered_rows_total", "Rows refused because their MAC is missing or wrong", ["table"], registry=core.metrics.registry)
_reported: set[tuple] = set()
_reported_lock = threading.Lock()


def enforced() -> bool:
    return core.vault.is_unlocked()


def _canon(value, kind: str) -> str:
    if value is None:
        return "\x00"                                     # distinct from the empty string
    if kind == "json":
        return json.dumps(value, sort_keys=True)
    if kind == "ts":
        return str(int(value.timestamp())) if isinstance(value, datetime) else str(int(value))
    if kind == "bool":
        return "1" if value else "0"
    return str(value)


def sign_row(table: str, row: dict) -> str:
    """The MAC for `row` (a dict with at least the MAC'd columns of `table`). Raises VaultLocked when the vault is locked."""
    return core.vault.row_mac(table, *(_canon(row[c], k) for c, k in FIELDS[table]))


def _key(table: str, row: dict) -> tuple:
    return tuple(str(row[c]) for c in PK[table])


def verify_row(table: str, row: dict | None) -> bool:
    """True when the row may be used. A missing row is False. While the vault is locked every row passes (nothing to check against).
    While it is unlocked the row needs a valid MAC: a missing or wrong one is False (the caller treats the row as absent), logged
    (table and primary key only), counted and audited once per row per process."""
    if not row:
        return False
    if not enforced():
        return True
    try:
        if core.vault.check_row_mac(table, row.get("mac"), *(_canon(row[c], k) for c, k in FIELDS[table])):
            return True
    except Exception:  # noqa: BLE001  (locked mid-call, odd column types: fail closed)
        return False
    key = (table, *_key(table, row))
    tampered.labels(table).inc()
    with _reported_lock:
        first = key not in _reported
        _reported.add(key)
    if first:
        pk = "/".join(key[1:])
        log.warning("row refused, MAC missing or wrong: table=%s key=%s", table, pk)
        try:
            from . import audit
            with core.db.connect(retries=1) as c:
                audit.log(c, "system", "row_mac_invalid", f"{table}:{pk}", table=table)
        except Exception:  # noqa: BLE001  (auditing must never break a lookup)
            pass
    return False


def valid(table: str, rows: list[dict]) -> list[dict]:
    return [r for r in rows if verify_row(table, r)]


def _where(table: str, pk: tuple) -> tuple[str, tuple]:
    return " AND ".join(f"{c}=%s" for c in PK[table]), pk


def seal(conn, table: str, where: str, params: tuple = ()) -> int:
    """Sign the rows of `table` matching `where`, in the caller's transaction. ONLY for rows this process has just written (or `adopt`):
    it signs whatever the row holds now. While the vault is locked the MAC is cleared (the row is ignored once the vault opens, until
    it is adopted)."""
    rows = conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()           # noqa: S608  (table is a constant of this module)
    unlocked = enforced()
    for r in rows:
        w, p = _where(table, tuple(r[c] for c in PK[table]))
        conn.execute(f"UPDATE {table} SET mac=%s WHERE {w}", (sign_row(table, r) if unlocked else None, *p))   # noqa: S608
    return len(rows)


def update(conn, table: str, assignments: str, set_params: tuple, where: str, params: tuple = ()) -> int:
    """UPDATE ... SET `assignments` WHERE `where`, then re-sign the rows that carried a valid MAC BEFORE the change (a planted or edited
    row is never legitimised by an unrelated update). While the vault is locked the MAC is cleared in the same statement. Returns the
    number of rows changed."""
    if not enforced():
        return conn.execute(f"UPDATE {table} SET {assignments}, mac=NULL WHERE {where}", (*set_params, *params)).rowcount    # noqa: S608
    before = conn.execute(f"SELECT * FROM {table} WHERE {where} FOR UPDATE", params).fetchall()             # noqa: S608
    good = [tuple(r[c] for c in PK[table]) for r in before if verify_row(table, r)]
    n = conn.execute(f"UPDATE {table} SET {assignments} WHERE {where}", (*set_params, *params)).rowcount     # noqa: S608
    for pk in good:
        w, p = _where(table, pk)
        seal(conn, table, w, p)
    return n


def clear(conn) -> None:
    """Forget every MAC (the vault was reset: they are under a key that no longer exists)."""
    for t in TABLES:
        conn.execute(f"UPDATE {t} SET mac=NULL")                                              # noqa: S608


def snapshot(conn) -> dict[str, list]:
    """Primary keys of every row that verifies now (used around a data-key rotation, which changes every MAC). Includes the kit's own
    web_session and api_token rows (their MACs also change with the data key and the kit does not re-sign them)."""
    out: dict[str, list] = {}
    out["web_session"] = [r["token_hash"] for r in conn.execute("SELECT * FROM web_session").fetchall()
                          if core.vault.check_row_mac("web_session", r["mac"], r["token_hash"], r["subject"], r["name"], r["email"],
                                                      json.dumps(sorted(r["roles"])), r["source"], int(r["expires_at"].timestamp()))]
    out["api_token"] = [r["id"] for r in conn.execute("SELECT * FROM api_token WHERE revoked_at IS NULL").fetchall()
                        if core.vault.check_row_mac("api_token", r["mac"], r["id"], r["name"], r["token_hash"], r["prefix"], r["scope"], 0)]
    for t in TABLES:
        out[t] = [tuple(r[c] for c in PK[t]) for r in conn.execute(f"SELECT * FROM {t}").fetchall() if verify_row(t, r)]        # noqa: S608
    return out


def reseal(conn, snap: dict[str, list]) -> int:
    n = 0
    for token_hash in snap.pop("web_session", []):
        r = conn.execute("SELECT * FROM web_session WHERE token_hash=%s", (token_hash,)).fetchone()
        if r:
            conn.execute("UPDATE web_session SET mac=%s WHERE token_hash=%s", (core.local.session_mac(
                r["token_hash"], r["subject"], r["name"], r["email"], r["roles"], r["source"], r["expires_at"]), token_hash))
    for tid in snap.pop("api_token", []):
        r = conn.execute("SELECT * FROM api_token WHERE id=%s", (tid,)).fetchone()
        if r:
            conn.execute("UPDATE api_token SET mac=%s WHERE id=%s", (core.local.token_mac(
                r["id"], r["name"], r["token_hash"], r["prefix"], r["scope"], False), tid))
    for t, pks in snap.items():
        for pk in pks:
            w, p = _where(t, pk)
            n += seal(conn, t, w, p)
    return n


def adopt(conn) -> dict[str, int]:
    """Sign every row that has no MAC (existing databases), the kit's sessions and tokens included. TRUSTS THE DATABASE AS IT IS NOW.
    Needs the vault open. Commits."""
    if not enforced():
        raise RuntimeError("adopt needs an open vault")
    counts = {t: seal(conn, t, "mac IS NULL") for t in TABLES}
    counts.update({f"kit {k}": v for k, v in core.local.adopt_rows(conn).items()})       # commits the transaction
    return counts


def _adopt_on_first_setup() -> None:
    """Setting the admin password for the first time creates the vault: the rows written until then (the echo demo, anything created
    while the vault was locked) carry no MAC and would be ignored from now on. The operator has just set the deployment up, so they are
    signed at that moment; a deployment that already has a vault uses `llmgw adopt-rowmacs`."""
    original = core.local.set_admin_password

    def set_admin_password(conn, *args, **kwargs):
        result = original(conn, *args, **kwargs)
        if enforced():
            adopt(conn)
        return result

    core.local.set_admin_password = set_admin_password


_adopt_on_first_setup()
