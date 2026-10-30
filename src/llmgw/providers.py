"""Upstream provider accounts and the one function that calls them.

Kinds: `openai` (any OpenAI-compatible endpoint; the key is encrypted by the vault, the URL is checked by the NetGuard when it
is saved and again on every call; the key is bound to the origin it was saved with and is never sent anywhere else) and `echo` (built in: no network, no key, a deterministic reply, for demos and tests; a request with tools gets a
plain text reply, and only a last user message `call:<tool> {json}` makes it answer with that tool call, to test a tool round-trip).
"""
import base64
import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass
from urllib.parse import urlsplit

from apgkit.llm import LLMError, redact
from apgkit.vault import VaultError, VaultLocked

from . import core

KINDS = {"openai", "echo"}
PRESETS = [
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"id": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    {"id": "custom", "name": "Other OpenAI-compatible endpoint", "base_url": ""},
]
_PUBLIC = ("id, name, kind, base_url, (api_key_enc IS NOT NULL) AS has_key, api_key_last4, key_origin, enabled, created_at, updated_at, "
           "(SELECT count(*) FROM alias_target t WHERE t.provider_id = provider.id) AS targets")


class ProviderError(ValueError):
    pass


def origin_of(url: str | None) -> str:
    """Where a request to `url` goes, in a canonical form: scheme, host, port and path prefix (query and fragment ignored,
    default ports and a trailing slash removed). A stored key is bound to this value; any difference is a different destination."""
    try:
        parts = urlsplit((url or "").strip())
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if not scheme or not host:
        return ""
    if port is not None and (scheme, port) in {("https", 443), ("http", 80)}:
        port = None
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}{':' + str(port) if port else ''}{parts.path.rstrip('/')}"


def list_providers(conn) -> list[dict]:
    return conn.execute(f"SELECT {_PUBLIC} FROM provider ORDER BY lower(name)").fetchall()


def get_provider(conn, provider_id: int) -> dict:
    row = conn.execute(f"SELECT {_PUBLIC} FROM provider WHERE id=%s", (provider_id,)).fetchone()
    if not row:
        raise ProviderError("provider not found")
    return row


def _need_vault(conn) -> None:
    if not core.vault.is_initialized(conn):
        raise ProviderError("set an admin password first (Settings), or start with LLMGW_ENCRYPTION_KEY: provider keys are stored encrypted")


def _clean(value: str | None) -> str | None:
    return (value or "").strip() or None


def _check(kind: str, base_url: str | None) -> str | None:
    if kind not in KINDS:
        raise ProviderError("kind must be 'openai' or 'echo'")
    if kind == "echo":
        return None
    try:
        return core.netguard.check(base_url or "")
    except ValueError as exc:       # UnsafeURL
        raise ProviderError(f"base URL rejected: {exc}") from exc


def create(conn, *, name: str, kind: str, base_url: str | None, api_key: str | None, enabled: bool = True) -> int:
    name = name.strip()
    url = _check(kind, base_url)
    enc = last4 = bound = None
    if _clean(api_key) and kind == "openai":
        _need_vault(conn)
        enc, last4, bound = core.vault.encrypt(api_key.strip()), api_key.strip()[-4:], origin_of(url)
    if conn.execute("SELECT 1 FROM provider WHERE lower(name)=lower(%s)", (name,)).fetchone():
        raise ProviderError("a provider with that name already exists")
    row = conn.execute("INSERT INTO provider (name, kind, base_url, api_key_enc, api_key_last4, key_origin, enabled) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id", (name, kind, url, enc, last4, bound, enabled)).fetchone()
    conn.commit()
    return row["id"]


def update(conn, provider_id: int, *, name: str, base_url: str | None, api_key: str | None, clear_key: bool, enabled: bool) -> None:
    cur = get_provider(conn, provider_id)
    name, url = name.strip(), _check(cur["kind"], base_url)
    if conn.execute("SELECT 1 FROM provider WHERE lower(name)=lower(%s) AND id<>%s", (name, provider_id)).fetchone():
        raise ProviderError("a provider with that name already exists")
    sets, args = ["name=%s", "base_url=%s", "enabled=%s", "updated_at=now()"], [name, url, enabled]
    new_key = bool(_clean(api_key)) and cur["kind"] == "openai"
    if cur["has_key"] and not new_key and not clear_key and origin_of(url) != origin_of(cur["base_url"]):
        # A stored key is never carried to another destination: whoever changes where requests go must supply the key again.
        raise ProviderError("the URL (scheme, host, port or path) changed, so the stored key cannot be reused: "
                            "enter the provider key again in the same request, or clear the key")
    if new_key:
        _need_vault(conn)
        sets += ["api_key_enc=%s", "api_key_last4=%s", "key_origin=%s"]
        args += [core.vault.encrypt(api_key.strip()), api_key.strip()[-4:], origin_of(url)]
    elif clear_key:
        sets += ["api_key_enc=NULL", "api_key_last4=NULL", "key_origin=NULL"]
    conn.execute(f"UPDATE provider SET {', '.join(sets)} WHERE id=%s", (*args, provider_id))
    conn.commit()


