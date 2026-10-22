"""Instance defaults kept in the shared key/value table."""
from apgkit import settings_store as store

DEFAULTS = {"default_rpm": 60, "default_embedding_dim": 768, "usage_retention_days": 0}
LIMITS = {"default_rpm": (1, 100_000), "default_embedding_dim": (1, 8192), "usage_retention_days": (0, 3650)}


def get_all(conn) -> dict[str, int]:
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        raw = store.get(conn, f"cfg_{key}")
        if raw is not None and raw.lstrip("-").isdigit():
            out[key] = int(raw)
    return out


def update(conn, values: dict[str, int]) -> dict[str, int]:
    for key, value in values.items():
        lo, hi = LIMITS[key]
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        if key == "usage_retention_days" and 0 < value < 32:
            raise ValueError("usage_retention_days must be 0 (keep everything) or at least 32: the monthly budget is computed from these rows")
        store.put(conn, f"cfg_{key}", str(value))
    conn.commit()
    return get_all(conn)
