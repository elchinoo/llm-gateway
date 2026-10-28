"""No database needed: request validation, token maths, the echo provider, the rate window, CSV safety."""
import base64
import json
import struct

import pytest

from llmgw import aliases, gateway, providers, usage
from llmgw.gateway import GatewayError


def msgs(text="hi"):
    return [{"role": "user", "content": text}]


def test_validate_chat_allows_only_the_allow_list():
    fields, chars = gateway.validate("chat", {"model": "m", "messages": msgs("hello"), "temperature": 0.5, "stream": False, "seed": None})
    assert fields == {"messages": msgs("hello"), "temperature": 0.5} and chars == 5
    for bad in ({"functions": []}, {"user": "x"}, {"stream_options": {}}, {"logprobs": True}):
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


# ---- tool calling ----------------------------------------------------------------------------------------------------
def tool(name="lookup", **fn):
    return {"type": "function", "function": {"name": name, "description": "d", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}, **fn}}


def call(cid="call_1", name="lookup", args='{"q": "x"}'):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def size(*tools):
    return sum(len(json.dumps(t, separators=(",", ":"))) for t in tools)


def chat(msgs_, **extra):
    return gateway.validate("chat", {"model": "m", "messages": msgs_, **extra})


def test_tools_are_forwarded_unchanged_and_counted_as_prompt():
    tools = [tool(), tool("other", strict=True)]
    fields, chars = chat(msgs("hi"), tools=tools, tool_choice="auto", parallel_tool_calls=False)
    assert fields["tools"] == tools and fields["tool_choice"] == "auto" and fields["parallel_tool_calls"] is False
    assert chars == 2 + size(*tools)
    for ch in ("none", "required", {"type": "function", "function": {"name": "other"}}):
        assert chat(msgs(), tools=tools, tool_choice=ch)[0]["tool_choice"] == ch
    assert chat(msgs(), tool_choice="none")[0]["tool_choice"] == "none"             # allowed without tools
    assert chat(msgs(), tools=[{"type": "function", "function": {"name": "bare"}}])                  # description and parameters are optional


@pytest.mark.parametrize("extra", [
    {"tools": []}, {"tools": "x"}, {"tools": [tool()] * 2}, {"tools": [{"type": "retrieval"}]}, {"tools": [{"type": "function"}]},
    {"tools": [{"type": "function", "function": {"name": "bad name"}}]}, {"tools": [{"type": "function", "function": {"name": "x" * 65}}]},
    {"tools": [{"type": "function", "function": {"name": "a"}, "extra": 1}]}, {"tools": [tool(extra=1)]},
    {"tools": [tool(description="x" * 5000)]}, {"tools": [tool(parameters=[])]}, {"tools": [tool(parameters={"type": "string"})]},
    {"tools": [tool(parameters={"type": "object", "properties": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {}}}}}}}}}})]},
    {"tools": [tool(parameters={"type": "object", "x": "y" * 20000})]}, {"tools": [tool(strict="yes")]},
    {"tools": [tool(f"t{i}") for i in range(65)]},
    {"tools": [tool()], "tool_choice": "sometimes"}, {"tools": [tool()], "tool_choice": 1}, {"tools": [tool()], "tool_choice": {"type": "function"}},
    {"tools": [tool()], "tool_choice": {"type": "function", "function": {"name": "missing"}}},
    {"tools": [tool()], "tool_choice": {"type": "function", "function": {"name": "lookup", "x": 1}}},
    {"tool_choice": "auto"}, {"tool_choice": "required"}, {"tools": [tool()], "parallel_tool_calls": "no"}, {"parallel_tool_calls": True},
])
def test_bad_tools_are_refused(extra):
    with pytest.raises(GatewayError) as e:
        chat(msgs(), **extra)
    assert e.value.status == 400 and e.value.code.startswith("invalid_") and "tool" in e.value.code


def test_tool_total_size_is_bounded():
    big = [tool(f"t{i}", description="d" * 4000, parameters={"type": "object", "x": "y" * 12000}) for i in range(10)]
    with pytest.raises(GatewayError, match="in total"):
        chat(msgs(), tools=big)


