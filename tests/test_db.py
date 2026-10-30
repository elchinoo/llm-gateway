"""The whole gateway against a real Postgres, with a mock upstream (httpx.MockTransport) for real providers."""
import json
import logging

import httpx
import pytest

from tests.conftest import (
    CSRF,
    UPSTREAM_KEY,
    add_alias,
    add_client,
    add_provider,
    all_table_text,
    ask,
    bearer,
    chat_ok,
    needs_db,
)

pytestmark = needs_db

from llmgw import core, metrics, migrate  # noqa: E402
from llmgw.db import connect  # noqa: E402


def metric(client, op, series: str) -> float:
    """Value of one exposition line such as `llmgw_failovers_total` or `x_total{a="b"}`; counters are per process, so tests compare deltas."""
    for line in client.get("/metrics", headers=op).text.splitlines():
        if line.startswith(series + " "):
            return float(line.split()[-1])
    return 0.0


def usage_rows(**where):
    with connect() as conn:
        rows = conn.execute("SELECT * FROM usage_event ORDER BY id").fetchall()
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


# ---- the built-in echo demo ---------------------------------------------------------------------------------------
def test_echo_works_out_of_the_box(client, op):
    assert {a["alias"] for a in client.get("/api/models", headers=op).json()["items"]} == {"echo-chat", "echo-embed", "chat-default", "embed-default"}
    _, key = add_client(client, op)
    r = ask(client, key, "hello gateway")
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "echo-chat" and body["choices"][0]["message"]["content"].endswith("hello gateway")
    assert body["usage"]["total_tokens"] == body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"] > 0
    e = client.post("/v1/embeddings", headers=key, json={"model": "echo-embed", "input": ["a", "b"]}).json()
    assert len(e["data"]) == 2 and len(e["data"][0]["embedding"]) == 768
    again = client.post("/v1/embeddings", headers=key, json={"model": "echo-embed", "input": "a"}).json()
    assert again["data"][0]["embedding"] == e["data"][0]["embedding"] != e["data"][1]["embedding"]   # deterministic per input
    assert abs(sum(v * v for v in e["data"][0]["embedding"]) - 1) < 1e-6
    assert client.get("/v1/models", headers=key).json()["data"][0]["id"] in {"echo-chat", "echo-embed", "chat-default", "embed-default"}


def test_embedding_dimension_is_set_on_the_alias(client, op):
    pid = next(p["id"] for p in client.get("/api/providers", headers=op).json()["items"] if p["kind"] == "echo")
    add_alias(client, op, "tiny-embed", [(pid, "e")], kind="embedding", embedding_dim=8)
    _, key = add_client(client, op)
    out = client.post("/v1/embeddings", headers=key, json={"model": "tiny-embed", "input": "x"}).json()
    assert len(out["data"][0]["embedding"]) == 8


def test_the_echo_demo_is_seeded_once(client, op):
    from llmgw import aliases
    for a in ("echo-chat", "echo-embed", "chat-default", "embed-default"):
        assert client.delete(f"/api/models/{a}", headers=op).status_code == 200
    pid = client.get("/api/providers", headers=op).json()["items"][0]["id"]
    assert client.delete(f"/api/providers/{pid}", headers=op).status_code == 200
    with connect() as conn:
        assert aliases.seed_echo(conn) is False


# ---- default aliases and what `ecoctl apply` does with the admin API --------------------------------------------------
def test_the_default_aliases_are_seeded_on_echo_and_work_for_a_new_client(client, op):
    key = add_client(client, op, "trustdesk", allowed_aliases=["chat-default", "embed-default"])[1]
    r = ask(client, key, "ping", model="chat-default")
    assert r.status_code == 200 and r.json()["model"] == "chat-default" and "ping" in r.json()["choices"][0]["message"]["content"]
    e = client.post("/v1/embeddings", headers=key, json={"model": "embed-default", "input": "x", "dimensions": 768}).json()
    assert len(e["data"][0]["embedding"]) == 768                                  # what TrustDesk and PagerLens expect (EMBEDDING_DIM)
    plain = client.post("/v1/embeddings", headers=key, json={"model": "embed-default", "input": "x"}).json()
    assert len(plain["data"][0]["embedding"]) == 768
    assert {m["id"] for m in client.get("/v1/models", headers=key).json()["data"]} == {"chat-default", "embed-default"}


def test_repointing_the_default_alias_changes_every_client_without_touching_them(client, op):
    key = add_client(client, op, "pagerlens", allowed_aliases=["chat-default"])[1]
    pid = next(p["id"] for p in client.get("/api/providers", headers=op).json()["items"] if p["kind"] == "echo")
    add_alias(client, op, "chat-default", [(pid, "echo-v2")])
    assert ask(client, key, "hi", model="chat-default").json()["choices"][0]["message"]["content"].startswith("Echo from echo-v2")


def test_default_alias_seeding_is_idempotent_and_never_overwrites_an_operators_alias(client, op):
    from llmgw import aliases
    pid = next(p["id"] for p in client.get("/api/providers", headers=op).json()["items"] if p["kind"] == "echo")
    add_alias(client, op, "chat-default", [(pid, "operator-choice")], description="mine")
    with connect() as conn:
        assert aliases.seed_echo(conn) is False and aliases.seed_default_aliases(conn) is False      # a restart changes nothing
    got = {a["alias"]: a for a in client.get("/api/models", headers=op).json()["items"]}
    assert got["chat-default"]["targets"][0]["upstream_model"] == "operator-choice" and got["chat-default"]["description"] == "mine"
    # deleted on purpose: stays deleted
    assert client.delete("/api/models/embed-default", headers=op).status_code == 200
    with connect() as conn:
        assert aliases.seed_default_aliases(conn) is False
    assert "embed-default" not in {a["alias"] for a in client.get("/api/models", headers=op).json()["items"]}


def test_a_gateway_seeded_before_the_defaults_existed_gets_them_once_without_touching_anything_else(client, op):
    from apgkit import settings_store as store

    from llmgw import aliases
    with connect() as conn:
        conn.execute("DELETE FROM model_alias WHERE alias IN ('chat-default', 'embed-default')")
        store.delete(conn, "seeded_defaults")                                                       # the state of an older deployment
        conn.commit()
    pid = next(p["id"] for p in client.get("/api/providers", headers=op).json()["items"] if p["kind"] == "echo")
    add_alias(client, op, "echo-chat", [(pid, "tuned")])                                           # an operator's change to an existing alias
    with connect() as conn:
        assert aliases.seed_default_aliases(conn) is True
        assert aliases.seed_default_aliases(conn) is False
    got = {a["alias"]: a for a in client.get("/api/models", headers=op).json()["items"]}
    assert {"chat-default", "embed-default"} <= set(got) and got["echo-chat"]["targets"][0]["upstream_model"] == "tuned"
    assert got["chat-default"]["targets"][0]["provider_kind"] == "echo" and got["embed-default"]["embedding_dim"] == 768


def test_no_echo_provider_means_no_default_aliases_and_no_resurrected_demo(client, op):
    from apgkit import settings_store as store

    from llmgw import aliases
    with connect() as conn:
        conn.execute("TRUNCATE provider RESTART IDENTITY CASCADE")                                                       # cascades to aliases
        store.delete(conn, "seeded_defaults")
        conn.commit()
        assert aliases.seed_default_aliases(conn) is False
        assert conn.execute("SELECT count(*) AS n FROM provider").fetchone()["n"] == 0