def delete(conn, provider_id: int) -> None:
    get_provider(conn, provider_id)
    if conn.execute("SELECT 1 FROM alias_target WHERE provider_id=%s", (provider_id,)).fetchone():
        raise ProviderError("this provider is still a target of a model alias; remove it there first")
    conn.execute("DELETE FROM provider WHERE id=%s", (provider_id,))
    conn.commit()


# ---- calling a provider -----------------------------------------------------------------------------------------
@dataclass
class Upstream:
    """The outcome of one attempt. `fail` is empty on success; otherwise one of: locked, unreachable, rejected_url,
    too_large, bad_response, key_origin_mismatch, http_429, http_5xx, http_4xx. `retryable` says whether the next target should be tried."""
    status: int = 200
    data: dict | None = None
    fail: str = ""
    message: str = ""
    retry_after: int | None = None

    @property
    def ok(self) -> bool:
        return not self.fail

    @property
    def retryable(self) -> bool:
        return self.fail in {"locked", "unreachable", "rejected_url", "key_origin_mismatch", "too_large", "bad_response", "http_429", "http_5xx"}


def _estimate_tokens(chars: int) -> int:
    return math.ceil(chars / 4) if chars else 0


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def call_chars(calls) -> int:
    """Characters of a message's tool calls (function names plus argument strings), for token estimates. Tolerant of bad shapes."""
    n = 0
    for c in calls if isinstance(calls, list) else []:
        fn = c.get("function") if isinstance(c, dict) else None
        if isinstance(fn, dict):
            n += sum(len(v) for k, v in fn.items() if k in ("name", "arguments") and isinstance(v, str))
    return n


# Testing aid: a tool round-trip without a model. Only the echo provider reads this prefix.
ECHO_CALL = re.compile(r"call:([A-Za-z0-9_-]{1,64})(?:[ \t]+(.*))?\Z", re.S)


def _echo_tool_call(messages: list, body: dict) -> dict | None:
    """`call:<tool> {json}` as the last user message, when that tool is offered, tool_choice is not "none" and no tool call has
    happened since: the reply is that tool call. Anything else (no match, unknown tool, arguments not a JSON object) is a plain reply,
    so a request that merely carries tools never calls one. After the tool's result the loop ends in text, not in another call."""
    tools = body.get("tools") or []
    if not tools or body.get("tool_choice") == "none":
        return None
    idx = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
    if idx is None or any(m.get("role") == "tool" or m.get("tool_calls") for m in messages[idx + 1:]):
        return None
    found = ECHO_CALL.match(_text_of(messages[idx].get("content")).strip())
    if not found or found[1] not in {(t.get("function") or {}).get("name") for t in tools if isinstance(t, dict)}:
        return None
    try:
        args = json.loads(found[2] or "{}")
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    key = hashlib.sha256(json.dumps([found[1], args, len(messages)], sort_keys=True).encode()).hexdigest()[:12]
    return {"id": f"call_echo_{key}", "type": "function", "function": {"name": found[1], "arguments": json.dumps(args, sort_keys=True)}}


def _echo_prompt_tokens(body: dict) -> int:
    """Message text, tool calls and tool schemas all count, as they do in the gateway's own estimate."""
    n = sum(_estimate_tokens(len(_text_of(m.get("content"))) + call_chars(m.get("tool_calls"))) + 3 for m in body["messages"])
    return n + _estimate_tokens(len(json.dumps(body["tools"], separators=(",", ":")))) if body.get("tools") else n