def test_tool_call_round_trip_messages():
    convo = [{"role": "user", "content": "q"}, {"role": "assistant", "content": None, "tool_calls": [call("a"), call("b", args="{}")]},
             {"role": "tool", "tool_call_id": "b", "content": "rb"}, {"role": "tool", "tool_call_id": "a", "content": "ra"},
             {"role": "assistant", "content": "done", "tool_calls": []}, {"role": "user", "content": "more"}]
    fields, chars = chat(convo, tools=[tool()])
    assert fields["messages"] == convo
    assert chars == 1 + (len("lookup") * 2 + len('{"q": "x"}') + 2) + 2 + 2 + 4 + 4 + size(tool())
    chat(convo[:3] + [{"role": "tool", "tool_call_id": "a", "content": ""}])                         # empty result is fine
    chat([{"role": "assistant", "content": "text", "tool_calls": [call()]}, {"role": "tool", "tool_call_id": "call_1", "content": "r"}])


@pytest.mark.parametrize("convo", [
    [{"role": "assistant", "content": None}],                                                         # null content without calls
    [{"role": "user", "content": "q", "tool_calls": [call()]}],                                       # calls on a non-assistant
    [{"role": "tool", "tool_call_id": "call_1", "content": "r"}],                                      # result with no call
    [{"role": "assistant", "content": None, "tool_calls": [call()]}, {"role": "tool", "content": "r"}],            # no id
    [{"role": "assistant", "content": None, "tool_calls": [call()]}, {"role": "tool", "tool_call_id": "zzz", "content": "r"}],
    [{"role": "assistant", "content": None, "tool_calls": [call()]}, {"role": "tool", "tool_call_id": "call_1", "content": "r"},
     {"role": "tool", "tool_call_id": "call_1", "content": "r"}],                                      # answered twice
    [{"role": "assistant", "content": None, "tool_calls": [call()]}, {"role": "tool", "tool_call_id": "call_1", "content": ["x"]}],
    [{"role": "assistant", "content": None, "tool_calls": [call()]}, {"role": "tool", "tool_call_id": "call_1", "content": None}],
    [{"role": "assistant", "content": None, "tool_calls": [call()]}, {"role": "user", "content": "x"}, {"role": "tool", "tool_call_id": "call_1", "content": "r"}],
    [{"role": "user", "content": "q", "tool_call_id": "call_1"}],
    [{"role": "assistant", "content": None, "tool_calls": [call(), call()]}],                         # duplicate ids
    [{"role": "assistant", "content": None, "tool_calls": [call(cid="")]}],
    [{"role": "assistant", "content": None, "tool_calls": [call(cid="x" * 129)]}],
    [{"role": "assistant", "content": None, "tool_calls": [call(name="bad name")]}],
    [{"role": "assistant", "content": None, "tool_calls": [call(args={"q": 1})]}],                    # arguments must be a string
    [{"role": "assistant", "content": None, "tool_calls": [call(args="x" * 70000)]}],
    [{"role": "assistant", "content": None, "tool_calls": [{**call(), "type": "custom"}]}],
    [{"role": "assistant", "content": None, "tool_calls": [{"type": "function", "function": {"name": "n", "arguments": "{}"}}]}],      # no id
    [{"role": "assistant", "content": None, "tool_calls": [{"id": "a", "function": {"name": "n", "arguments": "{}"}}]}],              # no type
    [{"role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function", "function": {"name": "n"}}]}],           # no arguments
    [{"role": "assistant", "content": None, "tool_calls": call()}],
    [{"role": "assistant", "content": None, "tool_calls": [call(f"c{i}") for i in range(65)]}],
    [{"role": "assistant", "content": None, "tool_calls": [call()], "function_call": {}}],
])
def test_bad_tool_messages_are_refused(convo):
    with pytest.raises(GatewayError) as e:
        chat(convo)
    assert e.value.status == 400 and e.value.code == "invalid_messages"


