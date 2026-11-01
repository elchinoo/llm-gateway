"""Row MACs: a database write alone (no vault data key) cannot plant a client, a client key, a provider or a route.
Every attack here is plain SQL, as an attacker with write access but without the data key would run it."""
import logging

import pytest
from apgkit.ids import sha256
from psycopg.types.json import Jsonb

from tests.conftest import UPSTREAM_KEY, add_alias, add_client, add_provider, ask, bearer, chat_ok, needs_db

pytestmark = needs_db

from llmgw import cli, core, rowmac  # noqa: E402
from llmgw.db import connect  # noqa: E402

PASSWORD = "correct horse battery staple"
PLANTED = "lg_planted-key-for-the-forgery-tests-000000"


def sql(query, *params):
    with connect() as c:
        cur = c.execute(query, params)
        rows = cur.fetchall() if cur.description else []
        c.commit()
    return rows


def count(table):
    return rowmac.tampered.labels(table)._value.get()


def plant_client(name="evil", key=PLANTED, mac=None, key_mac=None, aliases=("*",)):
    """A client and a key that hash to `key`, inserted directly."""
    cid = sql("INSERT INTO client (name, allowed_aliases, mac) VALUES (%s,%s,%s) RETURNING id", name, Jsonb(list(aliases)), mac)[0]["id"]
    sql("INSERT INTO client_key (client_id, name, prefix, key_hash, mac) VALUES (%s,'',%s,%s,%s)", cid, key[:11], sha256(key), key_mac)
    return cid


