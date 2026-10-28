import asyncio
import contextlib
import sys
import threading
from pathlib import Path

import anyio.to_thread
import psycopg
from apgkit import masking
from apgkit.auth import Principal, TokenError
from apgkit.auth.rp import safe_return_to
from apgkit.guard import SecurityGuard
from apgkit.ratelimit import FailureLimiter
from apgkit.vault import VaultLocked
from apgkit.web import is_https, mount_kit_static, public_url
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, aliases, audit, clients, core, gateway, hardening, usage
from . import metrics as m
from . import settings as cfg
from .config import get_settings
from .core import COOKIE, CSRF_HEADER, fed, local
from .db import close_pool, connect
from .gateway import MAX_BODY, GatewayError

STATIC = Path(__file__).resolve().parent / "static"
_announced = False
_stop = threading.Event()
_threads: list[threading.Thread] = []
data_limiter = FailureLimiter(limit=30)     # bad keys on /v1, per address; valid keys are never blocked by it


def _announce_setup_code() -> None:
    global _announced
    if not _announced:
        _announced = True
        print("\n=== LLM gateway first-run setup ===\n"
              "Open the app in your browser and enter this one-time code to create the admin password:\n"
              f"    {local.setup_code()}\n"
              "(It changes every time the server restarts until setup is complete.)\n", file=sys.stderr, flush=True)


def _startup() -> None:
    """Seed the echo demo, unlock the vault if a platform secret exists, announce the setup code."""
    try:
        with connect(retries=2) as conn:
            aliases.seed_echo(conn)
            aliases.seed_default_aliases(conn)
            if fed.enabled and not core.vault.is_initialized(conn):
                core.vault.initialize_with_platform_secret(conn)   # single sign-on only: no admin password to wrap the key with
                conn.commit()
            core.vault.try_unlock_with_platform_secret(conn)
            if local.needs_setup(conn) and not fed.enabled:
                _announce_setup_code()
    except Exception:  # noqa: BLE001 - the database may be asleep; everything retries on the first request
        pass


def _housekeeping(stop: threading.Event) -> None:
    """Once an hour: drop usage rows older than the retention setting (0 keeps everything)."""
    while not stop.wait(3600):
        try:
            with connect(retries=1) as conn:
                usage.prune(conn, cfg.get_all(conn)["usage_retention_days"])
        except Exception:  # noqa: BLE001 - try again next hour
            pass


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    _stop.clear()
    anyio.to_thread.current_default_thread_limiter().total_tokens = get_settings().max_concurrent
    _startup()
    t = threading.Thread(target=_housekeeping, args=(_stop,), daemon=True, name="llmgw-housekeeping")
    t.start()
    _threads.append(t)
    # grants after startup (agentpg db-users create, default privileges) reopen the base tables: re-assert the masking every 5 minutes
    reassert = asyncio.create_task(masking.reassert_forever(
        core.db.connect, hardening.apply, hardening.REASSERT_EVERY, deny=hardening.REASSERT_DENY, metrics=core.metrics))
    try:
        yield
    finally:
        reassert.cancel()
        await asyncio.gather(reassert, return_exceptions=True)
        _stop.set()
        for th in _threads:
            th.join(timeout=3)
        _threads.clear()
        close_pool()


# ---- operator authentication ---------------------------------------------------------------------------------
# The mechanics (local admin, operator tokens, federated JWTs, sessions, rate limiting) are agentpg-kit's `Authenticator`;
# here we only name the permission each route needs. Client keys are a separate kind of principal.
_LEGACY = {"read": "usage:read", "full": "settings:manage", "metrics": "metrics:scrape"}


def require(*perms: str):
    """Operator dependency: the caller needs ANY of the permissions (or an old scope name: read, full, metrics).
    A client key never passes: it carries only `llm:use`, and these routes ask for operator permissions."""
    wanted = [_LEGACY.get(p, p) for p in (perms or ("usage:read",))]

    def dep(request: Request) -> Principal:
        p = core.auth.authenticate(request)
        if p is None:
            raise HTTPException(401, "authentication required")
        if p.kind == "client" or not any(p.can(w) for w in wanted):
            raise HTTPException(403, f"this credential cannot do that (it needs one of: {', '.join(wanted)})")
        return p

    return dep


