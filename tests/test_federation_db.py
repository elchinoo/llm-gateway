"""LLM gateway federated: people and services authenticate with the ecosystem identity provider; roles decide what they may do."""
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from apgkit.auth import Federation, RelyingParty, TokenVerifier
from joserfc import jwt
from joserfc.jwk import RSAKey

from tests.conftest import CSRF, ask, needs_db

pytestmark = needs_db

from llmgw import api, core  # noqa: E402
from llmgw.db import connect  # noqa: E402

ISS, AUD = "https://id.example.org", "llmgw"
KEY = RSAKey.generate_key(2048, {"kid": "k1", "use": "sig", "alg": "RS256"})


class Provider:
    def __init__(self):
        self.nonce = None
        self.id_claims = {"sub": "usr_1", "name": "Ada", "email": "ada@example.org", "roles": ["llmgw:operator"]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISS, "jwks_uri": f"{ISS}/jwks", "authorization_endpoint": f"{ISS}/oauth/authorize",
                                             "token_endpoint": f"{ISS}/oauth/token", "end_session_endpoint": f"{ISS}/oauth/logout"})
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [KEY.as_dict(private=False)]})
        if path == "/oauth/token":
            claims = {"iss": ISS, "aud": AUD, "exp": int(time.time()) + 300, "nonce": self.nonce, **self.id_claims}
            return httpx.Response(200, json={"id_token": jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY), "access_token": "x"})
        return httpx.Response(404)

    def http(self):
        return lambda: httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=False)


def access(**claims) -> dict:
    base = {"iss": ISS, "aud": AUD, "sub": "usr_1", "exp": int(time.time()) + 600, "sub_type": "user"}
    base.update(claims)
    return {"Authorization": "Bearer " + jwt.encode({"alg": "RS256", "kid": "k1", "typ": "at+jwt"}, base, KEY)}


def service(scope: str, name: str = "trustdesk") -> dict:
    return access(sub=name, sub_type="service", scope=scope, client_id=name, client_name=name)


@pytest.fixture(autouse=True)
def _federated(monkeypatch, client):
    provider = Provider()
    verifier = TokenVerifier(ISS, AUD, http=provider.http())
    monkeypatch.setattr(core.auth, "verifier", verifier)
    monkeypatch.setattr(api, "fed", Federation(ISS, AUD, verifier, RelyingParty(ISS, AUD, None, http=provider.http()), True))
    client.provider = provider
    yield


def test_roles_decide_what_a_person_may_do(client):
    admin, operator = access(sub="u1", roles=["llmgw:admin"]), access(sub="u2", roles=["llmgw:operator"])
    viewer, stranger = access(sub="u3", roles=["llmgw:viewer"]), access(sub="u4", roles=["otherapp:admin"])
    for who in (admin, operator, viewer):
        for path in ("/api/overview", "/api/clients", "/api/models", "/api/providers", "/api/usage"):
            assert client.get(path, headers=who).status_code == 200, path
    assert client.get("/api/overview", headers=stranger).status_code == 403                  # signed in, but no role in this app
    assert client.get("/api/audit", headers=admin).status_code == 200 and client.get("/api/audit", headers=operator).status_code == 200
    assert client.get("/api/audit", headers=viewer).status_code == 403
    pid = client.get("/api/providers", headers=admin).json()["items"][0]["id"]
    body = {"kind": "chat", "targets": [{"provider_id": pid, "upstream_model": "m"}]}
    assert client.put("/api/models/x", headers=viewer, json=body).status_code == 403
    assert client.put("/api/models/x", headers=operator, json=body).status_code == 200
    assert client.post("/api/clients", headers=viewer, json={"name": "a"}).status_code == 403
    assert client.post("/api/clients", headers=operator, json={"name": "a"}).status_code == 201
    # operators manage clients and models but not provider keys, tokens, settings or the vault
    assert client.post("/api/providers", headers=operator, json={"name": "p", "kind": "echo"}).status_code == 403
    assert client.post("/api/providers", headers=admin, json={"name": "p", "kind": "echo"}).status_code == 201
    assert client.post(f"/api/providers/{pid}/test", headers=operator, json={"model": "m"}).status_code == 403
    assert client.put("/api/settings", headers=operator, json={"default_rpm": 5}).status_code == 403
    assert client.put("/api/settings", headers=admin, json={"default_rpm": 5}).status_code == 200
    assert client.post("/api/tokens", headers=operator, json={"name": "x", "scope": "full"}).status_code == 403
    assert client.post("/api/vault/unlock", headers=operator, json={"password": "x"}).status_code == 403
    assert client.get("/metrics", headers=admin).status_code == 200 and client.get("/metrics", headers=operator).status_code == 403
    audit = client.get("/api/audit", headers=admin).json()["items"]
    assert {i["actor"] for i in audit} >= {"u1", "u2"}                                       # the person is named in the trail


