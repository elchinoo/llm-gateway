"""Prometheus metrics. The shared family names (http_requests_total, http_request_duration_seconds, db_up, app_info,
process_*) come from agentpg-kit, so one PagerLens agent preset reads every app; the gateway's own are added here.
Label values are admin-defined (client names, model aliases, provider names) or fixed words, so cardinality is bounded."""
from prometheus_client import Counter, Histogram

from .core import db, metrics

APP = metrics.app
REGISTRY = metrics.registry
observe = metrics.observe

REQUESTS = Counter("llmgw_requests_total", "Data-plane requests by client, model alias and HTTP status", ["client", "alias", "status"],
                   registry=REGISTRY)
TOKENS = Counter("llmgw_tokens_total", "Tokens metered (upstream usage, or an estimate); kind=reasoning is the thinking part already inside completion", ["client", "alias", "kind"], registry=REGISTRY)
UPSTREAM = Histogram("llmgw_upstream_latency_seconds", "Latency of calls to providers", ["provider"], registry=REGISTRY,
                     buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120))
EMPTY_CONTENT = Counter("llmgw_empty_content_total", "200 answers with empty content but a reasoning field (a thinking model cut off by max_tokens)",
                        ["alias"], registry=REGISTRY)
BUDGET_REJECTIONS = Counter("llmgw_budget_rejections_total", "Requests refused because the monthly token budget was used up", ["client"],
                            registry=REGISTRY)
FAILOVERS = Counter("llmgw_failovers_total", "Times a request moved on to the next target of a model alias", registry=REGISTRY)


def _probe() -> dict[str, float]:
    """Evaluated at scrape time: is the database reachable."""
    with db.connect(retries=1) as conn:
        conn.execute("SELECT 1")
    return {"db_up": 1}


metrics.add_db_collector(_probe)
metrics.add_insights_collector(lambda: db.connect(retries=1))    # db_insights_* gauges (EDB query statistics, cached): the gateway's own database shows up in PagerLens


def render() -> tuple[bytes, str]:
    return metrics.render()
