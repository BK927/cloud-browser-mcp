"""Local diagnostics. No production credentials, profiles or settings are changed."""

import asyncio
import base64
import io
import os
import platform
import shutil
import socket
import tempfile
from importlib import metadata
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import SettingsError

from .config import Settings


def find_chromium(configured: str | None = None) -> str | None:
    candidates = [configured] if configured else []
    candidates.extend(shutil.which(name) for name in ("chromium", "chromium-browser", "google-chrome"))
    if platform.system() == "Windows":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.extend(str(Path(base) / rel) for rel in (
                    "Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"
                ))
    elif platform.system() == "Darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return next((str(Path(value)) for value in candidates if value and Path(value).is_file()), None)


def doctor_report() -> dict:
    versions = {}
    for package in ("mcp", "DrissionPage", "fastapi", "pillow"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    # Never print ValidationError input/context, which could contain .env secrets.
    valid = True
    config_issues = []
    try:
        cfg = Settings()
    except (ValidationError, SettingsError) as exc:
        valid = False
        if isinstance(exc, ValidationError):
            config_issues = [
                {"field": ".".join(str(x) for x in err["loc"]) or "deployment", "type": err["type"]}
                for err in exc.errors(include_input=False, include_context=False, include_url=False)
            ]
        else:
            config_issues = [{"field": "environment", "type": "settings_parse_error"}]
        try:
            cfg = Settings(development=True)
        except (ValidationError, SettingsError):
            cfg = None
    detected = find_chromium(cfg.chromium_path if cfg else None)
    checks = {
        "configuration_valid": valid,
        "browser_dependency_installed": versions["DrissionPage"] is not None,
        "chromium_detected": detected is not None,
        "configured_chromium_exists": bool(cfg and Path(cfg.chromium_path).is_file()),
        "oauth_callbacks_configured": bool(cfg and cfg.oauth_redirect_uris),
        "admin_password_hash_configured": bool(cfg and cfg.admin_password_hash),
        "network_isolation_operator_asserted": bool(cfg and cfg.network_isolated),
        "manual_control_enabled": bool(cfg and cfg.manual_control_enabled),
        "manual_console_assets_present": bool(cfg and cfg.novnc_dir.is_dir()),
        "manual_display_compatible": bool(cfg and not cfg.headless and cfg.max_sessions == 1),
    }
    return {
        "checks": checks,
        "configuration_issues": config_issues,
        "detected_chromium": detected,
        "dependencies": versions,
        "can_run_isolated_self_test": bool(detected and versions["DrissionPage"] and versions["mcp"]),
        "deployment_verified": False,
        "chatgpt_connection_verified": False,
        "next_step": "Use self-test for local MCP/Chromium validation; see docs/CHATGPT_SETUP.md for deployment.",
    }


async def self_test(chromium_path: str | None = None) -> dict:
    """Exercise authenticated Streamable HTTP -> worker -> Chromium -> MCP image.

    Listens only on a random loopback port, visits only a new blank tab, and
    destroys its temporary OAuth store and browser profile on exit.
    """
    import httpx2
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from PIL import Image

    from .server import create_apps

    executable = (str(Path(chromium_path)) if Path(chromium_path).is_file() else None) if chromium_path else find_chromium()
    if not executable:
        return {"ok": False, "error": "CHROMIUM_NOT_FOUND", "hint": "Pass --chromium with an installed Chromium executable."}
    with tempfile.TemporaryDirectory(prefix="cloud-browser-self-test-") as folder:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        cfg = Settings(
            _env_file=None, _env_prefix="CB_ISOLATED_SELF_TEST_",
            development=True, data_dir=Path(folder), public_origin=f"http://127.0.0.1:{port}",
            control_origin="http://127.0.0.1:1", public_port=port, control_port=1,
            chromium_path=executable, headless=True, browser_proxy="", manual_control_enabled=False,
        )
        public, _, _, auth = create_apps(cfg)
        auth.store.put("grant", "isolated-local-self-test", {"active": True})
        token = auth.issue("isolated-local-self-test")["access_token"]
        server = uvicorn.Server(uvicorn.Config(public, host="127.0.0.1", port=port, log_level="error", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        steps = []
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.03)
            if not server.started:
                return {"ok": False, "error": "LOCAL_SERVER_NOT_STARTED"}
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=60) as http:
                async with streamable_http_client(cfg.resource, http_client=http) as streams:
                    async with ClientSession(*streams) as client:
                        await client.initialize()
                        steps.append("mcp_initialized")
                        tools = (await client.list_tools()).tools
                        names = [tool.name for tool in tools]
                        required = {"browser_open", "browser_observe", "browser_act", "browser_close", "browser_status"}
                        if not required.issubset(names):
                            return {"ok": False, "error": "MISSING_TOOLS", "tools": names}
                        opened = (await client.call_tool("browser_open", {})).structured_content
                        if not opened or opened["status"] != "ok":
                            return {"ok": False, "error": "BROWSER_OPEN_FAILED", "steps": steps}
                        steps.append("real_chromium_opened")
                        args = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
                        seen = await client.call_tool("browser_observe", args | {"mode": "visual"})
                        image = next((item for item in seen.content if item.type == "image"), None)
                        if not image:
                            return {"ok": False, "error": "MCP_IMAGE_MISSING", "steps": steps}
                        decoded = Image.open(io.BytesIO(base64.b64decode(image.data)))
                        if decoded.size != (1024, 768):
                            return {"ok": False, "error": "UNEXPECTED_IMAGE_SIZE", "steps": steps}
                        steps.append("mcp_image_decoded")
                        status = (await client.call_tool("browser_status", {"session_id": args["session_id"]})).structured_content
                        await client.call_tool("browser_close", {"session_id": args["session_id"], "scope": "session"})
                        steps.append("session_closed")
                        return {
                            "ok": True, "steps": steps, "tools": names,
                            "image": {"mime_type": image.mime_type, "width": decoded.width, "height": decoded.height},
                            "capabilities": status.get("capabilities", {}) if status else {},
                            "production_configuration_used": False,
                            "chatgpt_vision_verified": False,
                        }
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "steps": steps}
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 15)
            finally:
                sock.close()
