"""Operator API (/api): providers, model aliases, clients and their keys, usage, audit and settings. Every route names the
permission it needs (see core.PERMISSIONS). Keys are returned once, at creation, and never again."""
from typing import Any

from apgkit.auth import Principal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import aliases, audit, clients, providers, usage
from . import settings as cfg
from .db import connect


class _B(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderBody(_B):
    name: str = Field(min_length=1, max_length=60)
    kind: str = Field("openai", pattern="^(openai|echo)$")
    base_url: str | None = Field(None, max_length=300)
    api_key: str | None = Field(None, max_length=500)
    enabled: bool = True


class ProviderPatch(_B):
    name: str = Field(min_length=1, max_length=60)
    base_url: str | None = Field(None, max_length=300)
    api_key: str | None = Field(None, max_length=500)
    clear_key: bool = False
    enabled: bool = True


class TestBody(_B):
    model: str = Field(min_length=1, max_length=120, description="the upstream model name to ping")


class TargetBody(_B):
    provider_id: int
    upstream_model: str = Field(min_length=1, max_length=120)
    extra_body: Any = Field(None, description="operator-set JSON object merged into the upstream request (wins over the client's fields); "
                                              "validated by aliases.validate_extra_body")


class AliasBody(_B):
    kind: str = Field(pattern="^(chat|embedding)$")
    description: str = Field("", max_length=200)
    enabled: bool = True
    embedding_dim: int | None = Field(None, ge=1, le=8192)
    price_prompt_per_m: float | None = Field(None, ge=0, le=1_000_000)
    price_completion_per_m: float | None = Field(None, ge=0, le=1_000_000)
    targets: list[TargetBody] = Field(min_length=1, max_length=aliases.MAX_TARGETS)


class ClientBody(_B):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field("", max_length=200)
    allowed_aliases: list[str] = Field(["*"], min_length=1, max_length=100)
    rpm_limit: int | None = Field(None, ge=1, le=100_000, description="requests per minute; omit for the instance default")
    unlimited_rpm: bool = False
    monthly_token_budget: int | None = Field(None, ge=1, le=10**13)
    budget_mode: str = Field("hard", pattern="^(hard|soft)$")
    issue_key: bool = True


class ClientPatch(_B):
    description: str | None = Field(None, max_length=200)
    allowed_aliases: list[str] | None = Field(None, min_length=1, max_length=100)
    rpm_limit: int | None = Field(None, ge=1, le=100_000)
    monthly_token_budget: int | None = Field(None, ge=1, le=10**13)
    budget_mode: str | None = Field(None, pattern="^(hard|soft)$")
    enabled: bool | None = None


class KeyBody(_B):
    name: str = Field("", max_length=60)


class RotateBody(_B):
    grace_minutes: int = Field(0, ge=0, le=10_080, description="keep the old key working this long so the app can switch")


class SettingsBody(_B):
    default_rpm: int | None = None
    default_embedding_dim: int | None = None
    usage_retention_days: int | None = None


def _bad(exc: Exception, code: int = 422) -> HTTPException:
    return HTTPException(code, str(exc))


def _extra_bodies(conn, alias: str) -> list[dict]:
    try:
        return [t["extra_body"] for t in aliases.get(conn, alias)["targets"]]
    except aliases.AliasError:
        return []


def _extra_audit(before: list[dict], targets: list[TargetBody]) -> dict:
    """What the audit trail says about extra_body: the operator's own non-secret request fields, only when there are or were any
    (a provider's key can never be in it, and the docs tell operators not to paste one)."""
    after = [aliases.validate_extra_body(t.extra_body) for t in targets]
    if not any(after) and not any(before):
        return {}
    return {"extra_body": {str(i + 1): v for i, v in enumerate(after) if v}, "extra_body_changed": after != before}


def build_router(require) -> APIRouter:
    r = APIRouter(prefix="/api")

    # ---- providers --------------------------------------------------------------------------------------------
    @r.get("/providers")
    def list_providers(_: Principal = Depends(require("config:read"))):
        with connect() as conn:
            return {"items": providers.list_providers(conn), "presets": providers.PRESETS}

    @r.post("/providers", status_code=201)
    def create_provider(body: ProviderBody, p: Principal = Depends(require("providers:manage"))):
        with connect() as conn:
            try:
                pid = providers.create(conn, name=body.name, kind=body.kind, base_url=body.base_url, api_key=body.api_key, enabled=body.enabled)
            except providers.ProviderError as exc:
                raise _bad(exc) from exc
            audit.log(conn, p.actor, "provider.create", body.name, kind=body.kind, base_url=body.base_url, has_key=bool(body.api_key), enabled=body.enabled)
            return providers.get_provider(conn, pid)

    @r.put("/providers/{provider_id}")
    def update_provider(provider_id: int, body: ProviderPatch, p: Principal = Depends(require("providers:manage"))):
        with connect() as conn:
            try:
                providers.update(conn, provider_id, name=body.name, base_url=body.base_url, api_key=body.api_key, clear_key=body.clear_key,
                                 enabled=body.enabled)
            except providers.ProviderError as exc:
                raise _bad(exc, 404 if "not found" in str(exc) else 422) from exc
            audit.log(conn, p.actor, "provider.update", body.name, base_url=body.base_url, key_changed=bool(body.api_key), key_cleared=body.clear_key,
                      enabled=body.enabled)
            return providers.get_provider(conn, provider_id)

    @r.delete("/providers/{provider_id}")
    def delete_provider(provider_id: int, p: Principal = Depends(require("providers:manage"))):
        with connect() as conn:
            try:
                row = providers.get_provider(conn, provider_id)
                providers.delete(conn, provider_id)
            except providers.ProviderError as exc:
                raise _bad(exc, 404 if "not found" in str(exc) else 409) from exc
            audit.log(conn, p.actor, "provider.delete", row["name"])
        return {"deleted": provider_id}

    @r.post("/providers/{provider_id}/test")
    def test_provider(provider_id: int, body: TestBody, p: Principal = Depends(require("providers:manage"))):
        with connect() as conn:
            row = conn.execute("SELECT * FROM provider WHERE id=%s", (provider_id,)).fetchone()
            if not row:
                raise HTTPException(404, "provider not found")
            out = providers.test(row, body.model)
            audit.log(conn, p.actor, "provider.test", row["name"], model=body.model, ok=out["ok"])
        return out

    # ---- model aliases ------------------------------------------------------------------------------------------
    @r.get("/models")
    def list_models(_: Principal = Depends(require("config:read"))):
        with connect() as conn:
            return {"items": aliases.list_aliases(conn), "default_embedding_dim": cfg.get_all(conn)["default_embedding_dim"]}

    @r.put("/models/{alias}")
    def put_model(alias: str, body: AliasBody, p: Principal = Depends(require("models:manage"))):
        with connect() as conn:
            dim = body.embedding_dim or cfg.get_all(conn)["default_embedding_dim"]
            try:
                before = _extra_bodies(conn, alias)
                created = aliases.upsert(conn, alias, kind=body.kind, description=body.description, enabled=body.enabled, embedding_dim=dim,
                                         price_prompt_per_m=body.price_prompt_per_m, price_completion_per_m=body.price_completion_per_m,
                                         targets=[t.model_dump() for t in body.targets])
            except aliases.AliasError as exc:
                raise _bad(exc) from exc
            audit.log(conn, p.actor, "model.create" if created else "model.update", alias, kind=body.kind, enabled=body.enabled,
                      targets=[f"{t.provider_id}:{t.upstream_model}" for t in body.targets], **_extra_audit(before, body.targets),
                      price_prompt_per_m=body.price_prompt_per_m,
                      price_completion_per_m=body.price_completion_per_m, embedding_dim=dim)
            return aliases.get(conn, alias)

    @r.delete("/models/{alias}")
    def delete_model(alias: str, p: Principal = Depends(require("models:manage"))):
        with connect() as conn:
            try:
                aliases.delete(conn, alias)
            except aliases.AliasError as exc:
                raise _bad(exc, 404) from exc
            audit.log(conn, p.actor, "model.delete", alias)
        return {"deleted": alias}

    # ---- clients and keys ---------------------------------------------------------------------------------------
    @r.get("/clients")
    def list_clients(_: Principal = Depends(require("config:read"))):
        with connect() as conn:
            return {"items": clients.list_clients(conn), "default_rpm": cfg.get_all(conn)["default_rpm"]}

    @r.post("/clients", status_code=201)
    def create_client(body: ClientBody, p: Principal = Depends(require("clients:manage"))):
        with connect() as conn:
            rpm = None if body.unlimited_rpm else (body.rpm_limit or cfg.get_all(conn)["default_rpm"])
            try:
                cid = clients.create(conn, name=body.name, description=body.description, allowed_aliases=body.allowed_aliases, rpm_limit=rpm,
                                     monthly_token_budget=body.monthly_token_budget, budget_mode=body.budget_mode)
            except clients.ClientError as exc:
                raise _bad(exc) from exc
            audit.log(conn, p.actor, "client.create", body.name.lower(), allowed_aliases=body.allowed_aliases, rpm_limit=rpm,
                      monthly_token_budget=body.monthly_token_budget, budget_mode=body.budget_mode)
            out = {"client": clients.get(conn, cid)}
            if body.issue_key:
                kid, raw = clients.create_key(conn, cid, "default")
                audit.log(conn, p.actor, "key.issue", body.name.lower(), key_id=kid)
                out["key"] = {"id": kid, "key": raw, "name": "default"}
            return out

    @r.patch("/clients/{client_id}")
    def patch_client(client_id: int, body: ClientPatch, p: Principal = Depends(require("clients:manage"))):
        fields = body.model_dump(exclude_unset=True)
        for k in ("description", "allowed_aliases", "budget_mode", "enabled"):
            if k in fields and fields[k] is None:
                raise HTTPException(422, f"{k} cannot be null")
        with connect() as conn:
            try:
                before = clients.update(conn, client_id, fields)
            except clients.ClientError as exc:
                raise _bad(exc, 404 if "not found" in str(exc) else 422) from exc
            audit.log(conn, p.actor, "client.update", before["name"], changed={k: (v if k != "allowed_aliases" else "list") for k, v in fields.items()})
            return clients.get(conn, client_id)

    @r.delete("/clients/{client_id}")
    def delete_client(client_id: int, p: Principal = Depends(require("clients:manage"))):
        with connect() as conn:
            try:
                row = clients.get(conn, client_id)
                clients.delete(conn, client_id)
            except clients.ClientError as exc:
                raise _bad(exc, 404) from exc
            audit.log(conn, p.actor, "client.delete", row["name"])
        return {"deleted": client_id}

    @r.get("/clients/{client_id}/keys")
    def list_keys(client_id: int, _: Principal = Depends(require("config:read"))):
        with connect() as conn:
            try:
                clients.get(conn, client_id)
            except clients.ClientError as exc:
                raise _bad(exc, 404) from exc
            return {"items": clients.list_keys(conn, client_id)}

    @r.post("/clients/{client_id}/keys", status_code=201)
    def issue_key(client_id: int, body: KeyBody, p: Principal = Depends(require("clients:manage"))):
        with connect() as conn:
            try:
                kid, raw = clients.create_key(conn, client_id, body.name)
                name = clients.get(conn, client_id)["name"]
            except clients.ClientError as exc:
                raise _bad(exc, 404) from exc
            audit.log(conn, p.actor, "key.issue", name, key_id=kid)
        return {"id": kid, "key": raw, "name": body.name}

    @r.delete("/clients/{client_id}/keys/{key_id}")
    def revoke_key(client_id: int, key_id: int, p: Principal = Depends(require("clients:manage"))):
        with connect() as conn:
            if not clients.revoke_key(conn, client_id, key_id):
                raise HTTPException(404, "key not found or already revoked")
            audit.log(conn, p.actor, "key.revoke", clients.get(conn, client_id)["name"], key_id=key_id)
        return {"revoked": key_id}

    @r.post("/clients/{client_id}/keys/{key_id}/rotate", status_code=201)
    def rotate_key(client_id: int, key_id: int, body: RotateBody, p: Principal = Depends(require("clients:manage"))):
        with connect() as conn:
            try:
                new_id, raw = clients.rotate_key(conn, client_id, key_id, body.grace_minutes)
            except clients.ClientError as exc:
                raise _bad(exc, 404) from exc
            audit.log(conn, p.actor, "key.rotate", clients.get(conn, client_id)["name"], old_key_id=key_id, new_key_id=new_id,
                      grace_minutes=body.grace_minutes)
        return {"id": new_id, "key": raw, "replaces": key_id, "grace_minutes": body.grace_minutes}

    # ---- usage ----------------------------------------------------------------------------------------------
    @r.get("/overview")
    def overview(days: int = Query(7, ge=1, le=366), _: Principal = Depends(require("usage:read"))):
        with connect() as conn:
            return usage.overview(conn, days)

    @r.get("/usage")
    def usage_summary(group: str = Query("client", pattern="^(client|alias|provider|day)$"), days: int = Query(30, ge=1, le=366),
                      client: str | None = Query(None, max_length=40), alias: str | None = Query(None, max_length=60),
                      _: Principal = Depends(require("usage:read"))):
        with connect() as conn:
            return {"group": group, "days": days, "items": usage.summary(conn, group, days, client, alias)}

    @r.get("/usage.csv")
    def usage_csv(group: str = Query("client", pattern="^(client|alias|provider|day)$"), days: int = Query(30, ge=1, le=366),
                  client: str | None = Query(None, max_length=40), alias: str | None = Query(None, max_length=60),
                  _: Principal = Depends(require("usage:read"))):
        with connect() as conn:
            body = usage.to_csv(usage.summary(conn, group, days, client, alias), group)
        return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="usage-by-{group}.csv"'})

    @r.get("/usage/recent")
    def usage_recent(limit: int = Query(50, ge=1, le=200), errors_only: bool = False, _: Principal = Depends(require("usage:read"))):
        with connect() as conn:
            return {"items": usage.recent(conn, limit, errors_only)}

    # ---- audit and settings -----------------------------------------------------------------------------------
    @r.get("/audit")
    def audit_trail(limit: int = Query(100, ge=1, le=500), before: int | None = None, _: Principal = Depends(require("audit:read"))):
        with connect() as conn:
            return {"items": audit.recent(conn, limit, before)}

    @r.get("/settings")
    def get_settings(_: Principal = Depends(require("settings:manage"))):
        with connect() as conn:
            return cfg.get_all(conn)

    @r.put("/settings")
    def put_settings(body: SettingsBody, p: Principal = Depends(require("settings:manage"))):
        values = body.model_dump(exclude_none=True)
        with connect() as conn:
            try:
                out = cfg.update(conn, values)
            except ValueError as exc:
                raise _bad(exc) from exc
            audit.log(conn, p.actor, "settings.update", "", **values)
        return out

    return r

