"""The data plane: validate an OpenAI-style request, enforce the client's limits, call the alias's targets in order, meter it.

Only an allow-list of fields is forwarded (function tools and their tool_choice/parallel_tool_calls, but no `user` and no
provider-specific passthrough), the request is never streamed, and neither prompts, tool schemas, tool arguments nor completions
are stored anywhere: a usage row holds counts, names, latency and status.
"""
import json
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

from . import core, providers, usage
from . import metrics as m
from .usage import Event

MAX_BODY = 512 * 1024
MAX_MESSAGES, MAX_INPUTS = 500, 256
ROLES = {"system", "user", "assistant", "developer", "tool"}
CHAT_FIELDS = {"model", "messages", "max_tokens", "temperature", "top_p", "stop", "response_format", "seed", "presence_penalty",
               "frequency_penalty", "n", "tools", "tool_choice", "parallel_tool_calls"}
# Tool calling (function tools only). Bounds keep one request from carrying an unbounded schema or argument payload.
MAX_TOOLS, MAX_TOOL_BYTES, MAX_TOOLS_BYTES = 64, 16 * 1024, 128 * 1024
MAX_TOOL_DEPTH, MAX_TOOL_DESCRIPTION = 8, 4096
MAX_CALLS_PER_MESSAGE, MAX_CALL_ARGS, MAX_CALL_ID = 64, 64 * 1024, 128
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
CHOICES = {"none", "auto", "required"}
EMBED_FIELDS = {"model", "input", "dimensions", "encoding_format"}
REASONING_FIELDS = ("reasoning", "reasoning_content")
EMPTY_NOTE = "empty-content-reasoning-only"
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
    raise GatewayError(400, "invalid_messages", f"{where}: content must be a string (or a list of text parts); images and audio are not supported")


def _bad(code: str, message: str) -> GatewayError:
    return GatewayError(400, code, message)


def _depth(value, limit: int, level: int = 1) -> bool:
    """True when the JSON value nests at most `limit` levels (it stops descending at the limit, so it cannot overflow)."""
    if level > limit:
        return False
    kids = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return all(_depth(k, limit, level + 1) for k in kids)


def _tools(tools) -> tuple[int, set[str]]:
    """Check `tools`: a list of function tools, bounded in count and size. Returns (characters, names)."""
    if not isinstance(tools, list) or not 1 <= len(tools) <= MAX_TOOLS:
        raise _bad("invalid_tools", f"'tools' must be a list of 1 to {MAX_TOOLS} function tools")
    names: set[str] = set()
    total = 0
    for i, tool in enumerate(tools):
        where = f"tools[{i}]"
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(tool, dict) or set(tool) != {"type", "function"} or tool["type"] != "function" or not isinstance(fn, dict):
            raise _bad("invalid_tools", f"{where} must be {{type: 'function', function: {{name, description?, parameters?}}}}; only function tools are supported")
        if set(fn) - {"name", "description", "parameters", "strict"} or not isinstance(fn.get("name"), str) or not TOOL_NAME.match(fn["name"]):
            raise _bad("invalid_tools", f"{where}.function needs a name of 1 to 64 letters, digits, '_' or '-', and only name, description, parameters, strict")
        if fn["name"] in names:
            raise _bad("invalid_tools", f"{where}: duplicate tool name {fn['name']!r}")
        names.add(fn["name"])
        desc = fn.get("description")
        if desc is not None and not (isinstance(desc, str) and len(desc) <= MAX_TOOL_DESCRIPTION):
            raise _bad("invalid_tools", f"{where}.function.description must be a string of at most {MAX_TOOL_DESCRIPTION} characters")
        if "strict" in fn and fn["strict"] is not None and not isinstance(fn["strict"], bool):
            raise _bad("invalid_tools", f"{where}.function.strict must be a boolean")
        params = fn.get("parameters")
        if params is not None and not (isinstance(params, dict) and params.get("type", "object") == "object" and _depth(params, MAX_TOOL_DEPTH)):
            raise _bad("invalid_tools", f"{where}.function.parameters must be a JSON-schema object (type 'object') nested at most {MAX_TOOL_DEPTH} levels")
        size = len(json.dumps(tool, separators=(",", ":"), ensure_ascii=False))
        if size > MAX_TOOL_BYTES:
            raise _bad("invalid_tools", f"{where} is larger than {MAX_TOOL_BYTES // 1024} KB")
        total += size
    if total > MAX_TOOLS_BYTES:
        raise _bad("invalid_tools", f"'tools' are larger than {MAX_TOOLS_BYTES // 1024} KB in total")
    return total, names


