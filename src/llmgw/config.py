import os
from dataclasses import dataclass

from apgkit.config import read_secret


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_key: str            # optional static full-access operator key
    public_url: str
    pool_max: int
    upstream_timeout: float
    max_concurrent: int


def get_settings() -> Settings:
    try:
        timeout = float(os.environ.get("LLMGW_UPSTREAM_TIMEOUT", "60"))
    except ValueError:
        timeout = 60.0
    return Settings(
        database_url=os.environ.get("DATABASE_URL") or os.environ.get("DB_EXTERNAL_DSN")
        or os.environ.get("PSQL_CONNECTION_STRING") or "",
        api_key=read_secret("LLMGW_API_KEY", "/etc/llmgw/api_key"),
        public_url=os.environ.get("LLMGW_PUBLIC_URL", "").rstrip("/"),
        pool_max=int(os.environ.get("LLMGW_POOL_MAX", "20")),
        max_concurrent=int(os.environ.get("LLMGW_MAX_CONCURRENT", "64")),
        upstream_timeout=min(max(timeout, 1.0), 120.0),     # bounded: a stuck provider cannot hold a worker for long
    )
