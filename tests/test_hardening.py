"""Database insights (EDB extensions, degrading) and read-only / AI hardening proven with a real read-only login role."""
import os
import re

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from tests.conftest import UPSTREAM_KEY, add_client, add_provider, bearer, needs_db

LOGIN = "llmgw_ro_login"


def test_kit_is_at_least_0_3_0():
    import apgkit
    assert tuple(int(x) for x in apgkit.__version__.split(".")[:3]) >= (0, 3, 0)


@pytest.fixture
def ro(client, op, vault_ready, upstream):
    """A login role in a1p_readonly holding what AgentPG's default privileges give it, after the real migrate path ran the hook."""
    from llmgw import migrate
    from llmgw.db import connect
    add_provider(client, op)                                   # a provider with an (encrypted) upstream key
    add_client(client, op)                                     # a client with a key (its hash is stored)
    with connect() as c:
        c.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='a1p_readonly') THEN CREATE ROLE a1p_readonly NOLOGIN; END IF; END $$")
        c.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{LOGIN}') THEN CREATE ROLE {LOGIN} LOGIN; END IF; END $$")
        c.execute(f"GRANT a1p_readonly TO {LOGIN}")
        c.execute("GRANT USAGE ON SCHEMA public TO a1p_readonly")
        c.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO a1p_readonly")
        c.commit()
    migrate.run()
    conn = psycopg.connect(make_conninfo(os.environ["TEST_DATABASE_URL"], user=LOGIN), row_factory=dict_row, autocommit=True)
    yield conn
    conn.close()


@needs_db
def test_read_only_login_sees_masked_providers_and_no_secret_tables(client, op, ro):
    from llmgw import hardening
    for t in hardening.DENIED_TABLES:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ro.execute(f'SELECT * FROM "{t}"')
    for q in ("SELECT * FROM provider", "SELECT api_key_enc FROM provider"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ro.execute(q)
    rows = ro.execute("SELECT * FROM provider_masked").fetchall()
    assert rows and all("api_key_enc" not in r and "api_key_last4" not in r for r in rows)
    assert any(r["has_key"] for r in rows) and any(r["name"] == "up1" for r in rows)
    assert UPSTREAM_KEY[-4:] not in str(rows)
    with pytest.raises(psycopg.Error):
        ro.execute("UPDATE provider_masked SET enabled = false")
    for t in ("client", "usage_event", "audit_event", "model_alias"):        # no personal data, no secrets: still readable
        ro.execute(f"SELECT count(*) FROM {t}")


@needs_db
def test_every_secret_looking_column_is_covered(client, op, ro):
    """Guards new migrations: a hash/secret/ciphertext/key column must sit in a denied table or be left out of a masked view."""
    from llmgw import hardening
    from llmgw.db import connect
    pat = re.compile(r"hash|secret|_enc$|private|verifier|password|token")
    with connect() as c:
        cols = c.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public' AND table_name NOT LIKE '%\\_masked'").fetchall()
    uncovered = []
    for r in cols:
        t, col = r["table_name"], r["column_name"]
        if not pat.search(col) or t in hardening.DENIED_TABLES:
            continue
        readable = ro.execute("SELECT has_table_privilege(%s, 'SELECT') AS ok", (t,)).fetchone()["ok"]
        if t in hardening.MASKED_VIEWS and not readable and col not in hardening.MASKED_VIEWS[t][1]:
            continue
        # token COUNTS (usage, budgets) are numbers, not credentials
        if re.search(r"(^|_)tokens?(_|$)", col) and col not in ("token_hash",):
            continue
        uncovered.append((t, col))
    assert not uncovered, f"secret-looking columns readable by a1p_readonly: {uncovered}"


@needs_db
def test_hardening_is_idempotent_and_quiet_without_the_role(client, op, ro):
    from llmgw import hardening, migrate
    from llmgw.db import connect
    with connect() as c:
        a, b = hardening.apply(c), hardening.apply(c)
        assert a == b and all(a["masked"].values()) and all(a["denied"].values())
        assert hardening.apply(c, role="no_such_readonly_role") == {"role_exists": False, "masked": {}, "denied": {}}
    migrate.run()                                              # the real path again: still fine


@needs_db
def test_insights_route_is_admin_only_and_degrades_on_plain_postgres(client, op):
    assert client.get("/api/insights").status_code in (401, 403)
    read = bearer(client.post("/api/tokens", headers=op, json={"name": "r", "scope": "read"}).json()["token"])
    assert client.get("/api/insights", headers=read).status_code == 403                   # statement text is admin-only
    _, key = add_client(client, op)
    assert client.get("/api/insights", headers=key).status_code == 403
    r = client.get("/api/insights?order=mean&limit=5", headers=op)
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"sources", "top_queries", "tuner", "advisor"}
    assert d["tuner"]["available"] is False and d["advisor"]["available"] is False       # no EDB extensions on the throwaway Postgres
    assert d["sources"]["edb_stat_monitor"]["available"] is False and d["sources"]["edb_stat_monitor"]["reason"]
    assert client.get("/api/insights?order=bogus", headers=op).status_code == 200       # a bad order falls back
    assert client.get("/api/insights?limit=0", headers=op).status_code == 422


@needs_db
def test_insights_metrics_are_exported(client, op):
    from llmgw import metrics
    body, _ = metrics.render()
    assert b'db_insights_available{app="llmgw"}' in body
    assert b"db_insights_tuner_recommendations" not in body             # absent when edb_pg_tuner is unavailable
