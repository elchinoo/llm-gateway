"""This app's instances of the shared blocks (agentpg-kit), and the authorization model of the LLM gateway.

Permissions are what the code checks (`clients:manage`); roles (given to people by the identity provider) and scopes (given to
services and local API tokens) are mapped onto them here. Client keys are a separate kind of principal that can only reach the
data plane (`llm:use`), never an operator route; and no operator credential, wildcard included, can reach the data plane.
"""
import httpx
from apgkit import metrics as metrics_mod
from apgkit.auth import AppAuth, Authenticator, LocalAuth, from_env
from apgkit.db import Database
from apgkit.llm import LLMClient
from apgkit.netguard import NetGuard
from apgkit.vault import Vault

from . import __version__
from .config import get_settings

APP = "llmgw"
COOKIE = "lg_session"
CSRF_HEADER = "x-llmgw-csrf"
CLIENT_KEY_PREFIX = "lg_"

db = Database(lambda: get_settings().database_url, pool_max=get_settings().pool_max)
vault = Vault("LLMGW")
netguard = NetGuard("LLMGW_ALLOW_PRIVATE_URLS")
llm = LLMClient(netguard, vault, http_factory=lambda: httpx.Client(timeout=get_settings().upstream_timeout, follow_redirects=False))
metrics = metrics_mod.Metrics(APP, __version__)

# Every vault-encrypted column, so `rotate_data_key` re-encrypts it. The context binds a ciphertext to its row: a value copied to
# another provider (or column) no longer decrypts. `key_origin` is a separate column and rotation never touches it.
# FOR UPDATE: a concurrent provider edit waits until the rotation commits instead of being overwritten with an old-key value.
PROVIDER_KEY_CONTEXT = "provider.api_key_enc:{id}"
vault.register_store("provider.api_key_enc",
                     "SELECT id, api_key_enc AS value FROM provider WHERE api_key_enc IS NOT NULL ORDER BY id FOR UPDATE",
                     "UPDATE provider SET api_key_enc = %(value)s WHERE id = %(id)s",
                     context=PROVIDER_KEY_CONTEXT)

ALL = frozenset({"*"})
PERMISSIONS = {
    "usage:read": "see usage, spend and the overview",
    "config:read": "see providers, model aliases and clients (never keys)",
    "audit:read": "read the audit trail of changes",
    "clients:manage": "create clients, set limits and budgets, issue, rotate and revoke client keys",
    "models:manage": "create and change model aliases and their ordered targets",
    "providers:manage": "add providers, store their API keys, test them",
    "settings:manage": "defaults, operator tokens, the admin password, unlocking the vault",
    "metrics:scrape": "scrape /metrics",
    # client keys and identity-provider service tokens
    "llm:use": "call the OpenAI-compatible data plane (/v1)",
}
ROLE_PERMISSIONS = {
    "admin": ALL,
    "operator": frozenset({"usage:read", "config:read", "audit:read", "clients:manage", "models:manage"}),
    "viewer": frozenset({"usage:read", "config:read"}),
}
READ = frozenset({"usage:read", "config:read"})
SCOPE_PERMISSIONS = {
    "read": READ, "metrics": frozenset({"metrics:scrape"}), "full": ALL,
    "llmgw.use": frozenset({"llm:use"}), "llmgw.read": READ, "llmgw.metrics": frozenset({"metrics:scrape"}),
}
fed = from_env("LLMGW", APP)
APP_AUTH = AppAuth(APP, fed.client_id, ROLE_PERMISSIONS, SCOPE_PERMISSIONS)


def _on_reset(conn) -> None:
    conn.execute("UPDATE provider SET api_key_enc = NULL, api_key_last4 = NULL")


local = LocalAuth(APP_AUTH, env_prefix="LLMGW", token_prefix="lgt_", token_scopes={"read", "metrics", "full"}, vault=vault,
                  on_reset=_on_reset)
# bearer_resolvers is filled in by clients.py (client keys need the pool that lives there)
auth = Authenticator(APP_AUTH, local, db, cookie_name=COOKIE, verifier=fed.verifier, bearer_resolvers={})
public_url_env = "LLMGW_PUBLIC_URL"
