import os
from dataclasses import dataclass

from apgkit.config import database_url, read_secret


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_key: str            # optional static full-access operator key
    public_url: str
    pool_max: int
    upstream_timeout: float
    max_concurrent: int


def get_settings() -> Settings:
    """Built on every call (nothing cached) so the secrets it holds are read when used, after a sealed start has been unsealed."""
    try:
        timeout = float(os.environ.get("LLMGW_UPSTREAM_TIMEOUT", "60"))
    except ValueError:
        timeout = 60.0
    return Settings(
        database_url=database_url(),     # a secret: the sealed value once unsealed, else the environment. Never read at import
        api_key=read_secret("LLMGW_API_KEY", "/etc/llmgw/api_key"),
        public_url=os.environ.get("LLMGW_PUBLIC_URL", "").rstrip("/"),
        pool_max=int(os.environ.get("LLMGW_POOL_MAX", "20")),
        max_concurrent=int(os.environ.get("LLMGW_MAX_CONCURRENT", "64")),
        upstream_timeout=min(max(timeout, 1.0), 120.0),     # bounded: a stuck provider cannot hold a worker for long
    )