def test_extra_keys_in_tool_calls_are_dropped_not_forwarded():
    sent = [{"role": "user", "content": "q"}, {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "index": 0, "extra_content": {"x": 1}, "function": {"name": "lookup", "arguments": "{}", "strict": True}}]},
        {"role": "tool", "tool_call_id": "a", "content": "r"}]
    fields, chars = chat(sent)
    assert fields["messages"][1]["tool_calls"] == [call("a", args="{}")]
    assert chars == 1 + len("lookup") + 2 + 1


def test_completion_counting_includes_tool_calls_and_a_tool_call_is_not_reasoning_only():
    msg = {"role": "assistant", "content": None, "tool_calls": [call()], "reasoning": "thinking"}
    data = {"choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}]}
    assert gateway._completion_chars(data) == len("lookup") + len('{"q": "x"}')
    assert gateway.reasoning_only(data) is False
    assert gateway.reasoning_only({"choices": [{"message": {"content": None, "reasoning": "t"}}]}) is True


def test_echo_with_tools_answers_in_text_and_never_calls_a_tool():
    body = {"messages": msgs("call:lookup {}"), "tools": [tool()]}
    out = providers.echo_call("chat", {**body, "messages": msgs("why is it slow?")}, "m", 8)
    msg = out["choices"][0]["message"]
    assert msg["content"] == "Echo from m: why is it slow?" and "tool_calls" not in msg and out["choices"][0]["finish_reason"] == "stop"
    plain = providers.echo_call("chat", {"messages": msgs("why is it slow?")}, "m", 8)
    assert out["usage"]["prompt_tokens"] > plain["usage"]["prompt_tokens"]                           # the schemas are counted
    # a directive for a tool that was not offered, bad JSON, a non-object, tool_choice none: all plain text
    for text, extra in (("call:other {}", {}), ("call:lookup {oops", {}), ("call:lookup [1]", {}), ("call:lookup {}", {"tool_choice": "none"}),
                        ("please call:lookup {}", {})):
        r = providers.echo_call("chat", {"messages": msgs(text), "tools": [tool()], **extra}, "m", 8)
        assert r["choices"][0]["finish_reason"] == "stop" and r["choices"][0]["message"]["content"] == f"Echo from m: {text}", text
    assert providers.echo_call("chat", {"messages": msgs("call:lookup {}")}, "m", 8)["choices"][0]["finish_reason"] == "stop"   # no tools offered


def test_echo_call_directive_makes_one_tool_call_then_text():
    ask_ = [{"role": "system", "content": "s"}, {"role": "user", "content": 'call:lookup {"q": "abc", "n": 2}'}]
    out = providers.echo_call("chat", {"messages": ask_, "tools": [tool()]}, "m", 8)
    ch = out["choices"][0]
    assert ch["finish_reason"] == "tool_calls" and ch["message"]["content"] is None
    (tc,) = ch["message"]["tool_calls"]
    assert tc["type"] == "function" and tc["function"]["name"] == "lookup" and json.loads(tc["function"]["arguments"]) == {"q": "abc", "n": 2}
    assert out["usage"]["completion_tokens"] > 0 and out["usage"]["total_tokens"] == out["usage"]["prompt_tokens"] + out["usage"]["completion_tokens"]
    assert out == providers.echo_call("chat", {"messages": ask_, "tools": [tool()]}, "m", 8)           # deterministic
    gateway.validate("chat", {"model": "m", "messages": ask_ + [ch["message"], {"role": "tool", "tool_call_id": tc["id"], "content": "r"}], "tools": [tool()]})
    after = providers.echo_call("chat", {"messages": ask_ + [ch["message"], {"role": "tool", "tool_call_id": tc["id"], "content": "r"}],
                                         "tools": [tool()]}, "m", 8)
    assert after["choices"][0]["finish_reason"] == "stop" and "tool_calls" not in after["choices"][0]["message"]   # no loop
    bare = providers.echo_call("chat", {"messages": [{"role": "user", "content": "call:lookup"}], "tools": [tool()]}, "m", 8)
    assert json.loads(bare["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {}


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
