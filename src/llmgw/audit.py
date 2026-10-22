"""Audit trail of operator changes: who, what, when. Callers pass identifiers and non-secret settings, never keys."""
from psycopg.types.json import Jsonb


def log(conn, actor: str, action: str, target: str = "", **detail) -> None:
    conn.execute("INSERT INTO audit_event (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
                 (actor[:80] or "unknown", action, target[:120], Jsonb(detail)))
    conn.commit()


def recent(conn, limit: int = 100, before: int | None = None) -> list[dict]:
    return conn.execute("SELECT id, at, actor, action, target, detail FROM audit_event WHERE (%s::bigint IS NULL OR id < %s) "
                        "ORDER BY id DESC LIMIT %s", (before, before, limit)).fetchall()
