"""Usage metering: one row per model request (metadata only) and the queries behind the dashboard, the API and the CSV export."""
import csv
import io
from dataclasses import dataclass

from .clients import month_start_sql


@dataclass
class Event:
    client_id: int | None
    client_name: str
    key_id: int | None
    endpoint: str
    alias: str | None = None
    provider_name: str | None = None
    upstream_model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False
    cost_usd: float = 0.0
    latency_ms: int = 0
    status: int = 500
    error_class: str | None = None
    attempts: int = 0
    over_budget: bool = False
    reasoning_tokens: int | None = None     # thinking tokens (already inside completion_tokens), when the upstream reports them


def record(conn, e: Event) -> None:
    conn.execute(
        "INSERT INTO usage_event (client_id, client_name, key_id, endpoint, alias, provider_name, upstream_model, prompt_tokens, "
        "completion_tokens, total_tokens, estimated, cost_usd, latency_ms, status, error_class, attempts, over_budget, reasoning_tokens) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (e.client_id, e.client_name, e.key_id, e.endpoint, e.alias, e.provider_name, e.upstream_model, e.prompt_tokens,
         e.completion_tokens, e.total_tokens, e.estimated, e.cost_usd, e.latency_ms, e.status, e.error_class, e.attempts, e.over_budget, e.reasoning_tokens))
    conn.commit()


def month_tokens(conn, client_id: int) -> int:
    row = conn.execute(f"SELECT coalesce(sum(total_tokens),0)::bigint AS n FROM usage_event WHERE client_id=%s AND created_at >= {month_start_sql()}",
                       (client_id,)).fetchone()
    conn.rollback()
    return row["n"]


def seconds_to_next_month(conn) -> int:
    row = conn.execute(f"SELECT ceil(extract(epoch FROM ({month_start_sql()} + interval '1 month') - now()))::int AS s").fetchone()
    conn.rollback()
    return max(1, row["s"])


GROUPS = {
    "client": "client_name",
    "alias": "coalesce(alias, '(none)')",
    "provider": "coalesce(provider_name, '(none)')",
    "day": "to_char(timezone('UTC', created_at), 'YYYY-MM-DD')",
}
_AGG = ("count(*)::int AS requests, count(*) FILTER (WHERE status >= 400)::int AS errors, "
        "coalesce(sum(prompt_tokens),0)::bigint AS prompt_tokens, coalesce(sum(completion_tokens),0)::bigint AS completion_tokens, "
        "coalesce(sum(total_tokens),0)::bigint AS total_tokens, coalesce(sum(reasoning_tokens),0)::bigint AS reasoning_tokens, "
        "coalesce(sum(cost_usd),0)::float AS cost_usd, "
        "count(*) FILTER (WHERE estimated)::int AS estimated_requests, coalesce(round(avg(latency_ms)),0)::int AS avg_latency_ms, "
        "coalesce(round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)),0)::int AS p95_latency_ms, "
        "count(*) FILTER (WHERE over_budget)::int AS over_budget_requests")


def _where(days: int, client: str | None, alias: str | None) -> tuple[str, list]:
    return ("created_at >= now() - make_interval(days => %s) AND (%s::text IS NULL OR client_name=%s) AND (%s::text IS NULL OR alias=%s)",
            [days, client, client, alias, alias])


def summary(conn, group: str, days: int = 30, client: str | None = None, alias: str | None = None) -> list[dict]:
    if group not in GROUPS:
        raise ValueError("group must be one of: " + ", ".join(GROUPS))
    where, args = _where(days, client, alias)
    rows = conn.execute(f"SELECT {GROUPS[group]} AS key, {_AGG} FROM usage_event WHERE {where} GROUP BY 1 "
                        f"ORDER BY {'1' if group == 'day' else 'total_tokens DESC, requests DESC'}", args).fetchall()
    conn.rollback()
    return rows


def recent(conn, limit: int = 50, errors_only: bool = False) -> list[dict]:
    rows = conn.execute("SELECT id, created_at, client_name, endpoint, alias, provider_name, upstream_model, total_tokens, reasoning_tokens, estimated, "
                        "cost_usd::float AS cost_usd, latency_ms, status, error_class, attempts, over_budget FROM usage_event "
                        "WHERE (NOT %s OR status >= 400) ORDER BY id DESC LIMIT %s", (errors_only, limit)).fetchall()
    conn.rollback()
    return rows


def overview(conn, days: int) -> dict:
    where, args = _where(days, None, None)
    totals = conn.execute(f"SELECT {_AGG} FROM usage_event WHERE {where}", args).fetchone()
    daily = summary(conn, "day", days)
    models = summary(conn, "alias", days)[:8]
    errors = conn.execute(f"SELECT coalesce(error_class, 'http_' || status) AS error_class, count(*)::int AS n FROM usage_event "
                          f"WHERE {where} AND status >= 400 GROUP BY 1 ORDER BY n DESC LIMIT 8", args).fetchall()
    month = {r["client_id"]: r for r in conn.execute(
        f"SELECT client_id, coalesce(sum(total_tokens),0)::bigint AS tokens, count(*)::int AS requests, coalesce(sum(cost_usd),0)::float AS cost_usd "
        f"FROM usage_event WHERE client_id IS NOT NULL AND created_at >= {month_start_sql()} GROUP BY 1")}
    period = {r["key"]: r for r in summary(conn, "client", days)}
    clients = []
    for c in conn.execute("SELECT id, name, rpm_limit, monthly_token_budget, budget_mode, enabled FROM client ORDER BY name"):
        m, p = month.get(c["id"], {}), period.get(c["name"], {})
        used, budget = m.get("tokens", 0), c["monthly_token_budget"]
        clients.append({"name": c["name"], "enabled": c["enabled"], "requests": p.get("requests", 0), "tokens": p.get("total_tokens", 0),
                        "cost_usd": p.get("cost_usd", 0.0), "errors": p.get("errors", 0), "budget": budget, "budget_mode": c["budget_mode"],
                        "tokens_month": used, "cost_month": m.get("cost_usd", 0.0),
                        "budget_used": None if not budget else round(used / budget, 4), "over_budget": bool(budget and used >= budget)})
    conn.rollback()
    banner = {"hard": [c["name"] for c in clients if c["over_budget"] and c["budget_mode"] == "hard"],
              "soft": [c["name"] for c in clients if c["over_budget"] and c["budget_mode"] == "soft"]}
    return {"days": days, "totals": totals, "daily": daily, "clients": clients, "models": models, "errors": errors, "budget_alerts": banner}


def _safe_cell(v):
    """Spreadsheet formula injection: a name that starts with = + - or @ would run as a formula when the CSV is opened."""
    return "'" + v if isinstance(v, str) and v[:1] in {"=", "+", "-", "@"} else v


def to_csv(rows: list[dict], group: str) -> str:
    out = io.StringIO()
    cols = ["key", "requests", "errors", "prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens", "estimated_requests", "cost_usd",
            "avg_latency_ms", "p95_latency_ms", "over_budget_requests"]
    w = csv.writer(out)
    w.writerow([group if c == "key" else c for c in cols])
    for r in rows:
        w.writerow([_safe_cell(r[c]) for c in cols])
    return out.getvalue()


def prune(conn, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    row = conn.execute("WITH d AS (DELETE FROM usage_event WHERE created_at < now() - make_interval(days => %s) RETURNING 1) "
                       "SELECT count(*)::int AS n FROM d", (retention_days,)).fetchone()
    conn.commit()
    return row["n"]