def _tool_choice(body: dict, names: set[str] | None) -> None:
    choice = body.get("tool_choice")
    if choice is None and body.get("parallel_tool_calls") is None:
        return
    if choice is not None:
        named = choice.get("function") if isinstance(choice, dict) else None
        ok = choice in CHOICES if isinstance(choice, str) else (isinstance(choice, dict) and set(choice) == {"type", "function"}
                                                               and choice["type"] == "function" and isinstance(named, dict)
                                                               and set(named) == {"name"} and isinstance(named["name"], str))
        if not ok:
            raise _bad("invalid_tool_choice", "'tool_choice' must be none, auto, required or {type: 'function', function: {name}}")
        if choice != "none" and not names:
            raise _bad("invalid_tool_choice", "'tool_choice' needs 'tools' (only 'none' may be sent without them)")
        if isinstance(choice, dict) and named["name"] not in names:
            raise _bad("invalid_tool_choice", f"'tool_choice' names {named['name'][:40]!r}, which is not in 'tools'")
    if body.get("parallel_tool_calls") is not None:
        if not isinstance(body["parallel_tool_calls"], bool):
            raise _bad("invalid_parallel_tool_calls", "'parallel_tool_calls' must be a boolean")
        if not names:
            raise _bad("invalid_parallel_tool_calls", "'parallel_tool_calls' needs 'tools'")


def _tool_calls(calls, where: str) -> tuple[int, list[str]]:
    """Check an assistant message's `tool_calls`. Returns (characters of names + arguments, call ids)."""
    if not isinstance(calls, list) or not 1 <= len(calls) <= MAX_CALLS_PER_MESSAGE:
        raise _bad("invalid_messages", f"{where}.tool_calls must be a list of 1 to {MAX_CALLS_PER_MESSAGE} tool calls")
    chars, ids, clean = 0, [], []
    for j, call in enumerate(calls):
        at = f"{where}.tool_calls[{j}]"
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(call, dict) or not {"id", "type", "function"} <= set(call) or call["type"] != "function" or not isinstance(fn, dict):
            raise _bad("invalid_messages", f"{at} must be {{id, type: 'function', function: {{name, arguments}}}}")
        cid = call["id"]
        if not isinstance(cid, str) or not 1 <= len(cid) <= MAX_CALL_ID or not cid.isprintable() or cid in ids:
            raise _bad("invalid_messages", f"{at}.id must be a unique printable string of 1 to {MAX_CALL_ID} characters")
        if not {"name", "arguments"} <= set(fn) or not isinstance(fn["name"], str) or not TOOL_NAME.match(fn["name"]) \
                or not isinstance(fn["arguments"], str) or len(fn["arguments"]) > MAX_CALL_ARGS:
            raise _bad("invalid_messages", f"{at}.function needs a tool name and `arguments` as a string of at most {MAX_CALL_ARGS // 1024} KB")
        ids.append(cid)
        chars += len(fn["name"]) + len(fn["arguments"])
        clean.append({"id": cid, "type": "function", "function": {"name": fn["name"], "arguments": fn["arguments"]}})
    calls[:] = clean        # providers add keys such as `index`; they are dropped, not forwarded
    return chars, ids


