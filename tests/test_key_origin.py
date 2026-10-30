"""A provider's stored key is bound to the origin it was saved with: changing the destination needs the key again, and the
key is never sent anywhere else."""
from pathlib import Path

from tests.conftest import (
    UPSTREAM_KEY,
    add_alias,
    add_client,
    add_provider,
    all_table_text,
    ask,
    chat_ok,
    needs_db,
)

pytestmark = needs_db

NEW = "https://other.example.test/v1"


def put(client, op, pid, **kw):
    body = {"name": "up1", "base_url": "https://llm1.example.test/v1", **kw}
    return client.put(f"/api/providers/{pid}", headers=op, json=body)


def row(client, op, pid):
    return next(i for i in client.get("/api/providers", headers=op).json()["items"] if i["id"] == pid)


def seen_keys(upstream):
    return [c.headers.get("authorization") for c in upstream.calls]


def test_changing_the_url_without_the_key_is_refused(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    for url in (NEW, "https://llm1.example.test:8443/v1", "https://llm1.example.test/other", "https://llm1.example.test/"):
        r = put(client, op, pid, base_url=url)
        assert r.status_code == 422 and "enter the provider key again" in r.json()["detail"], url
    cur = row(client, op, pid)
    assert cur["base_url"] == "https://llm1.example.test/v1" and cur["has_key"]
    assert UPSTREAM_KEY not in r.text


def test_changing_the_url_with_the_key_rebinds_it(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    r = put(client, op, pid, base_url=NEW, api_key="sk-second-KEY-9999")
    assert r.status_code == 200 and r.json()["key_origin"] == NEW and r.json()["api_key_last4"] == "9999"
    upstream.hosts["other.example.test"] = chat_ok(usage={"prompt_tokens": 1, "completion_tokens": 1})
    assert ask(client, key, model="gpt").status_code == 200
    assert seen_keys(upstream) == ["Bearer sk-second-KEY-9999"]


def test_same_origin_edits_keep_the_key_and_clearing_it_is_allowed(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    assert put(client, op, pid, base_url="HTTPS://LLM1.example.test:443/v1/", name="renamed").status_code == 200
    assert put(client, op, pid, enabled=False).status_code == 200
    r = put(client, op, pid, base_url=NEW, clear_key=True)
    assert r.status_code == 200 and not r.json()["has_key"] and r.json()["key_origin"] is None
    assert put(client, op, pid, base_url="https://third.example.test/v1").status_code == 200   # no key stored: nothing to leak


def test_a_stored_key_is_not_sent_to_a_different_origin(client, op, vault_ready, upstream):
    from llmgw.db import connect

    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = upstream.hosts["other.example.test"] = chat_ok()
    with connect() as conn:       # a change that bypasses the API (database write)
        conn.execute("UPDATE provider SET base_url=%s WHERE id=%s", (NEW, pid))
        conn.commit()
    r = ask(client, key, model="gpt")
    assert r.status_code == 502 and "key_origin_mismatch" in r.json()["error"]["message"]
    assert upstream.calls == []
    assert UPSTREAM_KEY not in r.text
    assert client.post(f"/api/providers/{pid}/test", headers=op, json={"model": "m"}).json()["ok"] is False
    assert upstream.calls == []
    # a key with no recorded origin is also refused
    with connect() as conn:
        conn.execute("UPDATE provider SET base_url='https://llm1.example.test/v1', key_origin=NULL WHERE id=%s", (pid,))
        conn.commit()
    assert ask(client, key, model="gpt").status_code == 502 and upstream.calls == []


def test_migration_binds_existing_rows_to_their_current_url(client, op, vault_ready, upstream):
    from llmgw import migrate
    from llmgw.db import connect

    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    sql = (Path(migrate.MIGRATIONS) / "003_provider_key_origin.sql").read_text()
    with connect() as conn:       # the state of a row written before the column existed, then the migration again (idempotent)
        conn.execute("UPDATE provider SET key_origin=NULL WHERE id=%s", (pid,))
        conn.execute(sql)
        conn.execute(sql)
        conn.commit()
    upstream.hosts["llm1.example.test"] = chat_ok(usage={"prompt_tokens": 1, "completion_tokens": 1})
    assert ask(client, key, model="gpt").status_code == 200
    assert row(client, op, pid)["key_origin"] == "https://llm1.example.test/v1"


def test_ssrf_guard_still_applies_to_the_new_url(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    for url in ("http://127.0.0.1/v1", "https://169.254.169.254/v1", "http://other.example.test/v1"):
        r = put(client, op, pid, base_url=url, api_key="sk-x-1234")
        assert r.status_code == 422 and "rejected" in r.json()["detail"], url


def test_the_admin_api_never_returns_the_key_and_errors_redact_it(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    upstream.hosts["llm1.example.test"] = lambda req: chat_ok(content=f"echo {req.headers['authorization']}")
    for r in (client.get("/api/providers", headers=op), client.get(f"/api/providers/{pid}/test", headers=op),
              put(client, op, pid, base_url=NEW), put(client, op, pid, name="x")):
        assert UPSTREAM_KEY not in r.text
    out = client.post(f"/api/providers/{pid}/test", headers=op, json={"model": "m"})
    assert UPSTREAM_KEY not in out.text
    from llmgw.db import connect
    with connect() as conn:
        assert UPSTREAM_KEY not in all_table_text(conn)
