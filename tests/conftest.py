import os

import httpx
import pytest

if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to run DB tests")

UPSTREAM_KEY = "sk-upstream-SECRET-0123456789abcdef"


@pytest.fixture(scope="session")
def _schema():
    if not os.environ.get("TEST_DATABASE_URL"):
        yield
        return
    from llmgw import migrate
    from llmgw.core import db

    with db.connect() as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    migrate.run()
    yield
    db.close_pool()


@pytest.fixture(scope="session")
def client(_schema):
    from fastapi.testclient import TestClient

    from llmgw import api

    with TestClient(api.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fresh(request, monkeypatch):
    """Every DB test starts from empty tables (plus the seeded echo demo), a locked vault and no cached state."""
    if "client" not in request.fixturenames:
        yield
        return
    from llmgw import aliases, api, core, gateway
    from llmgw.db import connect

    for var in ("LLMGW_API_KEY", "LLMGW_ENCRYPTION_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLMGW_API_KEY_FILE", "/nonexistent/key")
    monkeypatch.setenv("LLMGW_ENCRYPTION_KEY_FILE", "/nonexistent/key")
    monkeypatch.setenv("LLMGW_ALLOW_PRIVATE_URLS", "false")
    with connect() as conn:
        tables = [r["tablename"] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'")]
        conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")
        conn.commit()
        aliases.seed_echo(conn)
    for cache in (gateway.reset_rate_limits, core.auth.limiter.clear, api.data_limiter.clear):
        cache()
    core.local._setup_code = None
    request.getfixturevalue("client").cookies.clear()
    core.vault.lock()
    saved = core.llm._http
    yield
    core.llm._http = saved
    request.getfixturevalue("client").cookies.clear()
    core.vault.lock()


# ---- helpers -------------------------------------------------------------------------------------------------------
CSRF = {"X-LLMGW-CSRF": "1"}
STATIC_KEY = "operator-test-key-0123456789-abcdefgh"


@pytest.fixture
def op(monkeypatch):
    """Operator headers: the static full-access key."""
    monkeypatch.setenv("LLMGW_API_KEY", STATIC_KEY)
    return {"Authorization": f"Bearer {STATIC_KEY}"}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class Upstream:
    """A mock provider network: `hosts` maps a host name to a handler(request) -> httpx.Response (or an exception to raise)."""

    def __init__(self):
        self.hosts: dict = {}
        self.calls: list[httpx.Request] = []
        self.dns_private = False

    @staticmethod
    def host_of(request: httpx.Request) -> str:
        """The provider's name. The kit pins the connection to the validated address, so the URL carries the IP and the name travels
        in the Host header (and the TLS server name)."""
        return request.headers["host"].split(":")[0]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        fn = self.hosts[self.host_of(request)]
        out = fn(request) if callable(fn) else fn
        if isinstance(out, Exception):
            raise out
        return out

    def hosts_called(self) -> list[str]:
        return [self.host_of(c) for c in self.calls]


def chat_ok(content="hello from upstream", usage=None, **extra):
    body = {"id": "x", "object": "chat.completion", "model": "upstream-model-name",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], **extra}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


@pytest.fixture
def upstream(monkeypatch):
    """Mock network for real (non-echo) providers: transport injected into the kit's LLMClient; DNS answers a public address
    for *.test names unless `upstream.dns_private` is set."""
    from llmgw import core

    up = Upstream()
    core.llm._http = lambda: httpx.Client(transport=httpx.MockTransport(up.handler), follow_redirects=False)
    import socket
    real = socket.getaddrinfo

    def fake(host, port, *a, **k):
        if str(host).endswith(".test"):
            ip = "10.1.2.3" if up.dns_private else "93.184.216.34"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 443))]
        return real(host, port, *a, **k)

    monkeypatch.setattr("apgkit.netguard.socket.getaddrinfo", fake)
    return up


@pytest.fixture
def vault_ready(client):
    """An admin password exists, so the vault is initialised and unlocked (provider keys can be stored)."""
    from llmgw import core
    from llmgw.db import connect

    with connect() as conn:
        core.local.set_admin_password(conn, "correct horse battery staple")
    return "correct horse battery staple"


def add_provider(client, op, name="up1", host="llm1.example.test", key=UPSTREAM_KEY):
    r = client.post("/api/providers", headers=op, json={"name": name, "kind": "openai", "base_url": f"https://{host}/v1", "api_key": key})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_alias(client, op, alias, targets, kind="chat", **extra):
    """targets: [(provider_id, upstream_model), ...]"""
    r = client.put(f"/api/models/{alias}", headers=op, json={"kind": kind, **extra, "targets": [{"provider_id": p, "upstream_model": m} for p, m in targets]})
    assert r.status_code == 200, r.text
    return r.json()


def add_client(client, op, name="trustdesk", **extra):
    r = client.post("/api/clients", headers=op, json={"name": name, **extra})
    assert r.status_code == 201, r.text
    out = r.json()
    return out["client"]["id"], bearer(out["key"]["key"]) if out.get("key") else None


def ask(client, headers, content="hi", model="echo-chat", **extra):
    return client.post("/v1/chat/completions", headers=headers, json={"model": model, "messages": [{"role": "user", "content": content}], **extra})


def all_table_text(conn) -> str:
    """Every row of every table as text, to scan for things that must never be stored."""
    parts = []
    for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall():
        for row in conn.execute(f'SELECT t::text AS x FROM "{r["tablename"]}" t').fetchall():
            parts.append(row["x"])
    return "\n".join(parts)
