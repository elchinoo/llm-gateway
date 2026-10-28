"""Tiny plain-SQL migration runner (just Postgres: no ORM). The shared tables (settings, sessions, API tokens, OIDC state)
come from agentpg-kit."""
from pathlib import Path

from apgkit import migrate as kit_migrate
from apgkit.auth.schema import SQL as AUTH_SQL

from . import hardening
from .core import db

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def run() -> list[str]:
    with db.connect() as conn:
        conn.execute(AUTH_SQL)               # before the hooks, so hardening also sees the kit tables (sessions, tokens)
        conn.commit()
    return kit_migrate.run(db, MIGRATIONS, after_migrate=[hardening.apply])