def _message(msg, i: int, pending: set[str]) -> int:
    """Check one chat message and return its prompt characters. `pending` holds the tool-call ids still waiting for a result."""
    where = f"messages[{i}]"
    if not isinstance(msg, dict) or set(msg) - {"role", "content", "name", "tool_calls", "tool_call_id"} or msg.get("role") not in ROLES:
        raise _bad("invalid_messages", f"{where} must be an object with a role ({', '.join(sorted(ROLES))}), content and optionally name "
                                       "(assistant: tool_calls; tool: tool_call_id)")
    role, chars = msg["role"], 0
    if "name" in msg and (not isinstance(msg["name"], str) or len(msg["name"]) > 64):
        raise _bad("invalid_messages", f"{where}.name must be a short string")
    if "tool_calls" in msg and role != "assistant":
        raise _bad("invalid_messages", f"{where}: only assistant messages carry tool_calls")
    if "tool_call_id" in msg and role != "tool":
        raise _bad("invalid_messages", f"{where}: only tool messages carry tool_call_id")
    if role == "tool":
        cid = msg.get("tool_call_id")
        if not isinstance(msg.get("content"), str):
            raise _bad("invalid_messages", f"{where}: a tool message needs string content (the tool's result)")
        if not isinstance(cid, str) or cid not in pending:
            raise _bad("invalid_messages", f"{where}.tool_call_id must answer a tool call of the assistant message before it, once")
        pending.discard(cid)
        return len(msg["content"])
    if role == "assistant" and msg.get("tool_calls") not in (None, []):           # an empty list (some clients send it) means none
        chars, ids = _tool_calls(msg["tool_calls"], where)
        pending.clear()
        pending.update(ids)
        if msg.get("content") is not None:                       # null (or absent) content is fine next to tool calls
            chars += len(_text(msg["content"], where))
        return chars
    pending.clear()
    return len(_text(msg.get("content"), where))


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
        pending: set[str] = set()
        for i, msg in enumerate(msgs):
            chars += _message(msg, i, pending)
        names = None
        if body.get("tools") is not None:
            tool_chars, names = _tools(body["tools"])
            chars += tool_chars                                  # the schemas are prompt too: they count for budgets and estimates
        _tool_choice(body, names)
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
        if isinstance(msg, dict):
            n += providers.call_chars(msg.get("tool_calls"))
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
            "SELECT t.position, t.upstream_model, t.extra_body, p.* FROM alias_target t JOIN provider p ON p.id = t.provider_id WHERE t.alias=%s ORDER BY t.position",
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
        if ev.reasoning_tokens:
            m.TOKENS.labels(ev.client_name, label_alias, "reasoning").inc(ev.reasoning_tokens)
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
    except (ValueError, UnicodeDecodeError, RecursionError):
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
        res = providers.call(t, path, fields, t["upstream_model"], alias["embedding_dim"], t["extra_body"])
        if t["kind"] != "echo" and res.fail != "locked":
            m.UPSTREAM.labels(t["name"]).observe(time.perf_counter() - began)
        if res.ok:
            return _success(alias, res.data, prompt_chars, ev, path)
        last = res
        if not res.retryable:
            break
        if i + 1 < len(targets):
            m.FAILOVERS.inc()
    raise _upstream_error(last)


def reasoning_tokens_from(data: dict) -> int | None:
    """Thinking tokens the upstream reports (usage.completion_tokens_details.reasoning_tokens), or None when it does not."""
    u = data.get("usage")
    details = u.get("completion_tokens_details") if isinstance(u, dict) else None
    return _int(details.get("reasoning_tokens")) if isinstance(details, dict) else None


def reasoning_only(data: dict) -> bool:
    """True when the first choice has no answer text but carries reasoning: a thinking model that ran out of max_tokens."""
    choices = data.get("choices")
    msg = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    if not isinstance(msg, dict) or msg.get("content") not in (None, "") or msg.get("tool_calls"):
        return False                                         # a tool call is an answer: null content next to it is normal
    return any(msg.get(k) for k in REASONING_FIELDS)


def _success(alias: dict, data: dict, prompt_chars: int, ev: Event, path: str = "chat/completions") -> Result:
    p, c, total, est = tokens_from(data, prompt_chars, _completion_chars(data))
    ev.prompt_tokens, ev.completion_tokens, ev.total_tokens, ev.estimated = p, c, total, est
    ev.reasoning_tokens = reasoning_tokens_from(data)
    ev.cost_usd, ev.status = cost_of(alias, p, c), 200
    data["model"] = alias["alias"]
    headers = {}
    if path == "chat/completions" and reasoning_only(data):
        # Passed through unchanged: the app decides. The header and the metric tell the operator it happens.
        headers["x-llmgw-note"] = EMPTY_NOTE
        m.EMPTY_CONTENT.labels(alias["alias"]).inc()
    return Result(200, json.dumps(data).encode(), headers)


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
