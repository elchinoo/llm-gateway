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
    assert {a["alias"] for a in client.get("/api/models", headers=op).json()["items"]} == {"echo-chat", "echo-embed"}
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
    assert client.get("/v1/models", headers=key).json()["data"][0]["id"] in {"echo-chat", "echo-embed"}


def test_embedding_dimension_is_set_on_the_alias(client, op):
    pid = next(p["id"] for p in client.get("/api/providers", headers=op).json()["items"] if p["kind"] == "echo")
    add_alias(client, op, "tiny-embed", [(pid, "e")], kind="embedding", embedding_dim=8)
    _, key = add_client(client, op)
    out = client.post("/v1/embeddings", headers=key, json={"model": "tiny-embed", "input": "x"}).json()
    assert len(out["data"][0]["embedding"]) == 8


def test_the_echo_demo_is_seeded_once(client, op):
    from llmgw import aliases
    for a in ("echo-chat", "echo-embed"):
        assert client.delete(f"/api/models/{a}", headers=op).status_code == 200
    pid = client.get("/api/providers", headers=op).json()["items"][0]["id"]
    assert client.delete(f"/api/providers/{pid}", headers=op).status_code == 200
    with connect() as conn:
        assert aliases.seed_echo(conn) is False


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
    assert core.vault.decrypt(stored["api_key_enc"]) == UPSTREAM_KEY      # it is recoverable by the vault, which is the point


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
    for extra in ({"tools": []}, {"user": "u1"}, {"logit_bias": {}}, {"provider": "x"}, {"functions": []}):
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
    assert {m["id"] for m in client.get("/v1/models", headers=wide).json()["data"]} == {"echo-chat", "echo-embed"}
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