def test_service_scopes(client):
    read, metrics, use = service("llmgw.read", "dash"), service("llmgw.metrics", "prom"), service("llmgw.use", "trustdesk")
    assert client.get("/api/overview", headers=read).status_code == 200
    assert client.post("/api/clients", headers=read, json={"name": "x"}).status_code == 403
    assert client.get("/metrics", headers=read).status_code == 403
    assert client.get("/metrics", headers=metrics).status_code == 200 and client.get("/api/overview", headers=metrics).status_code == 403
    for path in ("/api/overview", "/api/clients", "/metrics"):
        assert client.get(path, headers=use).status_code == 403, path                         # the data-plane scope reaches no operator route
    add_client_direct("trustdesk")
    assert client.get("/v1/models", headers=read).status_code == 403 and client.get("/v1/models", headers=metrics).status_code == 403
    assert client.get("/v1/models", headers=use).status_code == 200


def add_client_direct(name: str, **fields) -> int:
    from llmgw import clients
    with connect() as conn:
        return clients.create(conn, name=name, description="", allowed_aliases=fields.get("allowed", ["*"]), rpm_limit=fields.get("rpm"),
                              monthly_token_budget=None, budget_mode="hard")


def test_a_service_token_with_llmgw_use_is_accepted_only_for_a_registered_client(client):
    r = ask(client, service("llmgw.use", "stranger-app"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "client_not_registered"
    cid = add_client_direct("trustdesk", rpm=2)
    r = ask(client, service("llmgw.use", "trustdesk"))
    assert r.status_code == 200 and r.json()["choices"]
    with connect() as conn:
        row = conn.execute("SELECT client_id, client_name, key_id, status FROM usage_event").fetchone()
    assert (row["client_id"], row["client_name"], row["key_id"], row["status"]) == (cid, "trustdesk", None, 200)
    assert ask(client, service("llmgw.use", "trustdesk")).status_code == 200
    assert ask(client, service("llmgw.use", "trustdesk")).status_code == 429                     # the client's own limits apply
    other = access(sub="trustdesk", sub_type="service", scope="llmgw.use", aud="some-other-app")
    assert ask(client, other).status_code == 401
    # allowed aliases and disabled clients are honoured for service tokens too
    add_client_direct("narrow", allowed=["echo-chat"])
    assert client.post("/v1/embeddings", headers=service("llmgw.use", "narrow"), json={"model": "echo-embed", "input": "x"}).status_code == 403
    with connect() as conn:
        conn.execute("UPDATE client SET enabled=false WHERE name='trustdesk'")
        conn.commit()
    assert ask(client, service("llmgw.use", "trustdesk")).json()["error"]["code"] == "client_disabled"


def test_people_and_wildcards_never_reach_the_data_plane(client):
    add_client_direct("usr_1")
    for who in (access(sub="usr_1", roles=["llmgw:admin"]), access(sub="usr_1", roles=["llmgw:operator"]),
                access(sub="usr_1", scope="llmgw.use")):          # even a user token that carries the scope: services only
        r = ask(client, who)
        assert r.status_code == 403 and r.json()["error"]["code"] == "wrong_credential"
    assert client.get("/v1/models", headers=access(roles=["llmgw:admin"])).status_code == 403


def test_tokens_for_another_app_or_with_a_bad_signature_are_refused(client):
    assert client.get("/api/overview", headers=access(aud="some-other-app", roles=["llmgw:admin"])).status_code == 401
    assert client.get("/api/overview", headers=access(exp=int(time.time()) - 3600, roles=["llmgw:admin"])).status_code == 401
    assert client.get("/api/overview", headers=access(iss="https://evil.example.org", roles=["llmgw:admin"])).status_code == 401
    other = RSAKey.generate_key(2048, {"kid": "k1", "use": "sig", "alg": "RS256"})
    forged = {"Authorization": "Bearer " + jwt.encode({"alg": "RS256", "kid": "k1"}, {"iss": ISS, "aud": AUD, "sub": "trustdesk", "sub_type": "service",
                                                                                      "scope": "llmgw.use", "exp": int(time.time()) + 60}, other)}
    add_client_direct("trustdesk")
    assert client.get("/api/overview", headers=forged).status_code == 401
    r = ask(client, forged)
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"


def test_single_sign_on_login_creates_a_role_bound_session(client):
    r = client.get("/auth/login", params={"next": "/#clients"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"{ISS}/oauth/authorize?")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://testserver/auth/callback"] and q["client_id"] == [AUD]
    client.provider.nonce = q["nonce"][0]
    cb = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == "/#clients" and "lg_session" in cb.cookies
    s = client.get("/api/session").json()
    assert s["state"] == "logged_in" and s["user"]["name"] == "Ada" and s["user"]["roles"] == ["llmgw:operator"] and s["user"]["source"] == "oidc"
    assert "clients:manage" in s["user"]["permissions"] and "providers:manage" not in s["user"]["permissions"]
    assert s["sso"]["login_url"] == "/auth/login" and s.get("state") != "setup_required"
    assert client.get("/api/clients").status_code == 200
    assert client.post("/api/clients", headers=CSRF, json={"name": "viasso"}).status_code == 201
    assert client.post("/api/providers", headers=CSRF, json={"name": "x", "kind": "echo"}).status_code == 403
    assert client.post("/v1/chat/completions", json={"model": "echo-chat", "messages": [{"role": "user", "content": "x"}]}).status_code == 401
    out = client.post("/api/logout", headers=CSRF).json()
    assert out["logout_url"].startswith(f"{ISS}/oauth/logout?") and "id_token_hint=" in out["logout_url"]
    assert client.get("/api/clients").status_code == 401


def _start_login(client):
    r = client.get("/auth/login", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    client.provider.nonce = q["nonce"][0]
    return r, q


def test_the_login_is_bound_to_the_browser_that_started_it(client):
    r, q = _start_login(client)
    assert "apg_oidc_bind=" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    cookie = client.cookies.get("apg_oidc_bind")
    assert cookie
    # another browser (no cookie), or one holding some other value, is refused and gets no session; the state is burnt either way
    client.cookies.clear()
    cb = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.headers["location"].startswith("/?sso_error=") and "lg_session" not in cb.cookies
    r, q = _start_login(client)
    client.cookies.set("apg_oidc_bind", "x" * 43)
    cb = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.headers["location"].startswith("/?sso_error=") and "lg_session" not in cb.cookies
    burnt = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert burnt.headers["location"].startswith("/?sso_error=")
    # the right browser finishes, and the binding cookie is cleared afterwards
    client.cookies.clear()
    r, q = _start_login(client)
    cb = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.headers["location"] == "/" and "lg_session" in client.cookies
    assert any("apg_oidc_bind" in c and "Max-Age=0" in c for c in cb.headers.get_list("set-cookie"))
    assert client.cookies.get("apg_oidc_bind") is None


def test_sso_refuses_people_without_a_role_here(client):
    client.provider.id_claims = {"sub": "usr_9", "name": "Nobody", "roles": ["otherapp:admin"]}
    r = client.get("/auth/login", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    client.provider.nonce = q["nonce"][0]
    cb = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.headers["location"].startswith("/?sso_error=") and "lg_session" not in cb.cookies
    assert client.get("/api/clients").status_code == 401
    again = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert again.headers["location"].startswith("/?sso_error=")                                  # the state was single-use


def test_the_vault_can_start_from_the_platform_secret_when_federated(client, monkeypatch, upstream):
    monkeypatch.setenv("LLMGW_ENCRYPTION_KEY", "platform-secret-for-tests-only-0123456789")
    api._startup()
    s = client.get("/api/session", headers=access(roles=["llmgw:admin"])).json()
    assert s["vault"]["initialized"] and s["vault"]["unlocked"] and s["state"] == "logged_in"      # no setup code, no admin password
    admin = access(roles=["llmgw:admin"])
    r = client.post("/api/providers", headers=admin, json={"name": "p", "kind": "openai", "base_url": "https://llm1.example.test/v1", "api_key": "k"})
    assert r.status_code == 201 and r.json()["has_key"] is True


def test_sso_endpoints_are_404_when_federation_is_off(client, monkeypatch):
    monkeypatch.setattr(api, "fed", Federation("", AUD, None, None, True))
    assert client.get("/auth/login", follow_redirects=False).status_code == 404
    assert client.get("/api/session").json()["sso"] is None


def test_client_keys_still_work_when_federated(client):
    cid = add_client_direct("local-app")
    from llmgw import clients
    with connect() as conn:
        _, raw = clients.create_key(conn, cid, "k")
    key = {"Authorization": f"Bearer {raw}"}
    assert ask(client, key).status_code == 200
    assert client.get("/api/overview", headers=key).status_code == 403