def _client(request: Request) -> str:
    return core.auth.client(request)


def _db_unavailable() -> HTTPException:
    return core.auth.db_unavailable()


app = FastAPI(title="LLM gateway", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# /v1 enforces its own 512 KB cap (OpenAI-shaped error, chunked bodies included); the guard's default 64 KB covers /api.
app.add_middleware(SecurityGuard, csrf_header=CSRF_HEADER, csrf_prefixes=("/api/",), body_prefixes=("/api/",), observe=core.metrics.observe)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
mount_kit_static(app)


@app.exception_handler(GatewayError)
async def _gateway_error(_: Request, exc: GatewayError):
    return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)


@app.exception_handler(VaultLocked)
async def _vault_locked(_: Request, exc: VaultLocked):
    return JSONResponse({"detail": str(exc)}, status_code=423)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}


@app.get("/readyz")
def readyz():
    try:
        with connect(retries=1) as conn:
            conn.execute("SELECT 1")
        return {"status": "ready", "masking": masking.status()}      # report only: drift never fails readiness
    except Exception:  # noqa: BLE001 - never echo connection details
        return JSONResponse({"status": "waking-database", "masking": masking.status()}, status_code=503)


@app.get("/metrics", dependencies=[Depends(require("metrics:scrape"))])
def prometheus_metrics():
    body, ctype = m.render()
    return Response(body, media_type=ctype)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# ---- data plane (/v1) ------------------------------------------------------------------------------------------------
def data_client(request: Request) -> tuple[dict, int | None]:
    """Who is calling the data plane: a client key, or an identity-provider service token with `llmgw.use` whose subject is a
    registered client. Everything else (operator sessions and tokens, the static key, user JWTs, wildcards) is refused."""
    raw = core.auth.bearer(request)
    if raw is None:
        raise GatewayError(401, "missing_api_key", "send your client key as 'Authorization: Bearer lg_...'", "authentication_error")
    try:
        p = core.auth._bearer(raw)       # not authenticate(): another caller's bad keys must not lock this one out
    except HTTPException as exc:
        raise GatewayError(503, "service_unavailable", str(exc.detail), "api_error", {"Retry-After": "5"}) from exc
    if p is None:
        who = _client(request)
        if data_limiter.limited(who):
            raise GatewayError(429, "too_many_failures", "too many invalid keys from this address; wait a minute", "rate_limit_error",
                               {"Retry-After": "60"})
        data_limiter.record(who)
        raise GatewayError(401, "invalid_api_key", "that key is not valid (it may have been revoked)", "authentication_error")
    if p.kind == "client":
        return p.extra["client"], p.ident
    if p.kind == "service" and p.can("llm:use") and "*" not in p.permissions:
        try:
            with connect(retries=1) as conn:
                row = clients.client_by_name(conn, p.subject)
        except (psycopg.Error, RuntimeError) as exc:
            raise GatewayError(503, "service_unavailable", "database unavailable; retry in a few seconds", "api_error", {"Retry-After": "5"}) from exc
        if row is None:
            raise GatewayError(403, "client_not_registered", f"no client named {p.subject[:60]!r} is registered at this gateway", "permission_error")
        return row, None
    raise GatewayError(403, "wrong_credential", "operator credentials cannot call /v1; use a client key (or a service token with scope llmgw.use)",
                       "permission_error")


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise GatewayError(413, "request_too_large", f"the request body is larger than {MAX_BODY // 1024} KB")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise GatewayError(413, "request_too_large", f"the request body is larger than {MAX_BODY // 1024} KB")
        chunks.append(chunk)
    return b"".join(chunks)


async def _model_call(request: Request, endpoint: str) -> Response:
    client, key_id = await run_in_threadpool(data_client, request)
    raw = await _read_body(request)
    res = await run_in_threadpool(gateway.handle, client, key_id, endpoint, raw)
    return Response(res.body, status_code=res.status, media_type="application/json", headers=res.headers)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await _model_call(request, "chat")


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    return await _model_call(request, "embeddings")


