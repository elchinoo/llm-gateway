"""No database needed: request validation, token maths, the echo provider, the rate window, CSV safety."""
import base64
import struct

import pytest

from llmgw import aliases, gateway, providers, usage
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
    row = {"key": "=HYPERLINK(1)", "requests": 1, "errors": 0, "prompt_tokens": 1, "completion_tokens": 1, "reasoning_tokens": 0, "total_tokens": 2,
           "estimated_requests": 0, "cost_usd": 0.0, "avg_latency_ms": 1, "p95_latency_ms": 1, "over_budget_requests": 0}
    out = usage.to_csv([row, {**row, "key": "@x"}, {**row, "key": "plain"}], "client").splitlines()
    assert out[1].startswith("'=HYPERLINK") and out[2].startswith("'@x") and out[3].startswith("plain,")


def test_gateway_error_shape():
    e = GatewayError(429, "budget_exceeded", "used up", "insufficient_quota")
    assert e.body() == {"error": {"message": "used up", "type": "insufficient_quota", "code": "budget_exceeded"}}


# ---- extra_body: validation, merge, reasoning detection ---------------------------------------------------------------------------
def test_extra_body_accepts_the_documented_presets():
    for ok in ({"reasoning": {"enabled": False}}, {"chat_template_kwargs": {"enable_thinking": False}}, {"reasoning_effort": "low"},
               {"thinking": {"type": "disabled"}}, {}, None):
        assert aliases.validate_extra_body(ok) == (ok or {})


@pytest.mark.parametrize("bad", [
    {"model": "x"}, {"messages": []}, {"input": "x"}, {"stream": True}, {"user": "u"}, {"n": 2}, {"ok": 1, "model": "x"},
    [], "text", 5, True, [{"a": 1}],
    {"a": {"b": {"c": {"d": {"e": 1}}}}}, {"a": [[[[1]]]]},
    {"big": "x" * 5000}, {"k" + str(i): "v" * 50 for i in range(100)},
    {"f": float("nan")}, {"f": float("inf")}, {"a": object()}, {"a": {1, 2}}, {"a": b"bytes"}, {1: "int key"},
])
def test_extra_body_rejects(bad):
    with pytest.raises(aliases.AliasError):
        aliases.validate_extra_body(bad)


def test_extra_body_limits_are_inclusive_at_the_edge():
    assert aliases.validate_extra_body({"a": {"b": {"c": {"d": 1}}}})        # 4 levels of objects
    assert aliases.validate_extra_body({"v": "x" * 4000})                    # under 4 KB
    with pytest.raises(aliases.AliasError):                                  # one level more is too deep
        aliases.validate_extra_body({"a": {"b": {"c": {"d": {}}}}})
    assert aliases.validate_extra_body({"nested": {"model": "fine here"}})   # a nested `model` is the provider's own field, not ours


def test_extra_body_merges_after_the_client_fields_and_cannot_touch_reserved_ones(monkeypatch):
    sent = {}

    class Resp:
        status_code, headers, text = 200, {}, ""
        def json(self): return {"choices": [{"message": {"content": "x"}}]}

    monkeypatch.setattr(providers.core.llm, "post_json", lambda base, path, key, body, **k: sent.update(body) or Resp())
    prov = {"kind": "openai", "api_key_enc": None, "base_url": "https://x.test/v1"}
    body = {"messages": msgs(), "max_tokens": 8, "temperature": 0.2}
    providers.call(prov, "chat/completions", body, "up-model", 8, {"reasoning": {"enabled": False}, "max_tokens": 512, "model": "evil", "n": 9, "user": "u"})
    assert sent == {"messages": msgs(), "max_tokens": 512, "temperature": 0.2, "reasoning": {"enabled": False}, "model": "up-model"}
    assert body == {"messages": msgs(), "max_tokens": 8, "temperature": 0.2}        # the caller's dict is not mutated
    sent.clear()
    providers.call(prov, "chat/completions", body, "up-model", 8)
    assert sent == {**body, "model": "up-model"}


def test_echo_provider_ignores_extra_body():
    a = providers.call({"kind": "echo"}, "chat/completions", {"messages": msgs("hi")}, "echo-1", 8, {"reasoning": {"enabled": False}})
    b = providers.call({"kind": "echo"}, "chat/completions", {"messages": msgs("hi")}, "echo-1", 8)
    assert a.data == b.data


def test_reasoning_tokens_and_reasoning_only_detection():
    d = {"usage": {"prompt_tokens": 9, "completion_tokens": 143, "completion_tokens_details": {"reasoning_tokens": 139}}}
    assert gateway.reasoning_tokens_from(d) == 139
    for none in ({}, {"usage": {}}, {"usage": {"completion_tokens_details": None}}, {"usage": {"completion_tokens_details": {"reasoning_tokens": "x"}}},
                 {"usage": {"completion_tokens_details": {"reasoning_tokens": -1}}}):
        assert gateway.reasoning_tokens_from(none) is None

    def reply(**msg):
        return {"choices": [{"message": {"role": "assistant", **msg}}]}

    assert gateway.reasoning_only(reply(content="", reasoning="thinking...")) is True
    assert gateway.reasoning_only(reply(content=None, reasoning_content="thinking...")) is True
    assert gateway.reasoning_only(reply(reasoning="thinking...")) is True                  # no content key at all
    assert gateway.reasoning_only(reply(content="OK", reasoning="thinking...")) is False
    assert gateway.reasoning_only(reply(content="")) is False                              # empty but no reasoning: nothing to note
    assert gateway.reasoning_only(reply(content="", reasoning="")) is False
    assert gateway.reasoning_only({"choices": []}) is False and gateway.reasoning_only({}) is False