def echo_call(endpoint: str, body: dict, model: str, dim: int) -> dict:
    """A deterministic fake provider: no network, no key. Chat repeats the last user message; embeddings are a unit vector
    derived from a hash of the input, so the same text always maps to the same vector."""
    if endpoint == "embeddings":
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        size = int(body.get("dimensions") or dim)
        data = []
        for i, text in enumerate(inputs):
            raw = hashlib.shake_256(text.encode()).digest(size * 4)
            vec = [(n / 2**32) * 2 - 1 for (n,) in struct.iter_unpack(">I", raw)]
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vec = [v / norm for v in vec]
            emb = base64.b64encode(struct.pack(f"<{size}f", *vec)).decode() if body.get("encoding_format") == "base64" else vec
            data.append({"object": "embedding", "index": i, "embedding": emb})
        tokens = sum(_estimate_tokens(len(t)) for t in inputs)
        return {"object": "list", "data": data, "model": model, "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}
    messages = body["messages"]
    last = next((_text_of(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    chosen = _echo_tool_call(messages, body)
    if chosen:
        prompt = _echo_prompt_tokens(body)
        completion = _estimate_tokens(call_chars([chosen]))
        return {"id": "chatcmpl-echo-" + hashlib.sha256(json.dumps(messages).encode()).hexdigest()[:16], "object": "chat.completion",
                "created": 0, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [chosen]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}
    reply = f"Echo from {model}: {last}".strip()
    cap = body.get("max_tokens")
    finish = "stop"
    if cap and len(reply) > cap * 4:
        reply, finish = reply[: cap * 4], "length"
    prompt = _echo_prompt_tokens(body)
    completion = _estimate_tokens(len(reply))
    return {"id": "chatcmpl-echo-" + hashlib.sha256(json.dumps(messages).encode()).hexdigest()[:16], "object": "chat.completion",
            "created": 0, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}


def _shape_ok(endpoint: str, data) -> bool:
    key = "choices" if endpoint == "chat/completions" else "data"
    return isinstance(data, dict) and isinstance(data.get(key), list) and bool(data[key])


def call(provider: dict, endpoint: str, body: dict, upstream_model: str, embedding_dim: int, extra_body: dict | None = None) -> Upstream:
    """One attempt against one target. `endpoint` is `chat/completions` or `embeddings`; `body` holds only allow-listed fields.
    The stored key is decrypted only when the provider's current URL has the same origin the key was saved with; otherwise
    nothing is sent. `extra_body` is the operator's per-target fields: merged after the client's, so they win; the gateway-owned keys are
    dropped here again even though they were refused when saved. The echo provider ignores it.
    Never raises for provider trouble: the outcome says what happened, with provider text passed through `redact`."""
    if provider["kind"] == "echo":
        return Upstream(data=echo_call("embeddings" if endpoint == "embeddings" else "chat", body, upstream_model, embedding_dim))
    key = ""
    if provider["api_key_enc"]:
        if not provider.get("key_origin") or origin_of(provider["key_origin"]) != origin_of(provider["base_url"]):
            return Upstream(502, fail="key_origin_mismatch", message="the provider's URL no longer matches the one its key was saved with; "
                            "re-enter the provider key to use it with the current URL")
        try:
            key = core.vault.decrypt(provider["api_key_enc"])
        except VaultLocked:
            return Upstream(503, fail="locked", message="the provider's key is locked; an administrator must unlock the vault")
        except VaultError:
            return Upstream(502, fail="bad_response", message="the stored provider key could not be decrypted; re-enter it")
    from .aliases import EXTRA_BODY_RESERVED
    extra = {k: v for k, v in (extra_body or {}).items() if k not in EXTRA_BODY_RESERVED}
    sent = {**body, **extra, "model": upstream_model}
    try:
        resp = core.llm.post_json(provider["base_url"], "/" + endpoint, key, sent, trusted=False, retries=0)
    except LLMError as exc:
        text = redact(str(exc), key)[:300]
        fail = "rejected_url" if "URL rejected" in text else "too_large" if "too large" in text else "unreachable"
        return Upstream(502, fail=fail, message=text)
    after = None
    ra = resp.headers.get("retry-after", "")
    if ra.isdigit():
        after = min(max(int(ra), 1), 120)
    if resp.status_code >= 400:
        detail = redact(resp.text[:300], key)
        fail = "http_429" if resp.status_code == 429 else "http_5xx" if resp.status_code >= 500 else "http_4xx"
        return Upstream(resp.status_code, fail=fail, message=detail, retry_after=after)
    try:
        data = resp.json()
    except ValueError:
        return Upstream(502, fail="bad_response", message="the provider did not answer with JSON")
    if not _shape_ok(endpoint, data):
        return Upstream(502, fail="bad_response", message="the provider's answer has no choices/data in the OpenAI shape")
    return Upstream(data=json.loads(redact(json.dumps(data), key)))


def test(provider: dict, model: str) -> dict:
    """One tiny call to prove the URL, the key and the model name work. Not metered; returns a short, redacted verdict."""
    import time
    started = time.perf_counter()
    res = call(provider, "chat/completions", {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}, model, 768)
    ms = round((time.perf_counter() - started) * 1000)
    if res.ok:
        return {"ok": True, "latency_ms": ms, "message": "the provider answered"}
    return {"ok": False, "latency_ms": ms, "http_status": res.status, "message": f"{res.fail}: {res.message}"[:300]}


__all__ = ["ProviderError", "Upstream", "call", "create", "delete", "call_chars", "echo_call", "get_provider", "list_providers",
           "test", "update"]
