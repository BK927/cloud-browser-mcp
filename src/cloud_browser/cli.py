import argparse
import asyncio
import getpass
import json

import uvicorn
from argon2 import PasswordHasher

from .config import Settings
from .resources import memory_state


async def serve(cfg):
    from .server import create_apps

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
    parser.add_argument("command", choices=["serve", "hash-password", "doctor", "revoke-all"])
    args = parser.parse_args()
    if args.command == "hash-password":
        password = getpass.getpass("Administrator password (at least 14 characters): ")
        if len(password) < 14 or password != getpass.getpass("Repeat password: "):
            parser.error("Password is too short or does not match")
        print(PasswordHasher().hash(password))
        return
    cfg = Settings()
    if args.command == "doctor":
        print(
            json.dumps(
                {
                    "origins_configured": bool(cfg.public_origin and cfg.control_origin),
                    "oauth_callbacks_configured": bool(cfg.oauth_redirect_uris),
                    "network_isolation_operator_asserted": cfg.network_isolated,
                    "manual_console_assets_present": cfg.novnc_dir.is_dir(),
                    "memory": memory_state(cfg.memory_reserve_mb),
                    "development": cfg.development,
                },
                indent=2,
            )
        )
    elif args.command == "revoke-all":
        from .store import Store

        store = Store(cfg.data_dir / "state.sqlite3")
        store.delete_kind("grant")
        store.close()
        print("All OAuth grants revoked.")
    else:
        asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
