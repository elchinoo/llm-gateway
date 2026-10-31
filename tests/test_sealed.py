"""Sealed boot: with APGKIT_SEALED set the app serves only /_seal/* and the health check until the operator unseals it."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from apgkit import config
from apgkit import sealed as sealing
from fastapi.testclient import TestClient

from .conftest import needs_db

PASS = "correct-horse-battery-staple-9"
DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")  # pragma: allowlist secret
OIDC_SECRET = "oidc-secret-" + "x" * 30  # pragma: allowlist secret


@pytest.fixture
def sealed_api(monkeypatch):
    """A second copy of llmgw.api built with APGKIT_SEALED set (the sealed state is read when the module is imported). The copy
    is private: the session-wide app the other tests use is untouched."""
    from llmgw import api as real_api
    from llmgw import core

    blob = sealing.seal({"DATABASE_URL": DB_URL, "LLMGW_API_KEY": "k" * 40, "LLMGW_OIDC_CLIENT_SECRET": OIDC_SECRET}, PASS)
    monkeypatch.setenv("APGKIT_SEALED", blob)
    spec = importlib.util.spec_from_file_location("llmgw.api_sealed", Path(real_api.__file__))
    module = importlib.util.module_from_spec(spec)
    sys.modules["llmgw.api_sealed"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("llmgw.api_sealed", None)
    sealing.set_active(real_api.sealed)       # the original instance (sealing off), as the other tests expect
    core.vault.lock()


def unseal(client, passphrase):
    key = client.get("/_seal/key").json()["public_key"]
    return client.post("/_seal/unseal", json=sealing.make_envelope(key, passphrase))


def test_sealed_app_serves_only_the_health_check_and_unseal_routes(sealed_api):
    c = TestClient(sealed_api.app)
    assert sealed_api.sealed.enabled and not sealed_api.sealed.unsealed
    assert c.get("/api/session").status_code == 503
    assert c.get("/v1/models").status_code == 503
    assert c.get("/healthz").status_code == 200
    assert c.get("/_seal/status").json() == {"state": "sealed"}
    # nothing is readable from the environment or the blob while sealed
    assert sealing.sealed_secret("LLMGW_API_KEY") is None and sealing.sealed_secret("DATABASE_URL") is None
    assert unseal(c, "wrong-wrong-wrong-wrong-1").status_code == 401
    assert not sealed_api.sealed.unsealed


@needs_db
def test_unseal_migrates_opens_the_vault_and_serves(sealed_api, _schema):
    from llmgw import core
    from llmgw.db import connect

    with connect() as conn:                    # a vault left by an earlier test is wrapped under another password
        core.vault.reset(conn)
        conn.commit()
    with TestClient(sealed_api.app) as c:
        assert c.get("/api/session").status_code == 503
        assert c.get("/healthz").status_code == 200
        assert unseal(c, PASS).json() == {"state": "unsealed"}
        assert c.get("/api/session").status_code == 200
        assert config.read_secret("LLMGW_API_KEY") == "k" * 40
        assert core.get_settings().database_url == DB_URL
        with connect() as conn:
            assert core.vault.is_initialized(conn)
            assert core.vault.is_unlocked()
        assert c.get("/_seal/key").status_code == 409
    with connect() as conn:                    # leave no vault wrapped under the test passphrase for the next test
        core.vault.reset(conn)
        conn.commit()


def test_no_blob_changes_nothing():
    from llmgw import api

    assert not api.sealed.enabled and api.sealed.unsealed
    assert TestClient(api.app).get("/healthz").status_code == 200


def test_oidc_client_secret_is_not_read_at_import(monkeypatch):
    from llmgw import core

    monkeypatch.setenv("LLMGW_OIDC_ISSUER", "https://idp.example.test")
    calls = []
    monkeypatch.setattr(core, "read_secret", lambda name, *a: calls.append(name) or "")
    fed = core._federation()
    assert fed.enabled and calls == []
    fed.rp._secret()                            # read on use, through read_secret
    assert calls == ["LLMGW_OIDC_CLIENT_SECRET"]


def test_database_url_is_read_when_used(monkeypatch):
    from llmgw.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql://a/one")
    assert get_settings().database_url == "postgresql://a/one"
    monkeypatch.setenv("DATABASE_URL", "postgresql://a/two")
    assert get_settings().database_url == "postgresql://a/two"