def _models(request: Request) -> list[dict]:
    client, _ = data_client(request)
    with core.db.pool().connection() as conn:
        items = gateway.list_models(conn, client) if client["enabled"] else []
    m.REQUESTS.labels(client["name"], "-", "200").inc()
    return items


@app.get("/v1/models")
async def list_models(request: Request):
    items = await run_in_threadpool(_models, request)
    return {"object": "list", "data": items}


@app.get("/v1/models/{alias}")
async def get_model(alias: str, request: Request):
    for item in await run_in_threadpool(_models, request):
        if item["id"] == alias:
            return item
    raise GatewayError(404, "model_not_found", f"unknown model alias {alias[:60]!r}")


# ---- operator: session, setup, sign-in, tokens ---------------------------------------------------------------------
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetupBody(_Body):
    code: str = Field(max_length=64)
    password: str = Field(max_length=256)


class LoginBody(_Body):
    password: str | None = Field(None, max_length=256)
    api_key: str | None = Field(None, max_length=512)


class UnlockBody(_Body):
    password: str = Field(max_length=256)


class PasswordBody(_Body):
    current: str | None = Field(None, max_length=256)
    new: str = Field(max_length=256)


class TokenBody(_Body):
    name: str = Field(min_length=1, max_length=60)
    scope: str = Field(pattern="^(read|metrics|full)$")


def _start_session(conn, request: Request, response: Response, **who) -> None:
    response.set_cookie(COOKIE, local.create_session(conn, **who), httponly=True, samesite="lax" if who else "strict",
                        secure=is_https(request), max_age=12 * 3600, path="/")


def _guard_attempts(request: Request) -> str:
    client = _client(request)
    if core.auth.limiter.limited(client):
        raise HTTPException(429, "too many failed attempts; wait a minute")
    return client


@app.get("/api/session")
def session_state(request: Request):
    """Public: tells the UI which screen to show. Reveals no secrets."""
    principal = None
    try:
        principal = core.auth.authenticate(request)
        with connect(retries=1) as conn:
            setup = local.needs_setup(conn) and not fed.enabled
            v = core.vault.status(conn)
            has_password = local.admin_configured(conn)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    if setup:
        _announce_setup_code()
    state = "setup_required" if setup else "logged_in" if principal and principal.kind != "client" else "logged_out"
    out = {"state": state, "vault": v, "has_admin_password": has_password, "version": __version__,
           "password_login": has_password and (fed.local_login or not fed.enabled),
           "key_login": local.static_key_configured() and (fed.local_login or not fed.enabled),
           "sso": {"issuer": fed.issuer, "login_url": "/auth/login"} if fed.enabled else None}
    if principal and state == "logged_in":
        out["user"] = {"name": principal.name, "email": principal.email, "source": principal.source, "roles": sorted(principal.roles),
                       "permissions": ["*"] if "*" in principal.permissions else sorted(principal.permissions)}
    return out


@app.get("/auth/login")
def sso_login(request: Request, next: str | None = None):
    if not fed.enabled:
        raise HTTPException(404, "single sign-on is not configured")
    try:
        with connect(retries=1) as conn:
            url = fed.rp.begin(conn, public_url(request, core.public_url_env) + "/auth/callback", next)
    except psycopg.Error as exc:
        raise _db_unavailable() from exc
    except Exception:  # noqa: BLE001 - the identity provider may be unreachable
        return RedirectResponse("/?sso_error=the identity provider is unavailable", status_code=303)
    return RedirectResponse(url, status_code=303)


@app.get("/auth/callback")
def sso_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not fed.enabled:
        raise HTTPException(404, "single sign-on is not configured")
    if error or not code or not state:
        return RedirectResponse("/?sso_error=the sign-in was cancelled or failed", status_code=303)
    try:
        with connect(retries=1) as conn:
            claims, back, id_token = fed.rp.finish(conn, public_url(request, core.public_url_env) + "/auth/callback", code, state)
            roles = [r for r in claims.get("roles", []) if isinstance(r, str)]
            if not core.APP_AUTH.permissions_for(roles=roles):
                return RedirectResponse("/?sso_error=your account has no role in the LLM gateway; ask an administrator", status_code=303)
            resp = RedirectResponse(safe_return_to(back), status_code=303)
            _start_session(conn, request, resp, subject=str(claims["sub"]), name=str(claims.get("name") or ""),
                           email=str(claims.get("email") or ""), roles=roles, source="oidc", id_token_hint=id_token)
            return resp
    except TokenError as exc:
        return RedirectResponse("/?sso_error=" + str(exc)[:120], status_code=303)


