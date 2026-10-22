"""The data plane: validate an OpenAI-style request, enforce the client's limits, call the alias's targets in order, meter it.

Only an allow-list of fields is forwarded (no tools, no `user`, no provider-specific passthrough), the request is never
streamed, and neither prompts nor completions are stored anywhere: a usage row holds counts, names, latency and status.
"""
import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from . import core, providers, usage
from . import metrics as m
from .usage import Event

MAX_BODY = 512 * 1024
MAX_MESSAGES, MAX_INPUTS = 500, 256
ROLES = {"system", "user", "assistant", "developer"}
CHAT_FIELDS = {"model", "messages", "max_tokens", "temperature", "top_p", "stop", "response_format", "seed", "presence_penalty",
               "frequency_penalty", "n"}
EMBED_FIELDS = {"model", "input", "dimensions", "encoding_format"}
ENDPOINTS = {"chat": ("chat/completions", "chat", CHAT_FIELDS), "embeddings": ("embeddings", "embedding", EMBED_FIELDS)}


class GatewayError(Exception):
    """An OpenAI-shaped error: `{"error": {"message", "type", "code"}}` with an HTTP status."""

    def __init__(self, status: int, code: str, message: str, type_: str = "invalid_request_error", headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.type, self.headers = status, code, message, type_, headers or {}

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.type, "code": self.code}}


# ---- request validation ----------------------------------------------------------------------------------------------
def _num(body, key, lo, hi, integer=False):
    v = body.get(key)
    if v is None:
        return
    ok = isinstance(v, int) if integer else isinstance(v, (int, float))
    if not ok or isinstance(v, bool) or not lo <= v <= hi:
        raise GatewayError(400, "invalid_" + key, f"'{key}' must be {'an integer' if integer else 'a number'} between {lo} and {hi}")


def _text(content, where: str) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
                                                    and set(p) <= {"type", "text"} for p in content):
        return "".join(p["text"] for p in content)
    raise GatewayError(400, "invalid_messages", f"{where}: content must be a string (or a list of text parts); images, audio and tool calls are not supported")


def validate(endpoint: str, body) -> tuple[dict, int]:
    """Return (fields to forward, prompt characters). Anything outside the allow-list is a 400 that names the field."""
    if not isinstance(body, dict):
        raise GatewayError(400, "invalid_body", "the request body must be a JSON object")
    allowed = ENDPOINTS[endpoint][2]
    if body.get("stream"):
        raise GatewayError(400, "stream_not_supported", "streaming is not supported by this gateway; send the request without 'stream' (or with false)")
    unknown = sorted(k for k in body if k not in allowed and k != "stream")
    if unknown:
        names = ", ".join(repr(str(k)[:40]) for k in unknown[:5])
        raise GatewayError(400, "unknown_field", f"unsupported field(s): {names}. Allowed: {', '.join(sorted(allowed))}")
    if not isinstance(body.get("model"), str) or not body["model"].strip() or len(body["model"]) > 80:
        raise GatewayError(400, "invalid_model", "'model' (a model alias) is required")
    out = {k: v for k, v in body.items() if k not in {"stream", "model"} and v is not None}
    chars = 0
    if endpoint == "chat":
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not 1 <= len(msgs) <= MAX_MESSAGES:
            raise GatewayError(400, "invalid_messages", f"'messages' must be a list of 1 to {MAX_MESSAGES} messages")
        for i, msg in enumerate(msgs):
            if not isinstance(msg, dict) or set(msg) - {"role", "content", "name"} or msg.get("role") not in ROLES:
                raise GatewayError(400, "invalid_messages", f"messages[{i}] must be an object with a role ({', '.join(sorted(ROLES))}), content and optionally name")
            chars += len(_text(msg.get("content"), f"messages[{i}]"))
            if "name" in msg and (not isinstance(msg["name"], str) or len(msg["name"]) > 64):
                raise GatewayError(400, "invalid_messages", f"messages[{i}].name must be a short string")
        _num(body, "max_tokens", 1, 131_072, True)
        _num(body, "temperature", 0, 2)
        _num(body, "top_p", 0, 1)
        _num(body, "seed", -2**63, 2**63 - 1, True)
        _num(body, "presence_penalty", -2, 2)
        _num(body, "frequency_penalty", -2, 2)
        if body.get("n") not in (None, 1) or isinstance(body.get("n"), bool):
            raise GatewayError(400, "invalid_n", "'n' must be 1: the gateway returns a single choice")
        stop = body.get("stop")
        if stop is not None and not (isinstance(stop, str) or (isinstance(stop, list) and len(stop) <= 4 and all(isinstance(s, str) for s in stop))):
            raise GatewayError(400, "invalid_stop", "'stop' must be a string or a list of up to 4 strings")
        rf = body.get("response_format")
        if rf is not None and not (isinstance(rf, dict) and rf.get("type") in {"text", "json_object", "json_schema"} and set(rf) <= {"type", "json_schema"}):
            raise GatewayError(400, "invalid_response_format", "'response_format' must be {type: text|json_object|json_schema[, json_schema]}")
    else:
        inp = body.get("input")
        items = inp if isinstance(inp, list) else [inp]
        if not items or len(items) > MAX_INPUTS or not all(isinstance(s, str) and s for s in items):
            raise GatewayError(400, "invalid_input", f"'input' must be a non-empty string or a list of up to {MAX_INPUTS} non-empty strings")
        chars = sum(len(s) for s in items)
        _num(body, "dimensions", 1, 8192, True)
        if body.get("encoding_format") not in (None, "float", "base64"):
            raise GatewayError(400, "invalid_encoding_format", "'encoding_format' must be 'float' or 'base64'")
    return out, chars


