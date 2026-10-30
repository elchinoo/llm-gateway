"""End to end: PagerLens's Ask (a real tool-calling loop against a real PagerLens database) talks to THIS gateway, running in
process on a loopback port, answered by the built-in `echo` provider.

It needs both repositories: the gateway's own virtualenv does not contain PagerLens, so the test skips there. Run it with PagerLens's
interpreter and the gateway's source on the path, one throwaway Postgres cluster, two databases:

    TEST_DATABASE_URL=postgresql://u:p@127.0.0.1:5432/llmgw_test \\
    TEST_PAGERLENS_DATABASE_URL=postgresql://u:p@127.0.0.1:5432/pagerlens_test \\
    PYTHONPATH=src ../pagerlens/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_pagerlens_ask_e2e.py

(The echo provider answers `call:<tool> {json}` as a last user message with that tool call: see the API reference.)
"""
import json
import math
import os
import socket
import threading
import time

import httpx
import pytest

pytest.importorskip("pagerlens")
if not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_PAGERLENS_DATABASE_URL")):
    pytest.skip("set TEST_DATABASE_URL and TEST_PAGERLENS_DATABASE_URL to run the PagerLens end-to-end test", allow_module_level=True)

import uvicorn  # noqa: E402
from pagerlens import accounts, ask, embedcfg, investigator, simulator, vault  # noqa: E402
from pagerlens import core as pl_core  # noqa: E402
from pagerlens import migrate as pl_migrate  # noqa: E402
from pagerlens.db import connect as pl_connect  # noqa: E402
from pagerlens.db import connect_vector  # noqa: E402

from llmgw import api, providers  # noqa: E402
from llmgw import core as gw_core  # noqa: E402
from llmgw import migrate as gw_migrate  # noqa: E402
from tests.conftest import all_table_text  # noqa: E402

OPERATOR_KEY = "operator-e2e-key-0123456789-abcdefgh"
GATEWAY_DB, PAGERLENS_DB = os.environ["TEST_DATABASE_URL"], os.environ["TEST_PAGERLENS_DATABASE_URL"]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """Both databases migrated from scratch, the gateway serving on loopback, PagerLens pointed at it with a client key."""
    mp = pytest.MonkeyPatch()
    # Both apps read their database from the same variable name, so each gets its own URL here instead.
    mp.setattr(gw_core.db, "_url", lambda: GATEWAY_DB)
    mp.setattr(pl_core.db, "_url", lambda: PAGERLENS_DB)
    for var in ("LLMGW_ENCRYPTION_KEY", "PAGERLENS_API_KEY", "PAGERLENS_ENCRYPTION_KEY", "PAGERLENS_ASK_PER_MINUTE",
                "PAGERLENS_ASK_GLOBAL_PER_MINUTE"):
        mp.delenv(var, raising=False)
    mp.setenv("LLMGW_API_KEY", OPERATOR_KEY)
    mp.setenv("LLMGW_API_KEY_FILE", "/nonexistent/key")
    mp.setenv("LLMGW_ENCRYPTION_KEY_FILE", "/nonexistent/key")
    mp.setenv("PAGERLENS_API_KEY_FILE", "/nonexistent/key")
    mp.setenv("PAGERLENS_ENCRYPTION_KEY_FILE", "/nonexistent/key")
    mp.setenv("LLM_API_KEY_FILE", "/nonexistent/key")
    for url, mig in ((GATEWAY_DB, gw_migrate), (PAGERLENS_DB, pl_migrate)):
        import psycopg
        with psycopg.connect(url) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        mig.run()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "the gateway did not start"
    base = f"http://127.0.0.1:{port}"
    made = httpx.post(f"{base}/api/clients", headers={"Authorization": f"Bearer {OPERATOR_KEY}"}, json={"name": "pagerlens"}, timeout=10)
    assert made.status_code == 201, made.text
    mp.setenv("LLM_BASE_URL", f"{base}/v1")                      # PagerLens's environment account: loopback, so trusted
    mp.setenv("LLM_CHAT_MODEL", "echo-chat")
    mp.setenv("LLM_API_KEY", made.json()["key"]["key"])
    embedcfg.set_active(embedcfg.EmbeddingConfig(base_url="", model="offline", dim=768, send_dimensions=False, trusted=True, account_id=None))
    vault.lock()
    ask.reset()
    with connect_vector() as conn:
        info = simulator.trigger(conn, "bad_deploy")
        conn.commit()
        investigator.investigate(conn, info["incident_id"], mode="rules")
    yield {"base": base, "incident": info["incident_id"]}
    server.should_exit = True
    thread.join(timeout=10)
    gw_core.db.close_pool()
    mp.undo()