@app.post("/api/setup")
def setup(body: SetupBody, request: Request, response: Response):
    client = _guard_attempts(request)
    try:
        with connect(retries=1) as conn:
            try:
                local.complete_setup(conn, body.code, body.password)
            except PermissionError as exc:
                core.auth.limiter.record(client)
                raise HTTPException(403, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            _start_session(conn, request, response)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    client = _guard_attempts(request)
    try:
        with connect(retries=1) as conn:
            ok = False
            if body.password is not None:
                ok = local.login_password(conn, body.password)
            elif body.api_key is not None and local.static_key_matches(body.api_key):
                ok = True
                core.vault.try_unlock_with_platform_secret(conn)
            if not ok:
                core.auth.limiter.record(client)
                raise HTTPException(401, "invalid credentials")
            _start_session(conn, request, response)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    out: dict = {"ok": True}
    try:
        with connect(retries=1) as conn:
            row = local.delete_session(conn, request.cookies.get(COOKIE))
        if row and row["source"] == "oidc" and fed.enabled:
            with contextlib.suppress(Exception):
                out["logout_url"] = fed.rp.logout_url(row["id_token_hint"], public_url(request, core.public_url_env) + "/")
    except (psycopg.Error, RuntimeError):
        pass  # the cookie is cleared regardless; the session row expires on its own
    response.delete_cookie(COOKIE, path="/")
    return out


@app.post("/api/vault/unlock")
def unlock_vault(body: UnlockBody, request: Request, p: Principal = Depends(require("settings:manage"))):
    client = _guard_attempts(request)
    with connect() as conn:
        if not core.vault.is_initialized(conn):
            raise HTTPException(409, "no secrets are stored yet; set an admin password first")
        if not core.vault.unlock_with_password(conn, body.password):
            core.auth.limiter.record(client)
            raise HTTPException(403, "wrong password")   # 403, not 401: the session itself is still valid
        conn.commit()
    return {"ok": True}


@app.post("/api/admin-password")
def admin_password(body: PasswordBody, request: Request, p: Principal = Depends(require("settings:manage"))):
    client = _guard_attempts(request)
    with connect() as conn:
        try:
            if local.admin_configured(conn):
                if body.current is None:
                    raise HTTPException(422, "current password is required")
                local.change_password(conn, body.current, body.new)
            else:
                local.set_admin_password(conn, body.new)
        except PermissionError as exc:
            core.auth.limiter.record(client)
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        conn.execute("DELETE FROM web_session")
        conn.commit()
        audit.log(conn, p.actor, "admin_password.change")
    return {"ok": True, "signed_out_everywhere": True}


@app.get("/api/tokens", dependencies=[Depends(require("settings:manage"))])
def list_tokens():
    with connect() as conn:
        return local.list_tokens(conn)


@app.post("/api/tokens")
def create_token(body: TokenBody, p: Principal = Depends(require("settings:manage"))):
    with connect() as conn:
        token_id, raw = local.create_token(conn, body.name.strip(), body.scope)
        audit.log(conn, p.actor, "token.create", body.name.strip(), scope=body.scope, token_id=token_id)
    return {"id": token_id, "token": raw, "scope": body.scope, "name": body.name.strip()}


@app.delete("/api/tokens/{token_id}")
def revoke_token(token_id: int, p: Principal = Depends(require("settings:manage"))):
    with connect() as conn:
        if not local.revoke_token(conn, token_id):
            raise HTTPException(404, "token not found or already revoked")
        audit.log(conn, p.actor, "token.revoke", str(token_id))
    return {"revoked": token_id}


from . import api_admin  # noqa: E402  (needs `app` and `require` defined above)

app.include_router(api_admin.build_router(require))
