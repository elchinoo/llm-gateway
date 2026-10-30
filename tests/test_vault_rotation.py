"""Vault v2 in the gateway: contexts, rotation of the data key (with and without a platform key), the origin binding, the pinned
provider call, and the admin entry points (API route and CLI)."""
import base64
import secrets

import pytest
from apgkit.vault import VaultError
from cryptography.fernet import Fernet

from tests.conftest import (
    UPSTREAM_KEY,
    add_alias,
    add_client,
    add_provider,
    ask,
    chat_ok,
    needs_db,
)

pytestmark = needs_db

from llmgw import core  # noqa: E402
from llmgw.db import connect  # noqa: E402

PASSWORD = "correct horse battery staple"
ROTATE = "/api/vault/rotate"


def stored(pid):
    with connect() as conn:
        return conn.execute("SELECT api_key_enc, key_origin, api_key_last4, base_url FROM provider WHERE id=%s", (pid,)).fetchone()


def ctx(pid):
    return f"provider.api_key_enc:{pid}"


def serve_one(client, op, upstream, host="llm1.example.test"):
    """Add a provider with a key, route one alias to it, and return (provider id, client key headers)."""
    pid = add_provider(client, op, host=host)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts[host] = chat_ok(usage={"prompt_tokens": 1, "completion_tokens": 1})
    return pid, key


def test_the_provider_key_is_a_v2_token_bound_to_its_row(client, op, vault_ready, upstream):
    p1 = add_provider(client, op, "a", "a.example.test")
    p2 = add_provider(client, op, "b", "b.example.test", key="sk-other-KEY-000000000000")
    one = stored(p1)
    assert one["api_key_enc"].startswith("v2$")
    assert core.vault.decrypt(one["api_key_enc"], ctx(p1)) == UPSTREAM_KEY
    with pytest.raises(VaultError):
        core.vault.decrypt(one["api_key_enc"], ctx(p2))             # moved to another row
    with pytest.raises(VaultError):
        core.vault.decrypt(one["api_key_enc"])                      # no context at all
    # a ciphertext copied over another provider's row (database write access) is useless there: the call is refused, nothing is sent
    with connect() as conn:
        conn.execute("UPDATE provider SET api_key_enc=%s WHERE id=%s", (one["api_key_enc"], p2))
        conn.commit()
    add_alias(client, op, "gpt", [(p2, "m")])
    _, key = add_client(client, op)
    upstream.hosts["b.example.test"] = chat_ok()
    r = ask(client, key, model="gpt")
    assert r.status_code == 502 and "could not be decrypted" in r.text and upstream.calls == []


def test_the_registered_stores_cover_every_encrypted_column():
    names = {s[0] if isinstance(s, tuple) else s for s in core.vault.stores()}
    assert names == {"provider.api_key_enc"}


def test_a_legacy_row_still_decrypts_sends_and_is_upgraded_by_rotation(client, op, vault_ready, upstream):
    pid, key = serve_one(client, op, upstream)
    legacy = Fernet(base64.urlsafe_b64encode(core.vault._data_key)).encrypt(UPSTREAM_KEY.encode()).decode()     # written before contexts
    with connect() as conn:
        conn.execute("UPDATE provider SET api_key_enc=%s WHERE id=%s", (legacy, pid))
        conn.commit()
    assert ask(client, key, model="gpt").status_code == 200
    assert upstream.calls[-1].headers["authorization"] == f"Bearer {UPSTREAM_KEY}"
    r = client.post(ROTATE, headers=op, json={"password": PASSWORD, "confirm": True})
    assert r.status_code == 200 and r.json()["rotated"] == {"provider.api_key_enc": 1}
    assert stored(pid)["api_key_enc"].startswith("v2$")
    assert ask(client, key, model="gpt").status_code == 200


