"""Upstream provider accounts and the one function that calls them.

Kinds: `openai` (any OpenAI-compatible endpoint; the key is encrypted by the vault, the URL is checked by the NetGuard when it
is saved and again on every call) and `echo` (built in: no network, no key, a deterministic reply, for demos and tests).
"""
import base64
import hashlib
import json
import math
import struct
from dataclasses import dataclass

from apgkit.llm import LLMError, redact
from apgkit.vault import VaultError, VaultLocked

from . import core

KINDS = {"openai", "echo"}
PRESETS = [
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"id": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    {"id": "custom", "name": "Other OpenAI-compatible endpoint", "base_url": ""},
]
_PUBLIC = ("id, name, kind, base_url, (api_key_enc IS NOT NULL) AS has_key, api_key_last4, enabled, created_at, updated_at, "
           "(SELECT count(*) FROM alias_target t WHERE t.provider_id = provider.id) AS targets")


class ProviderError(ValueError):
    pass


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
    enc = last4 = None
    if _clean(api_key) and kind == "openai":
        _need_vault(conn)
        enc, last4 = core.vault.encrypt(api_key.strip()), api_key.strip()[-4:]
    if conn.execute("SELECT 1 FROM provider WHERE lower(name)=lower(%s)", (name,)).fetchone():
        raise ProviderError("a provider with that name already exists")
    row = conn.execute("INSERT INTO provider (name, kind, base_url, api_key_enc, api_key_last4, enabled) VALUES (%s,%s,%s,%s,%s,%s) "
                       "RETURNING id", (name, kind, url, enc, last4, enabled)).fetchone()
    conn.commit()
    return row["id"]


def update(conn, provider_id: int, *, name: str, base_url: str | None, api_key: str | None, clear_key: bool, enabled: bool) -> None:
    cur = get_provider(conn, provider_id)
    name, url = name.strip(), _check(cur["kind"], base_url)
    if conn.execute("SELECT 1 FROM provider WHERE lower(name)=lower(%s) AND id<>%s", (name, provider_id)).fetchone():
        raise ProviderError("a provider with that name already exists")
    sets, args = ["name=%s", "base_url=%s", "enabled=%s", "updated_at=now()"], [name, url, enabled]
    if _clean(api_key) and cur["kind"] == "openai":
        _need_vault(conn)
        sets += ["api_key_enc=%s", "api_key_last4=%s"]
        args += [core.vault.encrypt(api_key.strip()), api_key.strip()[-4:]]
    elif clear_key:
        sets += ["api_key_enc=NULL", "api_key_last4=NULL"]
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
    too_large, bad_response, http_429, http_5xx, http_4xx. `retryable` says whether the next target should be tried."""
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
        return self.fail in {"locked", "unreachable", "rejected_url", "too_large", "bad_response", "http_429", "http_5xx"}


def _estimate_tokens(chars: int) -> int:
    return math.ceil(chars / 4) if chars else 0


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


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
    reply = f"Echo from {model}: {last}".strip()
    cap = body.get("max_tokens")
    finish = "stop"
    if cap and len(reply) > cap * 4:
        reply, finish = reply[: cap * 4], "length"
    prompt = sum(_estimate_tokens(len(_text_of(m.get("content")))) + 3 for m in messages)
    completion = _estimate_tokens(len(reply))
    return {"id": "chatcmpl-echo-" + hashlib.sha256(json.dumps(messages).encode()).hexdigest()[:16], "object": "chat.completion",
            "created": 0, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}


def _shape_ok(endpoint: str, data) -> bool:
    key = "choices" if endpoint == "chat/completions" else "data"
    return isinstance(data, dict) and isinstance(data.get(key), list) and bool(data[key])


def call(provider: dict, endpoint: str, body: dict, upstream_model: str, embedding_dim: int) -> Upstream:
    """One attempt against one target. `endpoint` is `chat/completions` or `embeddings`; `body` holds only allow-listed fields.
    Never raises for provider trouble: the outcome says what happened, with provider text passed through `redact`."""
    if provider["kind"] == "echo":
        return Upstream(data=echo_call("embeddings" if endpoint == "embeddings" else "chat", body, upstream_model, embedding_dim))
    key = ""
    if provider["api_key_enc"]:
        try:
            key = core.vault.decrypt(provider["api_key_enc"])
        except VaultLocked:
            return Upstream(503, fail="locked", message="the provider's key is locked; an administrator must unlock the vault")
        except VaultError:
            return Upstream(502, fail="bad_response", message="the stored provider key could not be decrypted; re-enter it")
    sent = {**body, "model": upstream_model}
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


__all__ = ["ProviderError", "Upstream", "call", "create", "delete", "echo_call", "get_provider", "list_providers",
           "test", "update"]