# ---- app-written rows verify ---------------------------------------------------------------------------------------------
def test_rows_written_by_the_app_are_signed_and_work(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    cid, key = add_client(client, op, allowed_aliases=["gpt", "echo-chat"])
    upstream.hosts["llm1.example.test"] = chat_ok()
    before = count("client")
    assert ask(client, key, model="gpt").status_code == 200
    assert ask(client, key, model="echo-chat").status_code == 200                         # the seeded demo was signed when the vault was created
    assert client.get("/v1/models", headers=key).status_code == 200
    for table in rowmac.TABLES:
        assert sql(f"SELECT count(*) AS n FROM {table} WHERE mac IS NULL")[0]["n"] == 0, table      # noqa: S608
    assert count("client") == before


def test_updates_resign_and_a_revoked_key_stays_revoked(client, op, vault_ready, upstream):
    cid, key = add_client(client, op)
    r = client.patch(f"/api/clients/{cid}", headers=op, json={"rpm_limit": 5, "description": "x"})
    assert r.status_code == 200 and ask(client, key).status_code == 200
    key_id = client.get(f"/api/clients/{cid}/keys", headers=op).json()["items"][0]["id"]
    assert client.delete(f"/api/clients/{cid}/keys/{key_id}", headers=op).status_code == 200
    assert ask(client, key).status_code == 401
    # a database write that clears revoked_at is not a re-activation: the MAC covers it
    sql("UPDATE client_key SET revoked_at = NULL WHERE id=%s", key_id)
    assert ask(client, key).status_code == 401


# ---- forged rows ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mac", [None, "AAAA"])
def test_a_planted_client_and_key_are_ignored_while_unlocked_and_accepted_while_locked(client, op, vault_ready, mac):
    plant_client(mac=mac, key_mac=mac)
    before = count("client_key")
    assert ask(client, bearer(PLANTED)).status_code == 401
    assert count("client_key") == before + 1
    core.vault.lock()                                     # no vault open: behaviour is unchanged
    assert ask(client, bearer(PLANTED)).status_code == 200


def test_a_planted_key_on_a_genuine_client_is_ignored(client, op, vault_ready):
    cid, key = add_client(client, op)
    sql("INSERT INTO client_key (client_id, name, prefix, key_hash) VALUES (%s,'',%s,%s)", cid, PLANTED[:11], sha256(PLANTED))
    assert ask(client, bearer(PLANTED)).status_code == 401 and ask(client, key).status_code == 200


def test_a_planted_client_is_not_a_service_principal(client, op, vault_ready):
    plant_client(name="svc")
    with connect() as conn:
        from llmgw import clients
        assert clients.client_by_name(conn, "svc") is None
    cid, _ = add_client(client, op, name="real")
    with connect() as conn:
        assert clients.client_by_name(conn, "real")["id"] == cid


@pytest.mark.parametrize("edit", [
    "UPDATE client SET allowed_aliases='[\"*\"]'::jsonb",
    "UPDATE client SET enabled = NOT enabled",
    "UPDATE client SET rpm_limit = 100000",
    "UPDATE client SET monthly_token_budget = NULL",
    "UPDATE client SET budget_mode = 'soft'",
    "UPDATE client SET name = 'other'",
])
def test_an_edited_client_authority_column_is_ignored(client, op, vault_ready, edit):
    _, key = add_client(client, op, allowed_aliases=["echo-chat"], rpm_limit=10, monthly_token_budget=1000)
    assert ask(client, key).status_code == 200
    sql(edit)
    assert ask(client, key).status_code == 401
    core.vault.lock()
    assert ask(client, key).status_code in (200, 403, 429)       # locked: the row is read as before (whatever the edit now means)


def test_a_key_moved_to_another_client_is_ignored(client, op, vault_ready):
    _, key = add_client(client, op, name="small", allowed_aliases=["echo-chat"])
    big, _ = add_client(client, op, name="big")
    sql("UPDATE client_key SET client_id=%s", big)
    assert ask(client, key).status_code == 401


# ---- routing -------------------------------------------------------------------------------------------------------------
def test_a_route_redirected_to_a_planted_provider_is_ignored(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok()
    upstream.hosts["evil.example.test"] = chat_ok("evil")
    evil = sql("INSERT INTO provider (name, kind, base_url) VALUES ('evil','openai','https://evil.example.test/v1') RETURNING id")[0]["id"]
    sql("UPDATE alias_target SET provider_id=%s WHERE alias='gpt'", evil)
    r = ask(client, key, model="gpt")
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_available_target" and upstream.calls == []


def test_an_unsigned_provider_in_a_signed_route_is_dropped(client, op, vault_ready, upstream):
    p1, p2 = add_provider(client, op, "a", "llm1.example.test"), add_provider(client, op, "b", "llm2.example.test")
    add_alias(client, op, "gpt", [(p1, "m"), (p2, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok("one")
    upstream.hosts["llm2.example.test"] = chat_ok("two")
    sql("UPDATE provider SET mac=NULL WHERE id=%s", p1)
    assert "two" in ask(client, key, model="gpt").text and upstream.hosts_called() == ["llm2.example.test"]


@pytest.mark.parametrize("edit", [
    "UPDATE provider SET base_url='https://evil.example.test/v1'",
    "UPDATE provider SET key_origin='https://evil.example.test/v1'",
    "UPDATE provider SET enabled = false",
    "UPDATE provider SET kind = 'echo'",
    "UPDATE alias_target SET upstream_model = 'something-else'",
    "UPDATE alias_target SET extra_body = '{\"x\": 1}'::jsonb",
    "UPDATE alias_target SET position = 1",
    "UPDATE model_alias SET kind = 'embedding'",
    "UPDATE model_alias SET enabled = false",
])
def test_an_edited_route_or_provider_column_is_ignored(client, op, vault_ready, upstream, edit):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = upstream.hosts["evil.example.test"] = chat_ok()
    assert ask(client, key, model="gpt").status_code == 200
    upstream.calls.clear()
    sql(edit)
    r = ask(client, key, model="gpt")
    assert r.status_code != 200 and upstream.calls == []
    assert UPSTREAM_KEY not in r.text


def test_a_copied_provider_key_does_not_sign_itself(client, op, vault_ready, upstream):
    """The ciphertext of one provider copied over another is refused by its MAC before the vault context is even asked."""
    p1 = add_provider(client, op, "a", "a.example.test")
    p2 = add_provider(client, op, "b", "b.example.test", key="sk-other-KEY-000000000000")
    enc = sql("SELECT api_key_enc FROM provider WHERE id=%s", p1)[0]["api_key_enc"]
    add_alias(client, op, "gpt", [(p2, "m")])
    _, key = add_client(client, op)
    sql("UPDATE provider SET api_key_enc=%s WHERE id=%s", enc, p2)
    assert ask(client, key, model="gpt").status_code == 503 and upstream.calls == []


def test_a_planted_alias_is_unknown_and_a_planted_provider_cannot_be_tested(client, op, vault_ready, upstream):
    _, key = add_client(client, op)
    evil = sql("INSERT INTO provider (name, kind, base_url, api_key_enc) VALUES ('evil','openai','https://evil.example.test/v1','x') RETURNING id")[0]["id"]
    sql("INSERT INTO model_alias (alias, kind) VALUES ('planted','chat')")
    sql("INSERT INTO alias_target (alias, position, provider_id, upstream_model) VALUES ('planted',0,%s,'m')", evil)
    upstream.hosts["evil.example.test"] = chat_ok()
    r = ask(client, key, model="planted")
    assert r.status_code == 404 and upstream.calls == []
    assert "planted" not in [m["id"] for m in client.get("/v1/models", headers=key).json()["data"]]
    assert client.post(f"/api/providers/{evil}/test", headers=op, json={"model": "m"}).status_code == 404 and upstream.calls == []


def test_locked_vault_accepts_unsigned_routes(client, op, vault_ready, upstream):
    evil = sql("INSERT INTO provider (name, kind) VALUES ('p2','echo') RETURNING id")[0]["id"]
    sql("INSERT INTO model_alias (alias, kind) VALUES ('planted','chat')")
    sql("INSERT INTO alias_target (alias, position, provider_id, upstream_model) VALUES ('planted',0,%s,'echo-1')", evil)
    _, key = add_client(client, op)
    assert ask(client, key, model="planted").status_code == 404
    core.vault.lock()
    assert ask(client, key, model="planted").status_code == 200


# ---- reporting -----------------------------------------------------------------------------------------------------------
def test_a_refused_row_is_logged_once_by_table_and_key_only(client, op, vault_ready, caplog):
    rowmac._reported.clear()                              # ids restart with every test; in production a primary key is never reused
    plant_client(key_mac=None, mac=None)
    with caplog.at_level(logging.WARNING, logger="llmgw.rowmac"):
        for _ in range(3):
            ask(client, bearer(PLANTED))
    lines = [r.getMessage() for r in caplog.records if r.name == "llmgw.rowmac"]
    assert len(lines) == 1 and "table=client_key" in lines[0]
    assert PLANTED not in lines[0] and sha256(PLANTED) not in lines[0]
    events = sql("SELECT target, detail FROM audit_event WHERE action='row_mac_invalid'")
    assert len(events) == 1 and events[0]["target"].startswith("client_key:") and events[0]["detail"] == {"table": "client_key"}


# ---- adoption, rotation, locked writes ---------------------------------------------------------------------------------
def test_adopt_rowmacs_signs_legacy_rows(client, op, vault_ready, upstream, monkeypatch):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok()
    for t in rowmac.TABLES:
        sql(f"UPDATE {t} SET mac=NULL")                       # noqa: S608  (what an upgraded database looks like)
    assert ask(client, key, model="gpt").status_code == 401
    core.vault.lock()                                         # the CLI opens the vault with the password, asked for on the terminal
    monkeypatch.setattr("getpass.getpass", lambda *_: PASSWORD)
    assert cli.main(["adopt-rowmacs"]) == 0
    assert core.vault.is_unlocked()
    assert ask(client, key, model="gpt").status_code == 200
    assert ask(client, key, model="echo-chat").status_code == 200
    for t in rowmac.TABLES:
        assert sql(f"SELECT count(*) AS n FROM {t} WHERE mac IS NULL")[0]["n"] == 0, t     # noqa: S608
    assert sql("SELECT count(*) AS n FROM audit_event WHERE action='rowmacs_adopted'")[0]["n"] == 1


def test_adopt_refuses_a_wrong_password(client, op, vault_ready, monkeypatch, capsys):
    core.vault.lock()
    monkeypatch.setattr("getpass.getpass", lambda *_: "not the password")
    assert cli.main(["adopt-rowmacs"]) == 1
    assert "refused" in capsys.readouterr().err or not core.vault.is_unlocked()
    assert not core.vault.is_unlocked()


def test_rotation_resigns_and_forgeries_stay_refused(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok()
    r = client.post("/api/vault/rotate", headers=op, json={"password": PASSWORD, "confirm": True})
    assert r.status_code == 200
    assert ask(client, key, model="gpt").status_code == 200 and ask(client, key, model="echo-chat").status_code == 200
    for t in rowmac.TABLES:
        with connect() as conn:
            assert all(rowmac.verify_row(t, row) for row in conn.execute(f"SELECT * FROM {t}").fetchall()), t   # noqa: S608
    plant_client()
    assert ask(client, bearer(PLANTED)).status_code == 401


def test_rows_written_while_locked_carry_no_mac_until_adopted(client, op):
    assert not core.vault.is_unlocked()
    cid, key = add_client(client, op, name="early")
    assert sql("SELECT mac FROM client WHERE id=%s", cid)[0]["mac"] is None and ask(client, key).status_code == 200
    with connect() as conn:
        core.local.set_admin_password(conn, PASSWORD)          # first setup: what exists now is signed
    assert sql("SELECT mac FROM client WHERE id=%s", cid)[0]["mac"] is not None and ask(client, key).status_code == 200
    # an edit while the vault is open re-signs; an edit made while it is locked clears the MAC instead of leaving a stale one
    assert client.patch(f"/api/clients/{cid}", headers=op, json={"rpm_limit": 7}).status_code == 200
    assert ask(client, key).status_code == 200
    core.vault.lock()
    assert client.patch(f"/api/clients/{cid}", headers=op, json={"rpm_limit": 8}).status_code == 200
    assert sql("SELECT mac FROM client WHERE id=%s", cid)[0]["mac"] is None


def test_reset_admin_forgets_the_macs(client, op, vault_ready):
    add_client(client, op)
    with connect() as conn:
        core.local.reset_admin(conn)
    for t in rowmac.TABLES:
        assert sql(f"SELECT count(*) AS n FROM {t} WHERE mac IS NOT NULL")[0]["n"] == 0, t      # noqa: S608