@pytest.fixture
def seen(monkeypatch):
    """Every chat body the echo provider receives, as it arrived from PagerLens through the gateway."""
    bodies: list[dict] = []
    real = providers.echo_call

    def spy(endpoint, body, model, dim):
        if endpoint == "chat":
            bodies.append(json.loads(json.dumps(body)))
        return real(endpoint, body, model, dim)

    monkeypatch.setattr(providers, "echo_call", spy)
    return bodies


def gateway_rows():
    with gw_core.db.connect() as conn:
        return conn.execute("SELECT client_name, endpoint, status, prompt_tokens, completion_tokens, estimated FROM usage_event ORDER BY id").fetchall()


def ask_turn(question):
    with pl_connect() as conn:
        cfg = ask.pick_account(conn, False, None)
    return ask.run_turn(cfg, "e2e", [{"role": "user", "content": question}])


def test_ask_answers_a_plain_question_through_the_gateway_without_calling_a_tool(stack, seen):
    before = len(gateway_rows())
    out = ask_turn("what is slow in the database?")
    assert out["steps"] == [] and out["usage"]["llm_calls"] == 1
    assert out["answer"] == "Echo from echo-1: what is slow in the database?"
    assert any("without looking anything up" in w for w in out["warnings"])
    (body,) = seen
    assert body["messages"][-1] == {"role": "user", "content": "what is slow in the database?"}
    assert {t["function"]["name"] for t in body["tools"]} == set(ask.TOOLS) and all(t["type"] == "function" for t in body["tools"])
    assert "tool_choice" not in body and "parallel_tool_calls" not in body
    rows = gateway_rows()[before:]
    assert [(r["client_name"], r["endpoint"], r["status"]) for r in rows] == [("pagerlens", "chat/completions", 200)]
    assert rows[0]["prompt_tokens"] >= math.ceil(len(json.dumps(body["tools"], separators=(",", ":"))) / 4)   # schemas are counted


def test_ask_runs_a_real_tool_round_trip_through_the_gateway(stack, seen):
    before = len(gateway_rows())
    out = ask_turn('call:list_incidents {"limit": 3}')
    assert out["steps"] == [{"tool": "list_incidents", "args": {"limit": 3}}]
    assert out["usage"]["llm_calls"] == 2 and out["usage"]["prompt_tokens"] > 0
    assert out["answer"].startswith("Echo from echo-1: call:list_incidents")
    first, second = seen
    assert "tool_calls" not in first["messages"][-1] and first["messages"][-1]["role"] == "user"
    # What PagerLens sent back on the second turn: the assistant's tool call (content null) and the tool's result, verbatim.
    assistant, result = second["messages"][-2], second["messages"][-1]
    assert assistant["role"] == "assistant" and assistant["content"] is None and len(assistant["tool_calls"]) == 1
    (call,) = assistant["tool_calls"]
    assert set(call) == {"id", "type", "function"} and call["type"] == "function" and set(call["function"]) == {"name", "arguments"}
    assert call["function"]["name"] == "list_incidents" and json.loads(call["function"]["arguments"]) == {"limit": 3}
    assert set(result) == {"role", "tool_call_id", "content"} and result["role"] == "tool" and result["tool_call_id"] == call["id"]
    assert isinstance(result["content"], str) and result["content"].startswith(ask.FENCE_OPEN)
    assert f"incident #{stack['incident']}" in result["content"] or str(stack["incident"]) in result["content"]
    rows = gateway_rows()[before:]
    assert [(r["endpoint"], r["status"], r["estimated"]) for r in rows] == [("chat/completions", 200, False)] * 2
    assert rows[1]["prompt_tokens"] > rows[0]["prompt_tokens"]                                    # the tool call and result were counted


def test_the_gateway_stored_nothing_of_the_tool_traffic(stack, seen):
    marker = "MARKER-QUESTION-5d1e"
    ask_turn(f'call:list_incidents {{"limit": 2}} {marker}')                  # not valid JSON after the name: plain answer, still no call
    ask_turn('call:list_incidents {"service": "checkout-api"}')
    with gw_core.db.connect() as conn:
        text = all_table_text(conn)
    for needle in (marker, "checkout-api", "list_incidents", "untrusted_data", "call_echo_", "Echo from"):
        assert needle not in text, needle
    assert isinstance(accounts.env_account(), dict)