def test_rotation_keeps_the_origin_binding_and_the_key_still_reaches_only_that_origin(client, op, vault_ready, upstream):
    pid, key = serve_one(client, op, upstream)
    before = stored(pid)
    assert ask(client, key, model="gpt").status_code == 200
    r = client.post(ROTATE, headers=op, json={"password": PASSWORD, "confirm": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "rotated": {"provider.api_key_enc": 1}, "restart_other_replicas": True}
    after = stored(pid)
    assert after["api_key_enc"] != before["api_key_enc"] and after["api_key_enc"].startswith("v2$")
    assert (after["key_origin"], after["api_key_last4"], after["base_url"]) == (before["key_origin"], before["api_key_last4"], before["base_url"])
    assert core.vault.decrypt(after["api_key_enc"], ctx(pid)) == UPSTREAM_KEY
    upstream.calls.clear()
    assert ask(client, key, model="gpt").status_code == 200                          # decrypt-to-send works under the new key
    assert [c.headers["authorization"] for c in upstream.calls] == [f"Bearer {UPSTREAM_KEY}"]
    # the binding still works: a URL changed behind the API's back gets nothing
    with connect() as conn:
        conn.execute("UPDATE provider SET base_url='https://other.example.test/v1' WHERE id=%s", (pid,))
        conn.commit()
    upstream.hosts["other.example.test"] = chat_ok()
    upstream.calls.clear()
    bad = ask(client, key, model="gpt")
    assert bad.status_code == 502 and "key_origin_mismatch" in bad.text and upstream.calls == []
    # and the API still refuses to carry the key to another URL without it being entered again
    r = client.put(f"/api/providers/{pid}", headers=op, json={"name": "up1", "base_url": "https://third.example.test/v1"})
    assert r.status_code == 422 and "enter the provider key again" in r.json()["detail"]
    log = client.get("/api/audit", headers=op).text
    assert "vault.rotate" in log and UPSTREAM_KEY not in log


def test_password_only_vault_rotates_the_data_key_and_providers_still_decrypt(client, op, upstream, monkeypatch):
    monkeypatch.delenv("LLMGW_ENCRYPTION_KEY", raising=False)
    with connect() as conn:
        core.local.set_admin_password(conn, PASSWORD)          # no platform key: the password is the only wrap
        assert core.vault.has_password_wrap(conn) and core.vault.status(conn)["auto_unlock"] is False
    pid, key = serve_one(client, op, upstream)
    old_key, old_token = core.vault._data_key, stored(pid)["api_key_enc"]
    r = client.post(ROTATE, headers=op, json={"password": PASSWORD, "confirm": True})
    assert r.status_code == 200
    assert core.vault._data_key != old_key and stored(pid)["api_key_enc"] != old_token
    with connect() as conn:
        assert conn.execute("SELECT 1 FROM app_setting WHERE key LIKE 'vault_%env%' AND value IS NOT NULL AND key NOT LIKE '%kdf%'").fetchone() is None
    core.vault.lock()                                          # a restart: only the password opens it
    assert ask(client, key, model="gpt").status_code == 503
    assert client.post("/api/vault/unlock", headers=op, json={"password": PASSWORD}).status_code == 200
    assert ask(client, key, model="gpt").status_code == 200
    assert upstream.calls[-1].headers["authorization"] == f"Bearer {UPSTREAM_KEY}"


def test_rotation_with_a_platform_key_rewraps_both_and_unlocks_on_restart(client, op, upstream, monkeypatch):
    monkeypatch.setenv("LLMGW_ENCRYPTION_KEY", secrets.token_urlsafe(32))
    with connect() as conn:
        core.local.set_admin_password(conn, PASSWORD)
    pid, key = serve_one(client, op, upstream)
    assert client.post(ROTATE, headers=op, json={"password": PASSWORD, "confirm": True}).status_code == 200
    core.vault.lock()
    with connect() as conn:
        assert core.vault.try_unlock_with_platform_secret(conn)                      # the platform wrap holds the NEW key
    assert ask(client, key, model="gpt").status_code == 200


def test_rotation_needs_confirm_the_password_and_the_permission(client, op, vault_ready, upstream):
    pid, key = serve_one(client, op, upstream)
    before = stored(pid)["api_key_enc"]
    assert client.post(ROTATE, headers=op, json={"password": PASSWORD}).status_code == 422          # no confirm
    assert client.post(ROTATE, headers=op, json={"confirm": True}).status_code == 403                # no password
    wrong = client.post(ROTATE, headers=op, json={"password": "wrong password!!", "confirm": True})
    assert wrong.status_code == 403 and "incorrect" in wrong.json()["detail"]
    assert stored(pid)["api_key_enc"] == before                                                      # nothing changed
    assert ask(client, key, model="gpt").status_code == 200
    assert client.post(ROTATE, json={"password": PASSWORD, "confirm": True}, headers={"X-LLMGW-CSRF": "1"}).status_code == 401
    assert client.post(ROTATE, headers=key, json={"password": PASSWORD, "confirm": True}).status_code in (401, 403)   # a client key is no operator


def test_cli_rotate_vault_requires_confirm_and_rotates(client, op, vault_ready, upstream, monkeypatch, capsys):
    from llmgw import cli

    pid, key = serve_one(client, op, upstream)
    before = stored(pid)["api_key_enc"]
    assert cli.main(["rotate-vault"]) == 2 and stored(pid)["api_key_enc"] == before
    monkeypatch.setenv("LLMGW_ADMIN_PASSWORD", PASSWORD)
    assert cli.main(["rotate-vault", "--confirm"]) == 0
    assert "restart every other replica" in capsys.readouterr().out
    assert stored(pid)["api_key_enc"] != before
    monkeypatch.setenv("LLMGW_ADMIN_PASSWORD", "not the password!!")
    assert cli.main(["rotate-vault", "--confirm"]) == 1
    assert ask(client, key, model="gpt").status_code == 200


# ---- the provider call is pinned to the validated address -----------------------------------------------------------------------------
def test_the_provider_call_goes_to_the_pinned_address_with_host_and_sni(client, op, vault_ready, upstream):
    pid, key = serve_one(client, op, upstream)
    assert ask(client, key, model="gpt").status_code == 200
    call = upstream.calls[-1]
    assert call.url.host == "93.184.216.34" and call.url.path == "/v1/chat/completions"     # the literal validated IP, no second lookup
    assert call.headers["host"] == "llm1.example.test"                                       # the name the provider sees
    assert call.extensions["sni_hostname"] == "llm1.example.test"                            # TLS name and certificate check use it too


def test_a_name_that_rebinds_to_a_private_address_is_refused_before_any_connection(client, op, vault_ready, upstream):
    pid, key = serve_one(client, op, upstream)
    upstream.dns_private = True        # the name now resolves to 10.1.2.3
    r = ask(client, key, model="gpt")
    assert r.status_code == 502 and upstream.calls == []
