"""Rotating the vault's data key (admin only): a new key, every stored provider key re-encrypted under it, both wraps rewritten, all
in one transaction. Used by `POST /api/vault/rotate` and `llmgw rotate-vault`. Nothing secret is logged or audited."""
import logging

from apgkit.vault import VaultError, VaultLocked

from . import audit, core

log = logging.getLogger("llmgw.vault")


class RotationError(ValueError):
    """A reason safe to show the operator (wrong password, vault not set up...)."""


def rotate(conn, password: str | None, actor: str) -> dict[str, int]:
    """Rotate on `conn` (no open transaction). `password` is the admin password; it is required when the vault has a password wrap
    (it is verified, and the wrap is rewritten). Returns the rows re-encrypted per store. Other replicas keep the old key in memory
    and must be restarted (or unlocked again) right after."""
    v = core.vault
    if not v.is_initialized(conn):
        raise RotationError("no secrets are stored yet; there is no key to rotate")
    conn.rollback()
    if not v.is_unlocked():
        v.try_unlock_with_platform_secret(conn)
        if not v.is_unlocked() and password is not None:
            v.unlock_with_password(conn, password)
        conn.commit()
    needs_password = v.has_password_wrap(conn)
    conn.rollback()
    if needs_password and password is None:
        raise RotationError("the admin password is required")
    try:
        counts = v.rotate_data_key(conn, password=password if needs_password else None)
    except VaultLocked as exc:
        raise RotationError("the vault is locked: enter the admin password") from exc
    except VaultError as exc:
        raise RotationError(str(exc)) from exc
    audit.log(conn, actor, "vault.rotate", counts=counts)
    log.warning("vault data key rotated by %s (rows re-encrypted: %s); restart every other replica", actor[:80], counts)
    return counts