# ---- per-client request rate (in memory, per process) -------------------------------------------------------------
_windows: dict[int, deque] = {}
_lock = threading.Lock()


def rate_check(client_id: int, rpm: int | None) -> None:
    """Sliding 60 s window per client. Per process: with N replicas a client can reach N times its limit."""
    if not rpm:
        return
    now = time.monotonic()
    with _lock:
        win = _windows.setdefault(client_id, deque())
        while win and now - win[0] >= 60:
            win.popleft()
        if len(win) >= rpm:
            wait = max(1, math.ceil(60 - (now - win[0])))
            raise GatewayError(429, "rate_limit_exceeded", f"this client is limited to {rpm} requests per minute; retry in {wait}s",
                               "rate_limit_error", {"Retry-After": str(wait)})
        win.append(now)


def reset_rate_limits() -> None:
    with _lock:
        _windows.clear()


# ---- usage numbers -------------------------------------------------------------------------------------------------
def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10**9 else None


def tokens_from(data: dict, prompt_chars: int, completion_chars: int) -> tuple[int, int, int, bool]:
    """(prompt, completion, total, estimated): the upstream's own `usage` when it is sane, else chars/4 marked as an estimate."""
    u = data.get("usage")
    if isinstance(u, dict) and _int(u.get("prompt_tokens")) is not None:
        p, c = _int(u["prompt_tokens"]), _int(u.get("completion_tokens", 0)) or 0
        t = _int(u.get("total_tokens"))
        return p, c, (t if t is not None and t >= p + c else p + c), False
    p, c = math.ceil(prompt_chars / 4), math.ceil(completion_chars / 4)
    return p, c, p + c, True


def _completion_chars(data: dict) -> int:
    n = 0
    for ch in data.get("choices") or []:
        msg = ch.get("message") if isinstance(ch, dict) else None
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            n += len(msg["content"])
    return n


def cost_of(alias: dict, prompt: int, completion: int) -> float:
    pp, pc = alias.get("price_prompt_per_m"), alias.get("price_completion_per_m")
    return round(prompt / 1e6 * float(pp or 0) + completion / 1e6 * float(pc or 0), 6)


# ---- one request -----------------------------------------------------------------------------------------------------
@dataclass
class Result:
    status: int
    body: bytes
    headers: dict


def _alias_row(conn, alias: str) -> dict | None:
    row = conn.execute("SELECT * FROM model_alias WHERE alias=%s", (alias,)).fetchone()
    if row:
        row["targets"] = conn.execute(
            "SELECT t.position, t.upstream_model, p.* FROM alias_target t JOIN provider p ON p.id = t.provider_id WHERE t.alias=%s ORDER BY t.position",
            (alias,)).fetchall()
    conn.rollback()
    return row


def known_alias(conn, name) -> str | None:
    """The alias name if it exists (so unknown, client-chosen strings never reach a table or a metric label)."""
    if not isinstance(name, str) or len(name) > 80:
        return None
    row = conn.execute("SELECT alias FROM model_alias WHERE alias=%s", (name,)).fetchone()
    conn.rollback()
    return row["alias"] if row else None


def handle(client: dict, key_id: int | None, endpoint: str, raw: bytes) -> Result:
    """Serve one chat or embeddings request for an authenticated client. Raises GatewayError for every refusal."""
    started = time.perf_counter()
    path, kind, _ = ENDPOINTS[endpoint]
    ev = Event(client["id"], client["name"], key_id, path)
    try:
        return _serve(client, endpoint, path, kind, raw, ev)
    except GatewayError as exc:
        ev.status, ev.error_class = exc.status, exc.code
        raise
    except Exception:
        ev.status, ev.error_class = 500, "internal_error"
        raise
    finally:
        ev.latency_ms = round((time.perf_counter() - started) * 1000)
        _finish(ev)


def _finish(ev: Event) -> None:
    label_alias = ev.alias or "-"
    m.REQUESTS.labels(ev.client_name, label_alias, str(ev.status)).inc()
    if ev.status < 400 and ev.total_tokens:
        m.TOKENS.labels(ev.client_name, label_alias, "prompt").inc(ev.prompt_tokens)
        m.TOKENS.labels(ev.client_name, label_alias, "completion").inc(ev.completion_tokens)
    try:
        with core.db.pool().connection() as conn:
            usage.record(conn, ev)
    except Exception as exc:  # noqa: BLE001 - metering must not turn a served request into an error; say so without content
        logging.getLogger("llmgw").warning("could not record a usage row: %s", type(exc).__name__)