def test_default_alias_names_and_embedding_size_are_configurable(client, op, monkeypatch):
    from llmgw import aliases
    monkeypatch.setenv("LLMGW_DEFAULT_CHAT_ALIAS", "smart")
    monkeypatch.setenv("LLMGW_DEFAULT_EMBED_ALIAS", "vectors")
    monkeypatch.setenv("LLMGW_DEFAULT_EMBED_DIM", "1536")
    with connect() as conn:
        conn.execute("TRUNCATE provider, model_alias, app_setting RESTART IDENTITY CASCADE")
        conn.commit()
        assert aliases.seed_echo(conn) is True
    got = {a["alias"]: a for a in client.get("/api/models", headers=op).json()["items"]}
    assert {"smart", "vectors", "echo-chat", "echo-embed"} == set(got) and got["vectors"]["embedding_dim"] == 1536
    monkeypatch.setenv("LLMGW_DEFAULT_CHAT_ALIAS", "not a valid alias!")
    assert aliases.default_alias_names()[0] == "chat-default"


def test_the_static_operator_key_can_do_what_ecoctl_needs(client, op):
    """Create a client per app, list clients, issue a key, find the active key and rotate it when it was lost: all with LLMGW_API_KEY."""
    made = client.post("/api/clients", headers=op, json={"name": "paybroker", "description": "ecoctl", "allowed_aliases": ["chat-default", "embed-default"],
                                                         "rpm_limit": 30, "monthly_token_budget": 1000, "budget_mode": "hard"})
    assert made.status_code == 201
    cid, first = made.json()["client"]["id"], made.json()["key"]["key"]
    assert first.startswith("lg_")
    listed = {c["name"]: c for c in client.get("/api/clients", headers=op).json()["items"]}
    assert listed["paybroker"]["rpm_limit"] == 30 and listed["paybroker"]["monthly_token_budget"] == 1000
    assert client.post("/api/clients", headers=op, json={"name": "paybroker"}).status_code == 422          # a second create is refused, not duplicated
    keys = client.get(f"/api/clients/{cid}/keys", headers=op).json()["items"]
    active = [k for k in keys if not k["revoked"]]
    assert len(active) == 1 and "key_hash" not in active[0] and first not in json.dumps(keys)             # the key is never listed again
    rotated = client.post(f"/api/clients/{cid}/keys/{active[0]['id']}/rotate", headers=op, json={"grace_minutes": 0}).json()
    assert rotated["key"] != first and rotated["key"].startswith("lg_")
    assert ask(client, bearer(first), model="chat-default").status_code == 401                              # the old key stopped at once
    assert ask(client, bearer(rotated["key"]), model="chat-default").status_code == 200


def test_a_budget_stops_one_app_without_affecting_another(client, op):
    a = add_client(client, op, "app-a", allowed_aliases=["chat-default"], monthly_token_budget=5, budget_mode="hard")[1]
    b = add_client(client, op, "app-b", allowed_aliases=["chat-default"], monthly_token_budget=1_000_000)[1]
    assert ask(client, a, "x" * 200, model="chat-default").status_code == 200                      # crosses the 5-token budget
    over = ask(client, a, "again", model="chat-default")
    assert over.status_code == 429 and over.json()["error"]["code"] == "budget_exceeded"
    assert ask(client, b, "hello", model="chat-default").status_code == 200


# ---- the upstream key never leaks -----------------------------------------------------------------------------------
def test_the_upstream_key_never_leaks(client, op, vault_ready, upstream, caplog):
    caplog.set_level(logging.DEBUG)
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "gpt-4o-mini")])
    _, key = add_client(client, op)
    seen = []

    # a 200 whose body echoes the key, then a 400 and a 500 whose error text contains it
    upstream.hosts["llm1.example.test"] = lambda r: chat_ok(content=f"my key is {UPSTREAM_KEY}", usage={"prompt_tokens": 3, "completion_tokens": 4})
    seen.append(ask(client, key, model="gpt"))
    upstream.hosts["llm1.example.test"] = httpx.Response(400, text=f"bad request for key {UPSTREAM_KEY}")
    seen.append(ask(client, key, model="gpt"))
    upstream.hosts["llm1.example.test"] = httpx.Response(500, text=f"boom {UPSTREAM_KEY}")
    seen.append(ask(client, key, model="gpt"))
    upstream.hosts["llm1.example.test"] = httpx.ConnectError(f"cannot connect with {UPSTREAM_KEY}")
    seen.append(ask(client, key, model="gpt"))
    assert [r.status_code for r in seen] == [200, 400, 502, 502]
    assert "***" in seen[0].text and "***" in seen[1].text
    # the key reached the provider (as the Authorization header) but nowhere else
    assert upstream.calls[0].headers["authorization"] == f"Bearer {UPSTREAM_KEY}"
    surfaces = [r.text for r in seen] + [r.text for r in (client.get("/api/providers", headers=op), client.get("/api/models", headers=op),
                                                         client.get("/api/overview", headers=op), client.get("/api/audit", headers=op),
                                                         client.get("/api/usage/recent", headers=op))]
    surfaces.append(client.get("/metrics", headers=op).text)
    surfaces.append(caplog.text)
    with connect() as conn:
        surfaces.append(all_table_text(conn))
        stored = conn.execute("SELECT api_key_enc, api_key_last4 FROM provider WHERE name='up1'").fetchone()
    assert stored["api_key_enc"] and UPSTREAM_KEY not in stored["api_key_enc"] and stored["api_key_last4"] == UPSTREAM_KEY[-4:]
    for text in surfaces:
        assert UPSTREAM_KEY not in text
    assert core.vault.decrypt(stored["api_key_enc"], f"provider.api_key_enc:{pid}") == UPSTREAM_KEY      # it is recoverable by the vault, which is the point


def test_provider_keys_need_the_vault(client, op, upstream):
    r = client.post("/api/providers", headers=op, json={"name": "x", "kind": "openai", "base_url": "https://llm1.example.test/v1", "api_key": "k"})
    assert r.status_code == 422 and "admin password" in r.text


# ---- SSRF -----------------------------------------------------------------------------------------------------------
def test_provider_urls_are_checked_on_save_and_on_every_call(client, op, vault_ready, upstream):
    for url in ("http://127.0.0.1:9/v1", "http://169.254.169.254/latest", "https://user:pw@llm1.example.test/v1", "http://10.0.0.5/v1",
                "https://llm1.example.test/v1?x=1", "ftp://llm1.example.test/", "http://llm1.example.test/v1"):
        r = client.post("/api/providers", headers=op, json={"name": "bad", "kind": "openai", "base_url": url, "api_key": "k"})
        assert r.status_code == 422, url
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok(usage={"prompt_tokens": 1, "completion_tokens": 1})
    assert ask(client, key, model="gpt").status_code == 200
    n = len(upstream.calls)
    upstream.dns_private = True       # the name now resolves to a private address (DNS rebinding after the save)
    r = ask(client, key, model="gpt")
    assert r.status_code == 502 and "rejected" in r.json()["error"]["message"]
    assert len(upstream.calls) == n   # nothing was sent
    assert client.put(f"/api/providers/{pid}", headers=op, json={"name": "up1", "base_url": "http://127.0.0.1/v1"}).status_code == 422


# ---- request shape ----------------------------------------------------------------------------------------------------
def test_unknown_fields_and_streaming_are_refused(client, op):
    _, key = add_client(client, op)
    for extra in ({"stream_options": {}}, {"user": "u1"}, {"logit_bias": {}}, {"provider": "x"}, {"functions": []}):
        r = ask(client, key, **extra)
        assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_field", extra
        assert list(extra)[0] in r.json()["error"]["message"]
    r = ask(client, key, stream=True)
    assert r.status_code == 400 and r.json()["error"]["code"] == "stream_not_supported"
    assert ask(client, key, stream=False).status_code == 200
    assert ask(client, key, n=2).status_code == 400
    assert ask(client, key, n=1).status_code == 200
    assert ask(client, key, max_tokens=0).status_code == 400
    assert ask(client, key, temperature=True).status_code == 400
    bad_msgs = [{"role": "tool", "content": "x"}, {"role": "user", "content": "x", "tool_calls": []},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}]
    for msg in bad_msgs:
        assert client.post("/v1/chat/completions", headers=key, json={"model": "echo-chat", "messages": [msg]}).status_code == 400
    assert client.post("/v1/embeddings", headers=key, json={"model": "echo-embed", "input": "x", "user": "u"}).status_code == 400
    assert client.post("/v1/chat/completions", headers=key, content=b"not json").json()["error"]["code"] == "invalid_json"
    assert client.post("/v1/chat/completions", headers=key, json=[1]).status_code == 400


