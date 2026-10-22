import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="llmgw")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply SQL migrations")
    sub.add_parser("serve", help="run the gateway and its dashboard")
    sub.add_parser("reset-admin", help="forget the admin password and delete stored provider keys (recovery)")
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
    elif args.cmd == "serve":
        _serve()
    elif args.cmd == "start":
        return _start()
    return 0


def _serve() -> None:
    import os

    import uvicorn

    uvicorn.run("llmgw.api:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))


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
