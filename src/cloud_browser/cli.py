import argparse
import asyncio
import getpass
import json

import uvicorn
from argon2 import PasswordHasher

from .config import Settings
from .diagnostics import doctor_report, self_test


async def serve(cfg):
    from .server import create_apps

    if cfg.native_config:
        from .native import verify_runtime

        verify_runtime(cfg)

    public, control, _, _ = create_apps(cfg)
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                app,
                host=cfg.bind_host,
                port=port,
                access_log=False,
                proxy_headers=False,
                log_level="warning",
            )
        )
        for app, port in ((public, cfg.public_port), (control, cfg.control_port))
    ]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="Personal Cloud Browser MCP")
    parser.add_argument(
        "command", choices=["serve", "hash-password", "doctor", "self-test", "revoke-all"]
    )
    parser.add_argument("--chromium", help="Chromium executable for isolated self-test only")
    args = parser.parse_args()
    if args.command == "hash-password":
        password = getpass.getpass("Administrator password (at least 14 characters): ")
        if len(password) < 14 or password != getpass.getpass("Repeat password: "):
            parser.error("Password is too short or does not match")
        print(PasswordHasher().hash(password))
        return
    if args.command == "doctor":
        print(json.dumps(doctor_report(), indent=2))
        return
    if args.command == "self-test":
        result = asyncio.run(self_test(args.chromium))
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
        return
    cfg = Settings()
    if args.command == "revoke-all":
        from .store import Store

        store = Store(cfg.data_dir / "state.sqlite3")
        store.delete_kind("grant")
        store.close()
        print("All OAuth grants revoked.")
    else:
        from .runtime import DataLock

        with DataLock(cfg.data_dir):
            asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
