import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="llmgw")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply SQL migrations")
    sub.add_parser("serve", help="run the gateway and its dashboard")
    sub.add_parser("reset-admin", help="forget the admin password and delete stored provider keys (recovery)")
    rot = sub.add_parser("rotate-vault", help="replace the vault data key and re-encrypt stored provider keys (restart other replicas after)")
    rot.add_argument("--confirm", action="store_true", help="required: acknowledges that other replicas must be restarted")
    sub.add_parser("start", help="container entrypoint: migrate, then serve")
    args = p.parse_args(argv)

    if args.cmd == "migrate":
        from .migrate import run

        applied = run()
        print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
    elif args.cmd == "reset-admin":
        from .core import local
        from .db import connect

        with connect() as conn:
            local.reset_admin(conn)
        print("admin password, vault and stored provider keys were cleared; the setup code flow is active again")
    elif args.cmd == "rotate-vault":
        return _rotate_vault(args.confirm)
    elif args.cmd == "serve":
        _serve()
    elif args.cmd == "start":
        return _start()
    return 0


def _rotate_vault(confirm: bool) -> int:
    import getpass
    import os

    from .core import vault
    from .db import connect
    from .vault_admin import RotationError, rotate

    if not confirm:
        print("refusing: pass --confirm (the data key is replaced; restart every other replica afterwards)", file=sys.stderr)
        return 2
    with connect() as conn:
        password = None
        if vault.has_password_wrap(conn):
            password = os.environ.get("LLMGW_ADMIN_PASSWORD") or getpass.getpass("admin password: ")
        conn.rollback()
        try:
            counts = rotate(conn, password, "cli")
        except RotationError as exc:
            print(f"rotation failed: {exc}", file=sys.stderr)
            return 1
    print("data key rotated; re-encrypted:", counts or "nothing", "- restart every other replica now")
    return 0


def _serve() -> None:
    import os

    import uvicorn

    # Forwarded headers are trusted only from the proxy in front (the default is this host alone); "*" would let any caller choose
    # the client address the rate limiter sees. Set LLMGW_FORWARDED_ALLOW_IPS to the proxy's address or CIDR (comma separated).
    uvicorn.run("llmgw.api:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("LLMGW_FORWARDED_ALLOW_IPS", "127.0.0.1"))


def _start() -> int:
    from .migrate import run

    try:
        applied = run()   # the connection retries with backoff, so a waking database is tolerated
    except Exception as exc:  # noqa: BLE001 - surface a readable cause, then fail the container
        print(f"startup failed while migrating: {exc}\nCheck that the database is reachable.", file=sys.stderr)
        return 1
    print("migrations applied:", ", ".join(applied) if applied else "none (up to date)")
    _serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