def _serve(client: dict, endpoint: str, path: str, kind: str, raw: bytes, ev: Event) -> Result:
    if not client["enabled"]:
        raise GatewayError(403, "client_disabled", "this client is disabled", "permission_error")
    rate_check(client["id"], client["rpm_limit"])
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise GatewayError(400, "invalid_json", "the request body is not valid JSON") from None
    with core.db.pool().connection() as conn:
        ev.alias = known_alias(conn, body.get("model") if isinstance(body, dict) else None)
    fields, prompt_chars = validate(endpoint, body)
    with core.db.pool().connection() as conn:
        alias = _alias_row(conn, body["model"])
        if not alias or not alias["enabled"]:
            raise GatewayError(404, "model_not_found", f"unknown model alias {body['model'][:60]!r}; GET /v1/models lists yours")
        if alias["kind"] != kind:
            raise GatewayError(400, "wrong_model_kind", f"{alias['alias']!r} is a {alias['kind']} model; this endpoint serves {kind} models")
        allowed = client["allowed_aliases"]
        if "*" not in allowed and alias["alias"] not in allowed:
            raise GatewayError(403, "model_not_allowed", f"this client may not use {alias['alias']!r}", "permission_error")
        over = False
        budget = client["monthly_token_budget"]
        if budget:
            used = usage.month_tokens(conn, client["id"])
            if used >= budget:
                if client["budget_mode"] == "hard":
                    m.BUDGET_REJECTIONS.labels(client["name"]).inc()
                    raise GatewayError(429, "budget_exceeded", f"this client's monthly token budget ({budget:,}) is used up",
                                       "insufficient_quota", {"Retry-After": str(usage.seconds_to_next_month(conn))})
                over = True
        ev.over_budget = over
    return _call_targets(alias, path, fields, prompt_chars, ev)


def _call_targets(alias: dict, path: str, fields: dict, prompt_chars: int, ev: Event) -> Result:
    last: providers.Upstream | None = None
    targets = [t for t in alias["targets"] if t["enabled"]]
    for i, t in enumerate(targets):
        ev.attempts += 1
        ev.provider_name, ev.upstream_model = t["name"], t["upstream_model"]
        began = time.perf_counter()
        res = providers.call(t, path, fields, t["upstream_model"], alias["embedding_dim"])
        if t["kind"] != "echo" and res.fail != "locked":
            m.UPSTREAM.labels(t["name"]).observe(time.perf_counter() - began)
        if res.ok:
            return _success(alias, res.data, prompt_chars, ev)
        last = res
        if not res.retryable:
            break
        if i + 1 < len(targets):
            m.FAILOVERS.inc()
    raise _upstream_error(last)


def _success(alias: dict, data: dict, prompt_chars: int, ev: Event) -> Result:
    p, c, total, est = tokens_from(data, prompt_chars, _completion_chars(data))
    ev.prompt_tokens, ev.completion_tokens, ev.total_tokens, ev.estimated = p, c, total, est
    ev.cost_usd, ev.status = cost_of(alias, p, c), 200
    data["model"] = alias["alias"]
    return Result(200, json.dumps(data).encode(), {})


def _upstream_error(res: providers.Upstream | None) -> GatewayError:
    if res is None:
        return GatewayError(503, "no_available_target", "every target of this model is disabled", "api_error")
    if res.fail == "locked":
        return GatewayError(503, "vault_locked", res.message, "api_error")
    if res.fail == "http_4xx" and res.status in {400, 413, 422}:
        return GatewayError(res.status, "upstream_rejected", f"the provider rejected the request (HTTP {res.status}): {res.message}")
    if res.fail == "http_4xx":
        return GatewayError(502, "upstream_misconfigured", f"the provider answered HTTP {res.status}; ask the gateway administrator to check the provider account", "api_error")
    if res.fail == "http_429":
        headers = {"Retry-After": str(res.retry_after)} if res.retry_after else {}
        return GatewayError(429, "upstream_rate_limited", "the provider is rate limiting this gateway; retry shortly", "rate_limit_error", headers)
    return GatewayError(502, "upstream_unavailable", f"no target could answer ({res.fail}): {res.message}"[:400], "api_error")


def list_models(conn, client: dict) -> list[dict]:
    rows = conn.execute("SELECT alias, kind, extract(epoch FROM created_at)::bigint AS created FROM model_alias WHERE enabled ORDER BY alias").fetchall()
    conn.rollback()
    allowed = client["allowed_aliases"]
    return [{"id": r["alias"], "object": "model", "created": r["created"], "owned_by": "llmgw", "kind": r["kind"]}
            for r in rows if "*" in allowed or r["alias"] in allowed]
