"""No database needed: request validation, token maths, the echo provider, the rate window, CSV safety."""
import base64
import struct

import pytest

from llmgw import gateway, providers, usage
from llmgw.gateway import GatewayError


def msgs(text="hi"):
    return [{"role": "user", "content": text}]


def test_validate_chat_allows_only_the_allow_list():
    fields, chars = gateway.validate("chat", {"model": "m", "messages": msgs("hello"), "temperature": 0.5, "stream": False, "seed": None})
    assert fields == {"messages": msgs("hello"), "temperature": 0.5} and chars == 5
    for bad in ({"tools": []}, {"user": "x"}, {"stream_options": {}}, {"logprobs": True}):
        with pytest.raises(GatewayError) as e:
            gateway.validate("chat", {"model": "m", "messages": msgs(), **bad})
        assert e.value.status == 400 and e.value.code == "unknown_field"
    with pytest.raises(GatewayError) as e:
        gateway.validate("chat", {"model": "m", "messages": msgs(), "stream": True})
    assert e.value.code == "stream_not_supported"


@pytest.mark.parametrize("body", [
    {"messages": msgs()}, {"model": "", "messages": msgs()}, {"model": "m"}, {"model": "m", "messages": []},
    {"model": "m", "messages": [{"role": "user"}]}, {"model": "m", "messages": [{"role": "function", "content": "x"}]},
    {"model": "m", "messages": msgs(), "top_p": 1.5}, {"model": "m", "messages": msgs(), "temperature": "hot"}, {"model": "m", "messages": msgs(), "stop": [1]},
    {"model": "m", "messages": msgs(), "stop": ["a", "b", "c", "d", "e"]}, {"model": "m", "messages": msgs(), "response_format": {"type": "xml"}},
    {"model": "m", "messages": msgs(), "n": 3}, {"model": "m", "messages": msgs(), "n": True}, {"model": "m", "messages": msgs(), "seed": 1.5},
])
def test_validate_chat_rejects_bad_values(body):
    with pytest.raises(GatewayError) as e:
        gateway.validate("chat", body)
    assert e.value.status == 400


def test_validate_embeddings():
    fields, chars = gateway.validate("embeddings", {"model": "e", "input": ["ab", "cde"], "dimensions": 8, "encoding_format": "base64"})
    assert fields["dimensions"] == 8 and chars == 5
    for bad in ({"input": ""}, {"input": []}, {"input": [1, 2]}, {"input": "x", "dimensions": 0}, {"input": "x", "encoding_format": "hex"},
                {"input": "x", "messages": []}, {"input": ["x"] * 300}):
        with pytest.raises(GatewayError):
            gateway.validate("embeddings", {"model": "e", **bad})


def test_text_parts_are_accepted_but_nothing_else():
    ok = [{"role": "user", "content": [{"type": "text", "text": "ab"}, {"type": "text", "text": "cd"}]}]
    assert gateway.validate("chat", {"model": "m", "messages": ok})[1] == 4
    with pytest.raises(GatewayError):
        gateway.validate("chat", {"model": "m", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]})


def test_token_accounting():
    assert gateway.tokens_from({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}, 0, 0) == (10, 5, 15, False)
    assert gateway.tokens_from({"usage": {"prompt_tokens": 7}}, 0, 0) == (7, 0, 7, False)                 # embeddings
    assert gateway.tokens_from({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 3}}, 0, 0)[2] == 15    # a total below the parts is not believed
    assert gateway.tokens_from({}, 9, 4) == (3, 1, 4, True)                                                 # chars / 4, rounded up, flagged
    assert gateway.tokens_from({"usage": {"prompt_tokens": True}}, 8, 8) == (2, 2, 4, True)
    assert gateway.tokens_from({"usage": {"prompt_tokens": -1}}, 8, 0)[3] is True
    assert gateway.cost_of({"price_prompt_per_m": 3, "price_completion_per_m": 15}, 2_000_000, 1_000_000) == 21.0
    assert gateway.cost_of({"price_prompt_per_m": None, "price_completion_per_m": None}, 5, 5) == 0.0


def test_rate_window_slides(monkeypatch):
    gateway.reset_rate_limits()
    now = [1000.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: now[0])
    gateway.rate_check(1, 2)
    now[0] += 10
    gateway.rate_check(1, 2)
    with pytest.raises(GatewayError) as e:
        gateway.rate_check(1, 2)
    assert e.value.status == 429 and e.value.headers["Retry-After"] == "50"
    now[0] += 51                       # the first request left the window
    gateway.rate_check(1, 2)
    gateway.rate_check(2, 2)           # another client is separate
    gateway.rate_check(3, None)        # no limit
    gateway.reset_rate_limits()


def test_echo_is_deterministic_and_plausible():
    a = providers.echo_call("embeddings", {"input": "hello"}, "e", 16)
    b = providers.echo_call("embeddings", {"input": ["hello", "other"]}, "e", 16)
    assert a["data"][0]["embedding"] == b["data"][0]["embedding"] and len(a["data"][0]["embedding"]) == 16
    assert b["data"][0]["embedding"] != b["data"][1]["embedding"]
    assert providers.echo_call("embeddings", {"input": "x", "dimensions": 4}, "e", 16)["data"][0]["embedding"].__len__() == 4
    raw = base64.b64decode(providers.echo_call("embeddings", {"input": "x", "encoding_format": "base64"}, "e", 8)["data"][0]["embedding"])
    assert len(struct.unpack("<8f", raw)) == 8
    c = providers.echo_call("chat", {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "abc"}]}, "m", 8)
    assert c["choices"][0]["message"]["content"] == "Echo from m: abc"
    assert c["usage"]["total_tokens"] == c["usage"]["prompt_tokens"] + c["usage"]["completion_tokens"]
    short = providers.echo_call("chat", {"messages": [{"role": "user", "content": "x" * 100}], "max_tokens": 2}, "m", 8)
    assert len(short["choices"][0]["message"]["content"]) == 8 and short["choices"][0]["finish_reason"] == "length"
    assert providers.echo_call("chat", {"messages": msgs("x")}, "m", 8) == providers.echo_call("chat", {"messages": msgs("x")}, "m", 8)


def test_csv_cells_cannot_run_as_formulas():
    row = {"key": "=HYPERLINK(1)", "requests": 1, "errors": 0, "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
           "estimated_requests": 0, "cost_usd": 0.0, "avg_latency_ms": 1, "p95_latency_ms": 1, "over_budget_requests": 0}
    out = usage.to_csv([row, {**row, "key": "@x"}, {**row, "key": "plain"}], "client").splitlines()
    assert out[1].startswith("'=HYPERLINK") and out[2].startswith("'@x") and out[3].startswith("plain,")


def test_gateway_error_shape():
    e = GatewayError(429, "budget_exceeded", "used up", "insufficient_quota")
    assert e.body() == {"error": {"message": "used up", "type": "insufficient_quota", "code": "budget_exceeded"}}