def test_only_allow_listed_fields_reach_the_provider(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "upstream-x")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok(usage={"prompt_tokens": 1, "completion_tokens": 1})
    r = ask(client, key, model="gpt", temperature=0.2, top_p=0.9, max_tokens=5, stop=["x"], seed=3, presence_penalty=0.1,
            frequency_penalty=0.1, response_format={"type": "json_object"})
    assert r.status_code == 200
    sent = json.loads(upstream.calls[0].content)
    assert sent["model"] == "upstream-x" and set(sent) == {"model", "messages", "temperature", "top_p", "max_tokens", "stop", "seed",
                                                          "presence_penalty", "frequency_penalty", "response_format"}
    assert r.json()["model"] == "gpt"          # the alias, not the upstream's own name


def test_body_cap_and_error_shapes(client, op):
    _, key = add_client(client, op)
    big = {"model": "echo-chat", "messages": [{"role": "user", "content": "x" * (600 * 1024)}]}
    r = client.post("/v1/chat/completions", headers=key, json=big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "request_too_large"
    r = client.post("/v1/chat/completions", headers=key, content=iter([b'{"model":"echo-chat","messages":[', b"x" * (600 * 1024)]))   # chunked
    assert r.status_code == 413
    for r, status, code in ((ask(client, key, model="nope"), 404, "model_not_found"),
                            (ask(client, key, model="echo-embed"), 400, "wrong_model_kind"),
                            (client.post("/v1/chat/completions", json={}), 401, "missing_api_key"),
                            (client.post("/v1/chat/completions", headers=bearer("lg_wrong"), json={}), 401, "invalid_api_key")):
        err = r.json()["error"]
        assert (r.status_code, err["code"]) == (status, code) and set(err) == {"message", "type", "code"}


# ---- who may use which model -----------------------------------------------------------------------------------------------
def test_a_client_only_uses_its_allowed_aliases(client, op):
    _, key = add_client(client, op, "narrow", allowed_aliases=["echo-chat"])
    assert ask(client, key).status_code == 200
    r = client.post("/v1/embeddings", headers=key, json={"model": "echo-embed", "input": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "model_not_allowed"
    assert [m["id"] for m in client.get("/v1/models", headers=key).json()["data"]] == ["echo-chat"]
    assert client.get("/v1/models/echo-embed", headers=key).status_code == 404
    _, wide = add_client(client, op, "wide")
    assert {m["id"] for m in client.get("/v1/models", headers=wide).json()["data"]} == {"echo-chat", "echo-embed", "chat-default", "embed-default"}
    assert client.post("/api/clients", headers=op, json={"name": "typo", "allowed_aliases": ["no-such"]}).status_code == 422


def test_a_revoked_key_stops_working_at_once(client, op):
    cid, key = add_client(client, op)
    assert ask(client, key).status_code == 200
    kid = client.get(f"/api/clients/{cid}/keys", headers=op).json()["items"][0]["id"]
    assert client.delete(f"/api/clients/{cid}/keys/{kid}", headers=op).status_code == 200
    r = ask(client, key)
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
    assert client.delete(f"/api/clients/{cid}/keys/{kid}", headers=op).status_code == 404


def test_rotation_with_a_grace_period_and_a_disabled_client(client, op):
    cid, old = add_client(client, op)
    kid = client.get(f"/api/clients/{cid}/keys", headers=op).json()["items"][0]["id"]
    r = client.post(f"/api/clients/{cid}/keys/{kid}/rotate", headers=op, json={"grace_minutes": 10})
    assert r.status_code == 201
    new = bearer(r.json()["key"])
    assert ask(client, new).status_code == 200 and ask(client, old).status_code == 200      # overlap
    with connect() as conn:
        conn.execute("UPDATE client_key SET revoked_at = now() - interval '1 second' WHERE id=%s", (kid,))
        conn.commit()
    assert ask(client, old).status_code == 401 and ask(client, new).status_code == 200
    assert client.patch(f"/api/clients/{cid}", headers=op, json={"enabled": False}).status_code == 200
    r = ask(client, new)
    assert r.status_code == 403 and r.json()["error"]["code"] == "client_disabled"
    assert client.get("/v1/models", headers=new).json()["data"] == []
    assert client.delete(f"/api/clients/{cid}", headers=op).status_code == 200
    assert ask(client, new).status_code == 401           # deleting a client deletes its keys


# ---- limits ------------------------------------------------------------------------------------------------------------------
def test_requests_per_minute_limit(client, op):
    _, a = add_client(client, op, "alpha", rpm_limit=2)
    _, b = add_client(client, op, "beta", rpm_limit=2)
    assert ask(client, a).status_code == 200 and ask(client, a).status_code == 200
    r = ask(client, a)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limit_exceeded" and 1 <= int(r.headers["Retry-After"]) <= 60
    assert ask(client, b).status_code == 200                      # per client
    _, free = add_client(client, op, "free", unlimited_rpm=True)
    assert all(ask(client, free).status_code == 200 for _ in range(5))


def test_monthly_budget_hard_and_soft(client, op):
    cid, hard = add_client(client, op, "hardy", monthly_token_budget=40, budget_mode="hard")
    ok = ask(client, hard, "x" * 120)
    assert ok.status_code == 200 and ok.json()["usage"]["total_tokens"] >= 40
    r = ask(client, hard)
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_exceeded" and int(r.headers["Retry-After"]) > 0
    assert usage_rows(client_name="hardy")[-1]["status"] == 429
    assert metric(client, op, 'llmgw_budget_rejections_total{client="hardy"}') == 1.0
    # last month's usage does not count
    _, other = add_client(client, op, "monthly", monthly_token_budget=40)
    with connect() as conn:
        cid2 = conn.execute("SELECT id FROM client WHERE name='monthly'").fetchone()["id"]
        conn.execute("INSERT INTO usage_event (client_id, client_name, endpoint, total_tokens, status, created_at) "
                     "VALUES (%s,'monthly','chat/completions',1000000,200, date_trunc('month', now() at time zone 'utc') - interval '1 day')", (cid2,))
        conn.commit()
    assert ask(client, other).status_code == 200
    # soft: allowed, flagged, and shown in the dashboard banner
    _, soft = add_client(client, op, "softy", monthly_token_budget=10, budget_mode="soft")
    assert ask(client, soft, "x" * 100).status_code == 200
    assert ask(client, soft).status_code == 200
    rows = usage_rows(client_name="softy")
    assert [r["over_budget"] for r in rows] == [False, True]
    o = client.get("/api/overview", headers=op).json()
    assert "softy" in o["budget_alerts"]["soft"] and "hardy" in o["budget_alerts"]["hard"]
    clients_view = {c["name"]: c for c in o["clients"]}
    assert clients_view["softy"]["over_budget"] and clients_view["softy"]["budget_used"] > 1


# ---- failover ----------------------------------------------------------------------------------------------------------------
def three_targets(client, op):
    ids = [add_provider(client, op, f"p{i}", f"llm{i}.example.test", f"key-{i}-secret") for i in (1, 2, 3)]
    add_alias(client, op, "gpt", [(ids[0], "m1"), (ids[1], "m2"), (ids[2], "m3")])
    return add_client(client, op)[1]


def test_failover_follows_the_order_on_5xx_429_and_connection_trouble(client, op, vault_ready, upstream):
    key = three_targets(client, op)
    failovers = metric(client, op, "llmgw_failovers_total")
    upstream.hosts.update({"llm1.example.test": httpx.Response(503, text="down"), "llm2.example.test": httpx.Response(429, headers={"Retry-After": "3"}),
                           "llm3.example.test": chat_ok(usage={"prompt_tokens": 5, "completion_tokens": 6})})
    r = ask(client, key, model="gpt")
    assert r.status_code == 200 and upstream.hosts_called() == ["llm1.example.test", "llm2.example.test", "llm3.example.test"]
    row = usage_rows()[-1]
    assert (row["provider_name"], row["upstream_model"], row["attempts"], row["total_tokens"]) == ("p3", "m3", 3, 11)
    assert metric(client, op, "llmgw_failovers_total") - failovers == 2
    upstream.calls.clear()
    upstream.hosts["llm1.example.test"] = httpx.ConnectError("refused")
    upstream.hosts["llm2.example.test"] = httpx.ReadTimeout("slow")
    assert ask(client, key, model="gpt").status_code == 200 and len(upstream.calls) == 3
    # everything down: 502 with the last error, one row, three attempts, no tokens, no cost
    upstream.hosts["llm3.example.test"] = httpx.Response(500, text="nope")
    r = ask(client, key, model="gpt")
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unavailable"
    row = usage_rows()[-1]
    assert (row["status"], row["attempts"], row["total_tokens"], float(row["cost_usd"]), row["error_class"]) == (502, 3, 0, 0.0, "upstream_unavailable")


def test_no_failover_on_a_client_error(client, op, vault_ready, upstream):
    key = three_targets(client, op)
    upstream.hosts.update({"llm1.example.test": httpx.Response(400, text="context too long"), "llm2.example.test": chat_ok(), "llm3.example.test": chat_ok()})
    r = ask(client, key, model="gpt")
    assert r.status_code == 400 and r.json()["error"]["code"] == "upstream_rejected" and "context too long" in r.json()["error"]["message"]
    assert upstream.hosts_called() == ["llm1.example.test"]
    upstream.calls.clear()
    upstream.hosts["llm1.example.test"] = httpx.Response(401, text="invalid api key")     # our provider key is wrong: not the caller's fault
    r = ask(client, key, model="gpt")
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_misconfigured" and upstream.hosts_called() == ["llm1.example.test"]
    upstream.calls.clear()
    upstream.hosts["llm1.example.test"] = httpx.Response(200, text="<html>not json</html>")     # a malformed answer is a provider fault: fail over
    assert ask(client, key, model="gpt").status_code == 200 and len(upstream.calls) == 2


def test_all_targets_rate_limited_is_a_429(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = httpx.Response(429, headers={"Retry-After": "7"})
    r = ask(client, key, model="gpt")
    assert r.status_code == 429 and r.json()["error"]["code"] == "upstream_rate_limited" and r.headers["Retry-After"] == "7"


# ---- usage math -----------------------------------------------------------------------------------------------------------------
def test_usage_math_cost_and_estimates(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")], price_prompt_per_m=2.0, price_completion_per_m=10.0)
    add_alias(client, op, "emb", [(pid, "e")], kind="embedding", price_prompt_per_m=1.0)
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok(content="y" * 40, usage={"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500})
    assert ask(client, key, model="gpt").status_code == 200
    row = usage_rows()[-1]
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"], row["estimated"]) == (1000, 500, 1500, False)
    assert float(row["cost_usd"]) == pytest.approx(0.007)             # 1000/1e6*2 + 500/1e6*10
    # no usage block: chars / 4, flagged
    upstream.hosts["llm1.example.test"] = chat_ok(content="y" * 40)
    ask(client, key, "x" * 101, model="gpt")
    row = usage_rows()[-1]
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"], row["estimated"]) == (26, 10, 36, True)
    # a nonsense usage block is not trusted either
    upstream.hosts["llm1.example.test"] = chat_ok(content="y" * 8, usage={"prompt_tokens": -5, "completion_tokens": "many"})
    ask(client, key, "x" * 8, model="gpt")
    assert usage_rows()[-1]["estimated"] is True
    # embeddings: prompt tokens only
    upstream.hosts["llm1.example.test"] = httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}], "usage": {"prompt_tokens": 7, "total_tokens": 7}})
    assert client.post("/v1/embeddings", headers=key, json={"model": "emb", "input": "hello"}).status_code == 200
    row = usage_rows()[-1]
    assert (row["endpoint"], row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == ("embeddings", 7, 0, 7)
    assert float(row["cost_usd"]) == pytest.approx(0.000007)
    # the aggregate views and the CSV agree with the rows
    by_client = client.get("/api/usage?group=client", headers=op).json()["items"][0]
    assert by_client["requests"] == 4 and by_client["estimated_requests"] == 2 and by_client["total_tokens"] == 1500 + 36 + 4 + 7
    csv_text = client.get("/api/usage.csv?group=alias", headers=op)
    assert csv_text.headers["content-type"].startswith("text/csv") and csv_text.text.splitlines()[0].startswith("alias,requests")
    assert {i["key"] for i in client.get("/api/usage?group=provider", headers=op).json()["items"]} == {"up1"}
    assert client.get("/api/usage?group=day", headers=op).json()["items"][0]["requests"] == 4


def test_tokens_and_requests_appear_in_metrics(client, op):
    _, key = add_client(client, op)
    series = 'llmgw_requests_total{alias="echo-chat",client="trustdesk",status="200"}'
    before = metric(client, op, series)
    r = ask(client, key)
    assert metric(client, op, series) - before == 1
    assert metric(client, op, 'llmgw_tokens_total{alias="echo-chat",client="trustdesk",kind="prompt"}') >= r.json()["usage"]["prompt_tokens"]
    assert metric(client, op, 'llmgw_tokens_total{alias="echo-chat",client="trustdesk",kind="completion"}') >= r.json()["usage"]["completion_tokens"]
    text = client.get("/metrics", headers=op).text
    for needle in ("http_requests_total", "db_up", "app_info", "llmgw_upstream_latency_seconds", "llmgw_failovers_total"):
        assert needle in text, needle
    ask(client, key, model="not-a-model-" + "z" * 50)             # an unknown alias must not become a label value
    assert "not-a-model" not in client.get("/metrics", headers=op).text


# ---- tool calling ------------------------------------------------------------------------------------------------------
TOOL = {"type": "function", "function": {"name": "lookup", "description": "find", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}
SECRET_ARG = "ARGUMENT-MARKER-91c2"


def tool_reply(**message):
    msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_9", "type": "function",
           "function": {"name": "lookup", "arguments": json.dumps({"q": SECRET_ARG})}}], **message}
    return httpx.Response(200, json={"id": "x", "object": "chat.completion", "model": "up",
                                     "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}]})


def test_tool_calls_are_forwarded_unchanged_and_answered_faithfully(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "upstream-x")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = tool_reply()
    convo = [{"role": "system", "content": "s"}, {"role": "user", "content": "find it"}]
    extra = {"tools": [TOOL], "tool_choice": {"type": "function", "function": {"name": "lookup"}}, "parallel_tool_calls": False}
    r = client.post("/v1/chat/completions", headers=key, json={"model": "gpt", "messages": convo, "max_tokens": 50, **extra})
    assert r.status_code == 200, r.text
    sent = json.loads(upstream.calls[0].content)
    assert sent["tools"] == [TOOL] and sent["tool_choice"] == extra["tool_choice"] and sent["parallel_tool_calls"] is False
    assert sent["messages"] == convo and sent["model"] == "upstream-x"
    ch = r.json()["choices"][0]
    assert ch["finish_reason"] == "tool_calls" and ch["message"]["content"] is None and r.json()["model"] == "gpt"
    (tc,) = ch["message"]["tool_calls"]
    assert tc == {"id": "call_9", "type": "function", "function": {"name": "lookup", "arguments": json.dumps({"q": SECRET_ARG})}}
    assert "x-llmgw-note" not in r.headers
    # second turn: the assistant's tool_calls (null content) and the tool result go back verbatim
    convo += [ch["message"], {"role": "tool", "tool_call_id": "call_9", "content": "RESULT-MARKER-77"}]
    upstream.hosts["llm1.example.test"] = chat_ok("the answer", usage={"prompt_tokens": 30, "completion_tokens": 4})
    r2 = client.post("/v1/chat/completions", headers=key, json={"model": "gpt", "messages": convo, "tools": [TOOL]})
    assert r2.status_code == 200 and r2.json()["choices"][0]["message"]["content"] == "the answer"
    assert json.loads(upstream.calls[1].content)["messages"] == convo
    with connect() as conn:
        rows = conn.execute("SELECT prompt_tokens, completion_tokens, estimated FROM usage_event ORDER BY id").fetchall()
        text = all_table_text(conn)
    assert rows[0]["estimated"] is True and rows[0]["completion_tokens"] == -(-(len("lookup") + len(json.dumps({"q": SECRET_ARG}))) // 4)
    assert rows[0]["prompt_tokens"] >= -(-len(json.dumps([TOOL], separators=(",", ":"))) // 4)    # the schema is in the estimate
    assert rows[1]["prompt_tokens"] == 30 and rows[1]["estimated"] is False
    assert SECRET_ARG not in text and "RESULT-MARKER" not in text and "find it" not in text and "call_9" not in text
    assert SECRET_ARG not in client.get("/api/usage/recent", headers=op).text


def test_tool_requests_are_refused_by_the_same_rules_as_before(client, op):
    _, key = add_client(client, op)
    base = {"model": "echo-chat", "messages": [{"role": "user", "content": "x"}]}
    def post(**extra):
        return client.post("/v1/chat/completions", headers=key, json={**base, **extra})
    r = post(tools=[TOOL], stream=True)
    assert r.status_code == 400 and r.json()["error"]["code"] == "stream_not_supported"
    for extra, code in (({"tools": []}, "invalid_tools"), ({"tools": [{"type": "retrieval"}]}, "invalid_tools"),
                        ({"tools": [TOOL], "tool_choice": "maybe"}, "invalid_tool_choice"),
                        ({"tools": [TOOL], "parallel_tool_calls": 1}, "invalid_parallel_tool_calls"),
                        ({"tools": [TOOL], "functions": []}, "unknown_field"), ({"tools": [TOOL], "user": "u"}, "unknown_field")):
        r = post(**extra)
        assert r.status_code == 400 and r.json()["error"]["code"] == code, (extra, r.text)
    r = post(messages=[{"role": "tool", "tool_call_id": "c", "content": "x"}])
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_messages"
    big = {"model": "echo-chat", "messages": base["messages"], "tools": [TOOL], "pad": "x" * (600 * 1024)}
    assert client.post("/v1/chat/completions", headers=key, json=big).status_code == 413


def test_tool_requests_keep_budget_and_rate_limits(client, op):
    _, key = add_client(client, op, monthly_token_budget=1, budget_mode="hard")
    body = {"model": "echo-chat", "messages": [{"role": "user", "content": "x"}], "tools": [TOOL]}
    assert client.post("/v1/chat/completions", headers=key, json=body).status_code == 200      # used up by the first call
    r = client.post("/v1/chat/completions", headers=key, json=body)
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_exceeded"
    _, key2 = add_client(client, op, name="slow", rpm_limit=1)
    assert client.post("/v1/chat/completions", headers=key2, json=body).status_code == 200
    assert client.post("/v1/chat/completions", headers=key2, json=body).status_code == 429


def test_echo_with_tools_is_plain_text_and_the_call_directive_round_trips(client, op):
    _, key = add_client(client, op)
    r = ask(client, key, "what is slow?", tools=[TOOL])
    assert r.status_code == 200 and r.json()["choices"][0]["finish_reason"] == "stop"
    assert r.json()["choices"][0]["message"]["content"] == "Echo from echo-1: what is slow?" and "tool_calls" not in r.json()["choices"][0]["message"]
    r = ask(client, key, 'call:lookup {"q": "a"}', tools=[TOOL])
    ch = r.json()["choices"][0]
    assert ch["finish_reason"] == "tool_calls" and ch["message"]["content"] is None
    tc = ch["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "lookup" and json.loads(tc["function"]["arguments"]) == {"q": "a"}
    msgs_ = [{"role": "user", "content": 'call:lookup {"q": "a"}'}, ch["message"], {"role": "tool", "tool_call_id": tc["id"], "content": "42"}]
    r2 = client.post("/v1/chat/completions", headers=key, json={"model": "echo-chat", "messages": msgs_, "tools": [TOOL]})
    assert r2.status_code == 200 and r2.json()["choices"][0]["finish_reason"] == "stop"
    assert r2.json()["usage"]["prompt_tokens"] > r.json()["usage"]["prompt_tokens"] - 1


def test_a_thinking_model_that_calls_a_tool_is_not_flagged_as_empty(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = tool_reply(reasoning="hmm")
    r = ask(client, key, model="gpt", tools=[TOOL])
    assert r.status_code == 200 and "x-llmgw-note" not in r.headers


# ---- no content is ever stored -----------------------------------------------------------------------------------------------
def test_prompts_completions_and_embedding_inputs_are_never_stored(client, op, vault_ready, upstream):
    marker = "ZEBRA-MARKER-7f3a9c"
    reply = "COMPLETION-MARKER-55e1"
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok(content=reply, usage={"prompt_tokens": 2, "completion_tokens": 2})
    assert ask(client, key, f"tell me about {marker}", model="gpt").status_code == 200
    assert ask(client, key, f"and also {marker}").status_code == 200                                 # echo
    assert client.post("/v1/embeddings", headers=key, json={"model": "echo-embed", "input": f"embed {marker}"}).status_code == 200
    ask(client, key, f"rejected {marker}", model="gpt", tools=[])                                    # refused requests too
    ask(client, key, f"unknown model {marker}", model="nonexistent")
    upstream.hosts["llm1.example.test"] = httpx.Response(500, text=f"error mentioning {marker}")
    ask(client, key, f"failing {marker}", model="gpt")
    with connect() as conn:
        text = all_table_text(conn)
        assert conn.execute("SELECT count(*) AS n FROM usage_event").fetchone()["n"] == 6
    assert marker not in text and reply not in text
    assert "ZEBRA" not in client.get("/api/usage/recent", headers=op).text


# ---- credentials never cross -------------------------------------------------------------------------------------------------------
def test_operator_credentials_are_refused_on_the_data_plane_and_client_keys_on_the_api(client, op):
    cid, key = add_client(client, op)
    token = client.post("/api/tokens", headers=op, json={"name": "t", "scope": "full"}).json()["token"]
    assert token.startswith("lgt_")
    body = {"model": "echo-chat", "messages": [{"role": "user", "content": "hi"}]}
    for who in (op, bearer(token)):
        r = client.post("/v1/chat/completions", headers=who, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "wrong_credential"
        assert client.get("/v1/models", headers=who).status_code == 403
    # a signed-in browser session (the local admin holds '*') does not unlock /v1 either
    from llmgw import core as c
    with connect() as conn:
        c.local.set_admin_password(conn, "correct horse battery staple")
    assert client.post("/api/login", headers=CSRF, json={"password": "correct horse battery staple"}).status_code == 200
    assert client.get("/api/clients").status_code == 200
    assert client.post("/v1/chat/completions", json=body).status_code == 401           # cookies are not credentials on /v1
    client.cookies.clear()
    # a client key on the operator API
    for path in ("/api/clients", "/api/overview", "/api/providers", "/api/audit", "/api/tokens", "/api/settings", "/metrics"):
        assert client.get(path, headers=key).status_code == 403, path
    assert client.post("/api/clients", headers=key, json={"name": "evil"}).status_code == 403
    assert client.get("/api/session", headers=key).json()["state"] == "logged_out"
    assert ask(client, key).status_code == 200


def test_local_token_scopes_keep_their_boundaries(client, op):
    def mk(scope):
        return bearer(client.post("/api/tokens", headers=op, json={"name": scope, "scope": scope}).json()["token"])
    read, metrics_tok = mk("read"), mk("metrics")
    assert client.get("/api/overview", headers=read).status_code == 200 and client.get("/api/clients", headers=read).status_code == 200
    assert client.post("/api/clients", headers=read, json={"name": "x"}).status_code == 403
    assert client.get("/api/audit", headers=read).status_code == 403
    assert client.get("/api/tokens", headers=read).status_code == 403
    assert client.get("/metrics", headers=read).status_code == 403
    assert client.get("/metrics", headers=metrics_tok).status_code == 200 and client.get("/api/overview", headers=metrics_tok).status_code == 403
    assert client.post("/api/clients", headers=op, json={"name": "x"}).status_code == 201


def test_browser_writes_need_the_csrf_header(client, op):
    from llmgw import core as c
    with connect() as conn:
        c.local.set_admin_password(conn, "correct horse battery staple")
    assert client.post("/api/login", headers=CSRF, json={"password": "correct horse battery staple"}).status_code == 200
    assert client.post("/api/clients", json={"name": "csrf"}).status_code == 403
    assert client.post("/api/clients", headers=CSRF, json={"name": "csrf"}).status_code == 201


# ---- the vault ---------------------------------------------------------------------------------------------------------------------------
def test_a_locked_vault_blocks_real_providers_but_not_echo(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    echo = next(p["id"] for p in client.get("/api/providers", headers=op).json()["items"] if p["kind"] == "echo")
    add_alias(client, op, "gpt-or-echo", [(pid, "m"), (echo, "fallback")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok(usage={"prompt_tokens": 1, "completion_tokens": 1})
    assert ask(client, key, model="gpt").status_code == 200
    core.vault.lock()
    r = ask(client, key, model="gpt")
    assert r.status_code == 503 and r.json()["error"]["code"] == "vault_locked"
    assert ask(client, key).status_code == 200                                    # echo-chat
    assert ask(client, key, model="gpt-or-echo").json()["choices"][0]["message"]["content"].startswith("Echo from fallback")
    n = len(upstream.calls)
    assert n == 1                                                               # nothing was sent while locked
    assert client.post("/api/providers", headers=op, json={"name": "y", "kind": "openai", "base_url": "https://llm1.example.test/v1", "api_key": "k"}).status_code == 423
    assert client.post("/api/vault/unlock", headers=op, json={"password": "wrong password"}).status_code == 403
    assert client.post("/api/vault/unlock", headers=op, json={"password": "correct horse battery staple"}).status_code == 200
    assert ask(client, key, model="gpt").status_code == 200


def test_provider_test_button_makes_one_tiny_call(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok()
    r = client.post(f"/api/providers/{pid}/test", headers=op, json={"model": "gpt-4o-mini"}).json()
    assert r["ok"] is True
    sent = json.loads(upstream.calls[0].content)
    assert sent["max_tokens"] == 1 and sent["model"] == "gpt-4o-mini" and len(upstream.calls) == 1
    upstream.hosts["llm1.example.test"] = httpx.Response(401, text=f"bad key {UPSTREAM_KEY}")
    r = client.post(f"/api/providers/{pid}/test", headers=op, json={"model": "m"}).json()
    assert r["ok"] is False and UPSTREAM_KEY not in json.dumps(r)
    assert usage_rows() == []                                                    # tests are not metered
    echo = next(p["id"] for p in client.get("/api/providers", headers=op).json()["items"] if p["kind"] == "echo")
    assert client.post(f"/api/providers/{echo}/test", headers=op, json={"model": "e"}).json()["ok"] is True


# ---- control plane details --------------------------------------------------------------------------------------------------------------------
def test_audit_trail_records_who_and_what_but_never_secrets(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    cid, key = add_client(client, op, monthly_token_budget=1000)
    client.patch(f"/api/clients/{cid}", headers=op, json={"rpm_limit": 5})
    kid = client.get(f"/api/clients/{cid}/keys", headers=op).json()["items"][0]["id"]
    new = client.post(f"/api/clients/{cid}/keys/{kid}/rotate", headers=op, json={}).json()
    client.put("/api/settings", headers=op, json={"default_rpm": 30})
    items = client.get("/api/audit", headers=op).json()["items"]
    actions = [i["action"] for i in items]
    for a in ("provider.create", "model.create", "client.create", "key.issue", "client.update", "key.rotate", "settings.update"):
        assert a in actions, a
    assert all(i["actor"] == "api key" for i in items)
    dump = json.dumps(items, default=str)
    assert UPSTREAM_KEY not in dump and new["key"] not in dump and key["Authorization"][7:] not in dump
    created = next(i for i in items if i["action"] == "provider.create")
    assert created["detail"]["has_key"] is True and "api_key" not in created["detail"]


def test_providers_in_use_cannot_be_deleted_and_validation(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "gpt", [(pid, "m")])
    assert client.delete(f"/api/providers/{pid}", headers=op).status_code == 409
    assert client.post("/api/providers", headers=op, json={"name": "UP1", "kind": "openai", "base_url": "https://llm2.example.test/v1"}).status_code == 422   # duplicate name
    assert client.put("/api/models/bad%20alias", headers=op, json={"kind": "chat", "targets": [{"provider_id": pid, "upstream_model": "m"}]}).status_code == 422
    assert client.put("/api/models/x", headers=op, json={"kind": "chat", "targets": [{"provider_id": 9999, "upstream_model": "m"}]}).status_code == 422
    assert client.put("/api/models/x", headers=op, json={"kind": "chat", "targets": []}).status_code == 422
    assert client.post("/api/clients", headers=op, json={"name": "Bad Name"}).status_code == 422
    assert client.post("/api/clients", headers=op, json={"name": "a"}).status_code == 201
    assert client.post("/api/clients", headers=op, json={"name": "a"}).status_code == 422
    assert client.patch("/api/clients/1", headers=op, json={"enabled": None}).status_code == 422
    assert client.put("/api/settings", headers=op, json={"usage_retention_days": 7}).status_code == 422
    assert client.put("/api/settings", headers=op, json={"default_rpm": 10}).json()["default_rpm"] == 10
    assert client.post("/api/clients", headers=op, json={"name": "b"}).json()["client"]["rpm_limit"] == 10      # the new default applies
    assert client.post("/api/clients", headers=op, json={"name": "c", "extra": 1}).status_code == 422


def test_a_disabled_alias_or_provider_is_not_served(client, op):
    _, key = add_client(client, op)
    pid = client.get("/api/providers", headers=op).json()["items"][0]["id"]
    assert client.put("/api/models/echo-chat", headers=op, json={"kind": "chat", "enabled": False, "targets": [{"provider_id": pid, "upstream_model": "e"}]}).status_code == 200
    assert ask(client, key).status_code == 404
    assert client.put("/api/models/echo-chat", headers=op, json={"kind": "chat", "targets": [{"provider_id": pid, "upstream_model": "e"}]}).status_code == 200
    assert client.put(f"/api/providers/{pid}", headers=op, json={"name": "echo", "enabled": False}).status_code == 200
    r = ask(client, key)
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_available_target"


def test_session_and_setup_flow(client):
    s = client.get("/api/session").json()
    assert s["state"] == "setup_required" and s["sso"] is None
    code = core.local.setup_code()
    assert client.post("/api/setup", headers=CSRF, json={"code": "WRONG-CODE-0000", "password": "correct horse battery staple"}).status_code == 403
    assert client.post("/api/setup", headers=CSRF, json={"code": code, "password": "short"}).status_code == 422
    assert client.post("/api/setup", headers=CSRF, json={"code": code, "password": "correct horse battery staple"}).status_code == 200
    s = client.get("/api/session").json()
    assert s["state"] == "logged_in" and s["user"]["permissions"] == ["*"] and s["vault"]["unlocked"]
    assert "lg_session" in client.cookies


# ---- database ------------------------------------------------------------------------------------------------------------------------------
def test_migrations_are_idempotent(client):
    with connect() as conn:
        before = conn.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public'").fetchone()["n"]
    assert migrate.run() == [] and migrate.run() == []
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public'").fetchone()["n"] == before
        # the SQL itself is also safe to run again for the shared tables
        from apgkit.auth.schema import SQL
        conn.execute(SQL)
        conn.commit()
    assert metrics is not None


# ---- extra_body (thinking switches), reasoning tokens, reasoning-only replies -------------------------------------------------
def put_alias(client, op, alias, targets, kind="chat"):
    return client.put(f"/api/models/{alias}", headers=op, json={"kind": kind, "targets": targets})


def body_of(upstream, i=-1):
    return json.loads(upstream.calls[i].content)


def test_extra_body_roundtrip_and_validation_over_the_api(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    eb = {"reasoning": {"enabled": False}}
    r = put_alias(client, op, "qwen", [{"provider_id": pid, "upstream_model": "qwen/q", "extra_body": eb}, {"provider_id": pid, "upstream_model": "b"}])
    assert r.status_code == 200 and [t["extra_body"] for t in r.json()["targets"]] == [eb, {}]
    got = next(a for a in client.get("/api/models", headers=op).json()["items"] if a["alias"] == "qwen")
    assert [t["extra_body"] for t in got["targets"]] == [eb, {}]
    # replacing without the field clears it
    r = put_alias(client, op, "qwen", [{"provider_id": pid, "upstream_model": "qwen/q"}])
    assert r.json()["targets"][0]["extra_body"] == {}
    put_alias(client, op, "qwen", [{"provider_id": pid, "upstream_model": "qwen/q", "extra_body": eb}])
    for bad in ({"model": "x"}, {"messages": []}, {"input": "x"}, {"stream": True}, {"user": "u"}, {"n": 2}, [], "str", 3, {"a": {"b": {"c": {"d": {"e": 1}}}}},
                {"v": "x" * 5000}):
        r = put_alias(client, op, "qwen", [{"provider_id": pid, "upstream_model": "m", "extra_body": bad}])
        assert r.status_code == 422, bad
    # nothing was saved by the rejected attempts
    got = next(a for a in client.get("/api/models", headers=op).json()["items"] if a["alias"] == "qwen")
    assert got["targets"][0]["extra_body"] == eb and got["targets"][0]["upstream_model"] == "qwen/q"


def test_extra_body_reaches_the_upstream_and_wins_over_the_client(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "x", [(pid, "m")])     # no extra_body: nothing added
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok()
    ask(client, key, model="x", max_tokens=8)
    assert body_of(upstream) == {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8, "model": "m"}
    r = put_alias(client, op, "x", [{"provider_id": pid, "upstream_model": "m",
                                     "extra_body": {"reasoning": {"enabled": False}, "max_tokens": 900, "temperature": 0}}])
    assert r.status_code == 200
    assert ask(client, key, model="x", max_tokens=8, temperature=1.0).status_code == 200
    sent = body_of(upstream)
    assert sent["reasoning"] == {"enabled": False} and sent["max_tokens"] == 900 and sent["temperature"] == 0     # operator wins
    assert sent["model"] == "m" and sent["messages"] == [{"role": "user", "content": "hi"}]
    # a different target of the same alias gets its own fields
    p2 = add_provider(client, op, name="up2", host="llm2.example.test")
    upstream.hosts["llm2.example.test"] = chat_ok()
    put_alias(client, op, "x", [{"provider_id": pid, "upstream_model": "m", "extra_body": {"reasoning": {"enabled": False}}},
                                {"provider_id": p2, "upstream_model": "n", "extra_body": {"reasoning_effort": "low"}}])
    upstream.hosts["llm1.example.test"] = httpx.Response(503, text="down")
    assert ask(client, key, model="x").status_code == 200
    first, second = body_of(upstream, -2), body_of(upstream, -1)
    assert "reasoning_effort" not in first and first["reasoning"] == {"enabled": False}
    assert second["reasoning_effort"] == "low" and "reasoning" not in second and second["model"] == "n"
    assert UPSTREAM_KEY not in json.dumps([body_of(upstream, i) for i in range(len(upstream.calls))])


def test_extra_body_on_embeddings_is_allowed_but_reserved_keys_stay_blocked(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    r = put_alias(client, op, "emb", [{"provider_id": pid, "upstream_model": "e", "extra_body": {"truncate": "END", "input": "x"}}], kind="embedding")
    assert r.status_code == 422
    r = put_alias(client, op, "emb", [{"provider_id": pid, "upstream_model": "e", "extra_body": {"truncate": "END", "input_type": "query"}}], kind="embedding")
    assert r.status_code == 200
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}], "usage": {"prompt_tokens": 2, "total_tokens": 2}})
    assert client.post("/v1/embeddings", headers=key, json={"model": "emb", "input": "hello"}).status_code == 200
    assert body_of(upstream) == {"input": "hello", "truncate": "END", "input_type": "query", "model": "e"}


def test_echo_provider_ignores_extra_body_end_to_end(client, op):
    pid = next(p["id"] for p in client.get("/api/providers", headers=op).json()["items"] if p["kind"] == "echo")
    _, key = add_client(client, op)
    plain = ask(client, key, "same text", model="echo-chat").json()
    assert put_alias(client, op, "echo2", [{"provider_id": pid, "upstream_model": "echo-1", "extra_body": {"reasoning": {"enabled": False}}}]).status_code == 200
    out = ask(client, key, "same text", model="echo2").json()
    assert out["choices"] == plain["choices"] and out["usage"] == plain["usage"]


def test_extra_body_audit_entry_and_never_secrets(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    eb = {"reasoning": {"enabled": False}}
    put_alias(client, op, "qwen", [{"provider_id": pid, "upstream_model": "m", "extra_body": eb}])
    put_alias(client, op, "qwen", [{"provider_id": pid, "upstream_model": "m", "extra_body": eb}])     # unchanged
    put_alias(client, op, "qwen", [{"provider_id": pid, "upstream_model": "m"}])                       # cleared
    put_alias(client, op, "plain", [{"provider_id": pid, "upstream_model": "m"}])                      # never had any
    items = client.get("/api/audit", headers=op).json()["items"]
    mine = [i for i in items if i["action"].startswith("model.") and i["target"] == "qwen"][::-1]
    assert [i["action"] for i in mine] == ["model.create", "model.update", "model.update"]
    assert mine[0]["detail"]["extra_body"] == {"1": eb} and mine[0]["detail"]["extra_body_changed"] is True
    assert mine[1]["detail"]["extra_body_changed"] is False
    assert mine[2]["detail"]["extra_body"] == {} and mine[2]["detail"]["extra_body_changed"] is True
    plain = next(i for i in items if i["target"] == "plain")
    assert "extra_body" not in plain["detail"]
    assert UPSTREAM_KEY not in json.dumps(items, default=str)


def test_reasoning_tokens_are_recorded_and_shown(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "qwen", [(pid, "m")])
    _, key = add_client(client, op)
    upstream.hosts["llm1.example.test"] = chat_ok(content="OK", usage={"prompt_tokens": 9, "completion_tokens": 143, "total_tokens": 152,
                                                                       "completion_tokens_details": {"reasoning_tokens": 139}})
    series = 'llmgw_tokens_total{alias="qwen",client="trustdesk",kind="reasoning"}'
    before = metric(client, op, series)
    assert ask(client, key, model="qwen").status_code == 200
    assert usage_rows()[-1]["reasoning_tokens"] == 139
    assert metric(client, op, series) - before == 139
    upstream.hosts["llm1.example.test"] = chat_ok(content="OK", usage={"prompt_tokens": 9, "completion_tokens": 3})
    ask(client, key, model="qwen")
    assert usage_rows()[-1]["reasoning_tokens"] is None                      # not reported: null, not 0
    by_alias = client.get("/api/usage?group=alias", headers=op).json()["items"]
    assert by_alias[0]["reasoning_tokens"] == 139 and by_alias[0]["completion_tokens"] == 146
    recent = client.get("/api/usage/recent", headers=op).json()["items"]
    assert [r["reasoning_tokens"] for r in recent] == [None, 139]
    assert client.get("/api/overview", headers=op).json()["totals"]["reasoning_tokens"] == 139
    assert "reasoning_tokens" in client.get("/api/usage.csv?group=alias", headers=op).text.splitlines()[0].split(",")


def test_reasoning_only_reply_passes_through_with_a_note_and_a_metric(client, op, vault_ready, upstream):
    pid = add_provider(client, op)
    add_alias(client, op, "qwen", [(pid, "m")])
    _, key = add_client(client, op)
    msg = {"role": "assistant", "content": "", "reasoning": "Let me think", "reasoning_content": "Let me think"}
    thinking = {"id": "x", "object": "chat.completion", "model": "m", "usage": {"prompt_tokens": 9, "completion_tokens": 8},
                "choices": [{"index": 0, "finish_reason": "length", "message": dict(msg)}]}
    series = 'llmgw_empty_content_total{alias="qwen"}'
    before = metric(client, op, series)
    upstream.hosts["llm1.example.test"] = httpx.Response(200, json=thinking)
    r = ask(client, key, model="qwen", max_tokens=8)
    assert r.status_code == 200 and r.headers["x-llmgw-note"] == "empty-content-reasoning-only"
    got = r.json()
    assert got["choices"][0]["message"] == msg and got["choices"][0]["finish_reason"] == "length"   # unchanged
    assert metric(client, op, series) - before == 1
    thinking["choices"][0]["message"]["content"] = None          # a null content counts too
    upstream.hosts["llm1.example.test"] = httpx.Response(200, json=thinking)
    assert ask(client, key, model="qwen").headers["x-llmgw-note"] == "empty-content-reasoning-only"
    assert metric(client, op, series) - before == 2
    # a real answer, even with reasoning attached, gets no note; neither does an empty answer with no reasoning
    for content, extra in (("OK", {"reasoning": "hm"}), ("", {})):
        thinking["choices"][0]["message"] = {"role": "assistant", "content": content, **extra}
        upstream.hosts["llm1.example.test"] = httpx.Response(200, json=thinking)
        r = ask(client, key, model="qwen")
        assert r.status_code == 200 and "x-llmgw-note" not in r.headers
    assert metric(client, op, series) - before == 2
    assert 'llmgw_empty_content_total{alias="nope' not in client.get("/metrics", headers=op).text      # labels come from operator-made aliases only


def test_upgrade_from_the_previous_schema_keeps_data_and_is_repeatable():
    """Apply 001 to a fresh schema, add rows, then run the later migrations: rows keep working, defaults fill in, a rerun is harmless."""
    import os

    import psycopg

    files = sorted(migrate.MIGRATIONS.glob("*.sql"))
    assert files[0].name == "001_init.sql" and len(files) >= 2
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS upg CASCADE")
        conn.execute("CREATE SCHEMA upg")
        try:
            conn.execute("SET search_path TO upg")
            conn.execute(files[0].read_text())
            conn.execute("INSERT INTO provider (name, kind) VALUES ('echo','echo')")
            conn.execute("INSERT INTO model_alias (alias, kind) VALUES ('a','chat')")
            conn.execute("INSERT INTO alias_target (alias, position, provider_id, upstream_model) VALUES ('a',0,1,'echo-1')")
            conn.execute("INSERT INTO usage_event (client_name, endpoint, status) VALUES ('c','chat/completions',200)")
            for _ in range(2):                              # twice: idempotent
                for f in files[1:]:
                    conn.execute(f.read_text())
            assert conn.execute("SELECT extra_body::text FROM alias_target").fetchone()[0] == "{}"
            assert conn.execute("SELECT reasoning_tokens FROM usage_event").fetchone()[0] is None
            conn.execute("INSERT INTO alias_target (alias, position, provider_id, upstream_model) VALUES ('a',1,1,'m2')")   # old-style insert still works
            assert conn.execute("SELECT extra_body::text FROM alias_target WHERE position=1").fetchone()[0] == "{}"
        finally:
            conn.execute("SET search_path TO public")
            conn.execute("DROP SCHEMA upg CASCADE")
    assert migrate.run() == []        # the real schema is already up to date: nothing is applied twice


def test_old_put_shape_without_extra_body_still_works(client, op):
    pid = next(p["id"] for p in client.get("/api/providers", headers=op).json()["items"] if p["kind"] == "echo")
    r = client.put("/api/models/legacy", headers=op, json={"kind": "chat", "targets": [{"provider_id": pid, "upstream_model": "echo-1"}]})
    assert r.status_code == 200 and r.json()["targets"][0]["extra_body"] == {}


# ---- adoption of the shared hardening ------------------------------------------------------------------------------------------------
def test_a_static_key_shorter_than_32_characters_is_refused(client, monkeypatch):
    monkeypatch.setenv("LLMGW_API_KEY", "short-but-real-key-0123456789")        # 28 characters
    assert client.get("/api/clients", headers=bearer("short-but-real-key-0123456789")).status_code == 401
    assert client.post("/api/login", headers=CSRF, json={"api_key": "short-but-real-key-0123456789"}).status_code == 401
    assert client.get("/api/session").json()["key_login"] is False


def test_a_busy_scrypt_pool_answers_503_with_retry_after(client, vault_ready, monkeypatch):
    from apgkit.passwords import ScryptBusy

    def busy(*a, **k):
        raise ScryptBusy()

    monkeypatch.setattr(core.local, "_check_password", busy)
    r = client.post("/api/login", headers=CSRF, json={"password": "correct horse battery staple"})
    assert r.status_code == 503 and r.headers["retry-after"] == "1"
    assert core.auth.limiter.limited("testclient") is False            # the attempt that was never judged is not held against the client


def test_password_logins_are_limited_before_hashing(client, vault_ready):
    codes = [client.post("/api/login", headers=CSRF, json={"password": f"wrong password {i}"}).status_code for i in range(40)]
    assert codes[0] == 401 and codes[-1] == 429
    assert client.post("/api/login", headers=CSRF, json={"password": "correct horse battery staple"}).status_code == 429


def test_the_launcher_trusts_forwarded_headers_only_from_the_configured_proxy(monkeypatch):
    import uvicorn

    from llmgw import cli

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.update(k))
    monkeypatch.delenv("LLMGW_FORWARDED_ALLOW_IPS", raising=False)
    cli._serve()
    assert seen["proxy_headers"] is True and seen["forwarded_allow_ips"] == "127.0.0.1"
    monkeypatch.setenv("LLMGW_FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    cli._serve()
    assert seen["forwarded_allow_ips"] == "10.0.0.0/8"
